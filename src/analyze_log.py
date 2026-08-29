# -*- coding: utf-8 -*-
"""analyze_log.py —— 跑批日志分析器(常态化的分析能力)。

用法:  python src/cli.py analyze [日志jsonl路径]
  - 不带路径 -> 读 logs/run_latest.jsonl(最近一次会话)
  - 输出: 跑批健康报告(阶段耗时/失败分布/进度/指标曲线/健康度检查)

健康度检查项:
  H1 抽取失败率   empty_rate 应 < 15% (超过: 检查 key/限流/prompt)
  H2 设定词条数   300章应 > 300 (远低: 增量复用/抽取未覆盖, 见 D1)
  H3 推理覆盖率   conclusions / 达标簇 应 > 30% (低: mine 中断/阈值)
  H4 设定碎片率   单场景词条占比应 < 50% (高: 抽取碎片化, 见 D2)
  H5 单例簇占比   应 < 40% (高: 证据碎片化, 见 D8)
"""
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")


def _load_records(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def analyze(path=None):
    if not path:
        path = os.path.join(LOGS_DIR, "run_latest.jsonl")
    if not os.path.exists(path):
        print(f"❌ 找不到日志: {path}")
        print("   先跑一次 collect/mine 生成日志, 或用: python src/cli.py analyze <具体jsonl>")
        return 1
    recs = _load_records(path)
    if not recs:
        print("日志为空")
        return 1

    L = "=" * 64
    print(L)
    print("跑批健康报告")
    print(L)

    # ---- 会话信息 ----
    ts = recs[0].get("ts", "?")
    dur = recs[-1].get("ts", ts)
    try:
        d1 = datetime.fromisoformat(ts)
        d2 = datetime.fromisoformat(dur)
        span = f"{(d2-d1).total_seconds()/60:.0f} 分钟"
    except Exception:
        span = "?"
    print(f"  会话: {ts}  |  跨度: {span}")
    print(f"  日志: {path}")

    # ---- 阶段汇总(取各 step 的最终 gauge/info) ----
    print("\n【阶段汇总】")
    # 从 step=collect/mine 的汇总记录取
    summaries = [r for r in recs if r.get("step") in ("collect", "mine", "extract")
                 and r.get("msg") in ("汇总",)]
    for r in summaries:
        f = {k: v for k, v in r.items() if k not in ("ts", "level", "step", "msg")}
        print(f"  [{r['step']}] " + " | ".join(f"{k}={v}" for k, v in f.items()))

    # ---- 错误/警告 ----
    errs = [r for r in recs if r.get("level") == "ERROR"]
    warns = [r for r in recs if r.get("level") == "WARN"]
    print(f"\n【错误/警告】ERROR={len(errs)} WARN={len(warns)}")
    for e in errs[:10]:
        f = {k: v for k, v in e.items() if k not in ("ts", "level", "step", "msg")}
        print(f"  [ERROR][{e.get('step')}] {e.get('msg')} {f}")

    # ---- 指标曲线(每25章图谱规模等) ----
    gauges = defaultdict(list)
    for r in recs:
        if r.get("step", "").startswith("gauge:"):
            step = r["step"][6:]
            gauges[step].append(r)
    for step, items in gauges.items():
        print(f"\n【指标·{step}】")
        # 取有 chapter 字段的(曲线), 否则取全部
        with_ch = [i for i in items if i.get("chapter")]
        if with_ch:
            for i in sorted(with_ch, key=lambda x: x.get("chapter", 0)):
                val = i.get("value", i.get("metric", ""))
                extra = {k: v for k, v in i.items()
                         if k not in ("ts", "level", "step", "msg", "metric", "value", "chapter")}
                print(f"    第{i.get('chapter')}章: {i.get('metric')}={val}"
                      + (f" {extra}" if extra else ""))
        else:
            for i in items:
                print(f"    {i.get('metric')}={i.get('value')}")

    # ---- 进度(最后一条 per step) ----
    print("\n【进度】")
    last_prog = {}
    for r in recs:
        if r.get("msg", "").startswith("进度"):
            last_prog[r.get("step")] = r
    for step, r in last_prog.items():
        print(f"  {step}: done={r.get('done')} total={r.get('total')} pct={r.get('pct')}% eta={r.get('eta_sec')}s")

    # ---- 健康度检查 ----
    print("\n【健康度】")
    ok = True
    def check(hid, name, cond, detail):
        nonlocal ok
        mark = "✅" if cond else "❌"
        if not cond:
            ok = False
        print(f"  {mark} {hid} {name}: {detail}")

    # H1 抽取失败率
    empty = [r for r in recs if r.get("msg") == "设定抽取统计"]
    if empty:
        r = empty[0]
        check("H1", "抽取失败率", r.get("empty_rate", 100) < 15,
              f"empty={r.get('empty')}/{r.get('scenes')} ({r.get('empty_rate')}%)")
    # H2 设定词条数
    terms = [r for r in recs if r.get("msg") == "设定图谱完成"]
    if terms:
        n = terms[-1].get("terms", 0)
        chapters = 300
        check("H2", "设定词条数", n >= chapters,
              f"terms={n} (章节数 {chapters}, 应≥章节数)")
    # H3 推理
    concl = [r for r in recs if r.get("msg") == "consolidate"]
    if concl:
        r = concl[0]
        rate = r.get("new_conclusions", 0) / max(r.get("total", 1), 1) * 100
        check("H3", "推理覆盖", rate > 30, f"结论 {r.get('new_conclusions')} 条 / 簇 {r.get('total')} ({rate:.0f}%)")
    # H4/H5 需要看产物, 日志里没有, 给出提示
    print("  ℹ️ H4 设定碎片率 / H5 单例簇占比 需结合产物统计(见 cli analyze 说明或审计脚本)")

    print("\n" + L)
    print("结论:", "✅ 整体健康" if ok else "❌ 存在需关注项(见上方 ❌)")
    print(L)
    return 0 if ok else 2


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else None
    sys.exit(analyze(path))


if __name__ == "__main__":
    main()
