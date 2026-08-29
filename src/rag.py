# -*- coding: utf-8 -*-
"""
rag.py —— 检索层
- FTS5:无需向量模型即可用(基于 summary/keywords/key_sentences/raw_text)
- bge-m3 向量:拉取后自动启用,做语义检索
"""
import sys
import os
import json
import sqlite3
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
import config as C


def _search_text(row):
    # v2 薄 schema: actinfo 内容优先(白盒检索), notes 兜底
    act = row.get("actinfo_json", "")
    if act:
        parts = []
        try:
            for it in json.loads(act):
                parts.append(str(it.get("content", "")))
                if it.get("type") == "event":
                    parts.append(" ".join(str(s) for s in it.get("scope", [])))
        except Exception:
            pass
        parts.append(str(row.get("notes", "")))
        return "\n".join(p for p in parts if p)
    # 旧 schema: summary/keywords/key_sentences/raw_text
    parts = []
    for k in ("summary", "keywords_json", "key_sentences_json", "raw_text"):
        v = row.get(k, "")
        if k.endswith("_json"):
            try:
                v = " ".join(json.loads(v)) if v else ""
            except Exception:
                v = ""
        parts.append(str(v))
    return "\n".join(parts)


def build_fts(conn):
    c = conn.cursor()
    c.execute("DELETE FROM scenes_fts")
    c.execute("SELECT scene_id, summary, keywords_json, key_sentences_json, raw_text, actinfo_json, notes FROM scenes WHERE extract_status='ok'")
    rows = c.fetchall()
    c.executemany(
        "INSERT INTO scenes_fts(scene_id, search_text) VALUES(?,?)",
        [(r[0], _search_text({"summary": r[1], "keywords_json": r[2], "key_sentences_json": r[3],
                              "raw_text": r[4], "actinfo_json": r[5], "notes": r[6]})) for r in rows],
    )
    conn.commit()
    return len(rows)


def fts_query(conn, q, limit=10):
    c = conn.cursor()
    try:
        c.execute(
            "SELECT s.scene_id, s.chapter_no, s.actinfo_json, s.\"where\", s.notes FROM scenes_fts f "
            "JOIN scenes s ON s.scene_id=f.scene_id WHERE scenes_fts MATCH ? ORDER BY rank LIMIT ?",
            (q, limit),
        )
    except Exception:
        # 用户未用引号包裹特殊字符时退化为 LIKE
        c.execute(
            "SELECT scene_id, chapter_no, actinfo_json, \"where\", notes FROM scenes WHERE raw_text LIKE ? LIMIT ?",
            (f"%{q}%", limit),
        )
    return c.fetchall()


def _embed(base, model, text):
    """单条向量化(仅用于查询;批量场景请用 embed_texts)。"""
    payload = {"model": model, "prompt": text}
    req = urllib.request.Request(
        base.rstrip("/") + "/api/embeddings",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8")).get("embedding")


def _cache_conn():
    """段落向量磁盘缓存(独立于产物库,可安全删除)。"""
    os.makedirs(C.DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(os.path.join(C.DATA_DIR, "vec_cache.db"))
    conn.execute("CREATE TABLE IF NOT EXISTS cache("
                 "key TEXT PRIMARY KEY, model TEXT, vec_json TEXT)")
    conn.commit()
    return conn


def embed_texts_cached(base, model, texts, batch_size=C.EMBED_BATCH_SIZE):
    """带磁盘缓存的批量向量化。

    全 30 章段落向量化约 5 分钟,反复调参/对比时不该重复付这个成本。
    以 (model, text) 的 sha1 为键落盘,命中即秒回。
    """
    import hashlib
    conn = _cache_conn()
    c = conn.cursor()
    keys = [hashlib.sha1((model + "\x00" + t).encode("utf-8")).hexdigest()
            for t in texts]

    hit = {}
    CH = 500
    for i in range(0, len(keys), CH):
        part = keys[i:i + CH]
        c.execute("SELECT key, vec_json FROM cache WHERE key IN (%s)"
                  % ",".join("?" * len(part)), part)
        for k, vj in c.fetchall():
            hit[k] = json.loads(vj)

    miss_idx = [i for i, k in enumerate(keys) if k not in hit]
    if miss_idx:
        fresh = embed_texts(base, model, [texts[i] for i in miss_idx], batch_size)
        rows = []
        for j, i in enumerate(miss_idx):
            v = fresh[j] if j < len(fresh) else None
            if v:
                hit[keys[i]] = v
                rows.append((keys[i], model, json.dumps(v)))
        if rows:
            c.executemany("INSERT OR REPLACE INTO cache(key,model,vec_json) "
                          "VALUES(?,?,?)", rows)
            conn.commit()
    conn.close()
    return [hit.get(k) for k in keys]


def embed_texts(base, model, texts, batch_size=C.EMBED_BATCH_SIZE):
    """批量向量化,走 /api/embed。

    实测(bge-m3): 逐条 /api/embeddings 约 2181ms/条;批量 /api/embed 约 51ms/条,
    快 42 倍 —— 批量请求内部并行处理。全书向量化必须走这条路径。
    返回与 texts 等长的向量列表(失败位置为 None)。
    """
    out = []
    for i in range(0, len(texts), batch_size):
        chunk = texts[i:i + batch_size]
        payload = {"model": model, "input": chunk}
        req = urllib.request.Request(
            base.rstrip("/") + "/api/embed",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=C.EMBED_TIMEOUT) as r:
                embs = json.loads(r.read().decode("utf-8")).get("embeddings") or []
        except Exception:
            embs = []
        if len(embs) != len(chunk):
            embs = list(embs) + [None] * (len(chunk) - len(embs))
        out.extend(embs)
    return out


def embed_and_index(conn, base=C.OLLAMA_BASE, model=C.EMBED_MODEL):
    """对已完成抽取的场景做 bge-m3 向量化并入库(批量)。返回成功数。"""
    c = conn.cursor()
    c.execute("SELECT scene_id, summary, keywords_json, key_sentences_json, raw_text, actinfo_json, notes FROM scenes WHERE extract_status='ok'")
    rows = c.fetchall()
    if not rows:
        return 0
    texts = [
        _search_text({"summary": r[1], "keywords_json": r[2],
                      "key_sentences_json": r[3], "raw_text": r[4],
                      "actinfo_json": r[5], "notes": r[6]})
        for r in rows
    ]
    vecs = embed_texts(base, model, texts)
    payload = [
        (rows[i][0], model, len(v), json.dumps(v))
        for i, v in enumerate(vecs) if v
    ]
    c.executemany(
        "INSERT OR REPLACE INTO embeddings(scene_id,model,dim,vec_json) VALUES(?,?,?,?)",
        payload,
    )
    conn.commit()
    return len(payload)


def _cosine(a, b):
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def vector_query(conn, base, model, q, limit=10):
    qvec = _embed(base, model, q)
    if not qvec:
        return []
    c = conn.cursor()
    c.execute("SELECT scene_id, vec_json FROM embeddings")
    rows = c.fetchall()
    scored = []
    for sid, vj in rows:
        vec = json.loads(vj)
        scored.append((_cosine(qvec, vec), sid))
    scored.sort(reverse=True)
    top = [sid for _, sid in scored[:limit]]
    c.execute("SELECT scene_id, chapter_no, actinfo_json, \"where\", notes "
              "FROM scenes WHERE scene_id IN (%s)" % ",".join("?" * len(top)), top)
    return c.fetchall()
