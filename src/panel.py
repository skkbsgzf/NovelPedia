# -*- coding: utf-8 -*-
"""
panel.py —— 进度面板（progress.json 原子写者 + 终端渲染 + 错误/token/ETA）

架构 v3 + stage2 专项设计的 CLI 面板地基。
设计要点：
  - 文件是唯一真相：所有执行器只调 panel_update() 原子写 logs/run_<id>.progress.json，
    终端渲染只是阅读器（不依赖 tty，非交互跑批也留档）。
  - token 计数：llm_client.chat 每次响应后调 panel.add_tokens()（见 llm_client 接入点）。
  - ETA：速率滑动平均外推；总 ETA = 未完成线程的最大剩余。
  - 错误分级：retryable(自动重试≤3) / fatal(不静默吞) / warn(降级标 gap)。

库用法：
    from panel import Panel
    p = Panel()                              # 自动生成 run_id
    p.thread("thread_extract",  total=48)    # 注册线程
    p.thread("thread_deep_read", total=50, is_chapter=True)
    p.progress("thread_extract", done=45)    # 推进
    p.add_tokens("glm-4-flash", 1200, 800)   # token 计数
    p.error("thread_deep_read", ch=28, msg="json 解析失败", retryable=True)
    p.module("char", stage=3, status="running")

CLI 用法：
    python src/panel.py demo --watch            # 模拟双线程演示（实时渲染）
    python src/panel.py render --run <id>       # 渲染一次快照
    python src/panel.py errors --run <id> [--by-task]
    python src/panel.py tokens  --run <id>
"""
import os
import json
import time
import shutil
import datetime
import threading

# ---- 日志根目录（与 pipeline 统一）----
DEFAULT_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _run_dir(log_dir, run_id):
    return os.path.join(log_dir, f"run_{run_id}")


