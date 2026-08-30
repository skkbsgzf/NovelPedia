# -*- coding: utf-8 -*-
"""
setting_agent.py —— 设定子agent(设定知识图谱专家)

职责:
  1. 从场景识别设定实体(特有名词/秩序/觐见之梯/诸神游戏等), 给客观定义。
  2. **从一开始(stage1)就向量化**: 每个设定实体用 bge-m3 算向量, 支持 RAG 检索。
  3. **内在关联推理**: 识别设定之间的关联(秩序<->觐见之梯<->诸神游戏), 生成 related 边。
  4. **名字归并**: 钢锯/粗糙的钢锯/接生宫锯 -> 规范实体"宫锯"(生成 aliases, 合并定义)。
  5. 维护 settings_graph.json(可增量扩充, 供 RAG + 可视化)。

settings_graph.json:
{
  "schema": 1,
  "terms": [ {name, category, definition, aliases, vector, related:[{to,type,how}], source_scenes, first_seen} ],
  "relations": [ {from, to, type, how} ],
  "meta": {...}
}
"""
import sys
import os
import json
import re
import datetime
import webnovel_lexicon
import knowledge_router

# 知识路由器: 观察抽取过程, 达到阈值才引入外部通识库(默认不加载, 见 knowledge_router.py)
_ROUTER = None


def get_router():
    """惰性创建全局知识路由器(同一次运行内共享观察状态)。"""
    global _ROUTER
    if _ROUTER is None:
        _ROUTER = knowledge_router.KnowledgeRouter(genre=os.getenv("NOVEL_GENRE", "").strip() or None)
    return _ROUTER


# global 通识库作用域: 默认关闭。仅当调用方通过环境变量 NOVEL_GENRE 指定类别时才按域加载。
# (cli.py --genre -> env NOVEL_GENRE -> 这里自动生效, 子进程无需额外改代码)
def _auto_scope_from_env():
    import os as _os
    g = (_os.getenv("NOVEL_GENRE") or "").strip()
    if not g:
        return
    try:
        dom = webnovel_lexicon.resolve_genre(g)
        webnovel_lexicon.set_scope(enabled=True, domains=[dom])
        print(f"[global 通识库] 已加载域: {dom} — 仅供参考, 与原文冲突以本次阅读到的为准")
    except Exception as e:
        print(f"[warn] 通识库加载失败: {e}")
_auto_scope_from_env()

sys.path.insert(0, os.path.dirname(__file__))
import llm_client
import config as C


def empty_graph():
    return {"schema": 1, "terms": [], "relations": [], "meta": {"schema": 1}}


