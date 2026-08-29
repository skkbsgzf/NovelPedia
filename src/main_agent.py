# -*- coding: utf-8 -*-
"""
main_agent.py —— 主agent(读者 · 主编剧 · 统筹者)

stage1 读感采用「证据收集 → 图谱整合 → 批量推理」架构(对应讨论结论):

  Stage1a 证据收集(clue_agent): 每场景一次轻量 LLM, 只输出「事实池」
           (原文摘录+涉及实体), 0推理, token 硬上限
  Stage1b 图谱整合(clue_agent): 证据 bge-m3 向量化 → 增量聚类(语义相近自动归簇)
           → clue_graph.json(证据/簇/实体/结论)
  Stage1c 批量推理(clue_agent): 簇权重达阈值 → 一次 LLM 调用出多条结论
           (fact/reason/hypothesis/chain + 引用证据ID)

本模块职责(编排层):
  - 调用 clue_agent 跑完整线索图谱
  - 文风统合(手法实例积累到阈值后批量分析)
  - 名词疑点登记 + 设定子agent 补全定义
  - 汇总输出 reader_feelings.json(结论+文风) / settings_map.json / reader_memory.json

doubt_index 是"杠精属性"连续旋钮:
  - 簇触发阈值: 0.3→6条证据, 0.5→4条, 0.7→3条, 0.9→2条 (线性插值)
  - 证据token预算: 低指数更短更快
"""
import sys
import os
import json
import datetime
import re

sys.path.insert(0, os.path.dirname(__file__))
import llm_client


# ======================================================================
# 数据结构初始化
# ======================================================================
def empty_feelings():
    return {"schema": 3, "entries": []}


def empty_settings_map():
    return {"schema": 1, "terms": []}


def empty_memory():
    return {
        "schema": 3,
        "position": {"chapter": 0, "scene": 0},
        "understanding": {"world_view": "", "main_thread": "", "tone": "",
                          "open_threads": []},
        "puzzles": [],       # 结论(疑点)登记: 来自 clue_graph.conclusions
        "techniques": [],    # 文风手法实例(待统合)
        "noun_doubts": [],   # [{term, ask, status(open/looked_up), meaning}]
        "resolved_noun_terms": {},
    }


def load_json(path, empty_factory):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            return d
        except Exception:
            return empty_factory()
    return empty_factory()


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ======================================================================
# 工具
# ======================================================================
def _similar(a, b):
    """判断两条疑点是否同一问题(共享关键名词/人物/疑问词)。"""
    if not a or not b:
        return False
    if a == b:
        return True
    ta = set(re.findall(r'[\u4e00-\u9fff]', a))
    tb = set(re.findall(r'[\u4e00-\u9fff]', b))
    if not ta or not tb:
        return False
    inter = len(ta & tb)
    return inter / min(len(ta), len(tb)) > 0.6


def update_memory_with_feelings(memory, scene, feelings, noun_doubts):
    """把场景感受并入工作记忆(名词待查 + 结论登记)。"""
    chapter = scene.get("chapter_no")
    scene_id = scene.get("scene_id")
    memory["position"] = {"chapter": chapter, "scene": scene_id}
    known_terms = {d["term"] for d in memory["noun_doubts"]}
    for nd in noun_doubts:
        term = str(nd.get("term", "")).strip()
        if term and term not in known_terms:
            memory["noun_doubts"].append({
                "term": term, "ask": nd.get("ask", ""),
                "status": "open", "meaning": ""})
            known_terms.add(term)
    return memory