class Panel:
    """进度面板：状态写者（原子 + 线程安全）。"""

    def __init__(self, run_id=None, log_dir=None):
        log_dir = log_dir or DEFAULT_LOG_DIR
        os.makedirs(log_dir, exist_ok=True)
        run_id = run_id or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_id = run_id
        self.dir = _run_dir(log_dir, run_id)
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "progress.json")
        self._lock = threading.Lock()
        self._init_state()

    # ---------- 状态 ----------
    def _init_state(self):
        state = {
            "run_id": self.run_id,
            "started_at": _now(),
            "stage2": {},
            "modules": {},
            "tokens": {"prompt": 0, "completion": 0, "total": 0, "by_model": {}},
            "eta": {},
            "errors": {"total": 0, "retryable": 0, "fatal": 0, "warn": 0, "recent": []},
        }
        self._write(state)

    def _write(self, state):
        # 原子写：临时文件 + os.replace，避免读端看到半个 JSON
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def _mutate(self, fn):
        """加载 → 修改 → 原子写回（线程安全）。fn(state) 就地修改。"""
        with self._lock:
            try:
                with open(self.path, encoding="utf-8") as f:
                    state = json.load(f)
            except Exception:
                state = {"run_id": self.run_id, "started_at": _now(),
                         "stage2": {}, "modules": {}, "tokens": {
                             "prompt": 0, "completion": 0, "total": 0, "by_model": {}},
                         "eta": {}, "errors": {"total": 0, "retryable": 0,
                                               "fatal": 0, "warn": 0, "recent": []}}
            fn(state)
            self._write(state)

    # ---------- 线程进度 ----------
    def thread(self, name, total, status="running", **kw):
        """注册/更新一个线程。is_chapter=True 时按章计数（ETA 用章速率）。"""
        self._mutate(lambda s: s.setdefault("stage2", {}).setdefault(
            name, {"total": total, "done": 0, "status": status, "elapsed_sec": 0,
                   "started_at": _now(), **kw}))

    def progress(self, name, done, status=None, **kw):
        """推进线程进度。自动维护 elapsed_sec 与 rate。"""
        def fn(s):
            t = s.setdefault("stage2", {}).get(name)
            if t is None:
                t = s.setdefault("stage2", {})[name] = {"total": 1, "done": 0,
                                                        "status": "running", "started_at": _now()}
            t["done"] = done
            t["elapsed_sec"] = round(time.time() - self._t0_of(t), 1)
            if status:
                t["status"] = status
            if t.get("total") and t["elapsed_sec"] > 0 and done > 0:
                t["rate_per"] = round(t["elapsed_sec"] / done, 3)
        self._mutate(fn)

    @staticmethod
    def _t0_of(t):
        try:
            return datetime.datetime.strptime(t.get("started_at", ""),
                                              "%Y-%m-%dT%H:%M:%S").timestamp()
        except Exception:
            return time.time()

    def thread_done(self, name, result="ok"):
        self._mutate(lambda s: s.setdefault("stage2", {}).get(name, {})
                     .__setitem__("status", "done"))

    # ---------- token 计数 ----------
    def add_tokens(self, model, prompt_tok, completion_tok):
        """llm_client 每次响应后调用。model 为空时归入 'unknown'。"""
        model = model or "unknown"

        def fn(s):
            tk = s.setdefault("tokens", {})
            tk["prompt"] = tk.get("prompt", 0) + int(prompt_tok or 0)
            tk["completion"] = tk.get("completion", 0) + int(completion_tok or 0)
            tk["total"] = tk.get("total", 0) + int(prompt_tok or 0) + int(completion_tok or 0)
            bm = tk.setdefault("by_model", {})
            e = bm.setdefault(model, {"prompt": 0, "completion": 0, "total": 0})
            e["prompt"] += int(prompt_tok or 0)
            e["completion"] += int(completion_tok or 0)
            e["total"] += int(prompt_tok or 0) + int(completion_tok or 0)
        self._mutate(fn)

    # ---------- 模块状态（架构 v3 维度模块）----------
    def module(self, name, stage=None, status="running", dep=None, **kw):
        def fn(s):
            m = s.setdefault("modules", {}).setdefault(name, {"status": status})
            m.update({"status": status, "elapsed_sec": max(
                0, round(time.time() - self._t0_of(m), 1))})
            if stage is not None:
                m["stage"] = stage
            if dep:
                m["dep"] = dep
            m.update(kw)
        self._mutate(fn)

    # ---------- 错误 ----------
    def error(self, task, msg, retryable=False, fatal=False, warn=False, **ctx):
        """记录错误。retryable 自动重试；fatal 标记 failed；warn 降级。"""
        def fn(s):
            er = s.setdefault("errors", {})
            kind = "warn" if warn else ("retryable" if retryable else
                                        ("fatal" if fatal else "other"))
            er["total"] = er.get("total", 0) + 1
            er[kind] = er.get(kind, 0) + 1
            rec = {"ts": _now(), "task": task, "msg": str(msg)[:300], "kind": kind}
            rec.update(ctx)
            rec.setdefault("retry", 0)
            recent = er.setdefault("recent", [])
            recent.append(rec)
            del recent[:-50]                      # 只留最近 50 条
        self._mutate(fn)

    def error_retried(self, task, msg, attempt, **ctx):
        """记录一次重试（retryable 场景），attempt 从 1 开始。"""
        def fn(s):
            er = s.setdefault("errors", {})
            er.setdefault("recent", []).append(
                {"ts": _now(), "task": task, "msg": f"[重试{attempt}] {msg}"[:300],
                 "kind": "retryable", "retry": attempt})
        self._mutate(fn)

    # ---------- ETA ----------
    def set_eta(self, name, remaining_sec):
        self._mutate(lambda s: s.setdefault("eta", {}).__setitem__(name, remaining_sec))

    # ---------- 元信息 ----------
    def meta(self, **kw):
        self._mutate(lambda s: s.setdefault("meta", {}).update(kw))

    def finish(self, status="ok"):
        self._mutate(lambda s: s.update({"finished_at": _now(), "final_status": status}))


# ======================================================================
# 渲染
# ======================================================================
def enable_ansi():
    """Windows 下激活 VT 转义序列（无副作用）。"""
    if os.name == "nt":
        os.system("")


def compute_eta(done, total, elapsed_sec, rate_per=None):
    """剩余秒数估算。优先用已有 rate，否则用平均速率。"""
    remain = total - done
    if remain <= 0:
        return 0
    rate = rate_per or (elapsed_sec / done if done > 0 else None)
    if not rate or rate <= 0:
        return None
    return round(remain * rate)


def bar(filled, total, width=16):
    if total <= 0:
        return "[" + " " * width + "]"
    ratio = min(1.0, filled / total)
    n = int(width * ratio)
    return "[" + "█" * n + "░" * (width - n) + "]"


