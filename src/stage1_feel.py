# -*- coding: utf-8 -*-
"""
stage1_feel.py —— Stage1 主agent读感 + 设定子agent 图谱（CLI 合入版）

流程:
  1. 从 Stage1 数据库(db.scenes, 抽取完成的场景)读取场景序列
  2. 主agent(main_agent)逐场景读, 产出阅读感受(疑点/文风/伏笔/沉思/泪点/笑点)
     + 名词疑点 + 工作记忆; 感受含 chain 思维链字段
  3. 设定子agent(setting_agent)补全名词客观定义 -> settings_map
  4. 设定知识图谱(settings_graph.json): 设定实体 + 内在关联 + bge-m3 向量(stage1 就向量化)
  5. 输出到 outputs/<小说名>_<日期>/stage1/

用法:
  python src/stage1_feel.py [--chapters N] [--backend glm] [--doubt-index 0.7]
"""
import sys
import os
import json
import sqlite3
import argparse
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C
import llm_client
import main_agent
import setting_agent


def load_scenes_from_db(db_path, chapters):
    """从 Stage1 数据库读取抽取完成的场景(与 _run_feel.py 一致的取数)。"""
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


def run(chapters=None, backend=None, doubt_index=None):
    t0 = time.time()
    # ---- 后端配置(与 cli.backend_env 对齐, 从 models.json 预设注册表读) ----
    cfg = llm_client.load_config()
    doubt_index = float(doubt_index) if doubt_index is not None else float(cfg.get("doubt_index", 0.7))
    model = None
    if backend:
        b = llm_client.get_preset(backend) or {}
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

    chapters = chapters or C.CHAPTERS
    scenes = load_scenes_from_db(C.DB_PATH, chapters)
    print(f"场景数: {len(scenes)} | 章节: {chapters} | doubt_index: {doubt_index}")

    # ---- 输出目录: outputs/<书>_<日期>/stage1/ ----
    out_dir = C.STAGE1_DIR
    os.makedirs(out_dir, exist_ok=True)
    feelings_path = os.path.join(out_dir, "reader_feelings.json")
    settings_path = os.path.join(out_dir, "settings_map.json")
    memory_path = os.path.join(out_dir, "reader_memory.json")
    graph_path = os.path.join(out_dir, "settings_graph.json")
    clue_path = os.path.join(out_dir, "clue_graph.json")

    # ---- 读感/记忆/线索图谱为"本次运行全新产物": 删除旧文件, 避免残留污染 ----
    # (settings_graph.json 保留: 设计为增量扩充, 跨次运行累积)
    for p in [feelings_path, settings_path, memory_path, clue_path]:
        if os.path.exists(p):
            os.remove(p)

    # ---- 主agent 读感(带 doubt_index) ----
    print("[1/3] 主agent 读感: 证据收集 -> 图谱整合 -> 批量推理 + 文风采样 ...")
    res = main_agent.run_main_agent(
        scenes, C.OLLAMA_BASE, model,
        feelings_path, settings_path, memory_path,
        clue_graph_path=clue_path, doubt_index=doubt_index,
        style_ratio=0.1, db_path=C.DB_PATH, chapters=chapters)
    print(f"      -> 结论 {res['puzzles']} | 证据 {res['evidence']} | 簇 {res['clusters']} | "
          f"名词 {res['settings_terms']} | 耗时 {time.time()-t0:.1f}s")

    # ---- 设定子agent 图谱(识别/归并/关联/向量化) ----
    t1 = time.time()
    print("[2/3] 设定子agent 知识图谱 ...")
    try:
        # 注意: 向量化永远用 bge-m3, 与 LLM 模型无关
        graph, n_terms, n_rels, n_vec = setting_agent.run_setting_agent(
            scenes, C.OLLAMA_BASE, model, graph_path)
        print(f"      -> 设定实体 {n_terms} | 关联 {n_rels} | 向量化 {n_vec} | 耗时 {time.time()-t1:.1f}s")
    except Exception as e:
        print(f"      [warn] 设定图谱失败: {e}")

    # ---- 思维链统计 ----
    try:
        feel = json.load(open(feelings_path, encoding="utf-8"))
        with_chain = sum(1 for e in feel.get("entries", []) if e.get("chain"))
        print(f"[3/3] 思维链: {with_chain}/{len(feel.get('entries', []))} 条带 chain 字段")
    except Exception:
        pass

    print(f"✅ stage1 读感完成 -> {out_dir} (总耗时 {time.time()-t0:.1f}s)")
    return res


def main():
    p = argparse.ArgumentParser(description="Stage1 主agent读感 + 设定图谱")
    p.add_argument("--chapters", type=int, default=None, help="处理章数(默认 settings.json)")
    p.add_argument("--backend", default=None, help="LLM 后端(glm/qwen3/xiaohongshu)")
    p.add_argument("--doubt-index", dest="doubt_index", type=float, default=None,
                   help="质疑指数 0-1 (控制思考深度/疑点数/track 触发; 默认 llm.config.json 的 doubt_index)")
    a = p.parse_args()
    run(chapters=a.chapters, backend=a.backend, doubt_index=a.doubt_index)


if __name__ == "__main__":
    main()
