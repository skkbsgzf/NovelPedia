# -*- coding: utf-8 -*-
"""
character_agent.py —— 人物维度 (Stage1 聚合 / Stage2 简历)

设计:
  Stage1 (零 LLM, 纯规则): 从 actinfo 按人物聚合
    - 出现章节/场景、说过的关键句(say)、做过的关键事(do)
    - 与其他人物共现次数(关系候选)
    - 产出 character_facts.json: {name, appearances, sayings, doings, co_occurrences}

  Stage2 (一次 LLM/人物 或 批量): 结合 actinfo 聚合 + 暗线证据簇
    - 生成简历: 身份推测 / 行为轨迹 / 关系网 / 暗线疑点(挂推理结论)
    - 产出 characters_resume.json: {name, identity, trajectory, relations, doubts}
"""
import sys
import os
import json
import re

sys.path.insert(0, os.path.dirname(__file__))
import llm_client


# ======================================================================
# Stage1: 从 actinfo 聚合人物事实(纯规则, 零 LLM)
# ======================================================================
def collect_from_actinfo(scenes):
    """从场景 actinfo 按人物聚合言行轨迹。
    返回 {name: {appearances, sayings[], doings[], co_occurrences{name:count}}}"""
    chars = {}
    for sc in scenes:
        chapter = sc.get("chapter_no")
        scene_id = sc.get("scene_id")
        who = sc.get("who") or []
        names_now = set()
        for w in who:
            nm = str(w.get("name", "")).strip()
            if nm:
                names_now.add(nm)
                chars.setdefault(nm, {
                    "appearances": [], "sayings": [], "doings": [],
                    "co_occurrences": {}})
        for act in (sc.get("actinfo") or []):
            who_a = str(act.get("who", "")).strip()
            content = str(act.get("content", "")).strip()
            channel = str(act.get("channel", "do")).strip()
            if not who_a or not content:
                continue
            c = chars.setdefault(who_a, {
                "appearances": [], "sayings": [], "doings": [],
                "co_occurrences": {}})
            c["appearances"].append((chapter, scene_id))
            blob = "第%s章scene%s: %s" % (chapter, scene_id, content[:80])
            if channel == "say":
                c["sayings"].append(blob)
            else:
                c["doings"].append(blob)
            # 共现(同场景其他人物)
            for other in names_now:
                if other and other != who_a:
                    c["co_occurrences"][other] = c["co_occurrences"].get(other, 0) + 1
    # 去重 + 截断
    for nm, c in chars.items():
        c["appearances"] = sorted(set(c["appearances"]))
        c["sayings"] = c["sayings"][-12:]
        c["doings"] = c["doings"][-12:]
        c["co_occurrences"] = dict(sorted(
            c["co_occurrences"].items(), key=lambda x: -x[1])[:8])
    return chars


def save_facts(path, chars):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"schema": 1, "characters": chars}, f, ensure_ascii=False, indent=2)


# ======================================================================
# Stage2: 简历生成(结合 actinfo + 暗线)
# ======================================================================
RESUME_SYSTEM = (
    "你是**资深编辑**。为小说人物生成**简历式档案**——像求职简历一样清晰: 身份/经历/关系/疑点。"
    "只输出 JSON。")

RESUME_USER = """小说人物「{name}」的行为档案(来自 actinfo 明线聚合):

【出现】{appearances}
【说过的话(节选)】{sayings}
【做过的事(节选)】{doings}
【共现人物(关系候选)】{co_occ}
【相关暗线证据】(原文线索): {clues}

请为「{name}」生成**简历式档案**(用真实内容填充, 不要复述本提示词):
{{
  "name": "{name}",
  "identity": "基于证据判断该人物的身份(2-3句具体内容)",
  "trajectory": "按时间顺序的行为轨迹摘要(3-4句, 只述事实, 引用具体行为)",
  "relations": [{{"name": "其他人物名", "relation": "基于共现与言行的关系判断"}}],
  "doubts": ["与该人物相关的暗线疑点(引用证据内容, 最多3条)"],
  "personality": "基于言行推断的性格(关键词+一句说明)"
}}
要求: 内容必须来自上述档案, 不脑补; 身份不确定就写'暂未明示'; relations 最多 5 个; 只输出 JSON 对象, 不要输出 JSON 示例或占位符文字。"""


def build_resume(name, facts, clue_evidence, base, model):
    """为单个角色生成简历。facts 是 collect_from_actinfo 的单人条目。"""
    if not facts:
        return None
    appearances = "、".join("第%s章scene%s" % (a[0], a[1]) for a in facts["appearances"][:10]) or "(未明示)"
    sayings = "；".join(facts["sayings"][-6:]) or "(无)"
    doings = "；".join(facts["doings"][-6:]) or "(无)"
    co_occ = "、".join("%s(%d次)" % (k, v) for k, v in facts["co_occurrences"].items()) or "(无)"
    clues = "；".join(clue_evidence[:5]) or "(无)"
    user = RESUME_USER.format(
        name=name, appearances=appearances, sayings=sayings,
        doings=doings, co_occ=co_occ, clues=clues)
    try:
        raw = llm_client.chat(RESUME_SYSTEM, user, json_mode=True,
                              num_predict=700, temperature=0.4)[0]
        d = json.loads(raw)
        if not isinstance(d, dict) or not d.get("name"):
            return None
        return {
            "name": name,
            "identity": str(d.get("identity", "")).strip(),
            "trajectory": str(d.get("trajectory", "")).strip(),
            "relations": [r for r in (d.get("relations") or [])
                          if isinstance(r, dict) and r.get("name")][:5],
            "doubts": [str(x).strip() for x in (d.get("doubts") or []) if x][:3],
            "personality": str(d.get("personality", "")).strip(),
            "appearances": facts["appearances"],
        }
    except Exception:
        return None


def build_all_resumes(chars, clue_evidence_by_entity, base, model, parallel=1):
    """为全部人物生成简历(按出现次数排序, 至少出现 2 次的优先)。
    返回简历列表。clue_evidence_by_entity: {实体名: [暗线文本]}"""
    from concurrent.futures import ThreadPoolExecutor
    ranked = sorted(chars.items(), key=lambda kv: -len(kv[1]["appearances"]))
    # 只给出现 >=2 次 或 有暗线关联 的人物建简历
    targets = [(nm, fc) for nm, fc in ranked
               if len(fc["appearances"]) >= 2 or nm in clue_evidence_by_entity]
    if parallel > 1 and len(targets) > 1:
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            resumes = list(pool.map(
                lambda t: build_resume(t[0], t[1],
                                       clue_evidence_by_entity.get(t[0], []),
                                       base, model), targets))
    else:
        resumes = [build_resume(nm, fc, clue_evidence_by_entity.get(nm, []), base, model)
                   for nm, fc in targets]
    return [r for r in resumes if r]


# ======================================================================
# 暗线证据按实体索引(供简历引用)
# ======================================================================
def index_clues_by_entity(clue_graph):
    """把 clue_graph 的证据按实体名索引: {实体: [暗线文本...]}"""
    out = {}
    for e in clue_graph.get("evidence", []):
        for ent in e.get("entities", []):
            out.setdefault(ent, []).append(e["text"])
    return out
