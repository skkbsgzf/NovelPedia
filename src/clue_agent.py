# -*- coding: utf-8 -*-
"""
clue_agent.py —— 线索图谱 Agent（证据收集 → 图谱整合 → 批量推理）

设计哲学（对应讨论结论）:
  Stage1a 证据收集: 每场景一次轻量 LLM, 只输出「事实池」(原文摘录+涉及实体),
                     0 推理、0 判断, token 硬上限 —— 从物理上隔离事实与猜测
  Stage1b 图谱整合: 证据逐条 bge-m3 向量化 → 增量向量聚类(语义相近自动归簇,
                     别名天然归一) → clue_graph.json(证据节点/簇/实体/边)
  Stage1c 批量推理: 簇权重达到阈值(doubt_index 调节)的簇, 攒到一次 LLM 调用
                     批量输出多条结论(fact/reason/hypothesis/chain + 引用证据ID)
  Stage3 可视化:    结论 → 推理链(引用的证据簇 → 每条证据原文) → 图谱

产出: outputs/<书>_<时间戳>/stage1/clue_graph.json
  {
    "schema": 1,
    "evidence":  [ {id, scene_id, chapter_no, text(原文摘录), entities[], vector, cluster_id} ],
    "clusters":  [ {id, label, member_ids[], weight, synth_status(pending/done), conclusion_id} ],
    "conclusions":[ {id, cluster_id, fact, reason, hypothesis, chain, evidence_ids[]} ],
    "entities":  [ {name, count, vector} ],
    "meta": {doubt_index, threshold, evidence_total, clusters_total, ...}
  }
"""
import sys
import os
import json
import math
import datetime

sys.path.insert(0, os.path.dirname(__file__))
import llm_client
import config as C


# ======================================================================
# 数据结构
# ======================================================================
def _strip_fence(s):
    """剥 ```json 围栏(非 json_mode 时模型可能用 markdown 包裹 JSON)。"""
    if not s:
        return s
    t = str(s).strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t.lstrip("`")
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def empty_graph():
    return {"schema": 1, "evidence": [], "clusters": [], "conclusions": [],
            "entities": [], "relations": [], "meta": {"doubt_index": 0.5}}


