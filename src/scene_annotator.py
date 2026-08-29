# -*- coding: utf-8 -*-
"""
scene_annotator.py —— 创作解析标注（P2: 拉片画布的"导演注释"层）

思路:
  1. pick_key_scenes: 从全书场景中挑"关键场景"(结论引用 + 情绪极值章 + 证据密集) ≈ 10-15%
  2. annotate_scenes: 分批批量 LLM 标注每个关键场景的创作意图
     plot_function ∈ [埋设伏笔,悬念制造,情绪铺垫,反转设计,高潮引爆,
                       节奏过渡,蒙太奇切换,人物塑造,世界观展露,信息解密]
     + note: 一句话说明作者为什么这么安排(拉片注释)
  3. 输出 stage1/scenes_annotations.json, 面板场景卡上 🎬 标记

成本: 每批 40 场景一次调用, 300 章 ~10 次; 只标关键场景, 不标全部。
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import llm_client
import config as C
from logbook import get_logbook as _get_logbook

PLOT_FUNCTIONS = ["埋设伏笔", "悬念制造", "情绪铺垫", "反转设计", "高潮引爆",
                  "节奏过渡", "蒙太奇切换", "人物塑造", "世界观展露", "信息解密"]

ANNOT_SYSTEM = (
    "你是**资深网文编辑**。从小说关键场景的摘要中, 判断每个场景的**创作意图**"
    "(作者为什么这么写), 只输出 JSON。")

ANNOT_USER = """以下是从小说中挑选的**关键场景摘要**(章节/地点/人物/原文片段):
{scenes}

请为**每一个**场景都标注(共 {n} 个, 一个都不能少):
- plot_function: 创作意图, 从 [{funcs}] 中选最贴切的一个(可自行补充更准确的词, 但保持简短)
- note: 一句话拉片注释(作者为什么这么安排, 具体可引用原文, 禁止空泛)

