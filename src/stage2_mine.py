# -*- coding: utf-8 -*-
"""
stage2_mine.py —— Stage2 四维度并行挖掘 (结合 stage1 原料, 强化结果)

输入: stage1 产物 (clue_graph / settings_graph / character_facts / style_samples)
四路并行 (ThreadPoolExecutor, 互不依赖):
  1. 剧情推理    clue_agent.synthesize_ready()      → clue_graph.json 写回结论
  2. 设定强化    setting_agent.strengthen()         → settings_system.json (体系分层/矛盾检测)
  3. 人物简历    character_agent.build_all_resumes() → characters_resume.json
  4. 文风分析    style_sampler.analyze_style()      → style_analysis.json

Stage2 结束 = 四维结果齐: 剧情结论 / 设定体系 / 人物简历 / 文风分析

用法:
  python src/stage2_mine.py [--chapters N] [--backend glm] [--doubt-index 0.5] [--parallel 4]
"""
import sys
import os
import json
import argparse
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C
import llm_client
import clue_agent
import setting_agent
from logbook import get_logbook as _get_logbook
import character_agent
import style_sampler
import style_baseline


def setup_backend(backend, doubt_index):
    cfg = llm_client.load_config()
    model = None
    if backend:
        b = cfg.get(backend) or {}
        os.environ["LLM_BACKEND"] = b.get("backend", "ollama")
        os.environ["LLM_BASE_URL"] = b.get("base_url", C.OLLAMA_BASE)
        os.environ["LLM_MODEL"] = b.get("model", C.EXTRACT_MODEL)
        os.environ["LLM_AUTH_SCHEME"] = b.get("auth_scheme", "none")
        os.environ["LLM_ENABLE_THINKING"] = "true" if b.get("enable_thinking", False) else "false"
        model = b.get("model")
        if b.get("auth_scheme", "none") != "none":
            key = os.environ.get("LLM_API_KEY") or C.LLM_API_KEY
            if key:
                os.environ["LLM_API_KEY"] = key
    os.environ["LLM_DOUBT_INDEX"] = str(doubt_index)
    import importlib
    importlib.reload(llm_client)
    return model


def _latest_stage1_dir():
    """找到最新的 stage1 产物目录(优先当前小说+时间戳; 否则扫 outputs 按 mtime 找含 clue_graph 的最新目录)。"""
    if os.path.exists(C.STAGE1_DIR) and os.path.exists(
            os.path.join(C.STAGE1_DIR, "clue_graph.json")):
        return C.STAGE1_DIR
    outputs = os.path.join(C.PROJECT_ROOT, "outputs")
    cands = []
    if os.path.exists(outputs):
        for d in os.listdir(outputs):
            s1 = os.path.join(outputs, d, "stage1")
            if os.path.exists(os.path.join(s1, "clue_graph.json")):
                try:
                    mt = os.path.getmtime(os.path.join(s1, "clue_graph.json"))
                except Exception:
                    mt = 0
                cands.append((mt, s1))
    # 按 clue_graph 修改时间倒序(最新产物优先), 避免跨书名字符串排序误选
    cands.sort(key=lambda x: -x[0])
    if not cands:
        return C.STAGE1_DIR
    # 优先当前小说的产物
    for _mt, s1 in cands:
        if C.NOVEL_NAME in s1:
            return s1
    return cands[0][1]


