# -*- coding: utf-8 -*-
"""
editor_review.py —— 全板块「编辑评述」agent

需求: 每个板块(概览/人物/设定/剧情/文风)结果展示之后, 加一段**专业编辑/专业读者视角**的
总结性评述: 作者习惯用什么方法 / 擅长什么 / 哪里不足 / 总结结论。
纯数据之外必须给"清晰的文字结论"。

实现:
  review_block(name, summary) -> 1 次 LLM -> {habits, strengths, weaknesses, summary}
  每个板块一次调用(batch=1 思路, dots.ai 单对象可靠), 5 板块 = 5 次调用。
  输出 stage1/editor_reviews.json: {schema, reviews: {板块: {text, ...}}}
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import llm_client
import config as C
from logbook import get_logbook as _get_logbook

REVIEW_SYSTEM = (
    "你是**资深网文编辑兼专业读者**, 对一部小说的拆书数据进行**编辑视角评述**。"
    "你的评述要像出版编辑给作者写的审读意见: 具体、诚实、有判断力。只输出 JSON。")

REVIEW_USER = """以下是「{block}」板块的机器聚合数据(来自小说拆书, 全部可溯源):

{data}

请以专业编辑/专业读者的视角, 给出**总结性文字评述**(不是复述数据, 是判断):
- habits:   这个作者**习惯使用**的方法/手法(2-3句, 具体到数据)
- strengths: 他**擅长**的地方(2-3句, 基于数据判断, 不空夸)
- weaknesses:他**做得不好或可改进**的地方(1-2句, 实事求是, 无则写"未见明显短板")
- summary:  整体总结结论(2-3句, 给读者/创作者的判断)

