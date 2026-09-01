# -*- coding: utf-8 -*-
"""
export_graph.py ——构建知识图谱数据 + 自包含HTML (角色关系图谱 + 设定图谱)
数据源
  - {--db}                    : scenes.who_json(每场景人物 / actinfo_json / notes / where
  - {--src}/characters.json   : 人物档案(身份/首次章关键事件/关系)
  - {--src}/settings.json     : 设定(类型/首见章related)
  - {--src}/outlines.json     : 章纲(主线, 供章节标题
产出(均在 {--out}):
  - graph_data.json : nodes/edges/chapters (每节点边带出现章节集合)
    (供 pedia index.html 内联消费, 不再单独产出图谱页面)
通用化 默认跑50 章《诡秘之主》对比产物 换任何小说只需指到对应的db 与LLM 产物目录。用法:
  python src/export_graph.py
  python src/export_graph.py --chapters 30 --db data/stage1_v2_30.db --src data/stage2_30
"""
import os
import re
import json
import sqlite3
import sys
import argparse
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C
import vizutil

DEFAULT_DB = C.DB_PATH
DEFAULT_SRC = C.STAGE2_DIR   # stage2 产物(characters/settings/outlines)在 outputs/<书>_<日期>/stage2/

# 设定类型 -> 颜色
SET_COLOR = {
    "人物身份": "#8b5cf6", "概念": "#06b6d4", "物品": "#f59e0b",
    "地点": "#10b981", "组织": "#ef4444", "力量": "#3b82f6",
}
CHAR_COLOR = "#4f7cff"

# Stage3 数据清洗: LLM 采集结果里同一实体的别名映射(本名 -> 规范名)。
# 采集算法常把同一历史人物拆成"本名"/"帝号/称号"两条 (如 罗塞尔·古斯塔夫/罗塞尔大帝),
# 在图谱层合并为一个节点, 避免同一人物在设定图谱里出现两次。
SETTING_ALIASES = {
    "罗塞尔大帝": "罗塞尔·古斯塔夫",   # 同一人 本名 vs 帝号
}

# 代号 -> 拥有者(人物身份设定里 "代号"这类实体应归属到真实角色, 与角色图谱联动)。
# 从 settings.description 里按 "X在Y中的代号" 自动抽取, 见 detect_codename_owner()。
CODENAME_RE = re.compile(r"(.+?)(?:在|是)?(.+?)中的代号")


def detect_codename_owner(desc, char_names):
    """从设定描述里抽取代号拥有者, 返回匹配到的角色主名 (无则 None)."""
    if not desc:
        return None
    m = CODENAME_RE.search(str(desc))
    if not m:
        return None
    owner = m.group(1).strip()
    if not owner:
        return None
    if owner in char_names:
        return owner
    for cand in char_names:
        if owner in cand or cand in owner:
            return cand
    return None