def load_graph(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            d.setdefault("terms", [])
            d.setdefault("relations", [])
            return d
        except Exception:
            return empty_graph()
    return empty_graph()


def save_graph(path, g):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(g, f, ensure_ascii=False, indent=2)


def _norm_aliases(t):
    names = {str(t.get("name", "")).strip()}
    for a in t.get("aliases", []) or []:
        if a:
            names.add(str(a).strip())
    names.discard("")
    return names


def find_term(g, name):
    """按 name/alias 检索设定实体。返回 (idx, term)。"""
    name = (name or "").strip()
    if not name:
        return None, None
    for i, t in enumerate(g["terms"]):
        if name in _norm_aliases(t):
            return i, t
    return None, None


# ======================================================================
# LLM: 场景 -> 设定实体(定义+关联)  [一次性出定义与关联]
# ======================================================================
SET_SYSTEM = (
    "你是网络小说世界观设定专家。从场景里识别**本书专有**的设定实体(特有名词/体系/规则/组织), "
    "给出**客观定义**, 并判断它与已有设定的**内在关联**。只输出 JSON。")

SET_USER = """下面是小说第{chapter_no}章 scene{scene_id}的抽取结果。

【场景】{where}
【人物】{who}
【行为事件】{actinfo}
【备注】{notes}
【已有设定名】(供判断关联/归并): {existing_terms}

【任务】识别本场景出现的**设定实体**(本书特有名词如'秩序''觐见之梯''诸神游戏''登神之路'等力量/规则/组织/地点/物品), 给定义 + 判断关联。
注意: 这些设定可能已在已有设定中(用已有名归并/复用), 也可能新增。

【输出】严格 JSON 数组, 每个元素:
{{
  "name": "设定名(规范名, 与已有同名则用已有名)",
  "category": "力量体系|规则|组织|地点|物品|世界观",
  "definition": "客观定义, 2-3句",
  "aliases": ["本场景出现的别名/近义名, 无则[]"],
  "related": [{{"to": "关联的设定名(已存在或本场景同现的)", "type": "关联|体系|地点|因果", "how": "一句话说明关联"}}]
}}

要求:
- **只收本书专有名词, 拒收通用名词**: 医院诊所学校酒吧、队友敌人朋友同伴、武器装备药水食物等现实/网文通用概念**一律不收**——它们没有记录意义。判定标准: 一个词放在别的小说里同样成立, 就不收。
  - 例外: 通用词被本书赋予**特指含义**时收(如医院在本书是'圣光医院'这个专有组织/被'【秩序】'改造过的特殊场所, 则收'圣光医院', 不收'医院')。
- name 若与已有设定是同一实体, 必须用已有规范名(归并, 不新建)。
- **aliases 只收"同一个实体的不同叫法/写法"**(如'钢锯/粗糙的钢锯/接生宫锯'是同一把锯), 绝不收同场景出现的**其他独立实体**(如'分娩'和'钢锯'是两个不同实体, 不能互为别名)。
- related 只在有明确内在关联时填(如'秩序'与'诸神游戏'的关系), 没有就[]。
- 只输出 JSON 数组。"""


def _parse_json_list(raw):
    """容错解析 LLM 的数组输出(兼容 json_mode=False 时的围栏/包装/截断)。
    顺序: 剥 ``` 围栏 → json.loads → dict 取数组 key → 原样返回; 失败返回 []。"""
    import re as _re
    if not raw:
        return []
    s = str(raw).strip()
    if s.startswith("```"):
        s = _re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s).strip()
    try:
        d = json.loads(s)
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            for k in ("terms", "entities", "data", "result", "items"):
                if isinstance(d.get(k), list):
                    return d[k]
            # 单对象(单实体): 包装成列表
            if d.get("name"):
                return [d]
        return []
    except Exception:
        pass
    # 截断的对象流 {..}{..} 兜底
    out = []
    for m in _re.finditer(r'\{[^{}]*"name"[^{}]*\}', s):
        try:
            out.append(json.loads(m.group()))
        except Exception:
            pass
    return out


def extract_settings(scene, base, model, existing_terms):
    """从场景抽设定实体(定义+关联)。返回 [(delta_dict), ...], 失败返回 []。"""
    import json as _j
    who = scene.get("who") or []
    actinfo = scene.get("actinfo") or []
    if not (who or actinfo):
        return []
    user = SET_USER.format(
        chapter_no=scene.get("chapter_no", "?"),
        scene_id=scene.get("scene_id", "?"),
        where=str(scene.get("where") or "未明示"),
        who=_j.dumps(who, ensure_ascii=False)[:500],
        actinfo=_j.dumps(actinfo, ensure_ascii=False)[:1200],
        notes=str(scene.get("notes") or "")[:300],
        existing_terms="、".join(existing_terms[:50]) or "(无)",
    )
    try:
        # 数组输出不能用 json_object 模式(dots.ai 等会回 {}), 用 json_mode=False + 容错解析
        raw = llm_client.chat(SET_SYSTEM, user, json_mode=False,
                              num_predict=1200, temperature=0.3)[0]
        return [x for x in _parse_json_list(raw)
                if isinstance(x, dict) and x.get("name")]
    except Exception:
        return []


# ======================================================================
# 卷积: 场景设定增量 -> 图谱(归并/关联/向量占位)
# ======================================================================
# ======================================================================
# 公版通用名词词库(跨书共享): src/generic_lexicon.json
# 判定标准: 一个词放在别的小说里同样成立, 就没有记录意义。
# 在卷积入口拦截 -> 通用名词根本不进图谱(比 normalize 兜底更早)。
# ======================================================================
_LEXICON = None


def load_lexicon():
    """加载公版通用词库(模块级缓存)。文件缺失时降级为空词库(不阻塞主流程)。"""
    global _LEXICON
    if _LEXICON is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generic_lexicon.json")
        try:
            with open(path, encoding="utf-8") as f:
                _LEXICON = json.load(f)
        except Exception as e:
            print(f"[warn] 通用词库加载失败({e}), 本次运行不过滤通用名词")
            _LEXICON = {"categories": {}, "patterns": [], "protected": {"prefixes": []}}
    return _LEXICON


