# -*- coding: utf-8 -*-
"""
stage1_collect.py —— Stage1 三维度并行收集 (收集原料, 不求完美)

三路并行 (ThreadPoolExecutor, 快的不等慢的):
  1. 设定图谱    setting_agent.run_setting_agent()  → settings_graph.json (增量扩充)
  2. 人物聚合    character_agent.collect_from_actinfo() → character_facts.json (纯规则)
  3. 文风采样    style_sampler.sample_style_text()  → style_samples.json (纯规则)

Stage1 结束 = 原料齐: 明线(actinfo在db) + 设定(settings_graph)
                + 人物(character_facts) + 文风采样(style_samples)
  （注: 实体注册表由 Pass0 单独产出 -> entity_registry.json, 供别名归一化, 不计入三路并行）
  （剧情暗线 / 剧情推理 / 拉片标注等深度推理已从 pedia 剥离, 迁至 studio）

用法:
  python src/stage1_collect.py [--chapters N] [--backend glm] [--doubt-index 0.5] [--parallel 4]
  python src/stage1_collect.py --only setting --force   # 仅重跑设定图谱并覆盖
  python src/stage1_collect.py --only character          # 仅重跑人物聚合(已存在则跳过)
"""
import sys
import os
import json
import sqlite3
import argparse
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C
import config_schema
import llm_client
import entity_registry
import setting_agent
import character_agent
from logbook import get_logbook as _get_logbook
import style_sampler


def load_scenes_from_db(db_path, chapters):
    """从 Stage1 数据库读取抽取完成的场景。"""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    rows = cur.execute(
        """SELECT scene_id, chapter_no, who_json, "where", actinfo_json, notes
           FROM scenes WHERE extract_status='ok' AND chapter_no<=?
           ORDER BY chapter_no, scene_id""", (chapters,)).fetchall()
    conn.close()
    scenes = []
    for sid, cn, wj, wh, aj, nt in rows:
        scenes.append({
            "scene_id": sid, "chapter_no": cn,
            "who": json.loads(wj or '[]'), "where": wh,
            "actinfo": json.loads(aj or '[]'), "notes": nt,
        })
    return scenes


def load_scenes_meta_from_db(db_path, chapters):
    """从 Stage1 数据库读取场景元数据（文风四维取经用）。注意: 当前 v2 抽取(schema 薄)
    不输出 emotion/rhetoric/plot_function, 这些字段为空是正常现象——情绪/修辞由
    stage2 analyze_style_v2 对 raw_text 做规则识别 + 一次批量 LLM 标注补全, 不拖慢 v2 抽取。"""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    rows = cur.execute(
        """SELECT scene_id, chapter_no, who_json, "where", raw_text
           FROM scenes WHERE extract_status='ok' AND chapter_no<=?
           ORDER BY chapter_no, scene_id""", (chapters,)).fetchall()
    conn.close()
    metas = []
    for sid, cn, wj, wh, rt in rows:
        raw = (rt or "")[:400]  # 截断, 控制 scenes_meta.json 体积
        metas.append({
            "scene_id": sid, "chapter_no": cn,
            "who": json.loads(wj or '[]') if wj else [],
            "where": wh or "",
            "raw_text": raw,
        })
    return metas


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

    修复(2026-09-01): 原来用 llm_client.load_config() 读顶层配置, 拿不到 models.json 的
    presets(load_config 只回退到顶层 doubt_index), 导致 b={}, LLM_MODEL 被默认成 qwen3:8b;
    再经 chat() 的 _resolve_model 劫持成 env 优先 -> 云端 URL + 本地模型名, HTTP 400 白烧。
    改用 get_preset(backend) 直接解析 models.json 预设, 确保 LLM_MODEL=预设模型名。
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