def run(chapters=None, backend=None, doubt_index=None, parallel=4):
    t0 = time.time()
    cfg = llm_client.load_config()
    doubt_index = float(doubt_index) if doubt_index is not None else float(cfg.get("doubt_index", 0.5))
    model = setup_backend(backend, doubt_index)
    chapters = chapters or C.CHAPTERS
    out_dir = _latest_stage1_dir()
    if not os.path.exists(out_dir):
        print(f"⚠️ 未找到 stage1 产物目录: {out_dir} (先运行 stage1_collect)")
        return {}

    clue_path = os.path.join(out_dir, "clue_graph.json")
    settings_graph_path = os.path.join(out_dir, "settings_graph.json")
    char_facts_path = os.path.join(out_dir, "character_facts.json")
    style_samples_path = os.path.join(out_dir, "style_samples.json")
    lb = _get_logbook()
    lb.section("mine", f"Stage2 挖掘 · 产物 {out_dir}")
    lb.info("mine", "开始", doubt_index=doubt_index, parallel=parallel, out_dir=str(out_dir))

    results = {}
    def _clue_mine():
        t1 = time.time()
        g = clue_agent.load_graph(clue_path)
        n, stats = clue_agent.synthesize_ready(g, C.OLLAMA_BASE, model, doubt_index)
        g["meta"]["conclusions_total"] = len(g.get("conclusions", []))
        g["meta"]["stage"] = "mined"
        g["meta"]["consolidate"] = stats
        clue_agent.save_graph(clue_path, g)
        lb.info("mine", "consolidate", archived_evidence=stats.get('archived_evidence', 0),
                archived_clusters=stats.get('archived_clusters', 0),
                refuted=stats.get('refuted', 0), new_conclusions=n,
                total=len(g.get("conclusions", [])), secs=round(time.time()-t1, 1))
        return ("clue", {"conclusions": n, "total": len(g.get("conclusions", [])),
                         "secs": time.time() - t1})
    def _setting_mine():
        t1 = time.time()
        try:
            sys_path = settings_graph_path
            graph = setting_agent.load_graph(sys_path)
            system = setting_agent.strengthen(graph, C.OLLAMA_BASE, model, doubt_index)
            out = os.path.join(out_dir, "settings_system.json")
            with open(out, "w", encoding="utf-8") as f:
                json.dump(system, f, ensure_ascii=False, indent=2)
            return ("setting", {"layers": len(system.get("layers", [])),
                                "conflicts": len(system.get("conflicts", [])),
                                "secs": time.time() - t1})
        except Exception as e:
            return ("setting", {"error": str(e), "secs": time.time() - t1})
    def _char_mine():
        t1 = time.time()
        chars = {}
        if os.path.exists(char_facts_path):
            with open(char_facts_path, encoding="utf-8") as f:
                chars = json.load(f).get("characters", {})
        clue_ev = {}
        if os.path.exists(clue_path):
            g = clue_agent.load_graph(clue_path)
            clue_ev = character_agent.index_clues_by_entity(g)
        resumes = character_agent.build_all_resumes(
            chars, clue_ev, C.OLLAMA_BASE, model, parallel)
        out = os.path.join(out_dir, "characters_resume.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "characters": resumes}, f, ensure_ascii=False, indent=2)
        return ("char", {"resumes": len(resumes), "secs": time.time() - t1})
    def _style_mine():
        t1 = time.time()
        out = os.path.join(out_dir, "style_analysis.json")
        style = None
        sm_path = os.path.join(out_dir, "scenes_meta.json")
        if os.path.exists(sm_path):
            # ---- v2 主路径: 组合式聚合（词/句/段/篇, 取经各维度产物） ----
            try:
                deps = {"scenes_meta": json.load(open(sm_path, encoding="utf-8")).get("scenes", [])}
                for k, fn in [("word_freq", "word_freq.json"),
                              ("sentence_stats", "sentence_stats.json")]:
                    p = os.path.join(out_dir, fn)
                    if os.path.exists(p):
                        deps[k] = json.load(open(p, encoding="utf-8"))
                for k, p in [("char_facts", char_facts_path),
                             ("clue_graph", clue_path),
                             ("settings_graph", settings_graph_path)]:
                    try:
                        d = json.load(open(p, encoding="utf-8"))
                        deps[k] = d.get("characters", d) if k == "char_facts" else d
                    except Exception:
                        deps[k] = {} if k != "char_facts" else {}
                try:
                    rp = os.path.join(out_dir, "characters_resume.json")
                    deps["resumes"] = json.load(open(rp, encoding="utf-8")).get("characters", [])
                except Exception:
                    deps["resumes"] = []
                try:
                    import sqlite3 as _sq
                    _conn = _sq.connect(C.DB_PATH)
                    deps["total_chars"] = _conn.execute(
                        "SELECT SUM(LENGTH(text)) FROM paragraphs WHERE chapter_no<=?",
                        (chapters,)).fetchone()[0] or 0
                    _conn.close()
                except Exception:
                    deps["total_chars"] = 0
                style = style_sampler.analyze_style_v2(deps, C.OLLAMA_BASE, model)
            except Exception as e:
                lb.error("style", "v2 聚合失败, 降级旧版", err=str(e)[:200])
                style = None
        if not style:
            # ---- 降级旧版（scenes_meta 缺失时, 保护旧产物/未跑完的批） ----
            corpus = None
            if os.path.exists(style_samples_path):
                with open(style_samples_path, encoding="utf-8") as f:
                    d = json.load(f)
                    corpus = {"sampled": d.get("sampled", []), "chars": d.get("chars", 0)}
            if not corpus or not corpus.get("sampled"):
                corpus = style_sampler.sample_style_text(chapters=chapters, ratio=0.1, seed=None)
            style = style_sampler.analyze_style(corpus, C.OLLAMA_BASE, model)
        if style:
            with open(out, "w", encoding="utf-8") as f:
                json.dump(style, f, ensure_ascii=False, indent=2)
            lb.info("style", "文风分析完成", schema=style.get("schema", 1),
                    secs=round(time.time()-t1, 1))
            # 跨书对比基准库: 聚合进 genre 桶 + all 桶(方案 B, 纯跨书积累)
            try:
                _st = style_baseline.stats_from_style(style)
                _genre = os.getenv("NOVEL_GENRE", "").strip() or None
                style_baseline.ingest(_genre, _st)
                lb.info("style", "基准库已更新", genre=_genre or "all-only")
            except Exception as _e:
                lb.error("style", "基准库更新失败", err=str(_e)[:200])
        return ("style", {"done": bool(style), "secs": time.time() - t1})

    tasks = {"剧情推理": _clue_mine, "设定强化": _setting_mine,
             "人物简历": _char_mine, "文风分析": _style_mine}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fn): name for name, fn in tasks.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                key, info = fut.result()
                results[key] = info
                print(f"  ✅ {name}: {info} ({info.get('secs', 0):.0f}s)")
            except Exception as e:
                print(f"  ❌ {name}: {e}")

    print(f"\n=== Stage2 挖掘完成 (总耗时 {time.time()-t0:.0f}s) ===")
    print(f"  剧情推理: {results.get('clue', {}).get('conclusions', 0)} 条新结论")
    print(f"  设定强化: {results.get('setting', {}).get('layers', 0)} 层体系 / "
          f"{results.get('setting', {}).get('conflicts', 0)} 处矛盾")
    print(f"  人物简历: {results.get('char', {}).get('resumes', 0)} 份")
    print(f"  文风分析: {'完成' if results.get('style', {}).get('done') else '失败'}")
    print(f"  产物目录: {out_dir}")
    return results


def main():
    p = argparse.ArgumentParser(description="Stage2 四维度并行挖掘")
    p.add_argument("--chapters", type=int, default=None)
    p.add_argument("--backend", default=None, help="LLM 后端(glm/qwen3/xiaohongshu)")
    p.add_argument("--doubt-index", dest="doubt_index", type=float, default=None)
    p.add_argument("--parallel", type=int, default=4, help="人物简历并发数(默认4)")
    a = p.parse_args()
    run(chapters=a.chapters, backend=a.backend, doubt_index=a.doubt_index,
        parallel=a.parallel)


if __name__ == "__main__":
    main()