def fmt_sec(sec):
    sec = int(sec or 0)
    if sec < 60:
        return f"~{sec}s"
    return f"~{sec // 60}m{sec % 60:02d}s"


def fmt_tok(n):
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)


def render(state):
    """把 progress state 渲染成多行文本（无 ANSI 也可用）。"""
    out = []
    run_id = state.get("run_id", "?")
    out.append(f"PEDIA · RUN {run_id}")
    out.append("-" * 52)

    threads = state.get("stage2", {}) or {}
    for name, t in threads.items():
        total = t.get("total", 0)
        done = t.get("done", 0)
        status = t.get("status", "?")
        el = t.get("elapsed_sec", 0)
        eta = compute_eta(done, total, el, t.get("rate_per"))
        lbl = name.replace("thread_", "线程 ").strip()
        line = f"{lbl:<14} {bar(done, total)} {done}/{total}  {status:<8} {el:>5.0f}s"
        if eta is not None:
            line += f"  ETA {fmt_sec(eta)}"
        if t.get("rate_per"):
            line += f"  {t['rate_per']:.1f}s/项"
        out.append(line)
        extra = t.get("_extra")
        if extra:
            out.append("   " + extra)
    if not threads:
        out.append("（无 stage2 线程）")

    # token 行
    tk = state.get("tokens", {}) or {}
    if tk.get("total"):
        out.append("-" * 52)
        out.append(f"TOKEN   输入 {fmt_tok(tk.get('prompt'))} · 输出 {fmt_tok(tk.get('completion'))}"
                   f" · 合计 {fmt_tok(tk.get('total'))}")
        bm = tk.get("by_model", {}) or {}
        if bm:
            parts = [f"{m} {fmt_tok(v['total'])}" for m, v in bm.items()]
            out.append("        分档 " + " · ".join(parts))

    # 模块行
    mods = state.get("modules", {}) or {}
    if mods:
        out.append("-" * 52)
        for name, m in mods.items():
            dep = f"  dep:{m['dep']}" if m.get("dep") else ""
            out.append(f"模块  {name:<12} stage{m.get('stage', '-')}  {m.get('status', '?'):<10}"
                       f" {m.get('elapsed_sec', 0):.0f}s{dep}")

    # 错误行
    er = state.get("errors", {}) or {}
    if er.get("total"):
        out.append("-" * 52)
        out.append(f"错误 {er.get('total')} | 可重试 {er.get('retryable', 0)}"
                   f" | 致命 {er.get('fatal', 0)} | 警告 {er.get('warn', 0)}"
                   f"  详情: cli panel errors --run {run_id}")
        recent = er.get("recent", [])
        if recent:
            r = recent[-1]
            out.append(f"  ↳ 最近: {r.get('task')} {r.get('msg', '')[:80]}")

    if state.get("finished_at"):
        out.append("-" * 52)
        out.append(f"完成 {state.get('final_status')} @ {state.get('finished_at')}")
    return "\n".join(out)


# ======================================================================
# CLI
# ======================================================================
def _load_run(log_dir, run_id):
    p = os.path.join(_run_dir(log_dir, run_id), "progress.json")
    if not os.path.exists(p):
        raise SystemExit(f"[panel] 找不到 {p}（run_id 不存在）")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def cli_render(log_dir, run_id, watch=False, interval=2):
    enable_ansi()
    while True:
        st = _load_run(log_dir, run_id)
        print("\033[2J\033[H", end="")       # 清屏 + 光标回位
        print(render(st))
        if not watch or st.get("finished_at"):
            break
        time.sleep(interval)
    if st.get("final_status") != "ok":
        return 1
    return 0


def cli_errors(log_dir, run_id, by_task=False):
    st = _load_run(log_dir, run_id)
    er = st.get("errors", {}) or {}
    if not er.get("total"):
        print("[panel] 无错误记录")
        return 0
    if by_task:
        agg = {}
        for r in er.get("recent", []):
            t = agg.setdefault(r.get("task", "?"), {"count": 0, "kinds": {}})
            t["count"] += 1
            t["kinds"][r.get("kind", "?")] = t["kinds"].get(r.get("kind", "?"), 0) + 1
            t["last"] = r.get("msg", "")
        print(f"{'任务':<30}{'次数':>4}  分级")
        for task, a in sorted(agg.items(), key=lambda x: -x[1]["count"]):
            kinds = " ".join(f"{k}:{c}" for k, c in a["kinds"].items())
            print(f"{task:<30}{a['count']:>4}  {kinds}  | {a['last'][:60]}")
    else:
        for r in er.get("recent", []):
            print(f"{r.get('ts')} [{r.get('kind')}] {r.get('task')} "
                  f"ch={r.get('ch', '-')} {r.get('msg', '')}")
    return 0 if not er.get("fatal") else 1