def is_generic_name(name):
    """判定是否公版通用名词。专有保护: 《》【】包裹的词视为专有。"""
    n = str(name or "").strip()
    if not n:
        return True
    lex = load_lexicon()
    for pre in (lex.get("protected", {}) or {}).get("prefixes", []):
        if n.startswith(pre):
            return False
    for words in (lex.get("categories", {}) or {}).values():
        if n in words:
            return True
    for pat in lex.get("patterns", []):
        try:
            if re.match(pat, n):
                return True
        except re.error:
            continue
    return False


def convolve_settings(scene, graph, deltas):
    """把一个场景的设定增量卷积进图谱(名字归并 + 关联 + 记录出现)。"""
    scene_id = scene.get("scene_id")
    chapter = scene.get("chapter_no")
    for delta in deltas:
        name = str(delta.get("name", "")).strip()
        if not name:
            continue
        # 公版词库拦截: 通用名词(医院/队友/S级xxx/2000分...)不入图谱
        if is_generic_name(name):
            continue
        idx, term = find_term(graph, name)
        if term is None:
            term = {"name": name, "category": str(delta.get("category", "")).strip() or "世界观",
                    "definition": "", "aliases": [], "vector": None,
                    "related": [], "source_scenes": [], "first_seen": None}
            graph["terms"].append(term)
            idx = len(graph["terms"]) - 1
        # 归并别名
        for a in delta.get("aliases", []) or []:
            a = str(a).strip()
            if a and a not in (term.get("aliases") or []) and a != term["name"]:
                term.setdefault("aliases", []).append(a)
        # 定义合并(非空则拼接, 信息不丢)
        dv = str(delta.get("definition", "")).strip()
        if dv and dv not in term.get("definition", ""):
            term["definition"] = (term.get("definition", "") + "；" + dv) if term.get("definition") else dv
        # 记录出现场景
        if scene_id not in (term.get("source_scenes") or []):
            term.setdefault("source_scenes", []).append(scene_id)
        if not term.get("first_seen"):
            term["first_seen"] = chapter
        # 止点: 每次出现都覆盖, 卷积结束即为"最后一次出现在第几章"(记忆衰减/归档判定用)
        term["last_seen"] = chapter
        # ①知识路由器: 观察(不写任何字段) → 按需唤醒外部通识库 → ②再打标。
        # 顺序很重要: 必须先观察让路由器决定是否加载, 之后的词条才能被打上体系标签。
        try:
            _rt = get_router()
            _before = _rt.loaded_domain
            _rt.step(name, term.get("category"), term.get("definition"))
            # 刚触发加载 -> 回溯给此前已抽到的词条补标(它们确实属于该体系, 不该因触发时机而漏标)
            if not _before and _rt.loaded_domain:
                webnovel_lexicon.annotate_terms(graph.get("terms") or [])
        except Exception:
            pass
        # ②公版体系图 RAG 打标(零 LLM 成本): 命中 webnovel_lexicon 的体系词条时,
        # 写入 wn_domain/wn_slot/wn_rank(仅参考, 绝不覆盖 definition/category)。
        try:
            wn = webnovel_lexicon.lookup_term(name)
            if wn:
                term["wn_domain"], term["wn_slot"], term["wn_rank"] = wn["domain"], wn["slot"], wn["rank"]
        except Exception:
            pass
        # 关联边
        for rel in delta.get("related", []) or []:
            to = str(rel.get("to", "")).strip()
            if to and to != term["name"]:
                edge = {"from": term["name"], "to": to, "type": rel.get("type", "关联"),
                        "how": str(rel.get("how", ""))[:120], "scene": scene_id}
                # 去重边
                if not any(e["from"] == edge["from"] and e["to"] == edge["to"]
                           for e in graph["relations"]):
                    graph["relations"].append(edge)
                # 挂到 term.related
                r = {"to": to, "type": edge["type"], "how": edge["how"]}
                if r not in (term.get("related") or []):
                    term.setdefault("related", []).append(r)
    return graph


