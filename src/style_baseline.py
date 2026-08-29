# -*- coding: utf-8 -*-
"""
style_baseline.py —— 文风跨书对比基准库（方案 B: 纯跨书积累）

结构:
  {schema, genres: {<genre>: {n_books, word: {pos: {w: {mean, n_books}}},
                              sentence: {k: {mean, n_books}},
                              rhetoric: {k: {mean, n_books}}}},
   all: 同上(全网桶)}

流程:
  每本书 stage2 mine 文风聚合完成 → ingest(genre, stats) 更新 genre 桶 + all 桶
  面板渲染 → cli 注入 get(genre) 结果 → 三柱图/对比列

初期无数据时 get() 返回空, 面板显示"待积累"占位 —— 这是方案 B 的预期行为。
"""
import os
import json

import config as C

BASE_PATH = os.path.join(C.DATA_DIR, "style_baseline.json")


def load():
    """读取基准库(不存在返回空结构)。"""
    try:
        if os.path.exists(BASE_PATH):
            with open(BASE_PATH, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and "genres" in d:
                return d
    except Exception:
        pass
    return {"schema": 1, "genres": {}, "all": {}}


def _save(d):
    os.makedirs(os.path.dirname(BASE_PATH), exist_ok=True)
    with open(BASE_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def _merge(bucket, stats):
    """把 stats(当前书的指标) 合并进 bucket(某桶的 {n_books, word, sentence, rhetoric})。
    stats = {word: {pos: {w: n}}, sentence: {k: v}, rhetoric: {k: v}}"""
    bucket["n_books"] = bucket.get("n_books", 0) + 1
    for cat in ("word", "sentence", "rhetoric"):
        sub = bucket.setdefault(cat, {})
        st = stats.get(cat) or {}
        for k, v in st.items():
            if not isinstance(v, dict):
                continue
            d = sub.setdefault(k, {})
            for w, val in v.items():
                rec = d.setdefault(w, {"mean": 0, "n_books": 0})
                rec["mean"] = round((rec["mean"] * rec["n_books"] + val) / (rec["n_books"] + 1), 4)
                rec["n_books"] += 1


def ingest(genre, stats):
    """文风聚合完成后调用: 更新 genre 桶 + all 桶。genre 可为 None(只进全网桶)。"""
    d = load()
    if genre:
        g = d["genres"].setdefault(genre, {"n_books": 0, "word": {}, "sentence": {}, "rhetoric": {}})
        _merge(g, stats)
    _merge(d["all"], stats)
    _save(d)
    return d


def get(genre=None):
    """返回面板可用的对比数据: {genre: 桶(或空), all: 桶}。"""
    d = load()
    return {
        "genre": d["genres"].get(genre) or {},
        "all": d.get("all") or {},
        "genre_name": genre,
    }


def stats_from_style(style):
    """从 analyze_style_v2 输出提取可聚合的统计(stats 结构)。
    word: 各词性词频; sentence: 长短比/对白/描写占比; rhetoric: 次/场景。"""
    w = style.get("word") or {}
    s = style.get("sentence") or {}
    p = style.get("para") or {}
    return {
        "word": {pos: {x["w"]: x["n"] for x in lst}
                 for pos, lst in (w.get("freq") or {}).items()},
        "sentence": {
            "long_short_ratio": s.get("long_short_ratio") or 0,
            "dialogue_pct": s.get("dialogue_pct") or 0,
            "action_pct": s.get("action_pct") or 0,
        },
        "rhetoric": {k: v.get("per_scene") or 0
                     for k, v in (p.get("rhetoric") or {}).items()},
    }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    print("基准库路径:", BASE_PATH)
    d = load()
    print("已有书籍数: genre 桶", {k: v.get("n_books") for k, v in d["genres"].items()},
          "| all 桶:", d["all"].get("n_books"))
    # 自测: ingest 一次假数据再 get
    fake = {"word": {"a": {"沉默": 30}}, "sentence": {"long_short_ratio": 0.8},
            "rhetoric": {"比喻": 0.2}}
    ingest("测试域", fake)
    g = get("测试域")
    print("自测 get:", json.dumps(g, ensure_ascii=False)[:200])