def cli_tokens(log_dir, run_id):
    st = _load_run(log_dir, run_id)
    tk = st.get("tokens", {}) or {}
    if not tk.get("total"):
        print("[panel] 无 token 记录")
        return 0
    print(f"输入 {tk.get('prompt'):,} · 输出 {tk.get('completion'):,} · 合计 {tk.get('total'):,}")
    for m, v in (tk.get("by_model", {}) or {}).items():
        print(f"  {m:<20} 输入 {v.get('prompt', 0):,} · 输出 {v.get('completion', 0):,}"
              f" · 小计 {v.get('total', 0):,}")
    return 0


def cli_demo(log_dir, watch=True, seconds=6):
    """模拟 stage2 双线程：抽取线（任务）+ 精读线（按章）+ token + 错误。"""
    import random
    p = Panel(log_dir=log_dir)
    p.meta(book="诡秘之主", demo=True)
    p.thread("thread_extract", total=48, status="running")
    p.thread("thread_deep_read", total=50, status="running")
    p.module("char", stage=3, status="running")
    p.module("rel", stage=None, status="waiting", dep="char")
    p._demo_err = False
    t0 = time.time()
    done_ex, done_dr = 0, 0
    while time.time() - t0 < seconds:
        done_ex = min(48, done_ex + random.randint(3, 8))
        done_dr = min(50, done_dr + random.randint(1, 3))
        p.progress("thread_extract", done=done_ex)
        p.progress("thread_deep_read", done=done_dr)
        p.add_tokens("glm-4-flash", random.randint(500, 2000), random.randint(200, 900))
        if done_dr >= 5 and not p._demo_err:
            p.error("thread_deep_read", msg="json 解析失败, 已重试", retryable=True, ch=5)
            p.error("thread_extract", msg="实体对齐异常: 梅莉莎 与 梅丽莎 疑为同一实体",
                    warn=True, task2="detect")
            p._demo_err = True
        if done_ex == 48:
            p.thread_done("thread_extract")
            p.set_eta("thread_extract", 0)
        if done_dr == 50:
            p.thread_done("thread_deep_read")
        if done_ex >= 48 and done_dr >= 50:
            break
        if watch:
            st = json.load(open(p.path, encoding="utf-8"))
            enable_ansi()
            print("\033[2J\033[H", end="")
            print(render(st))
            print(f"（demo 运行中，{max(0, seconds - int(time.time() - t0))}s 后结束）")
            time.sleep(0.8)
        else:
            time.sleep(0.5)
    p.finish("ok" if done_ex >= 48 and done_dr >= 50 else "incomplete")
    print(f"[panel] demo 完成 run_id={p.run_id}  progress={p.path}")
    return 0


def main():
    import argparse
    ap = argparse.ArgumentParser(prog="panel.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo", help="模拟 stage2 双线程演示")
    d.add_argument("--no-watch", action="store_true")
    d.add_argument("--seconds", type=int, default=6)

    r = sub.add_parser("render", help="渲染一次进度快照")
    r.add_argument("--run", required=True)
    r.add_argument("--watch", action="store_true")

    e = sub.add_parser("errors", help="错误记录")
    e.add_argument("--run", required=True)
    e.add_argument("--by-task", action="store_true")

    t = sub.add_parser("tokens", help="token 汇总")
    t.add_argument("--run", required=True)

    a = ap.parse_args()
    log_dir = os.environ.get("PEDIA_LOG_DIR") or DEFAULT_LOG_DIR
    if a.cmd == "demo":
        return cli_demo(log_dir, watch=not a.no_watch, seconds=a.seconds)
    if a.cmd == "render":
        return cli_render(log_dir, a.run, watch=a.watch)
    if a.cmd == "errors":
        return cli_errors(log_dir, a.run, by_task=a.by_task)
    if a.cmd == "tokens":
        return cli_tokens(log_dir, a.run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
