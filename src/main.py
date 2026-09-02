# -*- coding: utf-8 -*-
"""
main.py —— Stage1 Txt2db 编排入口
流程: ingest -> segment(rule|vector) -> extract(两轮) -> db -> fts -> 向量索引

用法:
  python src/main.py                                        # 默认 rule 模式,前30章
  python src/main.py --seg-mode vector --db data/stage1_vec.db
  python src/main.py --chapters 3 --db data/probe.db       # 小样验证
"""
import sys
import os
import time
import json
import argparse
import datetime

sys.path.insert(0, os.path.dirname(__file__))
import config as C
from ingest import ingest
from segment import segment
from extract import extract_all, split_attention
from db import (connect, init_db, save_paragraphs, save_chapters, save_preamble,
                save_scenes, set_meta, get_stats, assert_coverage)
from rag import build_fts, embed_and_index, embed_texts, embed_texts_cached


def make_embed_batch_fn(base, model=C.EMBED_MODEL):
    """构造批量向量化函数(带磁盘缓存);探测失败返回 None(降级为纯规则切分)。"""
    try:
        probe = embed_texts(base, model, ["探测可用性"])
        if not probe or not probe[0]:
            return None
    except Exception:
        return None
    return lambda texts: embed_texts_cached(base, model, texts)