# ======================================================================
# 向量化(从一开始) + 归一 + 关联, 完整跑
# ======================================================================
def vectorize_terms(graph, base, model=None):
    """对图谱里所有还没向量的设定实体, 用 bge-m3 算向量(stage1 就做, 支持 RAG)。
    model: 向量化模型名, 默认 C.EMBED_MODEL(bge-m3)。
    embed.off 开启时跳过(省本地 CPU; 向量仅用于 RAG 检索, 不影响主体产物)。"""
    import config_schema as _CS
    if _CS.get("embed.off"):
        return 0
    import rag
    import config as _C
    model = model or _C.EMBED_MODEL
    todo = [t for t in graph["terms"] if not t.get("vector")]
    if not todo:
        return 0
    texts = [t["name"] + "：" + (t.get("definition") or "") for t in todo]
    vecs = rag.embed_texts(base, model, texts)
    n = 0
    for t, v in zip(todo, vecs):
        if v:
            t["vector"] = v
            n += 1
    return n


def run_setting_agent(scenes, base, model, graph_path, parallel=4):
    """为场景序列构建设定知识图谱(识别/归并/关联/向量化)。
    parallel: 场景级并发(仅 LLM 抽取阶段并发, 卷积/向量化串行保确定性)。
    返回 (graph, 设定数, 关联系, 向量化数)。"""
    import time as _t
    from logbook import get_logbook as _lb
    lb = _lb()
    graph = load_graph(graph_path)
    n_scenes = len(scenes)
    t0 = _t.time()
    # 并发抽取: 每场景只调 LLM 拿 deltas(不碰共享 graph)
    if parallel > 1 and n_scenes > 1:
        from concurrent.futures import ThreadPoolExecutor
        def _one(sc):
            existing = [t["name"] for t in graph["terms"]]
            deltas = extract_settings(sc, base, model, existing)
            return sc, deltas
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            results = list(pool.map(_one, scenes))
    else:
        results = []
        for i, sc in enumerate(scenes):
            existing = [t["name"] for t in graph["terms"]]
            deltas = extract_settings(sc, base, model, existing)
            results.append((sc, deltas))
            if (i + 1) % 200 == 0:
                lb.progress("setting", done=i + 1, total=n_scenes)
    # 抽取统计(D1 排查: 成功/失败/每25章图谱规模)
    n_ok = sum(1 for _, d in results if d)
    n_empty = n_scenes - n_ok
    lb.info("setting", "设定抽取统计", scenes=n_scenes, ok=n_ok, empty=n_empty,
            empty_rate=round(n_empty / max(n_scenes, 1) * 100, 1),
            elapsed=round(_t.time() - t0, 1))
    # 串行卷积(共享 graph, 归并/加边保确定性) + 每25章图谱规模 gauge
    ch_done = {}
    for i, (sc, deltas) in enumerate(results):
        if deltas:
            convolve_settings(sc, graph, deltas)
        ch = sc.get("chapter_no")
        ch_done[ch] = True
        if ch % 25 == 0 or i == n_scenes - 1:
            lb.gauge("setting", "terms", len(graph["terms"]), chapter=ch)
            lb.gauge("setting", "relations", len(graph["relations"]), chapter=ch)
            lb.progress("setting", done=i + 1, total=n_scenes,
                        extra=f"terms={len(graph['terms'])}")
    # 向量化永远用 bge-m3(C.EMBED_MODEL), 不传 LLM 模型名
    n_vec = vectorize_terms(graph, base, model=None)
    save_graph(graph_path, graph)
    lb.info("setting", "设定图谱完成", terms=len(graph["terms"]),
            relations=len(graph["relations"]), vec=n_vec,
            elapsed=round(_t.time() - t0, 1))
    return graph, len(graph["terms"]), len(graph["relations"]), n_vec


