# -*- coding: utf-8 -*-
"""
entity_registry.py —— 全局实体注册表（pedia 核心能力：别名→规范名归一化）

职责（从原 clue_agent 剥离保全）:
  每章一次 LLM，抽取稳定实体（人物/组织/体系/地点/物品）+ 别名，
  产出 entity_registry.json。下游 build_stage2_compat / cli 归档据此做
  「克莱恩 = 周明瑞 = 愚者」式别名→规范名归一（union-find 连通分量）。

注意：本模块只做实体归一化，不含任何「剧情推理 / 暗线收集」逻辑——
深度推理（clue_graph）已整体迁移到 studio，pedia 开源仓不再保留。

产出：output/pedia_<书>_<日期>/stage1/entity_registry.json
  { chapter_no: [ {canonical, aliases[], category} ] }
"""
import sys
import os
import json as _j

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import llm_client
import config as C


# ======================================================================
# 工具
# ======================================================================
def _strip_fence(s):
    """剥 ```json 围栏（非 json_mode 时模型可能用 markdown 包裹 JSON）。"""
    if not s:
        return s
    t = str(s).strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t.lstrip("`")
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


# ======================================================================
# 全局实体注册表（每章抽实体 + 别名，供归一化 / prompt 注入）
# ======================================================================
REG_SYSTEM = (
    "你是小说的『实体注册官』。从章节文本里抽取**稳定实体**（人物/组织/体系/地点/物品），"
    "并归并别名。只输出 JSON。")

REG_USER = """以下是小说第{chapter_no}章的若干场景摘要：

{scenes}

请抽取本章**稳定实体**，每个实体给规范名 + 别名：
[
  {{"canonical": "规范名(如'程实')", "aliases": ["同一实体的其他叫法(如'程哥''实哥'), 无则[]"], "category": "人物|组织|体系|地点|物品"}}
]
要求：
- 只收**跨场景稳定出现**的实体（出现 ≥2 次或明显重要），不收一次性龙套。
- 别名必须是同一实体（如'医生'与'接生大夫'），不收同场景无关词。
- 每章最多 12 个实体。只输出 JSON 数组。"""


def extract_entities_per_chapter(scenes_by_chapter, base, model):
    """每章一次 LLM，抽稳定实体 + 别名。返回 {chapter_no: [实体dict]}。"""
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
            d = _j.loads(_strip_fence(raw))
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