【输出】严格 JSON 对象, 只有这四个字段:
{{"habits": "...", "strengths": "...", "weaknesses": "...", "summary": "..."}}
要求: 每个字段 30-80 字; 必须引用具体数据(数字/章节/词)支撑判断; 禁止"淋漓尽致/文笔优美"类空话。"""


def _summarize_style(style):
    """文风板块摘要。"""
    w = style.get("word") or {}
    s = style.get("sentence") or {}
    p = style.get("para") or {}
    c = style.get("chapter") or {}
    fa = [x["w"] for x in (w.get("freq") or {}).get("a", [])[:8]]
    fv = [x["w"] for x in (w.get("freq") or {}).get("v", [])[:8]]
    cw = w.get("content_words") or {}
    cw_all = [x["w"] for lst in cw.values() for x in lst[:4]]
    arc = c.get("arc") or []
    revs = c.get("reversals") or []
    return {
        "惯用形容词": fa, "惯用动词": fv,
        "设定相关词(已剔除)": cw_all[:10],
        "长短句比": s.get("long_short_ratio"),
        "对白占比": s.get("dialogue_pct"), "句式": s.get("types"),
        "修辞次数": {k: v.get("n") for k, v in (p.get("rhetoric") or {}).items()},
        "情绪曲线要点": (arc[:3] if len(arc) <= 6 else [arc[0], arc[len(arc)//3], arc[2*len(arc)//3], arc[-1]]),
        "反转/伏笔结论": [{"ch": r["ch"], "fact": r["fact"]} for r in revs[:5]],
    }


def _summarize_plot(clue, foreshadow, annot):
    """剧情板块摘要。"""
    cons = clue.get("conclusions") or []
    evs = clue.get("evidence") or []
    return {
        "推理结论数": len(cons),
        "结论样例": [{"ch": c.get("chapter_no"), "fact": str(c.get("fact", ""))[:60]} for c in cons[:5]],
        "暗线证据数": len(evs),
        "伏笔-回收链": [{"cluster": f.get("cluster_id"), "span": f.get("span"),
                        "n_ev": f.get("n_ev")} for f in (foreshadow or [])[:6]],
        "创作解析分布": _count_annot(annot),
    }


def _count_annot(annot):
    from collections import Counter
    return dict(Counter(s.get("plot_function") for s in (annot or []) if s.get("plot_function")))


def _summarize_char(resumes, char_facts):
    """人物板块摘要。"""
    rn = len(resumes)
    hubs = sorted([r for r in resumes if r.get("relations")],
                  key=lambda r: -len(r["relations"]))[:5]
    dbt = sorted([r for r in resumes if r.get("doubts")],
                 key=lambda r: -len(r["doubts"]))[:5]
    say_total = sum(len(v.get("sayings") or []) for v in (char_facts or {}).values())
    no_say = sum(1 for v in (char_facts or {}).values() if not v.get("sayings"))
    n_facts = len(char_facts or {})
    return {
        "人物总数": rn, "有语录人物占比": round((n_facts - no_say) / max(n_facts, 1) * 100, 1) if n_facts else 0,
        "语录总条数": say_total,
        "关系枢纽": [{"name": r.get("name"), "n": len(r.get("relations") or [])} for r in hubs],
        "疑点承载": [{"name": r.get("name"), "n": len(r.get("doubts") or [])} for r in dbt],
    }


def _summarize_setting(sg):
    """设定板块摘要。"""
    terms = sg.get("terms") or []
    rels = sg.get("relations") or []
    from collections import Counter
    return {
        "设定词条": len(terms), "关系": len(rels),
        "分类分布": dict(Counter(t.get("category") for t in terms).most_common(8)),
        "核心词条样例": [{"name": t.get("name"), "cat": t.get("category")} for t in terms[:10]],
    }


def _summarize_overview(meta):
    """概览板块摘要(全局)。"""
    return meta


def review_block(name, data):
    """单个板块一次 LLM 评述。返回 dict 或 None。"""
    lb = _get_logbook()
    user = REVIEW_USER.format(block=name, data=json.dumps(data, ensure_ascii=False)[:1800])
    try:
        raw = llm_client.chat(REVIEW_SYSTEM, user, json_mode=True,
                              num_predict=700, temperature=0.5, role="review")[0]
        d = json.loads(raw)
        if not isinstance(d, dict) or not d.get("summary"):
            # 容忍 {result:{...}}
            for k in ("result", "review", "data"):
                if isinstance(d.get(k), dict) and d[k].get("summary"):
                    d = d[k]
                    break
            else:
                return None
        return {
            "habits": str(d.get("habits", "")).strip(),
            "strengths": str(d.get("strengths", "")).strip(),
            "weaknesses": str(d.get("weaknesses", "")).strip(),
            "summary": str(d.get("summary", "")).strip(),
        }
    except Exception as e:
        lb.error("review", f"评述失败 {name}", err=str(e)[:150])
        return None


def review_all(s1_dir, base=None, model=None):
    """对全部板块生成评述。返回 {schema, reviews: {板块: {...}}}"""
    lb = _get_logbook()
    lb.section("review", "全板块编辑评述")
    out = {"schema": 1, "reviews": {}}
    # 读取产物
    def _load(fn, default=None):
        p = os.path.join(s1_dir, fn)
        if os.path.exists(p):
            try:
                return json.load(open(p, encoding="utf-8"))
            except Exception:
                pass
        return default

    clue = _load("clue_graph.json", {})
    style = _load("style_analysis.json", {})
    sg = _load("settings_graph.json", {})
    resumes = (_load("characters_resume.json", {}) or {}).get("characters", [])
    cf = (_load("character_facts.json", {}) or {}).get("characters", {})
    annot = (_load("scenes_annotations.json", {}) or {}).get("scenes", [])
    foreshadow = _load("foreshadow_meta.json", None)  # 没有就用空
    if not foreshadow:
        # 从 clue 现算(与 cli 同逻辑)
        foreshadow = []
        for _c in clue.get("clusters", []):
            mids = _c.get("member_ids") or []
            chs = sorted({e.get("chapter_no") for e in clue.get("evidence", [])
                          if e.get("id") in mids and e.get("chapter_no")})
            if len(chs) >= 2 and (chs[-1] - chs[0]) >= 5:
                foreshadow.append({"cluster_id": _c.get("id"), "span": [chs[0], chs[-1]],
                                   "n_ev": len(mids), "weight": _c.get("weight", 0)})

    blocks = {
        "概览": _summarize_overview({
            "章节数": _load("meta.json", {}).get("chapters") if False else None,
            "推理结论": len(clue.get("conclusions", [])),
            "暗线证据": len(clue.get("evidence", [])),
            "设定词条": len(sg.get("terms", [])),
            "人物": len(resumes),
            "伏笔-回收链": len(foreshadow),
        }),
        "人物": _summarize_char(resumes, cf),
        "设定": _summarize_setting(sg),
        "剧情": _summarize_plot(clue, foreshadow, annot),
        "文风": _summarize_style(style),
    }
    for name, data in blocks.items():
        if not data:
            continue
        lb.info("review", f"评述 {name}")
        r = review_block(name, data)
        if r:
            out["reviews"][name] = r
            lb.info("review", f"✅ {name}", summary=r["summary"][:40])
    with open(os.path.join(s1_dir, "editor_reviews.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    lb.info("review", "全部评述完成", blocks=len(out["reviews"]))
    return out


if __name__ == "__main__":
    import stage2_mine as M
    s1 = M._latest_stage1_dir()
    print("评述目标:", s1)
    r = review_all(s1)
    for k, v in r["reviews"].items():
        print(f"  [{k}] {v['summary'][:50]}")