# ======================================================================
# 设定名归一(钢锯/接生宫锯 -> 宫锯) — 全书整段归并兜底
# ======================================================================
def normalize_terms(graph, base, model, settings_path, scenes=None):
    """同义/近义设定名归并: 用 LLM 判断哪些是同一实体, 统一规范名 + 合并 aliases/definition。"""
    terms = graph["terms"]
    if len(terms) < 2:
        return 0
    names = [t["name"] for t in terms]
    norm_system = ("你是设定名归一专家。判断下列设定名哪些指向**同一实体**(近义/同义/指代同一物), "
                   "给出规范名。只输出 JSON。")
    norm_user = """以下设定名(可能含别名/近义名):
{names}

请找出指向**同一实体**的设定名组, 每组给一个规范名:
严格输出 JSON 数组:
[{{"canonical": "规范名", "aliases": ["同实体的其他名字"]}}]"""
    try:
        raw = llm_client.chat(norm_system, norm_user.format(names="、".join(names)),
                              json_mode=False, num_predict=800, temperature=0.3)[0]
        groups = _parse_json_list(raw)
        if not isinstance(groups, list):
            return 0
        merged = 0
        for grp in groups:
            canonical = str(grp.get("canonical", "")).strip()
            aliases = [str(a).strip() for a in (grp.get("aliases") or []) if a]
            if not canonical:
                continue
            # 找 canonical 对应实体
            idx, term = find_term(graph, canonical)
            if term is None:
                # canonical 不在图谱里, 则用组内第一个能在图谱找到的实体作为锚
                for a in aliases:
                    idx, term = find_term(graph, a)
                    if term:
                        canonical = term["name"]
                        break
            if term is None:
                continue
            tgt = term
            tgt_idx = idx
            # 只归并**同类别**的实体的别名(避免'分娩'事件 吞并 '钢锯'物品)
            for a in aliases:
                if a == tgt["name"]:
                    continue
                idx2, t2 = find_term(graph, a)
                if idx2 is not None and idx2 != tgt_idx:
                    # 类别不同则跳过(保守, 不跨类别合并)
                    if t2.get("category") and tgt.get("category") and t2["category"] != tgt["category"]:
                        continue
                    # 把 t2 的 aliases/definition/source_scenes 并入 tgt
                    for al in t2.get("aliases", []):
                        if al not in tgt.get("aliases", []):
                            tgt.setdefault("aliases", []).append(al)
                    if t2.get("definition") and t2["definition"] not in tgt.get("definition", ""):
                        tgt["definition"] = (tgt.get("definition", "") + "；" + t2["definition"]) if tgt.get("definition") else t2["definition"]
                    tgt["source_scenes"] = sorted(set(tgt.get("source_scenes", []) + t2.get("source_scenes", [])))
                    if a not in tgt.get("aliases", []):
                        tgt.setdefault("aliases", []).append(a)
                    graph["terms"].pop(idx2)
                    merged += 1
        save_graph(settings_path, graph)
        return merged
    except Exception:
        return 0


# ======================================================================
# 兼容接口: 主agent 下发名词查询(term -> 客观定义)
# 优先用 settings_graph 里的定义, 否则用 scenes 上下文临时查。
# ======================================================================
SETTING_QUERY_SYSTEM = (
    "你是网络小说设定/名词解释专家。根据给定上下文, 用客观口吻解释名词的含意, 只输出 JSON。")
SETTING_QUERY_USER = """名词「{term}」出现在以下场景上下文:
{contexts}

请给出「{term}」的**客观定义**(它是什么/代表什么, 2-3句), 不要用疑问口吻。
严格输出 JSON: {{"term": "{term}", "meaning": "客观定义", "category": "世界观|势力|物品|地点|规则|人物"}}"""
def query_term(term, scenes, base, model, graph_path=None, doubt_index=None):
    """查询名词的客观定义。优先从设定图谱拿, 否则用场景上下文临时查。
    doubt_index: 质疑指数, 控制上下文收集深度 (max_ctx)。
    """
    if doubt_index is None:
        doubt_index = llm_client.DOUBT_INDEX
    if graph_path and os.path.exists(graph_path):
        g = load_graph(graph_path)
        idx, t = find_term(g, term)
        if t and t.get("definition"):
            return t["definition"], t.get("category", "")
    # 临时查: 用场景上下文
    if not scenes:
        return None, None
    # 根据 doubt_index 调整 max_ctx
    max_ctx = 2 if doubt_index < 0.4 else 4 if doubt_index < 0.7 else 6
    ctx = _context_for_term(term, [s for s in scenes if isinstance(s, dict)], max_ctx)
    if not ctx:
        return None, None
    user = SETTING_QUERY_USER.format(term=term, contexts=ctx)
    try:
        raw = llm_client.chat(SETTING_QUERY_SYSTEM, user, json_mode=True,
                              num_predict=300, temperature=0.3)[0]
        d = json.loads(raw)
        return str(d.get("meaning", "")).strip(), str(d.get("category", "")).strip()
    except Exception:
        return None, None


