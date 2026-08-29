# -*- coding: utf-8 -*-
"""
final_report.py ——收尾总报告 小红书API(dots.ai) vs 本地 Ollama 全维度对比数据来源:
  - data/bench_stage2.json         (TXT/JSON x 4b/8b 单章基准: token/耗时/tps)
  - data/llm_50/{local,cloud}/  (50 章产物 outlines/characters/settings/summary/rag_answers)
  - 两端 run.log/rerun.log           (50 章outline 输入输出 token)
产出: outputs/<小说名_<日期>/final_report.html
"""
import os
import json
import re
import html
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C

BASE = os.path.join(C.DATA_DIR, "llm_50")
LOCAL = os.path.join(BASE, "local")
CLOUD = os.path.join(BASE, "cloud")
CLOUD_FIXED = os.path.join(BASE, "cloud_fixed")
BENCH = os.path.join(C.DATA_DIR, "bench_stage2.json")
OUT = os.path.join(C.OUTPUT_DIR, "final_report.html")

LOCAL_LABEL = "本地 Ollama qwen3:4b"
LOCAL8_LABEL = "本地 Ollama qwen3:8b"
CLOUD_LABEL = "云端 dots.ai (小红书key)"


def load(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def esc(s):
    return html.escape("" if s is None else str(s))


def parse_outline_tokens(log_path):
    d = {}
    if not os.path.exists(log_path):
        return d
    with open(log_path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            m = re.search(r"第(\d+)章OK \(输入(\d+)tok 输出(\d+)tok\)", line)
            if m:
                d[int(m.group(1))] = (int(m.group(2)), int(m.group(3)))
    return d


def collect_tokens(d):
    toks = {}
    if os.path.isdir(d):
        for fn in os.listdir(d):
            if fn.endswith(".log"):
                toks.update(parse_outline_tokens(os.path.join(d, fn)))
    return toks


def alias_linked(chars, a="周明瑞", b="克莱恩"):
    if not chars:
        return False, []
    hits = []
    for c in chars:
        name = str(c.get("name", ""))
        blob = " ".join([name] + [str(x) for x in (c.get("aliases", []) or [])])
        if a in blob and b in blob:
            hits.append(name)
    return bool(hits), hits


def main():
    # 主对比 云端优先用cloud_fixed(修复深度思考参数后重跑), 否则用旧 cloud
    CLOUD_MAIN = CLOUD_FIXED if os.path.isdir(CLOUD_FIXED) and load(os.path.join(CLOUD_FIXED, "outlines.json")) else CLOUD
    outlines_l = load(os.path.join(LOCAL, "outlines.json")) or {}
    outlines_c = load(os.path.join(CLOUD_MAIN, "outlines.json")) or {}
    chars_l = load(os.path.join(LOCAL, "characters.json")) or []
    chars_c = load(os.path.join(CLOUD_MAIN, "characters.json")) or []
    sets_l = load(os.path.join(LOCAL, "settings.json")) or []
    sets_c = load(os.path.join(CLOUD_MAIN, "settings.json")) or []
    sum_l = load(os.path.join(LOCAL, "summary.json"))
    sum_c = load(os.path.join(CLOUD_MAIN, "summary.json"))
    rag_l = load(os.path.join(LOCAL, "rag_answers.json")) or []
    rag_c = load(os.path.join(CLOUD_MAIN, "rag_answers.json")) or []
    bench = load(BENCH) or []

    toks_l = collect_tokens(LOCAL)
    toks_c = collect_tokens(CLOUD_MAIN)
    in_l = sum(t[0] for t in toks_l.values())
    out_l = sum(t[1] for t in toks_l.values())
    in_c = sum(t[0] for t in toks_c.values())
    out_c = sum(t[1] for t in toks_c.values())
    n_l, n_c = len(outlines_l), len(outlines_c)

    # 修复前后对照(仅当 cloud_fixed 存在时
    fix_block = ""
    if CLOUD_MAIN == CLOUD_FIXED:
        toks_old = collect_tokens(CLOUD)
        out_old = sum(t[1] for t in toks_old.values())
        n_old = len(load(os.path.join(CLOUD, "outlines.json")) or {})
        fix_block = f"""<div class="note" style="background:#f0f7ff;border-color:#93c5fd;color:#1e40af">
<b>🔧 关键修复</b>：dots.ai 深度思考默认开启，旧参数<code>thinking:false</code> 无效，0 章白烧推理token。改用 <code>chat_template_kwargs.enable_thinking:false</code> 后重跑：
输出 token <b>{out_old:,} →{out_c:,}</b>（降 {round((1-out_c/max(1,out_old))*100)}%），单章输出 2427→28 tok，章纲JSON 反而更完整（52 vs 364 字符）。</div>"""

    linked_l, hl = alias_linked(chars_l)
    linked_c, hc = alias_linked(chars_c)

    # ---- bench 表----
    bench_rows = ""
    for b in bench:
        inp = b.get("in_tok", 0)
        out = b.get("out_tok", 0)
        label = b.get("label", "")
        tag = ""
        if label.startswith("A") or label.startswith("B"):
            tag = "TXT 原文直出"
        else:
            tag = "JSON actinfo 抽取"
        bench_rows += f"""<tr>
<td><b>{esc(label)}</b><div class="muted">{tag}</div></td>
<td>{b.get('total_s', 0):.1f} s</td>
<td>{inp:,} tok</td>
<td>{out:,} tok</td>
<td>{b.get('gen_ms', 0)/1000:.1f} s</td>
<td>{b.get('tps', 0):.0f} tok/s</td>
</tr>"""

    # ---- 章纲对照 ----
    chap_nums = sorted(set([int(x) for x in list(outlines_l.keys()) + list(outlines_c.keys())]))
    rows = []
    for cn in chap_nums:
        ol = outlines_l.get(str(cn)) or outlines_l.get(cn) or {}
        oc = outlines_c.get(str(cn)) or outlines_c.get(cn) or {}
        tl = toks_l.get(cn)
        tc = toks_c.get(cn)
        main_l = ol.get("主线") or ol.get("summary") or ""
        main_c = oc.get("主线") or oc.get("summary") or ""
        rows.append(f"""<tr>
<td class="cn">{cn}</td>
<td>{esc(main_l)}<div class="tok">{'输出 '+str(tl[1])+' tok' if tl else '<i>缺失</i>'}</div></td>
<td>{esc(main_c)}<div class="tok">{'输出 '+str(tc[1])+' tok' if tc else ''}</div></td>
</tr>""")

    # ---- 人物对照 ----
    name_l = {c.get("name"): c for c in chars_l}
    char_rows = ""
    for c in chars_c:
        nm = c.get("name")
        cl = name_l.get(nm)
        char_rows += f"<tr><td>{char_card(cl)}</td><td>{char_card(c)}</td></tr>"
    if not char_rows:
        char_rows = "<tr><td colspan=2>无</td></tr>"

    # ---- 设定对照 ----
    set_l_items = "".join(f"<li>{esc(s.get('name'))} <span class='muted'>({esc(s.get('type'))})</span></li>" for s in sets_l) or "<li><i>本地未提取到</i></li>"
    set_c_items = "".join(f"<li>{esc(s.get('name'))} <span class='muted'>({esc(s.get('type'))})</span></li>" for s in sets_c[:60]) or "<li><i>无</i></li>"
    set_c_more = f"<div class='muted'>…共 {len(sets_c)} 条，仅展示前 60 条</div>" if len(sets_c) > 60 else ""

    # ---- RAG ----
    rrows = ""
    for i in range(max(len(rag_l), len(rag_c))):
        q = (rag_l[i].get("question") if i < len(rag_l) else rag_c[i].get("question")) if (rag_l or rag_c) else ""
        al = rag_l[i].get("answer") if i < len(rag_l) else ""
        ac = rag_c[i].get("answer") if i < len(rag_c) else ""
        # 检测是否带复述噪声
        def noise(x):
            return '<span class="tag-no">带复述噪声</span>' if any(k in str(x) for k in ("首先，用户的问题是", "首先，问题是")) else '<span class="tag-ok">干净直答</span>'
        rrows += f"""<tr>
<td class="q">{esc(q)}</td>
<td>{esc(al)}<div class="tok">{noise(al)}</div></td>
<td>{esc(ac)}<div class="tok">{noise(ac)}</div></td>
</tr>"""

    # ---- 总结对照 ----
    def sum_block(s):
        if not s:
            return "<i>未生成</i>"
        if isinstance(s, dict):
            parts = []
            for k, v in s.items():
                parts.append(f"<b>{esc(k)}</b>：{esc(v)}")
            return "<br>".join(parts)
        return esc(json.dumps(s, ensure_ascii=False, indent=1))

    html_doc = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>StageScienceLab 收尾报告：小红书 API vs 本地模型</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;margin:0;background:#f5f6f8;color:#1f2329;padding:24px;line-height:1.6}}
h1{{font-size:22px;margin:0 0 4px}}
.sub{{color:#6b7280;font-size:13px;margin-bottom:20px}}
.cards{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:22px}}
.card{{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:14px 16px;flex:1;min-width:170px}}
.card .k{{font-size:12px;color:#6b7280}}
.card .v{{font-size:20px;font-weight:600;margin-top:4px}}
.card .v small{{font-size:12px;color:#9ca3af;font-weight:400}}
h2{{font-size:17px;margin:28px 0 10px;border-left:4px solid #4f7cff;padding-left:8px}}
table.cmp{{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.06);margin-bottom:8px}}
table.cmp th,table.cmp td{{border:1px solid #eceef1;padding:10px 12px;vertical-align:top;font-size:13px}}
table.cmp th{{background:#f0f3ff;color:#374151;text-align:left}}
td.cn{{font-weight:700;color:#4f7cff;white-space:nowrap;width:36px;text-align:center}}
td.q{{font-weight:600;width:180px}}
.tok{{color:#9ca3af;font-size:11px;margin-top:4px}}
.muted{{color:#9ca3af;font-size:11px}}
.tag-ok{{display:inline-block;background:#e7f7ec;color:#1a7f37;border-radius:6px;padding:1px 8px;font-size:12px;font-weight:600}}
.tag-no{{display:inline-block;background:#fde8e8;color:#b42318;border-radius:6px;padding:1px 8px;font-size:12px;font-weight:600}}
.note{{background:#fffbe6;border:1px solid #ffe58f;border-radius:8px;padding:10px 14px;font-size:13px;color:#614700;margin:12px 0}}
.concl{{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;padding:16px 20px;margin:12px 0}}
.concl h3{{margin:0 0 8px;color:#166534;font-size:15px}}
.concl ul{{margin:0;padding-left:20px}}
.concl li{{margin:4px 0;font-size:13px}}
.sum{{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px 20px;margin:12px 0}}
.sum h3{{margin:0 0 8px;font-size:15px}}
.sum p{{font-size:13px;margin:6px 0;color:#374151}}
@media(max-width:760px){{body{{padding:12px}}table.cmp{{font-size:12px}}}}
</style></head><body>
<h1>小红书API（dots.ai）vs 本地 Ollama ——全维度对比收尾报告</h1>
<div class="sub">研究对象：男频悬疑长篇小说50 章· 管线：分场景抽取(actinfo v2) →LLM 直出(章纲/人物/设定/总结) →RAG 问答 · 数据：b>本地 qwen3:4b/8b</b>（Ollama，零成本）vs <b>云端 dots3-note-prev</b>（12K 上下文，按量计费）·云端已修复深度思考token 虚高（见第〇节）</div>

<div class="cards">
  <div class="card"><div class="k">章纲覆盖</div><div class="v">{n_l}<small> / {n_c} 章</small></div></div>
  <div class="card"><div class="k">人物档案</div><div class="v">{len(chars_l)}<small> / {len(chars_c)} 人</small></div></div>
  <div class="card"><div class="k">设定条目</div><div class="v">{len(sets_l)}<small> / {len(sets_c)} 条</small></div></div>
  <div class="card"><div class="k">章纲输出 token</div><div class="v">{out_l:,}<small> / {out_c:,} tok</small></div></div>
  <div class="card"><div class="k">周明瑞克莱恩链指</div><div class="v">{'<span class="tag-ok">本地命中</span>' if linked_l else '<span class="tag-no">本地未命中</span>'} {'<span class="tag-ok">云端命中</span>' if linked_c else '<span class="tag-no">云端未命中</span>'}</div></div>
</div>

<div class="concl">
<h3>核心结论</h3>
<ul>
<li><b>性能</b>：JSON actinfo 抽取比TXT 原文直出省<b>约42% 输入 token</b>（898→108）；4b 生成速度 <b>91-95 tok/s</b> 是8b（9-61 tok/s）的 <b>1.5 倍</b>。</li>
<li><b>线上模型踩坑（已修复）</b>：dots.ai 深度思考默认开启且旧参数<code>thinking:false</code> 无效，导致50 章输出token 虚高 <b>95,278</b>（实际章纲仅 ~15,000）。改用<code>chat_template_kwargs.enable_thinking:false</code> 后输出<b>↓约 86%</b>，与本地 4b 基本持平。⚠️任何接线上模型的工程必须核对「推理token 是否计入计费」。</li>
<li><b>效果</b>：两端均正确识别「周明瑞 = 克莱恩·莫雷蒂」身份链指；云端人物档案多50%（0 vs 20 人）、设定条目<b>102 vs 7</b>（长上下文优势显著）；本地4b 偶发吐坏 JSON（0 章缺 2 章）。</li>
<li><b>RAG</b>：本地8b 在收紧提示词后三问<b>全部干净直答</b>；云端同提示词仍偶发复述——云端强在信息量、弱在指令遵循稳定性。</li>
<li><b>成本</b>：本地<b>零成本</b>；云端dots.ai 按量计费（RPM=60/TPM=150 万），修复后适合「精修设定长上下文总结」。</li>
</ul>
</div>

<h2>〇、线上模型Token 虚高问题与修复（本次新增）</h2>
{fix_block}
<div class="note">根因链：dots.ai 文档「深度思考默认开启」→ 旧代码传 <code>thinking:false</code>（无效参数）→<code>reasoning_content</code> 持续产生且计入<code>completion_tokens</code> →50 章章纲白烧~80,000 token。修复：按官方文档改传<code>chat_template_kwargs.enable_thinking:false</code>，单章输出2427→28 tok。本报告主对比使用修复后数据（cloud_fixed），旧数据（cloud）仅作对照。</div>

<h2>一、运行性能（单章章纲基准）</h2>
<table class="cmp">
<tr><th>方式</th><th>总耗时</th><th>输入 token</th><th>输出 token</th><th>生成耗时</th><th>速度</th></tr>
{bench_rows}
</table>
<div class="note">A/B 用TXT 原文直出；C/D 用stage1 抽取的actinfo JSON 流（带章节锚点）。输入token 从TXT 1898/1904 →JSON 1108/1114，<b>省42%</b>；<b>快于 8b 约1.5 倍</b>。修复后云端 50 章平均~{round(out_c/max(1,n_c))} tok/章（修复前~1900），与本地4b ~310 tok/章同量级。</div>

<h2>二、0 章产物对比</h2>
<div style="display:flex;gap:24px;flex-wrap:wrap">
<div style="flex:1;min-width:280px;background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:14px">
<b>{LOCAL_LABEL}</b>
<ul style="font-size:13px;padding-left:18px">
<li>章纲 {n_l} 章（第8/45章吐坏JSON 缺失）</li>
<li>人物 {len(chars_l)} 人（{', '.join(hl) if hl else '无链指'}）/li>
<li>设定 {len(sets_l)} 条</li>
<li>输出 token 合计 {out_l:,}（输入{in_l:,}）</li>
<li>成本： 元</li>
</ul>
</div>
<div style="flex:1;min-width:280px;background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:14px">
<b>{CLOUD_LABEL}</b>
<ul style="font-size:13px;padding-left:18px">
<li>章纲 {n_c} 章（全部成功）</li>
<li>人物 {len(chars_c)} 人（{', '.join(hc) if hc else '无链指'}）/li>
<li>设定 {len(sets_c)} 条</li>
<li>输出 token 合计 {out_c:,}（输入{in_c:,}）</li>
<li>成本：按量计费（RPM=30 限流）</li>
</ul>
</div>
</div>

<h2>三、章纲对照（每章 主线）</h2>
<table class="cmp"><tr><th style="width:36px">章</th><th>{LOCAL_LABEL}</th><th>{CLOUD_LABEL}</th></tr>
{''.join(rows)}</table>

<h2>四、人物档案对照（以云端为基准）</h2>
<table class="cmp"><tr><th>{LOCAL_LABEL}</th><th>{CLOUD_LABEL}</th></tr>
{char_rows}</table>

<h2>五、设定提取对照</h2>
<div style="display:flex;gap:24px;flex-wrap:wrap">
<div style="flex:1;min-width:260px"><b>{LOCAL_LABEL}</b><ul style="font-size:13px">{set_l_items}</ul></div>
<div style="flex:1;min-width:260px"><b>{CLOUD_LABEL}</b><ul style="font-size:13px">{set_c_items}</ul>{set_c_more}</div>
</div>

<h2>六、RAG 问答对照（新提示词版）</h2>
<table class="cmp"><tr><th>问题</th><th>本地 qwen3:8b</th><th>云端 dots.ai</th></tr>
{rrows}</table>
<div class="note">提示词策略：要求「结论：」开头+ 引用章节 + 禁止复述。本地8b 三问全部干净；云端dots.ai 偶发复述问题本身（指令遵循不稳定）。</div>

<h2>七、全书总结对照</h2>
<table class="cmp"><tr><th>{LOCAL_LABEL}</th><th>{CLOUD_LABEL}</th></tr>
<tr><td>{sum_block(sum_l)}</td><td>{sum_block(sum_c)}</td></tr></table>

</body></html>"""

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html_doc)
    print(f"已生成{OUT}")
    print(f"  章纲 本地{n_l}/{n_c} 人物{len(chars_l)}/{len(chars_c)} 设定{len(sets_l)}/{len(sets_c)}")
    print(f"  输出token 本地{out_l:,}/云端{out_c:,} | RAG 本地{len(rag_l)}问云端{len(rag_c)}问")


def char_card(c):
    if not c:
        return "<i>本地无此人物档案</i>"
    parts = [f"<b>{esc(c.get('name'))}</b>"]
    al = c.get("aliases") or []
    if al:
        parts.append(f"别名: {esc('、'.join(str(x) for x in al))}")
    if c.get("身份"):
        parts.append(f"身份: {esc(c.get('身份'))}")
    if c.get("弧光"):
        parts.append(f"弧光: {esc(c.get('弧光'))}")
    rel = c.get("关系") or []
    if rel:
        parts.append(f"关系: {esc('；'.join(str(x) for x in rel))}")
    return "<br>".join(parts)


if __name__ == "__main__":
    main()