def run(chapters=None, backend=None, doubt_index=None, parallel=4, only=None, force=False):
    t0 = time.time()
    lb = _get_logbook()  # 统一日志器(logs/run_<ts>.log + .jsonl); 失败降级不阻塞
    lb.section("collect", "Stage1 四维收集")
    cfg = llm_client.load_config()
    doubt_index = float(doubt_index) if doubt_index is not None else float(cfg.get("doubt_index", 0.5))
    model = setup_backend(backend, doubt_index)
    chapters = chapters or C.CHAPTERS
    scenes = load_scenes_from_db(C.DB_PATH, chapters)
    print(f"场景数: {len(scenes)} | 章节: {chapters} | doubt_index: {doubt_index} | 并行: {parallel}")

    out_dir = C.STAGE1_DIR
    os.makedirs(out_dir, exist_ok=True)
    settings_graph_path = os.path.join(out_dir, "settings_graph.json")
    char_facts_path = os.path.join(out_dir, "character_facts.json")
    style_samples_path = os.path.join(out_dir, "style_samples.json")
    registry_path = os.path.join(out_dir, "entity_registry.json")

    # ---- 可插拔模块: --only 单模块重跑 / skip-if-exists / --force 覆盖 ----
    MODULES = {"registry": registry_path, "setting": settings_graph_path,
               "character": char_facts_path, "style": style_samples_path}
    targets = [only] if only else ["registry", "setting", "character", "style"]
    skipped = {}
    # 清理: 仅 --only 时清理目标文件(force); 全量时按原 clean 逻辑
    if only:
        clean = []
        if force:
            tgt = MODULES.get(only)
            if tgt:
                clean = [tgt]
    else:
        clean = [char_facts_path, style_samples_path, registry_path]
        if config_schema.get("collect.fresh"):
            clean.append(settings_graph_path)
    for p in clean:
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                open(p, "w", encoding="utf-8").close()

    # 跳过已存在且有效的产物(除非 --force)
    run_targets = []
    for m in targets:
        opath = MODULES[m]
        if not force and os.path.exists(opath) and _valid_json(opath):
            skipped[m] = True
            print(f"  ⏭ {m}: 产物已存在, 跳过 (--force 强制重跑)")
        else:
            run_targets.append(m)

    # ---- Pass0: 全局实体注册表(每章一次 LLM) ----
    # 仅在 targets 含 registry 时执行; 其他模块不再依赖它(暗线已剥离)
    if "registry" in run_targets:
        t1 = time.time()
        print("[Pass0] 全局实体注册表(每章抽稳定实体+别名) ...")
        scenes_by_ch = {}
        for sc in scenes:
            scenes_by_ch.setdefault(sc.get("chapter_no"), []).append(sc)
        registry = entity_registry.extract_entities_per_chapter(scenes_by_ch, C.OLLAMA_BASE, model)
        _atomic_write(registry_path, registry)
        n_reg = sum(len(v) for v in registry.values())
        print(f"        -> {n_reg} 个实体(跨 {len(registry)} 章) 耗时 {time.time()-t1:.0f}s")
        run_targets.remove("registry")

    results = {}
    NAME2MOD = {"设定图谱": "setting", "人物聚合": "character", "文风采样": "style"}
    def _setting():
        t1 = time.time()
        g, n_terms, n_rels, n_vec = setting_agent.run_setting_agent(
            scenes, C.OLLAMA_BASE, model, settings_graph_path, parallel)
        return ("setting", {"terms": n_terms, "rels": n_rels, "vec": n_vec,
                            "secs": time.time() - t1})
    def _char():
        t1 = time.time()
        chars = character_agent.collect_from_actinfo(scenes)
        character_agent.save_facts(char_facts_path, chars)
        return ("char", {"chars": len(chars), "secs": time.time() - t1})
    def _style():
        t1 = time.time()
        lb = _get_logbook()
        lb.section("style", "Stage1 文风采集（采样 + 词频 + 句统计 + 场景元数据）")
        corpus = style_sampler.sample_style_text(chapters=chapters, ratio=0.1, seed=None)
        _atomic_write(style_samples_path, {"schema": 1, "sampled": corpus["sampled"],
                       "chars": corpus["chars"], "long_chars": corpus["long_chars"],
                       "mid_chars": corpus.get("mid_chars", 0),
                       "short_chars": corpus["short_chars"]})
        lb.info("style", "采样完成", chars=corpus["chars"],
                mid_chars=corpus.get("mid_chars", 0),  # >0 即对白主体未被丢弃(F1 修复生效)
                sampled=len(corpus["sampled"]), secs=round(time.time()-t1, 1))
        # A1 场景元数据(文风取经: emotion/rhetoric/where/plot_function)
        try:
            metas = load_scenes_meta_from_db(C.DB_PATH, chapters)
            _atomic_write(os.path.join(out_dir, "scenes_meta.json"),
                          {"schema": 1, "n_scenes": len(metas), "scenes": metas})
            lb.gauge("style", "scenes_meta", len(metas))
        except Exception as e:
            lb.error("style", "scenes_meta 导出失败", err=str(e)[:200])
        # A2 全量词频
        try:
            wf = style_sampler.compute_word_freq(chapters=chapters, topk=100)
            _atomic_write(os.path.join(out_dir, "word_freq.json"), wf)
        except Exception as e:
            lb.error("style", "词频统计失败", err=str(e)[:200])
        # A3 全量句统计
        try:
            ss = style_sampler.compute_sentence_stats(chapters=chapters)
            _atomic_write(os.path.join(out_dir, "sentence_stats.json"), ss)
        except Exception as e:
            lb.error("style", "句统计失败", err=str(e)[:200])
        return ("style", {"chars": corpus["chars"], "secs": time.time() - t1})

    tasks = {"设定图谱": _setting, "人物聚合": _char, "文风采样": _style}
    tasks = {k: v for k, v in tasks.items() if NAME2MOD.get(k) in run_targets}
    if tasks:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(fn): name for name, fn in tasks.items()}
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    key, info = fut.result()
                    results[key] = info
                    lb.gauge("collect", name, info, secs=round(info.get("secs", 0), 1))
                    lb.info("collect", f"✅ {name}", secs=round(info.get("secs", 0), 1))
                except Exception as e:
                    lb.error("collect", f"❌ {name}", err=str(e)[:200])
    # 跳过的模块状态并入 results(标注 skip)
    for m in skipped:
        results[m] = {"status": "skip", "secs": 0}

    lb.section("collect", "Stage1 收集完成")
    lb.info("collect", "汇总",
            setting_terms=results.get('setting', {}).get('terms', 0),
            setting_rels=results.get('setting', {}).get('rels', 0),
            chars=results.get('char', {}).get('chars', 0),
            style_chars=results.get('style', {}).get('chars', 0),
            skipped=list(skipped.keys()),
            total_secs=round(time.time()-t0, 1), out_dir=str(out_dir))
    # 模块清单(供重跑/覆盖核查: 每个模块 ok/skip/fail)
    try:
        manifest = {"stage": "stage1", "secs": round(time.time()-t0, 1),
                    "modules": {m: (results.get(m) or {}).get("status", "ok") for m in MODULES}}
        _atomic_write(os.path.join(out_dir, "module_manifest.json"), manifest)
    except Exception:
        pass
    return results


def main():
    p = argparse.ArgumentParser(description="Stage1 三维度并行收集(可插拔/可重跑)")
    p.add_argument("--chapters", type=int, default=None)
    p.add_argument("--backend", default=None, help="LLM 后端(glm/qwen3/xiaohongshu)")
    p.add_argument("--doubt-index", dest="doubt_index", type=float, default=None)
    p.add_argument("--parallel", type=int, default=4, help="场景级并发数(默认4)")
    p.add_argument("--only", default=None,
                   choices=["registry", "setting", "character", "style"],
                   help="只重跑单个模块(失败重跑覆盖用)")
    p.add_argument("--force", action="store_true", help="强制覆盖已存在产物")
    a = p.parse_args()
    run(chapters=a.chapters, backend=a.backend, doubt_index=a.doubt_index,
        parallel=a.parallel, only=a.only, force=a.force)


if __name__ == "__main__":
    main()
