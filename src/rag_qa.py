# -*- coding: utf-8 -*-
"""
rag_qa.py —— v2 RAG 问答
query → bge-m3 向量检索相关场景 → 拼 actinfo 流 → qwen3:4b 回答
用法:
  python src/rag_qa.py --chapters 50 "周明瑞的头痛是怎么回事?"
  python src/rag_qa.py --chapters 50 --top 8 --model qwen3:8b "奥黛丽在灰雾世界的身份是什么?"
"""
import sys
import os
import json
import sqlite3
import argparse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
import config as C
import rag
import llm_client


def actinfo_dump(actinfo, where, notes):
    lines = []
    for it in actinfo:
        if it["type"] == "event":
            lines.append(f"事件: {it['content']} (影响: {','.join(it.get('scope', []))})")
        else:
            lines.append(f"{it['channel']}[{it['who']}]: {it['content']}")
    if where:
        lines.insert(0, f"地点: {where}")
    if notes:
        lines.append(f"备注: {notes}")
    return "；".join(lines)


def answer(model, question, context):
    # 统一走 llm_client: 默认本地 Ollama; 设 LLM_BACKEND=openai + LLM_API_KEY 即切云端
    return llm_client.chat(
        "你是一位小说问答助手。基于给定的场景事件流回答用户问题。"
        "只使用给定材料, 不要编造; 材料不足就如实说明。"
        "直接给出答案, 不要复述用户问题, 不要描述你的思考步骤。",
        f"以下是检索到的相关场景事件流:\n{context}\n\n问题: {question}\n请回答(2-4句, 引用第几章)。",
        model=model, num_ctx=16384, temperature=0.3,
        json_mode=False, num_predict=600,
    )[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question", help="问题")
    ap.add_argument("--chapters", type=int, default=50)
    ap.add_argument("--top", type=int, default=6, help="检索场景数")
    ap.add_argument("--model", default="qwen3:4b")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    db = args.db or os.path.join(C.DATA_DIR, f"stage1_v2_{args.chapters}.db")
    conn = sqlite3.connect(db)

    # 向量检索(无向量库时降级 FTS)
    try:
        rows = rag.vector_query(conn, C.OLLAMA_BASE, C.EMBED_MODEL, args.question, limit=args.top)
        method = "bge-m3 向量"
    except Exception as e:
        print("向量检索失败, 降级 FTS:", e)
        rows = rag.fts_query(conn, args.question, limit=args.top)
        method = "FTS5"
    if not rows:
        print("未检索到相关场景")
        return

    print(f"[{method}] 检索到 {len(rows)} 个相关场景:")
    ctx_parts = []
    for r in rows:
        sid, cn, act_j, where, notes = r
        act = json.loads(act_j or "[]")
        dump = actinfo_dump(act, where, notes)
        ctx_parts.append(f"[第{cn}章·scene{sid}] {dump}")
        print(f"  第{cn}章 scene{sid}: {dump[:70]}...")
    context = "\n".join(ctx_parts)

    print(f"\n[回答 · {args.model}]")
    ans = answer(args.model, args.question, context)
    print(ans)


if __name__ == "__main__":
    main()
