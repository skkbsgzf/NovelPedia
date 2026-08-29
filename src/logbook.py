# -*- coding: utf-8 -*-
"""logbook.py —— 跑批日志基础设施(统一日志/进度/指标/会话)。

设计目标(常态化):
  1. 双通道: 人类可读 .log(控制台同款) + 结构化 .jsonl(程序可分析)
  2. 分级: DEBUG/INFO/WARN/ERROR
  3. 阶段上下文: 每条日志带 step(extract/collect/mine/archive/...)
  4. 进度+ETA: progress() 输出 done/total/elapsed/eta
  5. 指标: gauge() 记录阶段性指标(词条数/失败数/推理数...), 供 cli analyze 做曲线
  6. 会话文件: logs/run_<时间戳>.log + .jsonl + run_latest.jsonl(最近会话指针)

用法:
  from logbook import log
  log.info('collect', '场景数', scenes=2452, chapters=300)
  log.progress('clue', done=1200, total=2452, elapsed=3600.5)
  log.gauge('setting', 'terms', n)
  log.error('clue', '提取失败', scene=101, err='timeout')

分析:  cli analyze  → 读 logs/run_latest.jsonl 生成跑批健康报告
"""
import json
import os
import threading
import time
from datetime import datetime

_LOGS_DIR = None
_LOCK = threading.Lock()


def _logs_path():
    global _LOGS_DIR
    if _LOGS_DIR is None:
        # 日志统一放 novel_pipeline/logs/
        _LOGS_DIR = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
        os.makedirs(_LOGS_DIR, exist_ok=True)
    return _LOGS_DIR


class _Logbook:
    """线程安全的双通道日志器。"""

    LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}

    def __init__(self, name="run", level="INFO", console=True):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.ts = ts
        self.level = self.LEVELS.get(level.upper(), 20)
        self.console = console
        d = _logs_path()
        self.log_path = os.path.join(d, f"{name}_{ts}.log")
        self.jsonl_path = os.path.join(d, f"{name}_{ts}.jsonl")
        self.latest_path = os.path.join(d, f"{name}_latest.jsonl")
        self._f_log = open(self.log_path, "w", encoding="utf-8")
        self._f_jsonl = open(self.jsonl_path, "w", encoding="utf-8")
        # 最近会话指针(供 cli analyze 默认读取) —— 写失败只降级, 绝不让跑批崩溃
        # (Windows 下 run_latest 可能被上一进程延迟占用, 导致 PermissionError)
        self._latest_ok = False
        try:
            with open(self.latest_path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"session": self.ts, "log": self.log_path,
                                    "jsonl": self.jsonl_path}, ensure_ascii=False) + "\n")
            self._latest_ok = True
        except OSError:
            self._latest_ok = False
        self._start = time.time()
        self.info("logbook", "会话开始", session=self.ts,
                  log=self.log_path, jsonl=self.jsonl_path)

    # ---------------- 底层 ----------------
    def _emit(self, level, step, msg, **fields):
        if self.LEVELS.get(level, 20) < self.level:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        line_h = f"[{ts}] [{step}] [{level}] {msg}"
        rec = {"ts": datetime.now().isoformat(timespec="seconds"),
               "level": level, "step": step, "msg": msg, **fields}
        with _LOCK:
            self._f_log.write(line_h + (" | " + json.dumps(fields, ensure_ascii=False) if fields else "") + "\n")
            self._f_log.flush()
            self._f_jsonl.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._f_jsonl.flush()
            if self.console:
                print(line_h, flush=True)

    # ---------------- 公开 API ----------------
    def debug(self, step, msg, **f):
        self._emit("DEBUG", step, msg, **f)

    def info(self, step, msg, **f):
        self._emit("INFO", step, msg, **f)

    def warn(self, step, msg, **f):
        self._emit("WARN", step, msg, **f)

    def error(self, step, msg, **f):
        self._emit("ERROR", step, msg, **f)

    def section(self, step, title):
        self._emit("INFO", step, "=" * 60 + f"\n  {title}\n" + "=" * 60)

    def progress(self, step, done, total, elapsed=None, extra=""):
        """进度 + ETA。elapsed 为累计秒, 自动算 ETA。"""
        elapsed = elapsed if elapsed is not None else time.time() - self._start
        if done > 0 and total > 0:
            eta = elapsed / done * (total - done)
            eta_s = f"{int(eta // 3600)}h{int((eta % 3600) // 60)}m"
            pct = done / total * 100
        else:
            eta_s, pct = "?", 0
        self._emit("INFO", step,
                   f"进度 {done}/{total} ({pct:.1f}%) 已用 {int(elapsed // 60)}m ETA {eta_s} {extra}",
                   done=done, total=total, pct=round(pct, 1), eta_sec=round(eta, 1) if done else None)

    def gauge(self, step, name, value, **extra):
        """阶段性指标(词条数/失败数/推理数/时长...), cli analyze 按 step+name 聚合出曲线。"""
        self._emit("INFO", f"gauge:{step}", f"{name}={value}", metric=name, value=value, **extra)

    def close(self):
        self.info("logbook", "会话结束", elapsed_sec=round(time.time() - self._start, 1))
        with _LOCK:
            self._f_log.close()
            self._f_jsonl.close()


_logbook = None


def get_logbook(name="run", level="INFO", console=True):
    global _logbook
    if _logbook is None:
        _logbook = _Logbook(name=name, level=level, console=console)
    return _logbook


def log():
    """获取全局日志器(惰性)。"""
    return get_logbook()


if __name__ == "__main__":
    lb = get_logbook(name="demo")
    lb.section("collect", "Stage1 收集")
    lb.info("collect", "场景数", scenes=2452, chapters=300)
    for i in range(1, 1001, 100):
        lb.progress("clue", done=i, total=1000)
    lb.gauge("setting", "terms", 119)
    lb.gauge("clue", "evidence", 6181)
    lb.error("clue", "提取失败", scene=101, err="timeout")
    lb.close()
    print("\n日志文件:", lb.log_path)
    print("jsonl   :", lb.jsonl_path)
