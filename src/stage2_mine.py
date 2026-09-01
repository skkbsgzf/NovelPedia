# -*- coding: utf-8 -*-
"""
stage2_mine.py —— Stage2 三维度并行挖掘 (结合 stage1 原料, 强化结果)

输入: stage1 产物 (settings_graph / character_facts / style_samples)
三路并行 (ThreadPoolExecutor, 互不依赖):
  1. 设定强化    setting_agent.strengthen()         → settings_system.json (体系分层/矛盾检测)
  2. 人物简历    character_agent.build_all_resumes() → characters_resume.json
  3. 文风分析    style_sampler.analyze_style()      → style_analysis.json

Stage2 结束 = 三维结果齐: 设定体系 / 人物简历 / 文风分析
  （剧情推理 / 暗线证据已从 pedia 剥离, 迁至 studio）

用法:
  python src/stage2_mine.py [--chapters N] [--backend glm] [--doubt-index 0.5] [--parallel 4]
  python src/stage2_mine.py --only setting --force   # 仅重跑设定强化并覆盖
  python src/stage2_mine.py --only character          # 仅重跑人物简历(已存在则跳过)
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
import setting_agent
from logbook import get_logbook as _get_logbook
import character_agent
import style_sampler
import style_baseline


def _atomic_write(path, obj):
    """原子写 JSON: 先写 .tmp 再 os.replace, 避免崩溃残留半截文件导致误判'已完成'。"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _valid_json(path):
    """产物是否已存在且为合法 JSON(用于 skip-if-exists 判定)。"""
    try:
        with open(path, encoding="utf-8") as f:
            json.load(f)
        return True
    except Exception:
        return False


def setup_backend(backend, doubt_index):
    """配置 LLM 后端环境变量, 返回 model 名。

    修复(2026-09-01): 原来用 llm_client.load_config() 读顶层配置拿不到 presets,
    LLM_MODEL 被默认成 qwen3:8b, 经 _resolve_model 劫持 -> 云端 URL + 本地模型名 400。
    改用 get_preset(backend) 解析 models.json 预设, 与 stage1_collect 一致。
    """
    model = None
    if backend:
        p = llm_client.get_preset(backend)
        if p:
            os.environ["LLM_BACKEND"] = p.get("backend", "ollama")
            os.environ["LLM_BASE_URL"] = p.get("base_url", C.OLLAMA_BASE)
            os.environ["LLM_MODEL"] = p.get("model", C.EXTRACT_MODEL)
            os.environ["LLM_AUTH_SCHEME"] = p.get("auth_scheme", "none")
            os.environ["LLM_ENABLE_THINKING"] = "true" if p.get("enable_thinking", False) else "false"
            model = p.get("model")
            if p.get("auth_scheme", "none") != "none":
                key = os.environ.get("LLM_API_KEY") or C.LLM_API_KEY
                if key:
                    os.environ["LLM_API_KEY"] = key
    os.environ["LLM_DOUBT_INDEX"] = str(doubt_index)
    import importlib
    importlib.reload(llm_client)
    return model


def _latest_stage1_dir():
    """找到最新的 stage1 产物目录(优先当前小说+时间戳; 否则扫 outputs 按 mtime 找含 character_facts 的最新目录)。"""
    if os.path.exists(C.STAGE1_DIR) and os.path.exists(
            os.path.join(C.STAGE1_DIR, "character_facts.json")):
        return C.STAGE1_DIR
    outputs = os.path.join(C.PROJECT_ROOT, "outputs")
    cands = []
    if os.path.exists(outputs):
        for d in os.listdir(outputs):
            s1 = os.path.join(outputs, d, "stage1")
            if os.path.exists(os.path.join(s1, "character_facts.json")):
                try:
                    mt = os.path.getmtime(os.path.join(s1, "character_facts.json"))
                except Exception:
                    mt = 0
                cands.append((mt, s1))
    # 按产物修改时间倒序(最新产物优先), 避免跨书名字符串排序误选
    cands.sort(key=lambda x: -x[0])
    if not cands:
        return C.STAGE1_DIR
    # 优先当前小说的产物
    for _mt, s1 in cands:
        if C.NOVEL_NAME in s1:
            return s1
    return cands[0][1]