def run(chapters=C.SAMPLE_CHAPTERS, base=C.OLLAMA_BASE, model=C.EXTRACT_MODEL,
        seg_mode=C.SEG_MODE, db_path=C.DB_PATH, report_path=None,
        extract_mode="two"):
    t0 = time.time()
    tag = f"[{seg_mode}]"
    print(f"{tag}[1/6] 读取小说并编号段落 (前 {chapters} 章) ...")
    paragraphs, chs = ingest(max_chapters=chapters)

    embed_batch_fn = None
    if seg_mode == "vector":
        embed_batch_fn = make_embed_batch_fn(base)
        if embed_batch_fn is None:
            print(f"{tag}  警告: bge-m3 不可用,自动降级为规则切分")
    ts = time.time()
    print(f"{tag}[2/6] 场景切分 (模式={'向量+规则' if embed_batch_fn else '纯规则'}"
          f"{f', 阈值={C.VECTOR_DROP_THRESHOLD}' if embed_batch_fn else ''}) ...")
    scenes = segment(paragraphs, chs, embed_batch_fn=embed_batch_fn,
                     max_chapters=chapters)
    print(f"{tag}  -> {len(scenes)} 个场景块, 耗时 {time.time()-ts:.1f}s")

    # ---- 读者注意力 · 动态切分(可替代规则切分) ----
    # extract_mode == "attention": 按章让 LLM 动态分场景, 覆盖规则切分
    if extract_mode == "attention":
        ts = time.time()
        print(f"{tag}[2.5/6] 读者注意力动态切分 (模型={model}) ...")
        by_ch = {c: [] for c in range(1, chapters + 1)}
        for p in paragraphs:
            cn = p["chapter_no"]
            if 1 <= cn <= chapters:
                by_ch.setdefault(cn, []).append(p)
        attn_scenes = []
        sid = 0
        for cn in sorted(by_ch):
            cparas = by_ch[cn]
            if not cparas:
                continue
            bounds = split_attention(cparas, base, model)
            if not bounds:
                # 切分失败回退: 该章整章一个场景
                bounds = [(cparas[0]["para_id"], cparas[-1]["para_id"])]
            for b_seq, (s, e) in enumerate(bounds, 1):
                seg = [p for p in cparas if s <= p["para_id"] <= e]
                if not seg:
                    continue
                sid += 1
                attn_scenes.append({
                    "scene_id": sid, "chapter_no": cn, "volume_no": 0,
                    "event_seq": b_seq, "start_para": s, "end_para": e,
                    "paras": seg,
                })
        scenes = attn_scenes
        print(f"{tag}  -> {len(scenes)} 个注意力场景块, 耗时 {time.time()-ts:.1f}s")

    ts = time.time()
    print(f"{tag}[3/6] 两轮抽取 (模型={model}, 并发={C.OLLAMA_NUM_PARALLEL}, "
          f"场景数={len(scenes)}, 模式={extract_mode}) ...")
    # 抽取增量缓存: 按 小说+seg_mode+extract_mode 落盘,被回收/中断可续跑
    # (含小说名防止跨书 scene_id 冲突复用旧抽取结果)
    from config import _NOVEL_SLUG
    cache_path = os.path.join(os.path.dirname(db_path),
                              f"extract_cache_{_NOVEL_SLUG}_{seg_mode}_{extract_mode}_{chapters}.json")
    records, failures = extract_all(scenes, base, model, cache_path=cache_path,
                                    extract_mode=extract_mode)
    print(f"{tag}  -> 成功 {len(records)} / 失败 {len(failures)}, "
          f"耗时 {(time.time()-ts)/60:.1f} 分钟")

    print(f"{tag}[4/6] 建库写入 {os.path.basename(db_path)} ...")
    conn = connect(db_path)
    init_db(conn)
    preamble = [p for p in paragraphs if p["chapter_no"] == 0]
    save_paragraphs(conn, paragraphs)
    save_chapters(conn, chs)
    save_preamble(conn, preamble)
    save_scenes(conn, scenes, records, model)

    # 段落覆盖校验: 前言必须显式标记,任何非前言段落丢失都视为真丢数据
    cov_ok, cov = assert_coverage(paragraphs, scenes, preamble)
    if not cov_ok:
        raise RuntimeError(
            f"段落覆盖校验失败! 丢失非前言段落 para_id={cov['lost_non_preamble']} "
            f"(total={cov['total']} covered={cov['covered']} preamble={cov['preamble']})")

    # ---- 卷积式知识库 (Stage1: 逐场景抽实体信息增量, 增量累积, 信息不丢) ----
    # 注意: 每场景 1 次 LLM(10166 场景 ≈ 15h); kb_off=1 时跳过(全书跑批建议开)
    import config_schema as _CS
    kb_path = C.KB_PATH
    kb_entities = 0
    kb_processed = 0
    if _CS.get("knowledge.kb_off"):
        print(f"{tag}[4b/6] 知识库卷积跳过 (kb_off=1, 省 {len(scenes)} 次 LLM 调用)")
    else:
        try:
            import knowledge
            # 把 records 的抽取结果(who/where/actinfo/notes)挂到场景
            rec_map = {r["scene_id"]: r for r in records}
            kb_scenes = []
            for sc in scenes:
                rec = rec_map.get(sc["scene_id"])
                if not rec:
                    continue
                kb_scenes.append({
                    "scene_id": sc["scene_id"], "chapter_no": sc["chapter_no"],
                    "who": rec.get("who", []), "where": rec.get("where", ""),
                    "actinfo": rec.get("actinfo", []), "notes": rec.get("notes", ""),
                })
            kb_cache_path = os.path.join(os.path.dirname(db_path),
                                         f"kb_cache_{_NOVEL_SLUG}_{extract_mode}_{chapters}.json")
            kb, kb_processed, kb_entities = knowledge.build_kb_from_scenes(
                kb_scenes, base, model, kb_path)
        except Exception as e:
            print(f"{tag}[warn] 知识库卷积失败: {e}")

    print(f"{tag}[5/6] 构建 FTS5 检索 ...")
    n_fts = build_fts(conn)

    n_vec = 0
    import config_schema as _CS
    if _CS.get("embed.off"):
        print(f"{tag}[6/6] 向量索引跳过 (embed.off=1, 省本地CPU, FTS 检索不受影响)")
    else:
        print(f"{tag}[6/6] bge-m3 向量索引 (RAG) ...")
        try:
            n_vec = embed_and_index(conn, base, C.EMBED_MODEL)
        except Exception as e:
            print("  向量化跳过:", e)

    stats = get_stats(conn)
    stats.update({
        "seg_mode": seg_mode,
        "extract_mode": extract_mode,
        "vector_seg_active": embed_batch_fn is not None,
        "vector_threshold": C.VECTOR_DROP_THRESHOLD if embed_batch_fn else None,
        "chapters": len(chs), "scenes": len(scenes),
        "preamble": len(preamble),
        "coverage": cov,
        "extract_ok": len(records), "failures": len(failures),
        "fts_indexed": n_fts, "vectors": n_vec,
        "elapsed_sec": round(time.time() - t0, 1),
        "db_path": db_path,
    })
    set_meta(conn, {
        "book": C.BOOK_PATH,
        "extract_model": model,
        "extract_mode": extract_mode,
        "embed_model": C.EMBED_MODEL,
        "chapters": chapters,
        "preamble": len(preamble),
        "coverage": cov,
        "seg_mode": seg_mode,
        "seg": {"min": C.SEG_MIN_PARA, "max": C.SEG_MAX_PARA,
                "overlap": C.SEG_OVERLAP,
                "vector_threshold": C.VECTOR_DROP_THRESHOLD},
        "vector_seg_active": embed_batch_fn is not None,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
    })
    conn.close()

    rp = report_path or os.path.join(
        C.DATA_DIR,
        os.path.basename(db_path).replace(".db", "_report.json"))
    with open(rp, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    # ---- 归档到 output/pedia_<book>_<date>/stage1/ + 导出 Excel ----
    try:
        os.makedirs(C.STAGE1_DIR, exist_ok=True)
        # db 副本
        import shutil
        if os.path.abspath(C.RUN_DB_COPY) != os.path.abspath(db_path):
            shutil.copy2(db_path, C.RUN_DB_COPY)
        # Excel 导出(纯 stdlib)
        from export_excel import export as _export_excel
        _export_excel(db_path, C.EXCEL_PATH)
        print(f"{tag}已归档: db 副本 + Excel -> {C.OUTPUT_DIR}")
    except Exception as e:
        print(f"{tag}[warn] 归档失败: {e}")
    print(f"{tag}完成:", json.dumps(stats, ensure_ascii=False))
    if failures:
        print(f"{tag}失败样例:", failures[:3])
    return stats, failures


def _cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chapters", type=int, default=C.SAMPLE_CHAPTERS)
    ap.add_argument("--base", default=C.OLLAMA_BASE)
    ap.add_argument("--model", default=C.EXTRACT_MODEL)
    ap.add_argument("--seg-mode", dest="seg_mode", default=C.SEG_MODE,
                    choices=["rule", "vector"])
    ap.add_argument("--extract-mode", dest="extract_mode", default="two",
                    choices=["two", "single", "v2", "attention"],
                    help="two=两轮(5W1H再叙事/文学) | single=单轮合并(~2.2x提速) | v2=薄schema单轮(who/when/where+actinfo+notes) | attention=读者注意力动态切分+抽取")
    ap.add_argument("--db", dest="db_path", default=C.DB_PATH)
    args = ap.parse_args()
    db_path = args.db_path
    if not os.path.isabs(db_path):
        db_path = os.path.join(C.DATA_DIR, db_path)
    return run(chapters=args.chapters, base=args.base, model=args.model,
               seg_mode=args.seg_mode, db_path=db_path,
               extract_mode=args.extract_mode)


if __name__ == "__main__":
    _cli()