# ======================================================================
# 主agent 调度(编排层): 证据收集 -> 图谱 -> 批量推理 + 文风 + 名词
# ======================================================================
def run_main_agent(scenes, base, model, feelings_path, settings_path,
                   memory_path, clue_graph_path=None, doubt_index=None,
                   style_ratio=0.1, style_seed=None, db_path=None, chapters=None):
    """主agent 编排: 线索图谱(收集/整合/批量推理) + 文风独立采样 + 名词查询。

    scenes: 每个是 {scene_id, chapter_no, who, where, actinfo, notes}
    clue_graph_path: 线索图谱输出路径(默认与 feelings 同目录)
    doubt_index: 杠精属性 (0-1), 默认从 llm_client.DOUBT_INDEX 读取。
    style_ratio/style_seed: 文风采样比例(默认10%)与随机种子(默认随机)。
    """
    if doubt_index is None:
        doubt_index = llm_client.DOUBT_INDEX
    feelings = load_json(feelings_path, empty_feelings)
    settings_map = load_json(settings_path, empty_settings_map)
    memory = load_json(memory_path, empty_memory)
    if clue_graph_path is None:
        clue_graph_path = os.path.join(os.path.dirname(feelings_path), "clue_graph.json")

    import clue_agent

    # ---- [1] 线索图谱: 证据收集(事实池) -> 向量聚类 -> 批量推理 ----
    print(f"  质疑指数 {doubt_index} | 簇触发阈值 {clue_agent.cluster_threshold(doubt_index)} 条证据")
    g, n_ev, n_cl, n_concl = clue_agent.run_clue_agent(
        scenes, base, model, clue_graph_path, doubt_index)

    # 结论 -> feelings entries (疑点)
    for concl in g.get("conclusions", []):
        feelings["entries"].append({
            "type": "疑点",
            "fact": concl["fact"],
            "reason": concl["reason"],
            "hypothesis": concl["hypothesis"],
            "chain": concl["chain"],
            "track": concl.get("track", True),
            "topic": concl["cluster_id"],
            "evidence": concl["evidence_ids"],
            "chapter_no": concl.get("chapter_no"),
        })
        # 登记到 memory.puzzles
        if not any(p.get("id") == concl["id"] for p in memory["puzzles"]):
            memory["puzzles"].append({
                "id": concl["id"], "topic": concl["cluster_id"],
                "fact": concl["fact"], "reason": concl["reason"],
                "hypothesis": concl["hypothesis"], "chain": concl["chain"],
                "evidence_ids": concl["evidence_ids"],
                "status": "open", "answer": None})

    # ---- [2] 名词疑点: 从证据实体里收集陌生名词 ----
    noun_doubts = _collect_noun_doubts(g, settings_map)
    for nd in noun_doubts:
        term = nd["term"]
        if not any(t["term"] == term for t in settings_map["terms"]):
            settings_map["terms"].append({
                "term": term, "meaning": "待查",
                "source_chapter": None})
        update_memory_with_feelings(memory, {"chapter_no": None, "scene_id": None},
                                    [], [nd])

    # ---- [3] 文风独立采样分析(全文随机10%, 长句:短句=7:3) ----
    try:
        import style_sampler
        corpus = style_sampler.sample_style_text(
            db_path=db_path, chapters=chapters, ratio=style_ratio, seed=style_seed)
        print(f"  文风采样: {len(corpus['sampled'])} 段, {corpus['chars']} 字 "
              f"(长句 {corpus['long_chars']} / 短句 {corpus['short_chars']})")
        style = style_sampler.analyze_style(corpus, base, model)
        if style:
            feelings["entries"].append({
                "type": "文风",
                "fact": style["fact"],
                "examples": style.get("examples", []),
                "chain": style.get("chain", ""),
                "track": False,
                "sampled_chars": style.get("sampled_chars"),
                "seed": style.get("seed"),
            })
            # 独立存档(供可视化): style_analysis.json
            import os as _os
            style_path = _os.path.join(_os.path.dirname(feelings_path),
                                       "style_analysis.json")
            save_json(style_path, style)
    except Exception as e:
        print(f"  [warn] 文风采样失败: {e}")

    # ---- [4] 设定子agent 补全名词含义 ----
    try:
        import setting_agent
        for nd in memory.get("noun_doubts", []):
            term = nd.get("term", "")
            existing = next((t for t in settings_map["terms"] if t["term"] == term), None)
            if existing and existing.get("meaning") and existing["meaning"] != "待查":
                nd["status"] = "looked_up"
                continue
            meaning, category = setting_agent.query_term(term, scenes, base, model)
            if meaning:
                if existing:
                    existing["meaning"] = meaning
                    existing["category"] = category
                else:
                    settings_map["terms"].append({
                        "term": term, "meaning": meaning, "category": category})
                nd["status"] = "looked_up"
                nd["meaning"] = meaning
    except Exception as e:
        print(f"  设定子agent 查询失败: {e}")

    save_json(feelings_path, feelings)
    save_json(settings_path, settings_map)
    save_json(memory_path, memory)
    return {"feelings": len(feelings["entries"]), "new": len(feelings["entries"]),
            "settings_terms": len(settings_map["terms"]),
            "puzzles": len(memory.get("puzzles", [])),
            "evidence": n_ev, "clusters": n_cl, "conclusions": n_concl,
            "noun_doubts": len(memory.get("noun_doubts", []))}


def _collect_noun_doubts(graph, settings_map):
    """从图谱实体里挑出陌生特有名词(出现频次>=2 且不在 settings_map 的)。"""
    known = {t["term"] for t in settings_map["terms"]}
    out = []
    for ent in sorted(graph.get("entities", []), key=lambda x: -x.get("count", 0)):
        name = ent.get("name", "")
        if not name or len(name) < 2 or name in known:
            continue
        # 过滤常见词: 只保留疑似特有名词(非纯人名称呼)
        if ent.get("count", 0) >= 2 and not _is_common_word(name):
            out.append({"term": name, "ask": "想知道它是什么意思?"})
            known.add(name)
    return out[:20]


_COMMON_WORDS = {"医生", "孕妇", "主角", "男人", "女人", "孩子", "老人", "众人",
                 "我们", "他们", "时间", "世界", "游戏", "秩序", "信仰"}


def _is_common_word(name):
    return name in _COMMON_WORDS