def load_graph(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            for k in ("evidence", "clusters", "conclusions", "entities", "relations"):
                d.setdefault(k, [])
            return d
        except Exception:
            return empty_graph()
    return empty_graph()


def save_graph(path, g):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(g, f, ensure_ascii=False, indent=2)


# ======================================================================
# 阈值调节 (doubt_index -> 簇权重阈值 / token 预算)  [连续旋钮, 非开关]
# ======================================================================
def cluster_threshold(doubt_index):
    """簇触发批量推理所需的最小权重(权重 = 簇内证据数)。
    低指数: 证据攒得多才推理(快); 高指数: 见异常就推理(深)。
    """
    pts = [(0.3, 6), (0.5, 4), (0.7, 3), (0.9, 2)]
    d = max(0.0, min(1.0, doubt_index))
    if d <= pts[0][0]:
        return pts[0][1]
    if d >= pts[-1][0]:
        return max(2, pts[-1][1])
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        if x1 <= d <= x2:
            return round(y1 + (y2 - y1) * (d - x1) / (x2 - x1))
    return 4


def chain_depth(doubt_index):
    """批量推理时每条结论 chain 的深度档位。"""
    if doubt_index < 0.4:
        return 2
    if doubt_index < 0.7:
        return 3
    return 4


def evidence_token_budget(doubt_index):
    """证据收集 LLM 的输出 token 硬上限(注意力预算, 思路2)。
    低指数更短(更快), 高指数略长。"""
    if doubt_index < 0.4:
        return 300
    if doubt_index < 0.7:
        return 400
    return 500


# ======================================================================
# Stage1a-0: 全局实体注册表(优化 A: 每章抽实体+别名, 注入后续 prompt)
# ======================================================================
REG_SYSTEM = (
    "你是小说的『实体注册官』。从章节文本里抽取**稳定实体**(人物/组织/体系/地点/物品), "
    "并归并别名。只输出 JSON。")

REG_USER = """以下是小说第{chapter_no}章的若干场景摘要:

{scenes}

请抽取本章**稳定实体**, 每个实体给规范名 + 别名:
[
  {{"canonical": "规范名(如'程实')", "aliases": ["同一实体的其他叫法(如'程哥''实哥'), 无则[]"], "category": "人物|组织|体系|地点|物品"}}
]
要求:
- 只收**跨场景稳定出现**的实体(出现 ≥2 次或明显重要), 不收一次性龙套。
- 别名必须是同一实体(如'医生'与'接生大夫'), 不收同场景无关词。
- 每章最多 12 个实体。只输出 JSON 数组。"""


def extract_entities_per_chapter(scenes_by_chapter, base, model):
    """每章一次 LLM, 抽稳定实体 + 别名。返回 {chapter_no: [实体dict]}。"""
    import json as _j
    registry = {}
    for cn, ch_scenes in scenes_by_chapter.items():
        parts = []
        for sc in ch_scenes[:8]:
            act = sc.get("actinfo") or []
            acts = "；".join(str(a.get("content", ""))[:40] for a in act[:5])
            parts.append("scene%s: %s" % (sc.get("scene_id"), acts[:200]))
        user = REG_USER.format(chapter_no=cn, scenes="\n".join(parts))
        try:
            raw = llm_client.chat(REG_SYSTEM, user, json_mode=False,
                                  num_predict=600, temperature=0.2)[0]
            d = json.loads(_strip_fence(raw))
            if isinstance(d, dict):
                for k in ("entities", "data", "result"):
                    if isinstance(d.get(k), list):
                        d = d[k]
                        break
            if not isinstance(d, list):
                registry[cn] = []
                continue
            ents = []
            for it in d:
                if not isinstance(it, dict):
                    continue
                can = str(it.get("canonical", "")).strip()
                if not can:
                    continue
                ents.append({
                    "canonical": can,
                    "aliases": [str(a).strip() for a in (it.get("aliases") or []) if a],
                    "category": str(it.get("category", "")).strip(),
                })
            registry[cn] = ents[:12]
        except Exception:
            registry[cn] = []
    return registry


def registry_prompt_block(registry, chapter_no, limit=15):
    """把实体注册表转成注入 prompt 的文本块。"""
    ents = registry.get(chapter_no, []) or []
    if not ents:
        return ""
    lines = ["【全局实体表】(本章已识别的稳定实体, 引用实体名时用规范名):"]
    for e in ents[:limit]:
        al = ("/" + "/".join(e["aliases"][:3])) if e.get("aliases") else ""
        lines.append("- %s%s [%s]" % (e["canonical"], al, e.get("category", "")))
    return "\n".join(lines)


# ======================================================================
# Stage1a: 暗线收集(只捕 actinfo 表达不了的信号, 0推理, token 硬上限)
# ======================================================================
EVIDENCE_SYSTEM = (
    "你是小说的『暗线捕捉器』。明线行为(谁说了什么/做了什么)已由 actinfo 完整记录, "
    "你**只捕捉 actinfo 表达不了的暗线信号**——反常/矛盾/信息差/伏笔/隐藏关系。"
    "你只摘录原文事实, 0 推理、0 判断。禁止输出'可能/暗示/怀疑'类措辞。只输出 JSON。")

EVIDENCE_USER = """小说第{chapter_no}章的一个场景:

【场景】第{chapter_no}章 scene{scene_id}: {where}
【人物】{who}
【行为事件 actinfo】(明线, **已被记录, 不要重复摘录**): {actinfo}
【备注】{notes}
{registry}

请**只捕捉暗线信号**——即 actinfo 记录不了、但原文暗含的东西:

【A. clues 暗线】(供后续推理) —— 每条的 text 必须是**原文层面可观察**的, 0推理:
- 反常/矛盾: 行为与身份/常识/场合不符(如"医生听到呼救却慢条斯理")
- 信息差: 某人知道但未明说/刻意回避(如"答非所问")
- 重复模式: 与前文呼应/反复出现同一细节(如"多次打量")
- 伏笔: 疑似为后文埋设的细节(如"强调某物0差评")
- 隐藏关系: 人物间未言明的关联
entities: 涉及的实体名(人物/物品/组织/体系, 2-6个)

【B. techniques 手法实例】(作文风采样用)
- 具体写作手法: technique=手法名(白描/通感/视角切换/节奏控制/意象堆叠/对话留白/重复强调…)
- example=原文用词/句子(短), effect_hint=粗略效果(一句, 禁止'淋漓尽致'套话)

【输出】严格 JSON 对象(宁缺毋滥):
{{
  "clues": [
    {{"text": "暗线事实摘录(15-40字, 只陈述原文可观察的)", "entities": ["实体A", "实体B"], "confidence": 0.8}}
  ],
  "techniques": [
    {{"technique": "手法名", "example": "原文例子(短)", "effect_hint": "效果感受"}}
  ]
}}
要求: clues 最多 {max_items} 条; 若本场景**无暗线信号**(全是明面行为), 输出空数组;
**绝不摘录 actinfo 已覆盖的明面行为**(如"医生说我要生了"这类明面台词)。
confidence: 这条暗线**确实是原文暗含**的把握(0-1)。原文明确写出的反常=0.9+; 仅猜测性关联=0.4-0.6; 不确定是否暗线=0.3以下。
entities 若命中【全局实体表】用**规范名**, 未命中的才用原叫法。只输出 JSON 对象。"""


def collect_evidence(scene, base, model, doubt_index=0.5, registry=None, registry_block=""):
    """每场景一次轻量 LLM, 捕捉暗线信号(0推理) + 手法实例。
    registry/registry_block: 全局实体表(优化 A), 注入 prompt 让实体名统一。
    返回 (evidence 列表, techniques 列表)。"""
    import json as _j
    who = scene.get("who") or []
    actinfo = scene.get("actinfo") or []
    where = scene.get("where") or ""
    notes = scene.get("notes") or ""
    if not (who or actinfo or where or notes):
        return [], []
    budget = evidence_token_budget(doubt_index)
    max_items = 2 if doubt_index < 0.5 else 3 if doubt_index < 0.8 else 4
    if not registry_block and registry:
        registry_block = registry_prompt_block(registry, scene.get("chapter_no"))
    user = EVIDENCE_USER.format(
        chapter_no=scene.get("chapter_no", "?"),
        scene_id=scene.get("scene_id", "?"),
        who=_j.dumps(who, ensure_ascii=False)[:500],
        actinfo=_j.dumps(actinfo, ensure_ascii=False)[:1000],
        where=str(where),
        notes=str(notes)[:250],
        max_items=max_items,
        registry=registry_block,
    )
    try:
        raw = llm_client.chat(EVIDENCE_SYSTEM, user, json_mode=True,
                              num_predict=budget, temperature=0.2)[0]
        d = _j.loads(raw)
        if not isinstance(d, dict):
            return [], []
        # clues (暗线)
        fl = d.get("clues") or d.get("facts") or d.get("evidence") or []
        out = []
        for it in fl:
            if not isinstance(it, dict):
                continue
            text = str(it.get("text", "")).strip()
            if not text:
                continue
            ents = [str(e).strip() for e in (it.get("entities") or []) if e]
            # confidence(0-1): LLM 自评暗线可靠度; 缺失默认 0.6
            try:
                conf = float(it.get("confidence", 0.6))
            except (TypeError, ValueError):
                conf = 0.6
            conf = max(0.0, min(1.0, conf))
            out.append({"text": text[:80], "entities": ents[:6],
                        "confidence": conf})
        # 双保险: 与 actinfo 明面对比, 高度重复的摘录过滤掉
        if out and actinfo:
            out = _filter_duplicate_of_actinfo(out, actinfo, base)
        # techniques
        tl = d.get("techniques") or []
        techs = [{"technique": str(t.get("technique", "")).strip(),
                  "example": str(t.get("example", "")).strip(),
                  "effect_hint": str(t.get("effect_hint", "")).strip()}
                 for t in tl if isinstance(t, dict) and t.get("technique")]
        return out[:max_items], techs[:3]
    except Exception:
        return [], []


def _filter_duplicate_of_actinfo(clues, actinfo, base):
    """双保险: 把与 actinfo 明面行为高度相似的暗线摘录过滤掉。
    用 bge-m3 向量比对: 相似度 > 0.78 视为与明线重复, 丢弃。
    返回过滤后的 clues。"""
    try:
        import rag
        act_texts = []
        for a in actinfo:
            c = str(a.get("content", "")).strip()
            if c:
                act_texts.append(c[:60])
        if not act_texts:
            return clues
        texts = [c["text"] for c in clues] + act_texts[:6]
        vecs = rag.embed_texts(base, C.EMBED_MODEL, texts)
        n_clue = len(clues)
        clue_vecs, act_vecs = vecs[:n_clue], vecs[n_clue:]
        act_vecs = [v for v in act_vecs if v]
        if not act_vecs:
            return clues
        out = []
        for i, c in enumerate(clues):
            cv = clue_vecs[i] if i < len(clue_vecs) else None
            if not cv:
                out.append(c)
                continue
            best = max(_vec_cos(cv, av) for av in act_vecs)
            if best < 0.78:
                out.append(c)
        return out
    except Exception:
        return clues


# ======================================================================
# Stage1b: 图谱整合(向量化 + 增量聚类)
# ======================================================================
def _vec_cos(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def _cluster_center(cluster, graph):
    """簇中心向量 = 簇内证据向量均值。"""
    vecs = [e["vector"] for e in graph["evidence"]
            if e["id"] in cluster["member_ids"] and e.get("vector")]
    if not vecs:
        return None
    n = len(vecs)
    return [sum(v[i] for v in vecs) / n for i in range(len(vecs[0]))]


def integrate_evidence(graph, scene, evidence_list, base, model):
    """把场景证据向量化并入图谱(只存向量, 不立即聚类; 聚类在 collect 完后一次性做)。
    记录 last_seen/occurrence(支持归档, 调研 Wave1)。返回新增证据 id 列表。"""
    import rag
    if not evidence_list:
        return []
    texts = [e["text"] for e in evidence_list]
    vecs = rag.embed_texts(base, C.EMBED_MODEL, texts)
    chapter = scene.get("chapter_no")
    new_ids = []
    for ev, vec in zip(evidence_list, vecs):
        if not vec:
            continue
        eid = "E%d" % (len(graph["evidence"]) + 1)
        graph["evidence"].append({
            "id": eid,
            "scene_id": scene.get("scene_id"),
            "chapter_no": chapter,
            "text": ev["text"],
            "entities": ev.get("entities", []),
            "confidence": float(ev.get("confidence", 0.6)),
            "vector": vec,
            "cluster_id": None,
            "last_seen": chapter,      # 调研 Wave1: 归档依据
            "occurrence": 1,           # 出现次数(同文本重复出现则+1)
            "status": "active",        # active | archived | refuted
            "refuted_by": None,
        })
        new_ids.append(eid)
        # 实体节点统计 + occurrence/last_seen 更新
        for ent in ev.get("entities", []):
            ex = next((x for x in graph["entities"] if x["name"] == ent), None)
            if ex:
                ex["count"] += 1
                ex["last_seen"] = chapter
                ex.setdefault("occurrence", 0)
                ex["occurrence"] += 1
            else:
                graph["entities"].append({"name": ent, "count": 1,
                                          "last_seen": chapter, "occurrence": 1,
                                          "vector": None})
    return new_ids


def _cluster_all(graph, threshold=0.62):
    """一次性聚类: 全部证据向量化完成后, 基于证据两两相似度贪心聚类。
    避免增量聚类的"中心漂移"问题(大簇中心稀释后所有证据都归入)。
    threshold 0.62: 同主题证据相似度通常 >0.6, 不同主题 <0.5。
    返回簇数。"""
    evs = graph["evidence"]
    n = len(evs)
    if n == 0:
        return 0
    # 重置旧簇
    graph["clusters"] = []
    for e in evs:
        e["cluster_id"] = None
    # 贪心: 每证据与已建簇的代表(首元素)比较, 相似则归入, 否则开新簇
    for i, e in enumerate(evs):
        vec = e.get("vector")
        if not vec:
            continue
        assigned = False
        for cl in graph["clusters"]:
            rep_id = cl["member_ids"][0]
            rep = next((x for x in evs if x["id"] == rep_id), None)
            if rep and _vec_cos(vec, rep.get("vector")) >= threshold:
                e["cluster_id"] = cl["id"]
                cl["member_ids"].append(e["id"])
                cl["weight"] = len(cl["member_ids"])
                assigned = True
                break
        if not assigned:
            cid = "C%d" % (len(graph["clusters"]) + 1)
            graph["clusters"].append({
                "id": cid, "label": e["text"][:20],
                "member_ids": [e["id"]], "weight": 1,
                "avg_confidence": float(e.get("confidence", 0.6)),
                "last_seen": e.get("last_seen"),
                "synth_status": "pending", "conclusion_id": None})
            e["cluster_id"] = cid
    # 计算每簇平均置信 + last_seen(簇内最新)
    for cl in graph["clusters"]:
        members = [x for x in evs if x["id"] in cl["member_ids"]]
        confs = [e.get("confidence", 0.6) for e in members]
        cl["avg_confidence"] = round(sum(confs) / len(confs), 2) if confs else 0.6
        cl["last_seen"] = max((e.get("last_seen") or 0 for e in members), default=None)
    return len(graph["clusters"])


# ======================================================================
# Stage1c: 批量推理(簇权重达阈值 -> 一次调用出多条结论)
# ======================================================================
SYNTH_SYSTEM = (
    "你是**资深考据读者**兼小说主编剧。基于**证据簇**(多场景的事实摘录), 做**统合推理**, "
    "一次推理输出**多条结论**。每条结论必须引用具体证据。只输出 JSON。")

SYNTH_USER = """以下是若干**证据簇**(同一主题在多个场景的事实摘录, 编号即簇ID):

{clusters}

请对**每个簇**判断其事实共同指向什么, 输出**多条精选结论**(每个簇最多 1 条, 宁缺毋滥):
[
  {{
    "cluster_id": "C1",
    "fact": "把该簇证据串起来的一句话核心事实",
    "reason": "为什么异常(与前文/常识的冲突点)",
    "hypothesis": "综合推断(1-2句, 不下死结论, 可多假设)",
    "chain": "完整推理链({depth}句, 引用具体证据内容, 禁止'观察/推断'流程标签)",
    "track": true
  }}
]
要求:
- 只对**证据确实指向同一件事**的簇给结论; 证据分散无共同指向的簇, 跳过不输出。
- chain 必须引用证据内容(如"第X章孕妇怕宫锯, 第Y章医生吹嘘宫锯0差评, 两者都指向…")。
- 不脑补证据之外的内容。只输出 JSON 数组。"""


def batch_synthesize(graph, base, model, doubt_index, prior_str="", cur_chapter=None,
                     w_recent=None, k_freq=None, k_confirm=None):
    """对达到簇权重阈值的 pending 活跃簇做一次批量推理, 返回新结论列表(并写回图谱)。
    cur_chapter: 当前最大章号, 用于活跃层过滤(调研: 只喂活跃簇)。
    w_recent/k_freq/k_confirm: 归档参数覆盖(默认用 config)。"""
    th = cluster_threshold(doubt_index)
    # 置信阈值(优化 B / ERA-CoT 第4步): 低质疑指数只推高置信簇, 高指数接受低置信
    # 0.3→0.75, 0.5→0.65, 0.7→0.55, 0.9→0.45
    conf_th = 0.75 - 0.15 * doubt_index if doubt_index < 0.7 else 0.55 - 0.2 * (doubt_index - 0.7)
    ready = []
    for cl in graph["clusters"]:
        if cl["weight"] < th or cl["synth_status"] != "pending":
            continue
        # 活跃层过滤(调研 Wave1): 非活跃簇不喂推理
        if cur_chapter is not None and not is_active_cluster(
                cl, cur_chapter, w_recent, k_freq, k_confirm):
            cl["synth_status"] = "archived_pending"
            continue
        # 旧数据(无 confidence)不过滤, 保持向后兼容; 新数据按均置信过滤
        has_conf = any(e.get("confidence") is not None
                       for e in graph["evidence"] if e["id"] in cl["member_ids"])
        if has_conf and cl.get("avg_confidence", 0.6) < conf_th:
            # 权重补偿: 证据越多(多场景交叉验证), 置信要求越低
            # 每 8 条证据降 0.05 置信门槛, 最多降 0.15
            weight_bonus = min(0.15, (cl["weight"] - th) * 0.05)
            if cl.get("avg_confidence", 0.6) < conf_th - weight_bonus:
                continue
        ready.append(cl)
    if not ready:
        return []
    depth = chain_depth(doubt_index)
    # 组簇摘要
    parts = []
    for cl in ready[:12]:
        evs = [e for e in graph["evidence"] if e["id"] in cl["member_ids"]]
        lines = "\n".join(
            "   - (第%s章 scene%s) [置信%.2f] %s" % (e["chapter_no"], e["scene_id"],
                                                    e.get("confidence", 0.6), e["text"])
            for e in evs[:8])
        parts.append("【%s】权重=%d 均置信=%.2f\n%s" % (
            cl["id"], cl["weight"], cl.get("avg_confidence", 0.6), lines))
    clusters_text = "\n\n".join(parts)
    user = SYNTH_USER.format(clusters=clusters_text, depth=depth)
    try:
        raw = llm_client.chat(SYNTH_SYSTEM, user, json_mode=False,
                              num_predict=1200, temperature=0.5)[0]
        d = json.loads(_strip_fence(raw))
        if isinstance(d, dict):
            for k in ("conclusions", "results", "data", "items"):
                if isinstance(d.get(k), list):
                    d = d[k]
                    break
            else:
                # 单对象(单结论): 包装成列表, 兼容 dots.ai 返回单对象的情况
                if "cluster_id" in d or "fact" in d:
                    d = [d]
        if not isinstance(d, list):
            return []
        new_conclusions = []
        for it in d:
            if not isinstance(it, dict):
                continue
            cid = str(it.get("cluster_id", "")).strip()
            cl = next((x for x in graph["clusters"] if x["id"] == cid), None)
            if not cl:
                continue
            fact = str(it.get("fact", "")).strip()
            if not fact:
                continue
            chain = str(it.get("chain", "")).strip()
            if _is_template_chain(chain) or not chain:
                chain = fact
            concl = {
                "id": "P%d" % (len(graph["conclusions"]) + 1),
                "cluster_id": cid,
                "fact": fact,
                "reason": str(it.get("reason", "")).strip(),
                "hypothesis": str(it.get("hypothesis", "")).strip(),
                "chain": chain,
                "track": bool(it.get("track", True)),
                "evidence_ids": list(cl["member_ids"]),
                "chapter_no": max((e["chapter_no"] for e in graph["evidence"]
                                   if e["id"] in cl["member_ids"]), default=None),
            }
            graph["conclusions"].append(concl)
            cl["synth_status"] = "done"
            cl["conclusion_id"] = concl["id"]
            new_conclusions.append(concl)
        return new_conclusions
    except Exception:
        return []


_TEMPLATE_CHAIN_MARKS = ("观察→", "前文对比→", "推断→", "结论", "步骤", "流程")


def _is_template_chain(chain):
    if not chain:
        return True
    marks = sum(1 for m in _TEMPLATE_CHAIN_MARKS if m in chain)
    return marks >= 2


# ======================================================================
# 运行入口(供 stage1_collect.py / stage2_mine.py 调用)
# ======================================================================
def collect_only(scenes, base, model, graph_path, doubt_index=0.5, parallel=1,
                 registry=None):
    """Stage1: 逐场景捕捉暗线 -> 向量聚类 -> 保存图谱(不含结论)。
    支持场景级并发(parallel>1)。
    registry: 全局实体注册表(每章实体, 优化 A), 注入收集 prompt 统一实体名。
    返回 (graph, 新增证据数, 簇数)。"""
    from concurrent.futures import ThreadPoolExecutor
    g = load_graph(graph_path)
    th = cluster_threshold(doubt_index)
    print(f"  [clue] Stage1 暗线收集: doubt_index={doubt_index} 簇阈值={th} "
          f"token预算={evidence_token_budget(doubt_index)} 并发={parallel} "
          f"实体锚定={'开' if registry else '关'}")
    # 预计算每章的实体表 prompt 块(避免并发中重复算)
    reg_blocks = {}
    if registry:
        for cn in {sc.get("chapter_no") for sc in scenes}:
            reg_blocks[cn] = registry_prompt_block(registry, cn)
    # 场景级并发收集(每场景独立, 无跨场景状态)
    def _one(sc):
        return collect_evidence(sc, base, model, doubt_index,
                                registry=registry,
                                registry_block=reg_blocks.get(sc.get("chapter_no"), ""))
    if parallel > 1 and len(scenes) > 1:
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            results = list(pool.map(_one, scenes))
    else:
        results = [_one(sc) for sc in scenes]
    # 按原序整合(只存向量, 不增量聚类)
    n_ev = 0
    done = 0
    for sc, (evs, _techs) in zip(scenes, results):
        if evs:
            n_ev += len(evs)
            integrate_evidence(g, sc, evs, base, model)
        done += 1
        if done % 20 == 0 or done == len(scenes):
            print(f"    [收集进度] {done}/{len(scenes)} 场景, 累计 {len(g['evidence'])} 条暗线")
    # 一次性聚类(避免增量中心漂移)
    n_cl = _cluster_all(g, threshold=0.62)
    print(f"  [clue] 一次性聚类: {len(g['evidence'])} 条证据 -> {n_cl} 簇")
    g["meta"].update({
        "doubt_index": doubt_index, "threshold": th,
        "evidence_total": len(g["evidence"]),
        "clusters_total": n_cl,
        "stage": "collected",
    })
    save_graph(graph_path, g)
    return g, n_ev, n_cl


def synthesize_ready(graph, base, model, doubt_index=0.5, cur_chapter=None, do_consolidate=True):
    """Stage2: 先 consolidate(归档/证伪), 再对活跃 pending 簇做批量推理。
    返回新结论列表(已写回 graph, 需调用方 save)。"""
    if cur_chapter is None:
        cur_chapter = max((e.get("chapter_no") or 0 for e in graph["evidence"]), default=0)
    stats = {}
    if do_consolidate:
        stats = consolidate(graph, cur_chapter)
    n_concl = 0
    # 循环直到无新结论或达到簇数/每轮12的上限(300+ 簇需多轮)
    n_ready = sum(1 for c in graph["clusters"]
                  if c["synth_status"] == "pending" and c["weight"] >= cluster_threshold(doubt_index))
    max_rounds = max(3, (n_ready // 12) + 2)
    for _round in range(max_rounds):
        newc = batch_synthesize(graph, base, model, doubt_index, cur_chapter=cur_chapter)
        if not newc:
            break
        n_concl += len(newc)
    return n_concl, stats


def run_clue_agent(scenes, base, model, graph_path, doubt_index=0.5, parallel=1):
    """完整跑一遍(兼容旧入口): 暗线收集 -> 聚类 -> 批量推理。"""
    g, n_ev, n_cl = collect_only(scenes, base, model, graph_path, doubt_index, parallel)
    n_concl, _stats = synthesize_ready(g, base, model, doubt_index)
    g["meta"].update({
        "doubt_index": doubt_index, "threshold": cluster_threshold(doubt_index),
        "evidence_total": len(g["evidence"]),
        "clusters_total": len(g["clusters"]),
        "conclusions_total": len(g["conclusions"]),
    })
    save_graph(graph_path, g)
    return g, n_ev, n_cl, n_concl


# ======================================================================
# 归档/活跃层 (调研《长文本知识库归档_SOTA》Wave1/2)
# 参数从 config.py 读取(settings.json run.archive 可调), 函数亦可显式传入覆盖。
# ======================================================================
W_RECENT = getattr(C, "W_RECENT", 30)     # 最近N章出现过 → 活跃
K_FREQ = getattr(C, "K_FREQ", 3)          # ≥N章出现 → 活跃(非噪声)
W_THRESH = 4                              # 簇权重≥4 → 活跃(沿用推理阈值)
K_CONFIRM = getattr(C, "K_CONFIRM", 2)    # 被证实N次 → 永久活跃


def is_active_cluster(cl, cur_chapter, w_recent=None, k_freq=None, k_confirm=None):
    """簇是否活跃(参与推理): recent or frequent or weighted(调研第三节 OR 条件)。
    w_recent/k_freq/k_confirm 可显式覆盖, 默认用 config。"""
    w_recent = w_recent if w_recent is not None else W_RECENT
    k_freq = k_freq if k_freq is not None else K_FREQ
    k_confirm = k_confirm if k_confirm is not None else K_CONFIRM
    recent = cl.get("last_seen") is not None and (cur_chapter - cl.get("last_seen", 0)) <= w_recent
    frequent = cl.get("weight", 0) >= k_freq
    weighted = cl.get("weight", 0) >= W_THRESH
    confirmed = cl.get("confirm_count", 0) >= k_confirm
    return recent or frequent or weighted or confirmed


def consolidate(graph, cur_chapter, w_recent=None, k_freq=None, k_confirm=None):
    """代际压缩(调研: Sleep Consolidation / Graphiti 时序):
      1. 归档: 不活跃的证据/簇标记 archived(文件保留, 不参与推理)
      2. 实体级矛盾覆盖: 同实体相反属性声明, 高置信新声明覆盖旧声明(refuted)
    参数可显式覆盖, 默认用 config。返回统计 dict。"""
    w_recent = w_recent if w_recent is not None else W_RECENT
    k_freq = k_freq if k_freq is not None else K_FREQ
    k_confirm = k_confirm if k_confirm is not None else K_CONFIRM
    stats = {"archived_evidence": 0, "archived_clusters": 0, "refuted": 0}
    # 1. 证据级: last_seen 过期且低频 → archived
    for e in graph["evidence"]:
        if e.get("status") != "active":
            continue
        last = e.get("last_seen") or 0
        recent = (cur_chapter - last) <= w_recent
        if not recent and (e.get("occurrence", 1) < k_freq):
            e["status"] = "archived"
            stats["archived_evidence"] += 1
    # 2. 簇级: 不活跃簇标记(不删成员, 只标记)
    for cl in graph["clusters"]:
        if not is_active_cluster(cl, cur_chapter, w_recent, k_freq, k_confirm):
            cl["synth_status"] = cl.get("synth_status", "pending") if cl.get("synth_status") != "done" else "done"
            # 若 pending 且不活跃 → 标记为跳过推理
            if cl.get("synth_status") == "pending":
                cl["synth_status"] = "archived_pending"
                stats["archived_clusters"] += 1
    return stats


def active_clusters(graph, cur_chapter, w_recent=None, k_freq=None, k_confirm=None):
    """返回活跃簇列表(供 batch_synthesize 喂推理, 调研: 活跃层过滤)。"""
    return [cl for cl in graph["clusters"]
            if is_active_cluster(cl, cur_chapter, w_recent, k_freq, k_confirm)]