def build_stage2_compat(out_dir):
    """治本收尾: 由 stage1 产物组装旧式 stage2/characters.json + settings.json,
    使 viz 主路径(build_detail / export_graph / gen_personality)直接可读,
    不再依赖各脚本内置的 stage1 fallback。
    字段契约与 build_detail._stage1_fallback 完全一致:
      - 人物(中文键): name/aliases/身份/首次出现章/关键事件/关系/弧光
      - 设定(英文键): name/type/description/first_seen/related/note
    规模控制: DETAIL_TOP_CHARS(默认80) / DETAIL_TOP_SETTINGS(默认800)。
    """
    stage2 = os.path.join(os.path.dirname(out_dir), "stage2")

    def _load(p, default=None):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return default if default is not None else {}

    # --- 人物骨架: character_facts 出场频次 top N ---
    cf = _load(os.path.join(out_dir, "character_facts.json"), {})
    freq = {n: len((v or {}).get("appearances") or [])
            for n, v in (cf.get("characters") or {}).items()}
    if not freq:
        return None
    top_chars = int(os.environ.get("DETAIL_TOP_CHARS", "80"))
    names = [n for n, _f in sorted(freq.items(), key=lambda kv: -kv[1])[:top_chars]]

    # --- 别名表: entity_registry(category==人物) canonical->aliases ---
    alias_map = {}
    er_path = os.path.join(out_dir, "entity_registry.json")
    if os.path.exists(er_path):
        er = _load(er_path, {})
        for lst in (er.values() if isinstance(er, dict) else []):
            if not isinstance(lst, list):
                continue
            for e in lst:
                if isinstance(e, dict) and e.get("category") == "人物":
                    canon = str(e.get("canonical") or "").strip()
                    if canon:
                        alias_map[canon] = [str(a) for a in (e.get("aliases") or [])]

    # --- 简历填充: characters_resume identity/trajectory/relations ---
    resume = _load(os.path.join(out_dir, "characters_resume.json"), {})
    by_name = {c.get("name"): c for c in (resume.get("characters") or [])
               if isinstance(c, dict)}

    chars = []
    for nm in names:
        v = (cf.get("characters") or {}).get(nm) or {}
        apps = v.get("appearances") or []
        chs = [a[0] for a in apps if isinstance(a, (list, tuple)) and a
               and isinstance(a[0], int)]
        r = by_name.get(nm) or {}
        traj = (r.get("trajectory") or "").replace("\n", " ")
        rels = []
        for rr in (r.get("relations") or [])[:8]:
            rn = str(rr.get("name", "")).strip()
            rd = str(rr.get("relation", "")).strip()
            if rn:
                rels.append(f"与{rn}: {rd}")
        if not rels:
            co = v.get("co_occurrences") or {}
            rels = [f"与{c}: 共现{n}次"
                    for c, n in sorted(co.items(), key=lambda kv: -kv[1])[:8]]
        events = [traj] if traj else [str(d) for d in (v.get("doings") or [])[:5]]
        chars.append({
            "name": nm,
            "aliases": alias_map.get(nm, []),
            "身份": r.get("identity") or "",
            "首次出现章": min(chs) if chs else None,
            "关键事件": events,
            "关系": rels,
            "弧光": traj,
        })

    # --- 设定: settings_graph terms 按出现场景数取 top N ---
    sg = _load(os.path.join(out_dir, "settings_graph.json"), {})
    terms = [t for t in (sg.get("terms") or []) if isinstance(t, dict)]
    top_sets = int(os.environ.get("DETAIL_TOP_SETTINGS", "800"))
    terms.sort(key=lambda t: len(t.get("source_scenes") or []), reverse=True)
    sets_ = []
    for t in terms[:top_sets]:
        nm = str(t.get("name", "")).strip()
        if not nm:
            continue
        rel = [str(r.get("to")) for r in (t.get("related") or [])
               if isinstance(r, dict) and r.get("to")]
        sets_.append({
            "name": nm, "type": t.get("category") or "",
            "description": t.get("definition") or "",
            "first_seen": t.get("first_seen"),
            "related": rel, "note": "",
        })

    os.makedirs(stage2, exist_ok=True)
    with open(os.path.join(stage2, "characters.json"), "w", encoding="utf-8") as f:
        json.dump(chars, f, ensure_ascii=False, indent=1)
    with open(os.path.join(stage2, "settings.json"), "w", encoding="utf-8") as f:
        json.dump(sets_, f, ensure_ascii=False, indent=1)
    return stage2, len(chars), len(sets_)