def load(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


def _stage1_fallback(SRC):
    """新架构兼容: mine 不再产出旧式 stage2/characters.json|settings.json,
    缺失时从 stage1 现成产物补全, 避免图谱读空。
      - 人物节点来自 entity_registry(仅 category==人物), canonical+aliases 与
        db 的 who_json 归一化对齐, 保证主要角色能按场景数据落点;
      - 设定节点来自 settings_graph.terms, 仅保留有 related 互联者, 控制规模
        (全书设定过万, 全量力导图不可用)。
    返回 (chars, sets_, alias) 其中 alias 为 别名/规范名 -> 规范名 的映射,
    供 who_json 归一化。
    """
    stage1 = os.path.join(os.path.dirname(SRC), "stage1")
    chars, sets_, alias = [], [], {}
    canon2aliases = {}
    er_path = os.path.join(stage1, "entity_registry.json")
    sg_path = os.path.join(stage1, "settings_graph.json")

    if os.path.exists(er_path):
        try:
            er = json.load(open(er_path, encoding="utf-8"))
            for lst in er.values():
                if not isinstance(lst, list):
                    continue
                for e in lst:
                    if not isinstance(e, dict):
                        continue
                    if e.get("category") != "人物":
                        continue
                    canon = str(e.get("canonical") or "").strip()
                    if not canon:
                        continue
                    alias[canon] = canon
                    als = canon2aliases.setdefault(canon, [])
                    for a in (e.get("aliases") or []):
                        a = str(a).strip()
                        if a and a not in als:
                            als.append(a)
                            alias[a] = canon
        except Exception:
            pass
    for canon, als in canon2aliases.items():
        chars.append({"name": canon, "aliases": als, "身份": "",
                      "首次出现章": None, "关键事件": [], "关系": []})

    if os.path.exists(sg_path):
        try:
            sg = json.load(open(sg_path, encoding="utf-8"))
            for t in (sg.get("terms") or []):
                if not isinstance(t, dict):
                    continue
                nm = str(t.get("name", "")).strip()
                if not nm:
                    continue
                rel = [str(r.get("to")) for r in (t.get("related") or [])
                       if isinstance(r, dict) and r.get("to")]
                if not rel:
                    continue  # 仅保留互联设定, 控制规模
                # 只保留互联度达阈值的设定, 避免上万孤立/弱节点(全书设定过万,
                # 力导图不可用且 4c 边构建随节点数线性放大)。阈值可用
                # GRAPH_MIN_REL 环境变量调整(默认 5: 与≥5 个其他设定互联)。
                min_rel = int(os.environ.get("GRAPH_MIN_REL", "5"))
                if len(rel) < min_rel:
                    continue
                sets_.append({
                    "name": nm,
                    "type": str(t.get("category", "")),
                    "description": str(t.get("definition", "")),
                    "first_seen": t.get("first_seen"),
                    "related": rel,
                    # 设定章节直接取自 settings_graph 的 source_scenes,
                    # 避免逐场景子串扫描(全书上万设定×上万场景 = 上亿次匹配, 极慢)
                    "_chapters": sorted(set(
                        [int(x) for x in (t.get("source_scenes") or []) if str(x).isdigit()]
                        + [x for x in (t.get("first_seen"), t.get("last_seen")) if isinstance(x, int)]
                    )),
                })
        except Exception:
            pass
    return chars, sets_, alias


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--out", default=C.OUTPUT_DIR)
    args = ap.parse_args()
    DB, SRC, OUT_DIR = args.db, args.src, args.out
    OUT_JSON = os.path.join(OUT_DIR, "graph_data.json")
    # ---- 1. 从db 读每场景人物 + actinfo/notes 文本 ----
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    rows = cur.execute(
        'SELECT chapter_no, who_json, actinfo_json, notes, "where" FROM scenes '
        "WHERE extract_status='ok'").fetchall()
    conn.close()

    # ---- 2. 读stage2 产物(提前, 别名/设定命中需要 ----
    chars = load(os.path.join(SRC, "characters.json")) or []
    sets_ = load(os.path.join(SRC, "settings.json")) or []
    outlines = load(os.path.join(SRC, "outlines.json")) or {}

    # 新架构兼容: mine 不再产出旧式 stage2/characters.json|settings.json,
    # 缺失时从 stage1 现成产物(entity_registry / settings_graph)补全。
    fb_chars, fb_sets, fb_alias = _stage1_fallback(SRC)
    if not chars:
        chars = fb_chars
    if not sets_:
        sets_ = fb_sets

    # 同人多条目归并: 按 aliases 无向图求连通分量, 得到去重人物主表
    # (原数据把 邓恩/邓恩·史密斯/马车夫、阿尔杰/阿尔杰·威尔逊 等拆成多条)
    chars, name2canon = vizutil.merge_alias_components(chars)

    # 别名映射: 所有别名-> 主名(含合并后各成员自身名); 先铺实体表(人物)再叠加角色自身
    alias2main = dict(fb_alias)
    main_chars = set()
    for c in chars:
        main = str(c.get("name", "")).strip()
        for al in (c.get("aliases") or []):
            alias2main.setdefault(str(al).strip(), main)
        alias2main.setdefault(main, main)
        main_chars.add(main)

    def norm(name):
        """把who_json 里的名字归一化到主要人物; 路人不映射则返回 None。"""
        n = str(name).strip()
        return alias2main.get(n) or None

    char_chapters = defaultdict(set)      # 人物名-> 出现章节
    cooccur = defaultdict(lambda: defaultdict(int))  # 人物A->B 共现次数
    char_meta = {}                        # 人物名-> {chapters, scenes}
    cooccur_chapters = defaultdict(set)   # (A,B) -> 章节集合

    for ch, who_j, act_j, notes, where in rows:
        try:
            who = json.loads(who_j or "[]")
        except Exception:
            who = []
        names = [norm(w.get("name", "")) for w in who if w.get("name")]
        names = sorted(set(n for n in names if n))   # 归一化去重+只留主要人物
        for n in names:
            char_chapters[n].add(ch)
            char_meta.setdefault(n, {"chapters": set(), "scenes": 0})
            char_meta[n]["chapters"].add(ch)
            char_meta[n]["scenes"] += 1
        # 共现: 同一场景内的人物两两成边
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = names[i], names[j]
                cooccur[a][b] += 1
                cooccur_chapters[frozenset((a, b))].add(ch)

    # ---- 3. 节点 ----
    nodes = []
    char_by_main = {}
    for c in chars:
        name = str(c.get("name", "")).strip()
        chapters = set(char_meta.get(name, {}).get("chapters", set()))
        # 从关键事件补章节
        for ev in (c.get("关键事件") or []):
            for m in re.findall(r"第(\d+)章", str(ev)):
                chapters.add(int(m))
        if c.get("首次出现章"):
            chapters.add(int(c["首次出现章"]))
        chapters = sorted(chapters)
        node = {
            "id": "c:" + name, "kind": "char", "name": name,
            "aliases": c.get("aliases") or [],
            "身份": c.get("身份", ""), "首次出现章": c.get("首次出现章"),
            "关键事件": c.get("关键事件") or [],
            "chapters": chapters,
            "scenes": char_meta.get(name, {}).get("scenes", 0),
            "weight": len(chapters),
            "color": CHAR_COLOR,
        }
        nodes.append(node)
        char_by_main[name] = node

    # 人名去重: settings.json 里常把已录入 characters.json 的人名再写一份"人物身份"
    # (如 梅丽莎/老尼尔/罗珊). 这类设定节点与角色节点完全重复, 在图谱层剔除,
    # 避免同一人物在角色图谱与设定图谱里各出现一次、关系被稀释。
    char_names = set(char_by_main)

    # 先按规范名合并同义设定(SETTING_ALIASES), 再剔除与角色重名的条目
    merged = {}
    order = []
    for s in sets_:
        name = str(s.get("name", "")).strip()
        if name in char_names:
            continue
        canon = SETTING_ALIASES.get(name, name)
        if canon not in merged:
            merged[canon] = {"type": s.get("type", ""), "desc": s.get("description", ""),
                             "first_seen": s.get("first_seen"), "related": [],
                             "chapters": sorted(set(s.get("_chapters", [])))}
            order.append(canon)
        g = merged[canon]
        if s.get("first_seen"):
            g["chapters"].append(int(s["first_seen"]))
        for r in (s.get("related") or []):
            if r not in g["related"]:
                g["related"].append(r)
        if len(str(s.get("description", ""))) > len(g["desc"]):
            g["desc"] = s.get("description", "")
        if s.get("type") and g["type"] != s.get("type"):
            g["type"] = s.get("type")

    set_by_name = {}
    for name in order:
        g = merged[name]
        chapters = sorted(set(g["chapters"]))
        node = {
            "id": "s:" + name, "kind": "setting", "name": name,
            "type": g["type"], "description": g["desc"],
            "first_seen": g["first_seen"],
            "chapters": chapters,
            "weight": len(chapters),
            "color": SET_COLOR.get(g["type"], "#9ca3af"),
        }
        nodes.append(node)
        set_by_name[name] = node

    # ---- 4. 边----
    edges = []
    relation_keys = set()      # 显式关系边对应的无序节点对, 供 4b O(1) 去重

    # 4a. 人物显式关系 (characters.关系: "与X: 描述")
    for c in chars:
        a = str(c.get("name", "")).strip()
        for rel in (c.get("关系") or []):
            m = re.match(r"与(.+?)[:：]\s*(.*)", str(rel).strip())
            if not m:
                continue
            b_raw = m.group(1).strip()
            label = m.group(2).strip()[:40]
            b_main = alias2main.get(b_raw)
            if not b_main:
                # 尝试部分匹配
                for cand in char_by_main:
                    if b_raw in cand or cand in b_raw:
                        b_main = cand
                        break
            if not b_main or b_main == a:
                continue
            key = frozenset((a, b_main))
            relation_keys.add(key)
            edges.append({
                "from": "c:" + a, "to": "c:" + b_main,
                "kind": "relation", "label": label or "关系",
                "chapters": sorted(cooccur_chapters.get(key, set())),
            })

    # 4b. 场景共现边(未在显式关系中出现的强共现
    for a, d in cooccur.items():
        for b, cnt in d.items():
            if cnt < 2:
                continue
            key = frozenset((a, b))
            if key in relation_keys:   # O(1) 去重, 避免逐边扫描(共现对多时 O(n^2) 卡死)
                continue
            edges.append({
                "from": "c:" + a, "to": "c:" + b,
                "kind": "cooccur", "label": f"同场景×{cnt}",
                "chapters": sorted(cooccur_chapters.get(key, set())),
            })

    # 4c. 设定 related 边(按合并后的规范名遍历, 同义设定的边归并到同一节点)
    seen_related = set()
    for name in order:
        g = merged[name]
        a_chs = set(g["chapters"])
        for r in g["related"]:
            b = str(r).strip()
            b = SETTING_ALIASES.get(b, b)
            b_id = None
            if b in set_by_name:
                b_id = "s:" + b
            elif b in alias2main:
                bm = alias2main[b]
                if bm in char_by_main:
                    b_id = "c:" + bm
            if not b_id or b_id == "s:" + name:
                continue
            key = frozenset(("s:" + name, b_id))
            if key in seen_related:
                continue
            seen_related.add(key)
            b_chs = set()
            if b in set_by_name:
                b_chs = set(set_by_name[b].get("chapters", []))
            elif b in char_chapters:
                b_chs = set(char_chapters[b])
            chs = sorted(a_chs | b_chs)
            edges.append({
                "from": "s:" + name, "to": b_id,
                "kind": "related", "label": "关联",
                "chapters": chs,
            })
        # 4d. 代号归属(代号->真实角色)暂缓: 原实现需对每设定做 O(设定×角色) 子串扫描,
        # 全书设定过万时单次 export 卡死数分钟; 后续改为预建 owner->角色索引再做 O(1) 关联。

    # ---- 5. 章节标题(泛化: 按实际数据最大章, 不写死50) ----
    ch_nos = set()
    for n in nodes:
        ch_nos.update(n.get("chapters") or [])
    for e in edges:
        ch_nos.update(e.get("chapters") or [])
    max_ch = max(ch_nos) if ch_nos else 0
    # 真实章标题取自 db 的 chapters; 章纲里的"主线"只是剧情摘要, 不是标题
    ch_title = {}
    try:
        c2 = sqlite3.connect(DB)
        ch_title = {r[0]: (r[1] or "") for r in c2.execute(
            "SELECT chapter_no, title FROM chapters")}
        c2.close()
    except Exception:
        pass
    chapters = []
    for cn in sorted(range(1, max_ch + 1)):
        title = ch_title.get(cn) or ""
        if not title:
            ol = outlines.get(str(cn)) or outlines.get(cn) or {}
            title = (ol.get("主线") or "")[:40]
        chapters.append({"no": cn, "title": title})

    # 权重 = 出现章数 + 关联边数 (出现广度 + 图谱连通度), 供节点大小分级
    degree = defaultdict(int)
    for e in edges:
        degree[e["from"]] += 1
        degree[e["to"]] += 1
    for n in nodes:
        n["weight"] = len(n.get("chapters") or []) + degree.get(n["id"], 0)

    # 控制规模: 全书人物极多, 全量力导图不可用; 角色节点按权重取 TopN,
    # 设定节点(已仅保留互联者)全部保留, 边仅保留两端都在保留集内。
    KEEP_CHARS = int(os.environ.get("GRAPH_TOP_CHARS", "600"))
    _sets = [n for n in nodes if n["kind"] == "setting"]
    _chars = sorted([n for n in nodes if n["kind"] == "char"],
                    key=lambda n: n.get("weight", 0), reverse=True)
    keep_ids = set(n["id"] for n in _sets) | set(n["id"] for n in _chars[:KEEP_CHARS])
    nodes = [n for n in nodes if n["id"] in keep_ids]
    edges = [e for e in edges if e["from"] in keep_ids and e["to"] in keep_ids]
    if len(_chars) > KEEP_CHARS:
        print(f"[info] 角色节点 {len(_chars)} 个, 按权重截断至 Top{KEEP_CHARS} (设 GRAPH_TOP_CHARS 可调)")

    data = {"nodes": nodes, "edges": edges, "chapters": chapters}
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    # ---- 6. 独立图谱页已废弃 ----
    # 旧版此处渲染 knowledge_graph.html (graph_template.html)。
    # 力导向图谱已内联进 pedia(index.html), 开源版不再产出独立图谱页, 模板已删除。
    print(f"已生成{OUT_JSON} ({os.path.getsize(OUT_JSON)/1024/1024:.1f} MB, "
          f"{len(nodes)} 节点 / {len(edges)} 边)")

    n_char = sum(1 for n in nodes if n["kind"] == "char")
    n_set = sum(1 for n in nodes if n["kind"] == "setting")
    n_rel = sum(1 for e in edges if e["kind"] == "relation")
    n_co = sum(1 for e in edges if e["kind"] == "cooccur")
    n_rd = sum(1 for e in edges if e["kind"] == "related")
    print(f"已生成{OUT_JSON}")
    print(f"  节点: 角色 {n_char} / 设定 {n_set} | 边 显式关系 {n_rel} / 共现 {n_co} / related {n_rd}")
    print(f"  章节: {len(chapters)} (1-{max_ch})")


if __name__ == "__main__":
    main()
