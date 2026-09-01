# -*- coding: utf-8 -*-
"""compare_10ch/analyze.py —— 10 章样本双模型抓取对比（dots3-note vs 本地 qwen3:8b）

输入: dots/entity_registry.json + dots/token_total.json + dots/run.log
      qwen/entity_registry.json + qwen/token_total.json + qwen/run.log
输出: stdout 汇总 + compare_result.md
精度口径:
  1. 规模: 实体总数 / 去重规范名 / 别名总数 / 平均每章实体数
  2. 命中率: 前 10 章必现实体名单(9 个)在 canonical 或 aliases 中的覆盖率
  3. 归一化: 克莱恩/周明瑞 是否归到同一 canonical(核心能力检查)
  4. 一致性: 两模型 canonical 集合的 Jaccard
效率口径: 耗时(秒) / prompt+completion token / 调用次数 / 单章均耗
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))

# 第 1-10 章必现实体名单（《诡秘之主》前 10 章剧情，canonical 或 aliases 命中即可）
MUST_HIT = [
    ("克莱恩", "克莱恩·莫雷蒂(主角)"),
    ("周明瑞", "穿越前身份(应归一至克莱恩)"),
    ("班森", "哥哥"),
    ("梅丽莎", "妹妹"),
    ("罗塞尔", "罗塞尔·古斯塔夫(日记)"),
    ("尼尔", "老尼尔(值夜者)"),
    ("邓恩", "邓恩·史密斯(值夜者队长)"),
    ("罗珊", "值夜者队员"),
    ("韦尔奇", "同学(第3章)"),
]

# 归一化硬指标: 这两个名字必须出现在同一个 canonical 的 canonical+aliases 里
ALIAS_PAIR = ("克莱恩", "周明瑞")


def load_registry(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)  # {chapter_no: [{canonical, aliases, category}]}


def load_tokens(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_secs(p):
    """从 run.log 抓 '-> N 个实体 耗时 Xs'。"""
    try:
        with open(p, encoding="utf-8") as f:
            t = f.read()
        m = re.search(r"耗时\s*([\d.]+)s", t)
        return float(m.group(1)) if m else None
    except Exception:
        return None


def flat(reg):
    """展平: [(chapter_no, ent)] 及去重 canonical 集、别名集。"""
    ents = []
    for cn, lst in reg.items():
        for e in lst:
            ents.append((int(cn), e))
    return ents


def stats(reg):
    ents = flat(reg)
    canon = set()
    aliases = set()
    per_ch = {}
    for cn, e in ents:
        canon.add(e["canonical"])
        aliases.update(e.get("aliases") or [])
        per_ch.setdefault(cn, []).append(e["canonical"])
    return {
        "total": len(ents), "uniq": len(canon), "aliases": len(aliases),
        "per_ch_avg": len(ents) / max(len(per_ch), 1),
        "chapters": sorted(per_ch.keys()),
        "canon_set": canon,
    }


def hit(reg):
    """必现实体命中率。"""
    all_ents = flat(reg)
    hit_list = []
    for name, note in MUST_HIT:
        ok = False
        for cn, e in all_ents:
            hay = e["canonical"] + "".join(e.get("aliases") or [])
            if name in hay:
                ok = True
                break
        hit_list.append((name, note, ok))
    return hit_list


def alias_check(reg):
    """归一化: 是否存在一个 canonical 同时涵盖 A/B。"""
    for cn, e in flat(reg):
        hay = e["canonical"] + "".join(e.get("aliases") or [])
        if ALIAS_PAIR[0] in hay and ALIAS_PAIR[1] in hay:
            return True, (int(cn), e["canonical"], e.get("aliases"))
    return False, None


def jaccard(a, b):
    inter = a & b
    union = a | b
    return round(len(inter) / max(len(union), 1), 3), len(inter), len(union)


def main():
    dots_reg = load_registry(os.path.join(HERE, "dots", "entity_registry.json"))
    qwen_reg = load_registry(os.path.join(HERE, "qwen", "entity_registry.json"))
    dots_tok = load_tokens(os.path.join(HERE, "dots", "token_total.json"))
    qwen_tok = load_tokens(os.path.join(HERE, "qwen", "token_total.json"))
    dots_s = load_secs(os.path.join(HERE, "dots", "run.log"))
    qwen_s = load_secs(os.path.join(HERE, "qwen", "run.log"))

    # 每模型 token: 取 by_model 中最大贡献者(或全量汇总)
    def tok(t):
        pt = t.get("prompt_total", 0)
        ct = t.get("completion_total", 0)
        return pt, ct, pt + ct, t.get("calls", 0), t.get("by_model", {})
    d_pt, d_ct, d_sum, d_calls, d_bm = tok(dots_tok)
    q_pt, q_ct, q_sum, q_calls, q_bm = tok(qwen_tok)

    ds, qs = stats(dots_reg), stats(qwen_reg)
    d_hits, q_hits = hit(dots_reg), hit(qwen_reg)
    d_alias, q_alias = alias_check(dots_reg), alias_check(qwen_reg)
    jac, inter, union = jaccard(ds["canon_set"], qs["canon_set"])

    lines = []
    A = lines.append
    A("# 10 章样本抓取对比：dots3-note vs 本地 qwen3:8b")
    A("")
    A("样本：《诡秘之主》第 1–10 章（前 10 章场景块，复用已抽取场景库，无重复 extract）")
    A("")
    A("## 效率")
    A("")
    A("| 指标 | dots3-note(小红书) | qwen3:8b(本地) |")
    A("|---|---|---|")
    A(f"| 耗时 | {dots_s:.1f}s | {qwen_s:.1f}s |")
    A(f"| 每章均耗 | {dots_s/10:.1f}s/章 | {qwen_s/10:.1f}s/章 |")
    A(f"| prompt tokens | {d_pt:,} | {q_pt:,} |")
    A(f"| completion tokens | {d_ct:,} | {q_ct:,} |")
    A(f"| 总 tokens | {d_sum:,} | {q_sum:,} |")
    A(f"| LLM 调用次数 | {d_calls} | {q_calls} |")
    A("")
    A("## 精度（实体抓取）")
    A("")
    A("| 指标 | dots3-note(小红书) | qwen3:8b(本地) |")
    A("|---|---|---|")
    A(f"| 实体总数(章×条) | {ds['total']} | {qs['total']} |")
    A(f"| 去重规范名 | {ds['uniq']} | {qs['uniq']} |")
    A(f"| 别名总数 | {ds['aliases']} | {qs['aliases']} |")
    A(f"| 平均每章实体数 | {ds['per_ch_avg']:.1f} | {qs['per_ch_avg']:.1f} |")
    d_hit_n = sum(1 for _, _, ok in d_hits if ok)
    q_hit_n = sum(1 for _, _, ok in q_hits if ok)
    A(f"| 必现实体命中率(9 个) | {d_hit_n}/9 | {q_hit_n}/9 |")
    A(f"| 克莱恩=周明瑞 归一 | {'✅ ' + d_alias[1][1] if d_alias[0] else '❌ 未归并'} | {'✅ ' + q_alias[1][1] if q_alias[0] else '❌ 未归并'} |")
    A(f"| canonical 一致性(Jaccard) | {jac} (交集 {inter} / 并集 {union}) | |")
    A("")
    A("### 必现实体逐项命中")
    A("")
    A("| 实体 | 说明 | dots3-note | qwen3:8b |")
    A("|---|---|---|---|")
    for i, (name, note) in enumerate(MUST_HIT):
        dh = "✅" if d_hits[i][2] else "❌"
        qh = "✅" if q_hits[i][2] else "❌"
        A(f"| {name} | {note} | {dh} | {qh} |")
    A("")
    A("### 归一化细节")
    A("")
    A(f"- dots3-note: {('找到 canonical=' + d_alias[1][1] + ' 章=' + str(d_alias[1][0]) + ' aliases=' + json.dumps(d_alias[1][2], ensure_ascii=False)) if d_alias[0] else '未找到同时含 克莱恩/周明瑞 的实体'}")
    A(f"- qwen3:8b: {('找到 canonical=' + q_alias[1][1] + ' 章=' + str(q_alias[1][0]) + ' aliases=' + json.dumps(q_alias[1][2], ensure_ascii=False)) if q_alias[0] else '未找到同时含 克莱恩/周明瑞 的实体'}")
    A("")
    A("### 模型 token 分组(by_model)")
    A("")
    A("| 模型 | prompt | completion | calls |")
    A("|---|---|---|---|")
    for m, v in (list(d_bm.items()) + list(q_bm.items())):
        A(f"| {m} | {v.get('prompt',0):,} | {v.get('completion',0):,} | {v.get('calls',0)} |")
    A("")

    txt = "\n".join(lines)
    with open(os.path.join(HERE, "compare_result.md"), "w", encoding="utf-8") as f:
        f.write(txt)
    print(txt)


if __name__ == "__main__":
    main()