【输出】严格 JSON 数组, 包含全部 {n} 个元素, 每个元素:
{{"scene_id": "原样回填", "plot_function": "创作意图", "note": "一句话注释(12-30字, 具体)"}}
要求: scene_id 必须与输入完全一致; 只输出 JSON 数组, 不要输出任何其他文字。"""


def pick_key_scenes(scenes_meta, clue_graph=None, arc=None, topk_ratio=0.12,
                    max_per_chapter=3):
    """挑选关键场景:
    1. 被推理结论引用的场景(evidence_ids → scene_id)
    2. 情绪极值章(arc 中 |sentiment| 最高的 15% 章)的场景
    3. 证据密集场景(n_ev>=3)
    合并去重, 总量 ≈ len(scenes)*topk_ratio, 每章最多 max_per_chapter。
    返回 [(scene, 原因)]"""
    by_id = {str(s.get("scene_id")): s for s in scenes_meta}
    pick = {}
    reasons = {}
    # 1) 结论引用
    if clue_graph:
        ev_by_scene = {}
        for e in clue_graph.get("evidence", []):
            ev_by_scene.setdefault(str(e.get("scene_id")), []).append(e)
        for c in clue_graph.get("conclusions", []):
            for eid in (c.get("evidence_ids") or []):
                sid = None
                for e in ev_by_scene.values():
                    for ee in e:
                        if ee.get("id") == eid:
                            sid = str(ee.get("scene_id"))
                            break
                    if sid:
                        break
                if sid and sid in by_id:
                    pick[sid] = by_id[sid]
                    reasons[sid] = "结论引用"
        # 结论 chapter 场景
        for c in clue_graph.get("conclusions", []):
            ch = c.get("chapter_no")
            if ch:
                for s in scenes_meta:
                    if s.get("chapter_no") == ch:
                        pick[str(s.get("scene_id"))] = s
                        reasons[str(s.get("scene_id"))] = "结论章"
    # 2) 情绪极值章
    if arc:
        n_ch = len(arc)
        ranked = sorted(arc, key=lambda a: -abs(a.get("sentiment", 0)))
        hot_chs = {a.get("ch") for a in ranked[:max(3, int(n_ch * 0.12))]}
        for s in scenes_meta:
            if s.get("chapter_no") in hot_chs:
                pick[str(s.get("scene_id"))] = s
                reasons.setdefault(str(s.get("scene_id")), "情绪极值")
    # 3) 证据密集
    for s in scenes_meta:
        if (s.get("n_ev") or 0) >= 3:
            pick[str(s.get("scene_id"))] = s
            reasons.setdefault(str(s.get("scene_id")), "证据密集")
    # 4) 每章上限 + 总量控制
    per_ch = {}
    out = []
    for sid, s in sorted(pick.items(), key=lambda kv: (-(kv[1].get("n_ev") or 0),
                                                         kv[1].get("chapter_no", 0))):
        ch = s.get("chapter_no")
        if per_ch.get(ch, 0) >= max_per_chapter:
            continue
        per_ch[ch] = per_ch.get(ch, 0) + 1
        out.append((s, reasons.get(sid, "")))
        if len(out) >= max(20, int(len(scenes_meta) * topk_ratio)):
            break
    return out


def annotate_scenes(scenes_meta, base, model, clue_graph=None, arc=None,
                    batch=1):
    """关键场景批量 LLM 标注。返回 {schema, scenes: [{scene_id, plot_function, note}]}
    batch=1: dots.ai 对 JSON 数组执行不佳(实测每次只回 1 个对象), 单场景一次调用保完成率。"""
    lb = _get_logbook()
    keys = pick_key_scenes(scenes_meta, clue_graph, arc)
    lb.info("annotate", "关键场景挑选", n=len(keys), total=len(scenes_meta),
            ratio=round(len(keys) / max(len(scenes_meta), 1) * 100, 1))
    if not keys:
        return {"schema": 1, "scenes": []}
    results = []
    for i in range(0, len(keys), batch):
        chunk = keys[i:i + batch]
        lines = []
        for s, reason in chunk:
            who = ",".join(str(w.get("name") or w) for w in (s.get("who") or [])[:4])
            txt = str(s.get("raw_text") or "").replace("\n", " ")[:110]
            lines.append('[scene_id="%s" 第%s章 地点:%s 人物:%s 原因:%s] %s'
                         % (s.get("scene_id"), s.get("chapter_no"),
                            s.get("where"), who, reason, txt))
        user = ANNOT_USER.format(scenes="\n".join(lines), funcs="/".join(PLOT_FUNCTIONS),
                                 n=len(chunk))
        lb.info("annotate", "批次 LLM", batch_no=i // batch + 1,
                scenes=len(chunk))
        try:
            raw = llm_client.chat(ANNOT_SYSTEM, user, json_mode=True,
                                  num_predict=2000, temperature=0.3, role="review")[0]
            arr = _extract_list(raw)
            lb.info("annotate", "批次解析", got=len(arr), want=len(chunk))
            for d in arr:
                if isinstance(d, dict) and d.get("scene_id"):
                    results.append({
                        "scene_id": str(d["scene_id"]),
                        "plot_function": str(d.get("plot_function", "")).strip() or "节奏过渡",
                        "note": str(d.get("note", "")).strip(),
                    })
        except Exception as e:
            lb.error("annotate", "批次失败", batch_no=i // batch + 1, err=str(e)[:150])
    # 去重
    seen, uniq = set(), []
    for r in results:
        if r["scene_id"] not in seen:
            seen.add(r["scene_id"])
            uniq.append(r)
    lb.info("annotate", "标注完成", annotated=len(uniq), batches=(len(keys) + batch - 1) // batch)
    return {"schema": 1, "scenes": uniq}


def _extract_list(raw):
    """容忍格式: 纯数组 / {"result":[...]} / 截断多对象流。"""
    if not raw:
        return []
    s = raw.strip()
    try:
        d = json.loads(s)
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            for k in ("result", "scenes", "data"):
                if isinstance(d.get(k), list):
                    return d[k]
    except Exception:
        pass
    # 截断的对象流 {..}{..}
    import re
    arr = []
    for m in re.finditer(r'\{[^{}]*"scene_id"[^{}]*\}', s):
        try:
            arr.append(json.loads(m.group()))
        except Exception:
            pass
    if arr:
        return arr
    # 3) 最后一个未闭合对象(截断)
    try:
        i = s.rfind('{"scene_id"')
        if i >= 0:
            return [json.loads(s[i:])]
    except Exception:
        pass
    return []


def annotate_file(scenes_meta_path, clue_path, out_path, base, model):
    """CLI 入口: 读 scenes_meta + clue_graph, 标注, 写 scenes_annotations.json"""
    scenes = json.load(open(scenes_meta_path, encoding="utf-8")).get("scenes", [])
    cg = {}
    if os.path.exists(clue_path):
        try:
            cg = json.load(open(clue_path, encoding="utf-8"))
        except Exception:
            pass
    # 从 style_analysis 拿 arc(情绪极值章)
    arc = None
    sp = os.path.join(os.path.dirname(out_path), "style_analysis.json")
    if os.path.exists(sp):
        try:
            arc = json.load(open(sp, encoding="utf-8")).get("chapter", {}).get("arc")
        except Exception:
            pass
    r = annotate_scenes(scenes, base, model, clue_graph=cg, arc=arc)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=2)
    return r


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="创作解析标注")
    p.add_argument("--stage1", default=None, help="stage1 产物目录(默认取最新)")
    a = p.parse_args()
    if a.stage1:
        s1 = a.stage1
    else:
        import stage2_mine as M
        s1 = M._latest_stage1_dir()
    r = annotate_file(os.path.join(s1, "scenes_meta.json"),
                      os.path.join(s1, "clue_graph.json"),
                      os.path.join(s1, "scenes_annotations.json"),
                      C.OLLAMA_BASE, None)
    print("标注完成:", len(r["scenes"]), "个场景")
