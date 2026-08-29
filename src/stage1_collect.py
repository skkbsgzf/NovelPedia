# -*- coding: utf-8 -*-
"""
stage1_collect.py —— Stage1 四维度并行收集 (收集原料, 不求完美)

四路并行 (ThreadPoolExecutor, 快的不等慢的):
  1. 设定图谱    setting_agent.run_setting_agent()  → settings_graph.json (增量扩充)
  2. 剧情暗线    clue_agent.collect_only()          → clue_graph.json (evidence+簇, 无结论)
  3. 人物聚合    character_agent.collect_from_actinfo() → character_facts.json (纯规则)
  4. 文风采样    style_sampler.sample_style_text()  → style_samples.json (纯规则)

Stage1 结束 = 原料齐: 明线(actinfo在db) + 暗线(clue_graph) + 设定(settings_graph)
                + 人物(character_facts) + 文风采样(style_samples)

用法:
  python src/stage1_collect.py [--chapters N] [--backend glm] [--doubt-index 0.5] [--parallel 4]
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
import llm_client
import clue_agent
import setting_agent
import character_agent
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


def setup_backend(backend, doubt_index):
    """配置 LLM 后端环境变量, 返回 model 名。"""
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


def run(chapters=None, backend=None, doubt_index=None, parallel=4):
    t0 = time.time()
    cfg = llm_client.load_config()
    doubt_index = float(doubt_index) if doubt_index is not None else float(cfg.get("doubt_index", 0.5))
    model = setup_backend(backend, doubt_index)
    chapters = chapters or C.CHAPTERS
    scenes = load_scenes_from_db(C.DB_PATH, chapters)
    print(f"场景数: {len(scenes)} | 章节: {chapters} | doubt_index: {doubt_index} | 并行: {parallel}")

    out_dir = C.STAGE1_DIR
    os.makedirs(out_dir, exist_ok=True)
    settings_graph_path = os.path.join(out_dir, "settings_graph.json")
    clue_path = os.path.join(out_dir, "clue_graph.json")
    char_facts_path = os.path.join(out_dir, "character_facts.json")
    style_samples_path = os.path.join(out_dir, "style_samples.json")
    registry_path = os.path.join(out_dir, "entity_registry.json")

    # 清理本次运行的旧产物(settings_graph 保留增量)
    for p in [clue_path, char_facts_path, style_samples_path, registry_path]:
        if os.path.exists(p):
            os.remove(p)

    # ---- Pass0: 全局实体注册表(每章一次 LLM, 优化 A) ----
    # 需在暗线收集前完成(注入 prompt 统一实体名)
    registry = {}
    if os.path.exists(registry_path):
        try:
            with open(registry_path, encoding="utf-8") as f:
                registry = json.load(f)
        except Exception:
            registry = {}
    if not registry:
        t1 = time.time()
        print("[Pass0] 全局实体注册表(每章抽稳定实体+别名) ...")
        scenes_by_ch = {}
        for sc in scenes:
            scenes_by_ch.setdefault(sc.get("chapter_no"), []).append(sc)
        registry = clue_agent.extract_entities_per_chapter(scenes_by_ch, C.OLLAMA_BASE, model)
        with open(registry_path, "w", encoding="utf-8") as f:
            json.dump(registry, f, ensure_ascii=False, indent=2)
        n_reg = sum(len(v) for v in registry.values())
        print(f"        -> {n_reg} 个实体(跨 {len(registry)} 章) 耗时 {time.time()-t1:.0f}s")

    results = {}
    def _setting():
        t1 = time.time()
        g, n_terms, n_rels, n_vec = setting_agent.run_setting_agent(
            scenes, C.OLLAMA_BASE, model, settings_graph_path, parallel)
        return ("setting", {"terms": n_terms, "rels": n_rels, "vec": n_vec,
                            "secs": time.time() - t1})
    def _clue():
        t1 = time.time()
        g, n_ev, n_cl = clue_agent.collect_only(
            scenes, C.OLLAMA_BASE, model, clue_path, doubt_index, parallel,
            registry=registry)
        return ("clue", {"evidence": n_ev, "clusters": n_cl, "secs": time.time() - t1})
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
        with open(style_samples_path, "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "sampled": corpus["sampled"],
                       "chars": corpus["chars"],
                       "long_chars": corpus["long_chars"],
                       "mid_chars": corpus.get("mid_chars", 0),
                       "short_chars": corpus["short_chars"]},
                      f, ensure_ascii=False, indent=2)
        lb.info("style", "采样完成", chars=corpus["chars"],
                mid_chars=corpus.get("mid_chars", 0),  # >0 即对白主体未被丢弃(F1 修复生效)
                sampled=len(corpus["sampled"]), secs=round(time.time()-t1, 1))
        # A1 场景元数据(文风取经: emotion/rhetoric/where/plot_function)
        try:
            metas = load_scenes_meta_from_db(C.DB_PATH, chapters)
            with open(os.path.join(out_dir, "scenes_meta.json"), "w", encoding="utf-8") as f:
                json.dump({"schema": 1, "n_scenes": len(metas), "scenes": metas},
                          f, ensure_ascii=False, indent=2)
            lb.gauge("style", "scenes_meta", len(metas))
        except Exception as e:
            lb.error("style", "scenes_meta 导出失败", err=str(e)[:200])
        # A2 全量词频
        try:
            wf = style_sampler.compute_word_freq(chapters=chapters, topk=100)
            with open(os.path.join(out_dir, "word_freq.json"), "w", encoding="utf-8") as f:
                json.dump(wf, f, ensure_ascii=False, indent=2)
        except Exception as e:
            lb.error("style", "词频统计失败", err=str(e)[:200])
        # A3 全量句统计
        try:
            ss = style_sampler.compute_sentence_stats(chapters=chapters)
            with open(os.path.join(out_dir, "sentence_stats.json"), "w", encoding="utf-8") as f:
                json.dump(ss, f, ensure_ascii=False, indent=2)
        except Exception as e:
            lb.error("style", "句统计失败", err=str(e)[:200])
        return ("style", {"chars": corpus["chars"], "secs": time.time() - t1})

    tasks = {"设定图谱": _setting, "剧情暗线": _clue,
             "人物聚合": _char, "文风采样": _style}
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

    lb.section("collect", "Stage1 收集完成")
    lb.info("collect", "汇总",
            setting_terms=results.get('setting', {}).get('terms', 0),
            setting_rels=results.get('setting', {}).get('rels', 0),
            evidence=results.get('clue', {}).get('evidence', 0),
            clusters=results.get('clue', {}).get('clusters', 0),
            chars=results.get('char', {}).get('chars', 0),
            style_chars=results.get('style', {}).get('chars', 0),
            total_secs=round(time.time()-t0, 1), out_dir=str(out_dir))
    return results


def main():
    p = argparse.ArgumentParser(description="Stage1 四维度并行收集")
    p.add_argument("--chapters", type=int, default=None)
    p.add_argument("--backend", default=None, help="LLM 后端(glm/qwen3/xiaohongshu)")
    p.add_argument("--doubt-index", dest="doubt_index", type=float, default=None)
    p.add_argument("--parallel", type=int, default=4, help="场景级并发数(默认4)")
    a = p.parse_args()
    run(chapters=a.chapters, backend=a.backend, doubt_index=a.doubt_index,
        parallel=a.parallel)


if __name__ == "__main__":
    main()
