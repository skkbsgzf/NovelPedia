# -*- coding: utf-8 -*-
"""
db.py —— Stage1 产物库(stage1.db)建表与写入
表: meta / paragraphs / chapters / preamble / scenes / beats / scenes_fts / embeddings

preamble = 书籍前言(BOM/书名/作者/简介/卷部标题),chapter_no=0,不进入场景切分,
但必须显式落库,否则无法区分"正确排除"与"真丢数据"。
"""
import sys
import os
import json
import sqlite3
import datetime

sys.path.insert(0, os.path.dirname(__file__))
import config as C


def connect(db_path=C.DB_PATH):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    return sqlite3.connect(db_path)


def init_db(conn):
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE TABLE IF NOT EXISTS paragraphs (
        para_id INTEGER PRIMARY KEY,
        volume_no INTEGER,
        chapter_no INTEGER,
        text TEXT
    );
    CREATE TABLE IF NOT EXISTS chapters (
        chapter_no INTEGER PRIMARY KEY,
        volume_no INTEGER,
        title TEXT,
        start_para INTEGER,
        end_para INTEGER
    );
    CREATE TABLE IF NOT EXISTS preamble (
        para_id INTEGER PRIMARY KEY,
        volume_no INTEGER,
        chapter_no INTEGER,
        text TEXT
    );
    CREATE TABLE IF NOT EXISTS scenes (
        scene_id INTEGER PRIMARY KEY,
        chapter_no INTEGER,
        volume_no INTEGER,
        event_seq INTEGER,
        start_para INTEGER,
        end_para INTEGER,
        raw_text TEXT,
        who_json TEXT,
        what TEXT,
        when_json TEXT,
        -- 注意: where/when 是 SQLite 保留字,列名必须加双引号,否则建表直接 syntax error
        "where" TEXT,
        why TEXT,
        how TEXT,
        pov TEXT,
        emotion_json TEXT,
        plot_function TEXT,
        rhetoric_json TEXT,
        key_sentences_json TEXT,
        summary TEXT,
        keywords_json TEXT,
        actinfo_json TEXT,
        notes TEXT,
        extract_model TEXT,
        extract_status TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS beats (
        beat_id INTEGER PRIMARY KEY AUTOINCREMENT,
        scene_id INTEGER,
        seq INTEGER,
        start_para INTEGER,
        end_para INTEGER,
        anchor TEXT,
        content TEXT
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS scenes_fts USING fts5(
        scene_id UNINDEXED,
        search_text
    );
    CREATE TABLE IF NOT EXISTS embeddings (
        scene_id INTEGER PRIMARY KEY,
        model TEXT,
        dim INTEGER,
        vec_json TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_scenes_chapter ON scenes(chapter_no);
    CREATE INDEX IF NOT EXISTS idx_beats_scene ON beats(scene_id);
    """)
    conn.commit()
    _assert_schema(conn)


# 建表自检:executescript 遇错会静默中断后续语句,必须显式验证表是否真的建成。
# 历史踩坑:where 作裸列名触发 syntax error,导致 scenes 表未建且无任何日志。
_EXPECT_TABLES = ("meta", "paragraphs", "chapters", "preamble", "scenes", "beats",
                  "scenes_fts", "embeddings")
_EXPECT_SCENE_COLS = ("scene_id", "chapter_no", "start_para", "end_para",
                      "who_json", "what", "when_json", "where", "why", "how",
                      "pov", "summary", "keywords_json", "extract_status")


def _assert_schema(conn):
    c = conn.cursor()
    c.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    have = {r[0] for r in c.fetchall()}
    missing = [t for t in _EXPECT_TABLES if t not in have]
    if missing:
        raise RuntimeError(f"建表失败,缺失表: {missing}(已建: {sorted(have)})")
    c.execute("PRAGMA table_info(scenes)")
    cols = {r[1] for r in c.fetchall()}
    lack = [x for x in _EXPECT_SCENE_COLS if x not in cols]
    if lack:
        raise RuntimeError(f"scenes 表缺列: {lack}")


def save_paragraphs(conn, paragraphs):
    c = conn.cursor()
    c.executemany(
        "INSERT OR REPLACE INTO paragraphs(para_id,volume_no,chapter_no,text) VALUES(?,?,?,?)",
        [(p["para_id"], p["volume_no"], p["chapter_no"], p["text"]) for p in paragraphs],
    )
    conn.commit()


def save_chapters(conn, chapters):
    c = conn.cursor()
    c.executemany(
        "INSERT OR REPLACE INTO chapters(chapter_no,volume_no,title,start_para,end_para) VALUES(?,?,?,?,?)",
        [(c_["chapter_no"], c_["volume_no"], c_["title"], c_["start_para"], c_["end_para"]) for c_ in chapters],
    )
    conn.commit()


def save_preamble(conn, preamble):
    """显式落库书籍前言(chapter_no=0);这些是正确排除出场景切分的段落,
    必须留存以便覆盖校验区分'正确排除'与'真丢数据'。"""
    c = conn.cursor()
    c.executemany(
        "INSERT OR REPLACE INTO preamble(para_id,volume_no,chapter_no,text) VALUES(?,?,?,?)",
        [(p["para_id"], p["volume_no"], p["chapter_no"], p["text"]) for p in preamble],
    )
    conn.commit()


def assert_coverage(all_paragraphs, scenes, preamble):
    """段落覆盖校验: 每个段落要么落在某个 scene 内,要么属于 preamble(正确排除)。

    历史踩坑: 修碎片合并后段落覆盖从 2732 掉到 2725,静默丢了 7 段 chapter_no=0 前言;
    因为排除本身正确却没显式标记,无法区分'正确排除'与'真丢数据'。
    返回值: (ok, dict) —— ok=False 时 detail 含丢失的非前言段落清单。
    """
    covered = set()
    for sc in scenes:
        for p in sc["paras"]:
            covered.add(p["para_id"])
    preamble_ids = {p["para_id"] for p in preamble}
    all_ids = {p["para_id"] for p in all_paragraphs}
    uncovered = all_ids - covered
    # 未被覆盖且不属于前言 = 真丢数据
    lost = uncovered - preamble_ids
    ok = (len(lost) == 0)
    detail = {
        "total": len(all_ids),
        "covered": len(covered),
        "preamble": len(preamble_ids),
        "uncovered": len(uncovered),
        "lost_non_preamble": sorted(lost),
    }
    return ok, detail


def _raw_text(scene):
    return "\n".join(p["text"] for p in scene["paras"])


def save_scenes(conn, scenes, records, model=C.EXTRACT_MODEL):
    """scenes=分段结果(含 paras), records=抽取结果(按 scene_id 对齐)。"""
    c = conn.cursor()
    rec_map = {r["scene_id"]: r for r in records}
    for sc in scenes:
        rec = rec_map.get(sc["scene_id"])
        if rec:
            c.execute(
                """INSERT OR REPLACE INTO scenes(
                    scene_id,chapter_no,volume_no,event_seq,start_para,end_para,raw_text,
                    who_json,what,when_json,"where",why,how,pov,emotion_json,plot_function,
                    rhetoric_json,key_sentences_json,summary,keywords_json,
                    actinfo_json,notes,extract_model,extract_status,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    sc["scene_id"], sc["chapter_no"], sc["volume_no"], sc["event_seq"],
                    sc["start_para"], sc["end_para"], _raw_text(sc),
                    json.dumps(rec.get("who", []), ensure_ascii=False),
                    rec.get("what", ""),
                    json.dumps(rec.get("when", {}), ensure_ascii=False),
                    rec.get("where", ""),
                    rec.get("why", ""),
                    rec.get("how", ""),
                    rec.get("pov", ""),
                    json.dumps(rec.get("emotion", {}), ensure_ascii=False),
                    rec.get("plot_function", ""),
                    json.dumps(rec.get("rhetoric", []), ensure_ascii=False),
                    json.dumps(rec.get("key_sentences", []), ensure_ascii=False),
                    rec.get("summary", ""),
                    json.dumps(rec.get("keywords", []), ensure_ascii=False),
                    json.dumps(rec.get("actinfo", []), ensure_ascii=False),
                    rec.get("notes", ""),
                    model, "ok", datetime.datetime.now().isoformat(timespec="seconds"),
                ),
            )
            # beats(仅旧 schema 记录有; v2 无此字段,自然跳过)
            for b in rec.get("beats", []):
                c.execute(
                    "INSERT INTO beats(scene_id,seq,start_para,end_para,anchor,content) VALUES(?,?,?,?,?,?)",
                    (sc["scene_id"], b["seq"], b["start_para"], b["end_para"], b.get("anchor", ""), b.get("content", "")),
                )
        else:
            # 分段完成但抽取失败/未跑:占位
            c.execute(
                """INSERT OR REPLACE INTO scenes(
                    scene_id,chapter_no,volume_no,event_seq,start_para,end_para,raw_text,
                    who_json,what,when_json,"where",why,how,extract_model,extract_status,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sc["scene_id"], sc["chapter_no"], sc["volume_no"], sc["event_seq"],
                 sc["start_para"], sc["end_para"], _raw_text(sc),
                 "[]", "", "{}", "", "", "", model, "pending", datetime.datetime.now().isoformat(timespec="seconds")),
            )
    conn.commit()


def set_meta(conn, kv: dict):
    c = conn.cursor()
    c.executemany("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                  [(k, json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v) for k, v in kv.items()])
    conn.commit()


def get_stats(conn):
    c = conn.cursor()
    stats = {}
    for t in ("paragraphs", "chapters", "scenes", "beats"):
        c.execute(f"SELECT COUNT(*) FROM {t}")
        stats[t] = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM scenes WHERE extract_status='ok'")
    stats["scenes_extracted"] = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM beats")
    stats["beats_total"] = c.fetchone()[0]
    return stats