def run(chapters=None, backend=None, doubt_index=None, parallel=4, only=None, force=False):
    t0 = time.time()
    cfg = llm_client.load_config()
    doubt_index = float(doubt_index) if doubt_index is not None else float(cfg.get("doubt_index", 0.5))
    model = setup_backend(backend, doubt_index)
    chapters = chapters or C.CHAPTERS
    out_dir = _latest_stage1_dir()
    if not os.path.exists(out_dir):
        print(f"⚠️ 未找到 stage1 产物目录: {out_dir} (先运行 stage1_collect)")
        return {}

    settings_graph_path = os.path.join(out_dir, "settings_graph.json")
    char_facts_path = os.path.join(out_dir, "character_facts.json")
    style_samples_path = os.path.join(out_dir, "style_samples.json")
    lb = _get_logbook()
    lb.section("mine", f"Stage2 挖掘 · 产物 {out_dir}")
    lb.info("mine", "开始", doubt_index=doubt_index, parallel=parallel, out_dir=str(out_dir))

    # ---- 可插拔模块: --only 单模块重跑 / skip-if-exists / --force 覆盖 ----
    settings_system_path = os.path.join(out_dir, "settings_system.json")
    characters_resume_path = os.path.join(out_dir, "characters_resume.json")
    style_analysis_path = os.path.join(out_dir, "style_analysis.json")
    MODULES = {"setting": settings_system_path, "character": characters_resume_path,
               "style": style_analysis_path}
    targets = [only] if only else ["setting", "character", "style"]
    # 全量(无 --only)时清理三产物再跑, 与 stage1 行为一致; --only 时仅清目标
    if only:
        clean = MODULES.get(only)
        clean = [clean] if clean else []
    else:
        clean = list(MODULES.values())
    if force:
        for p in clean:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    open(p, "w", encoding="utf-8").close()
    skipped = {}
    run_targets = []
    for m in targets:
        opath = MODULES[m]
        if not force and os.path.exists(opath) and _valid_json(opath):
            skipped[m] = True
            print(f"  ⏭ {m}: 产物已存在, 跳过 (--force 强制重跑)")
        else:
            run_targets.append(m)

    results = {}
    def _setting_mine():
        t1 = time.time()
        try:
            sys_path = settings_graph_path
            graph = setting_agent.load_graph(sys_path)
            system = setting_agent.strengthen(graph, C.OLLAMA_BASE, model, doubt_index)
            out = os.path.join(out_dir, "settings_system.json")
            _atomic_write(out, system)
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
        clue_ev = {}   # 深度推理(clue_graph)已迁至 studio, pedia 简历不再引用暗线
        resumes = character_agent.build_all_resumes(
            chars, clue_ev, C.OLLAMA_BASE, model, parallel)
        out = os.path.join(out_dir, "characters_resume.json")
        _atomic_write(out, {"schema": 1, "characters": resumes})
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
                for k, p in [                             ("char_facts", char_facts_path),
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
            _atomic_write(out, style)
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

    NAME2MOD = {"设定强化": "setting", "人物简历": "character", "文风分析": "style"}
    tasks = {"设定强化": _setting_mine, "人物简历": _char_mine, "文风分析": _style_mine}
    tasks = {k: v for k, v in tasks.items() if NAME2MOD.get(k) in run_targets}
    if tasks:
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
    else:
        print("  ⏭ 全部模块均已存在且有效, 跳过 (--force 可强制重跑)")
    # 跳过的模块状态并入 results(标注 skip)
    for m in skipped:
        results[m] = {"status": "skip", "secs": 0}

    # ---- 治本收尾: 原生产出旧式 stage2, viz 主路径不再依赖 fallback ----
    try:
        _s2 = build_stage2_compat(out_dir)
        if _s2:
            print(f"  ✅ stage2 兼容产物: {_s2[0]} (人物 {_s2[1]} / 设定 {_s2[2]})")
    except Exception as e:
        print(f"  ⚠️ stage2 兼容产物生成失败: {e}")

    print(f"\n=== Stage2 挖掘完成 (总耗时 {time.time()-t0:.0f}s) ===")
    print(f"  设定强化: {results.get('setting', {}).get('layers', 0)} 层体系 / "
          f"{results.get('setting', {}).get('conflicts', 0)} 处矛盾")
    print(f"  人物简历: {results.get('char', {}).get('resumes', 0)} 份")
    print(f"  文风分析: {'完成' if results.get('style', {}).get('done') else '失败'}")
    print(f"  产物目录: {out_dir}")
    # 模块清单(供重跑/覆盖核查: 每个模块 ok/skip/fail)
    try:
        manifest = {"stage": "stage2", "secs": round(time.time()-t0, 1),
                    "modules": {m: (results.get(m) or {}).get("status", "ok") for m in MODULES}}
        _atomic_write(os.path.join(out_dir, "module_manifest.json"), manifest)
    except Exception:
        pass
    return results


def main():
    p = argparse.ArgumentParser(description="Stage2 三维度并行挖掘(可插拔/可重跑)")
    p.add_argument("--chapters", type=int, default=None)
    p.add_argument("--backend", default=None, help="LLM 后端(glm/qwen3/xiaohongshu)")
    p.add_argument("--doubt-index", dest="doubt_index", type=float, default=None)
    p.add_argument("--parallel", type=int, default=4, help="人物简历并发数(默认4)")
    p.add_argument("--only", default=None,
                   choices=["setting", "character", "style"],
                   help="只重跑单个模块(失败重跑覆盖用)")
    p.add_argument("--force", action="store_true", help="强制覆盖已存在产物")
    a = p.parse_args()
    run(chapters=a.chapters, backend=a.backend, doubt_index=a.doubt_index,
        parallel=a.parallel, only=a.only, force=a.force)


if __name__ == "__main__":
    main()
