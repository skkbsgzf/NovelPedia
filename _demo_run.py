# -*- coding: utf-8 -*-
"""Demo 后台 runner：为一次上传跑完整流水线，写出 progress.json / result.json。
用法: python _demo_run.py <job_dir> <book_path> <chapters> <backend>
backend: xiaohongshu(默认) | glm | qwen3
"""
import os, sys, json, time, subprocess, glob, re, threading

ROOT = os.path.dirname(os.path.abspath(__file__))
# 小红书 key 从环境变量读(部署时配置); 若无则尝试配置里的兜底
XHS_KEY = os.environ.get("LLM_API_KEY", "")
GLM_KEY = os.environ.get("GLM_API_KEY", "")


def w(d, prog):
    p = os.path.join(d, "progress.json")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(prog, f, ensure_ascii=False)
    os.replace(tmp, p)


def slug_of(name):
    return re.sub(r"[^\w\u4e00-\u9fff-]", "_", name)


def count_json(path):
    try:
        return len(json.load(open(path, encoding="utf-8")))
    except Exception:
        return 0


def read_stdout_thread(proc, buf):
    try:
        for line in proc.stdout:
            buf.append(line)
    except Exception:
        pass


def backend_key(backend):
    # key 一律从环境变量读取（部署时配置），不落盘明文；缺失则返回空串（调用方会跳过 --key）
    if backend == "glm":
        return GLM_KEY
    if backend == "xiaohongshu":
        return XHS_KEY
    return ""




def build_env(name, book_path, chapters, backend):
    """构造子进程环境变量: 用 NOVEL_NAME/BOOK_PATH/CHAPTERS 覆盖, 避免共享 settings.json 竞态。"""
    env = dict(os.environ)
    env["NOVEL_NAME"] = name
    env["BOOK_PATH"] = book_path
    env["NOVEL_CHAPTERS"] = str(chapters)
    key = backend_key(backend)
    if key and backend != "qwen3":
        env["LLM_API_KEY"] = key
    return env

def run_stage1(job_dir, py, chapters, slug, backend, env):
    cache = os.path.join(ROOT, "data", f"extract_cache_{slug}_rule_v2_{chapters}.json")
    if os.path.exists(cache):
        os.remove(cache)
    key = backend_key(backend)
    cmd = [py, "src/cli.py", "extract", "--backend", backend]
    if key and backend != "qwen3":
        cmd += ["--key", key]
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    buf = []
    t = threading.Thread(target=read_stdout_thread, args=(proc, buf), daemon=True)
    t.start()
    w(job_dir, {"stage": "stage1-ingest", "percent": 5,
                "message": "解析小说 + 场景切分中…", "done": False})
    total = None
    while proc.poll() is None:
        for l in buf:
            m = re.search(r"(\d+) 个场景块", l)
            if m:
                total = int(m.group(1))
        if os.path.exists(cache):
            cnt = count_json(cache)
            if total:
                pct = min(92, 5 + int(cnt / total * 80))
            else:
                pct = 10
            w(job_dir, {"stage": "stage1-extract", "percent": pct,
                        "message": f"LLM 抽取中: {cnt}" + (f"/{total} 场景" if total else ""),
                        "done": False})
        time.sleep(2)
    t.join()
    out = "".join(buf)
    m = re.search(r"(\d+) 个场景块", out)
    total = int(m.group(1)) if m else 0
    cnt = count_json(cache) if os.path.exists(cache) else 0
    m2 = re.search(r"成功 (\d+) / 失败 (\d+)", out)
    ok, fail = (int(m2.group(1)), int(m2.group(2))) if m2 else (cnt, 0)
    w(job_dir, {"stage": "stage1", "percent": 100,
                "message": f"Stage1 完成: {total} 场景, 成功 {ok} / 失败 {fail}",
                "done": False})


def run_stage2(job_dir, py, chapters, slug, backend, env):
    proc = subprocess.Popen(
        [py, "src/cli.py", "build", "--backend", backend] +
        (["--key", backend_key(backend)] if backend != "qwen3" else []),
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    buf = []
    t = threading.Thread(target=read_stdout_thread, args=(proc, buf), daemon=True)
    t.start()
    done = 0
    while proc.poll() is None:
        for l in buf:
            m = re.search(r"第(\d+)章 OK", l)
            if m:
                done = max(done, int(m.group(1)))
        pct = min(95, 10 + int(done / max(1, chapters) * 80)) if done else 8
        w(job_dir, {"stage": "stage2", "percent": pct,
                    "message": f"Stage2 生成章纲/人物/设定: {done}/{chapters}", "done": False})
        time.sleep(2)
    t.join()
    w(job_dir, {"stage": "stage2", "percent": 100,
                "message": "Stage2 完成: 章纲 + 人物 + 设定", "done": False})


def run_stage3(job_dir, py, backend, env):
    proc = subprocess.Popen(
        [py, "src/cli.py", "viz", "--backend", backend] +
        (["--key", backend_key(backend)] if backend != "qwen3" else []),
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    out, _ = proc.communicate()
    w(job_dir, {"stage": "stage3", "percent": 100,
                "message": "Stage3 可视化产物生成", "done": False})
    return proc.returncode


def main():
    job_dir, book_path = sys.argv[1], sys.argv[2]
    chapters = int(sys.argv[3])
    backend = sys.argv[4] if len(sys.argv) > 4 else "xiaohongshu"
    name = os.path.splitext(os.path.basename(book_path))[0]
    slug = slug_of(name)
    py = sys.executable

    w(job_dir, {"stage": "init", "percent": 0, "message": "开始", "done": False})

    # 写 settings.json (novel)
    sp = os.path.join(ROOT, "settings.json")
    s = json.load(open(sp, encoding="utf-8"))
    s["novel"]["name"] = name
    s["novel"]["path"] = book_path
    s["novel"]["chapters"] = chapters
    json.dump(s, open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    env = build_env(name, book_path, chapters, backend)
    try:
        run_stage1(job_dir, py, chapters, slug, backend, env)
        run_stage2(job_dir, py, chapters, slug, backend, env)
        run_stage3(job_dir, py, backend, env)

        # 产物目录 = 由 env 确定的 config.OUTPUT_DIR (不再 glob 猜最新, 避免目录 mtime 不更新)
        import datetime as _dt
        outdir = os.path.join(ROOT, "outputs", "%s_%s" % (name, _dt.datetime.now().strftime("%Y%m%d")))
        if not os.path.exists(outdir):
            # 回退: 找含该书名的最新目录
            cands = [d for d in glob.glob(os.path.join(ROOT, "outputs", "*"))
                     if name in os.path.basename(d)]
            outdir = cands[0] if cands else outdir
        rel = os.path.relpath(outdir, ROOT) if os.path.exists(outdir) else ""
        result = {"novel": name, "output_dir": rel,
                  "index": rel + "/index.html" if rel else "",
                  "products": sorted(os.listdir(outdir)) if os.path.exists(outdir) else []}
        w(job_dir, {"stage": "done", "percent": 100, "message": "全部完成 ✅",
                    "done": True, "result": result})
    except Exception as e:
        import traceback
        w(job_dir, {"stage": "error", "percent": 0, "message": "失败",
                    "done": True, "error": str(e),
                    "traceback": traceback.format_exc()[-800:]})


if __name__ == "__main__":
    main()