def _context_for_term(term, scenes, max_ctx=4):
    """从场景里搜集含该名词的上下文片段(actinfo/notes/who)。"""
    parts = []
    n = 0
    for sc in scenes:
        blobs = []
        for it in (sc.get("actinfo") or []):
            c = str(it.get("content", ""))
            if term in c:
                blobs.append(c)
        if sc.get("where") and term in str(sc.get("where")):
            blobs.append(str(sc.get("where")))
        if str(sc.get("notes")) and term in str(sc.get("notes")):
            blobs.append(str(sc.get("notes")))
        for w in (sc.get("who") or []):
            wname = str(w.get("name", ""))
            if term in wname:
                blobs.append(wname)
        if blobs and n < max_ctx:
            parts.append("【第%s章 scene%s】%s" % (
                sc.get("chapter_no"), sc.get("scene_id"), "；".join(blobs[:3])))
            n += 1
    return "\n".join(parts)


# ======================================================================
# Stage2: 设定体系强化(分层 + 内在关联 + 矛盾检测)
# ======================================================================
STRENGTHEN_SYSTEM = (
    "你是**世界观架构师**。基于设定知识图谱, 整理出**清晰的设定体系**: 分层结构、"
    "核心关联、矛盾点。只输出 JSON。")

STRENGTHEN_USER = """以下是一部小说的设定图谱(实体+关联):
{terms}

请整理成**清晰的设定体系**:
{{
  "layers": [
    {{"name": "层级名(如'力量体系'/'秩序规则'/'世界背景')", "terms": ["属于该层的设定实体名", ...], "summary": "该层一句话概述"}}
  ],
  "core_links": [
    {{"from": "设定A", "to": "设定B", "relation": "关联描述(如'A是B的上位规则')"}}
  ],
  "conflicts": [
    {{"a": "设定A", "b": "设定B", "conflict": "矛盾/不一致之处"}}
  ],
  "summary": "整个世界观体系的一句话总结(设定体系清晰)"
}}
要求:
- layers 把实体按功能分层(力量/规则/组织/地点/物品/世界背景), 每层最多 6 个代表性实体
- core_links 只列**主干关联**(如'秩序'→'诸神游戏'), 最多 6 条
- conflicts 只列**确凿矛盾**(如某设定说X非有性繁殖但场景出现繁殖), 没有就 []
只输出 JSON。"""


def strengthen(graph, base, model, doubt_index=0.5):
    """Stage2: 设定体系强化——分层 + 主干关联 + 矛盾检测。
    返回 {layers, core_links, conflicts, summary}。"""
    terms = graph.get("terms", [])
    if not terms:
        return {"layers": [], "core_links": [], "conflicts": [],
                "summary": "(无设定图谱, 先运行 stage1_collect)"}
    # 取 top 实体(按 source_scenes 数或定义长度)
    top = sorted(terms, key=lambda t: -len(t.get("source_scenes") or []))[:30]
    lines = []
    for t in top:
        name = t.get("name", "")
        cat = t.get("category", "")
        dv = (t.get("definition") or "")[:60]
        lines.append("- %s [%s] %s" % (name, cat, dv))
    terms_text = "\n".join(lines)
    try:
        raw = llm_client.chat(STRENGTHEN_SYSTEM, STRENGTHEN_USER.format(terms=terms_text),
                              json_mode=True, num_predict=900, temperature=0.4)[0]
        d = json.loads(raw)
        if not isinstance(d, dict):
            return {"layers": [], "core_links": [], "conflicts": [],
                    "summary": ""}
        return {
            "layers": [x for x in (d.get("layers") or []) if isinstance(x, dict)][:8],
            "core_links": [x for x in (d.get("core_links") or []) if isinstance(x, dict)][:8],
            "conflicts": [x for x in (d.get("conflicts") or []) if isinstance(x, dict)][:6],
            "summary": str(d.get("summary", "")).strip(),
        }
    except Exception:
        return {"layers": [], "core_links": [], "conflicts": [], "summary": ""}