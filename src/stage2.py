# -*- coding: utf-8 -*-
"""
stage2.py —— 直出范式: 基于 stage1 的 actinfo 流, 大模型直接产出
  - 章纲(gen_chapter_outline)
  - 人物档案(gen_characters)
  - 设定提取(gen_settings)
  - 全书总结(gen_summary)
模型: qwen3:8b(直出是总结/链指任务, 质量优先; 4b 抽取层已够薄, 这里不省)
输入: stage1_v2_{chapters}.db
用法:
  python src/stage2.py --chapters 10 --outline 1        # 第1章章纲
  python src/stage2.py --chapters 10 --characters        # 全书人物档案
"""
import sys
import os
import json
import argparse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
import config as C
import llm_client

DB = None
# 上下文参数: 跟随 config(本地 Ollama 4096); 云端大模型可经环境变量/配置覆盖
MODEL = C.EXTRACT_MODEL
NUM_CTX = C.NUM_CTX
NUM_PREDICT = C.NUM_PREDICT


def _chat(system, user):
    # 统一走 llm_client: 默认本地 Ollama; 设 LLM_BACKEND=openai + LLM_API_KEY 即切云端
    return llm_client.chat(
        system, user, model=MODEL, num_ctx=NUM_CTX, temperature=0.3,
        json_mode=True, num_predict=NUM_PREDICT,
    )


def _parse_json(text):
    """容错 JSON 解析: 支持整段、对象、数组及 markdown 围栏包裹。
    对象优先(多数输出为 dict), 数组兜底(纯数组场景如设定列表)。"""
    if text is None:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
    if t.endswith("```"):
        t = t[: t.rfind("```")]
    t = t.strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    # 先对象(多数输出为 dict)
    io, ic = t.find("{"), t.rfind("}")
    if io != -1 and ic > io:
        try:
            return json.loads(t[io:ic + 1])
        except Exception:
            pass
    # 再数组(纯数组场景)
    ia, ib = t.find("["), t.rfind("]")
    if ia != -1 and ib > ia:
        try:
            return json.loads(t[ia:ib + 1])
        except Exception:
            pass
    return None


def load_scenes():
    conn = __import__("sqlite3").connect(DB)
    cur = conn.cursor()
    cur.execute('SELECT chapter_no, scene_id, actinfo_json, who_json, notes '
                'FROM scenes WHERE extract_status="ok" ORDER BY chapter_no, scene_id')
    by_ch = {}
    for cn, sid, act_j, who_j, notes in cur.fetchall():
        by_ch.setdefault(cn, []).append({
            "scene_id": sid,
            "actinfo": json.loads(act_j or "[]"),
            "who": json.loads(who_j or "[]"),
            "notes": notes or "",
        })
    conn.close()
    return by_ch


def actinfo_stream(scenes, chapter_no=None):
    lines = []
    for s in scenes:
        tag = f"[第{chapter_no}章·scene{s['scene_id']}]" if chapter_no else f"[scene{s['scene_id']}]"
        lines.append(tag)
        for it in s["actinfo"]:
            if it["type"] == "event":
                lines.append(f"  事件: {it['content']} (影响: {','.join(it.get('scope', []))})")
            else:
                lines.append(f"  {it['channel']}[{it['who']}]: {it['content']}")
        if s["notes"]:
            lines.append(f"  备注: {s['notes']}")
    return "\n".join(lines)


def gen_chapter_outline(chapter_no, by_ch):
    scenes = by_ch.get(chapter_no, [])
    stream = actinfo_stream(scenes, chapter_no=chapter_no)
    system = ("你是一位小说章节分析助手。输入是《%s》某一章的场景事件流"
              "(已从原文结构化抽取的 actinfo)。请输出该章章纲。" % C.NOVEL_NAME)
    user = f"""以下是第{chapter_no}章的场景事件流:
{stream}

请输出本章章纲, 严格 JSON:
{{
  "chapter": {chapter_no},
  "主线": "本章核心情节一句话",
  "情节点": ["按时间顺序的 3-6 个节点, 每节点一句话"],
  "出场人物": ["名字"],
  "场景": ["地点"],
  "伏笔备注": "本章埋下的伏笔/作者思路/情绪走向, 1-2 句"
}}
只输出 JSON。"""
    raw, pe, ev = _chat(system, user)
    return _parse_json(raw), raw, pe, ev


def gen_characters(by_ch, chapter_limit=None):
    chapters_max = max(sorted(by_ch.keys())) if by_ch else 0
    # 带章号的流: 每章一组, 明确标注第X章
    stream_parts = []
    all_scenes = []
    for cn in sorted(by_ch):
        if chapter_limit and cn > chapter_limit:
            break
        scenes = by_ch[cn]
        stream_parts.append(f"===== 第{cn}章 =====")
        stream_parts.append(actinfo_stream(scenes, chapter_no=cn))
        all_scenes.extend(scenes)
    stream = "\n".join(stream_parts)

    # 候选人物: who 字段出现次数排序, 取前 N(超过 1 次的)
    from collections import Counter
    cnt = Counter()
    for s in all_scenes:
        for w in s["who"]:
            cnt[w["name"]] += 1
    cands = [n for n, c in cnt.most_common() if c >= 2]
    cands_str = "、".join(cands)

    system = ("你是一位小说人物分析师。输入是某小说前若干章的场景事件流"
              "(结构化 actinfo)。请识别主要人物并归并同一人物(如 周明瑞=克莱恩·莫雷蒂),"
              "输出人物档案数组。")
    user = f"""以下是全部场景事件流:
{stream}

以下人物出现在场景的 who 字段中(按出现次数排序), 请你为**每一个**人物输出档案:
{ cands_str }

请输出, 严格 JSON:
{{
  "characters": [
    {{
      "name": "规范名",
      "aliases": ["同一个人物的其他名字/称呼"],
      "身份": "一句话身份描述",
      "首次出现章": 1,
      "关键事件": ["第X章: 一句话事件"],
      "关系": ["与谁什么关系"],
      "弧光": "人物发展轨迹, 1-2 句"
    }}
  ]
}}

**硬性要求(违反即错误)**:
- characters 数组必须覆盖上面列出的每一个候选人物(别名可合并, 但不能漏人);
- 只能基于上面输入的事件流, **严禁使用你对这本小说的其他记忆/知识**;
- 输入只有第 1-{chapters_max} 章, **关键事件里出现任何超出第 {chapters_max} 章的内容都是编造, 必须丢弃**;
- 同一人物的不同名字必须归并成一条档案。
只输出 JSON。"""
    raw, pe, ev = _chat(system, user)
    obj = _parse_json(raw)
    if isinstance(obj, dict) and "characters" in obj:
        obj = obj["characters"]
    obj = _sanitize_characters(obj, chapters_max)
    return obj, raw, pe, ev


def _sanitize_characters(chars, chapters_max):
    """程序侧硬校验: 丢弃关键事件中超出章节范围的内容(防模型用训练记忆编造章号)。"""
    if not isinstance(chars, list):
        return []
    import re
    out = []
    for c in chars:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name", "") or "").strip()
        if not name:
            continue
        # 首次出现章 clamp 到范围内
        fs = c.get("首次出现章", "")
        if isinstance(fs, str):
            m = re.search(r"\d+", fs)
            fs = int(m.group()) if m else 0
        if not isinstance(fs, int):
            fs = 0
        if fs > chapters_max:
            fs = 0
        # 关键事件: 只留章号 <= 上限的
        kept = []
        for ev_ in c.get("关键事件", []) or []:
            s = str(ev_)
            m = re.match(r"第?(\d+)章", s)
            if m and int(m.group(1)) > chapters_max:
                continue  # 超范围 = 编造, 丢弃
            kept.append(s)
        c = dict(c)
        c["关键事件"] = kept
        if fs:
            c["首次出现章"] = fs
        out.append(c)
    return out


def gen_outlines_all(by_ch, model=None):
    """批量生成全部章纲(单章输入天然分块)。返回 {chapter_no: outline}。"""
    out = {}
    for cn in sorted(by_ch):
        obj, raw, pe, ev = gen_chapter_outline(cn, by_ch)
        if not obj:  # 失败重试一次
            obj, raw, pe, ev = gen_chapter_outline(cn, by_ch)
        if obj:
            out[cn] = obj
            print(f"  第{cn}章 OK (输入{pe}tok 输出{ev}tok)")
        else:
            print(f"  第{cn}章 [失败] {raw[:120]}")
    return out


def gen_characters_chunked(by_ch, top_n=30, chapter_limit=None):
    """按人物分条生成档案。

    分两阶段解决"主角全书场景超上下文"问题:
      阶段1(分块摘要): 该人物场景按章切块(每块~8章, 保底<=3000字),
                       每块让 LLM 输出该人物在这几章的线索(身份/事件/关系/弧光);
      阶段2(合并终稿): 汇总各块摘要, 让 LLM 合并成完整档案。
    这样主角 50 章全覆盖, 不再因 num_ctx 截断只看到末尾章节。
    """
    from collections import Counter
    all_scenes = []
    for cn in sorted(by_ch):
        if chapter_limit and cn > chapter_limit:
            break
        all_scenes.extend(by_ch[cn])
    chapters_max = max(sorted(by_ch.keys())) if by_ch else 0

    cnt = Counter()
    for s in all_scenes:
        for w in s["who"]:
            cnt[w["name"]] += 1
    cands = [n for n, c in cnt.most_common(top_n) if c >= 3]
    print(f"候选人物 {len(cands)} 个: {cands}")

    # 每人相关场景(该名字出现在 who 或 actinfo.who)
    chars = []
    for name in cands:
        rel = []
        for s in all_scenes:
            in_who = any(w["name"] == name for w in s["who"])
            in_act = any(it.get("who") == name for it in s["actinfo"])
            if in_who or in_act:
                rel.append(s)
        if not rel:
            continue
        # 按章分组
        by_cn = {}
        for s in rel:
            cn = next((c for c, ss in by_ch.items() if any(x["scene_id"] == s["scene_id"] for x in ss)), 0)
            by_cn.setdefault(cn, []).append(s)

        other_names = [n for n in cands if n != name][:20]
        other_str = "、".join(other_names)

        # ---- 阶段1: 分块摘要 ----
        chs_sorted = sorted(by_cn)
        # 每块最多 8 章, 且内容长度尽量 <= 3000 字符
        blocks = []
        cur = []
        cur_len = 0
        for cn in chs_sorted:
            blk_text = ""
            for s in by_cn[cn]:
                for it in s["actinfo"]:
                    blk_text += f"  {it.get('channel','')}[{it.get('who','')}]: {it.get('content','')}\n"
                if s.get("notes"):
                    blk_text += f"  备注: {s['notes']}\n"
            cur.append((cn, blk_text))
            cur_len += len(blk_text)
            if len(cur) >= 8 or cur_len >= 3000:
                blocks.append(cur)
                cur, cur_len = [], 0
        if cur:
            blocks.append(cur)

        # 每块摘要
        summaries = []
        for bi, blk in enumerate(blocks, 1):
            head = "\n".join(f"===== 第{cn}章 =====" + txt for cn, txt in blk)
            user_s = f"""以下是人物「{name}」在第 {blk[0][0]}-{blk[-1][0]} 章的相关场景事件流:
{head}

请只输出该人物在这几章的关键信息, 严格 JSON:
{{
  "身份线索": "身份/背景的线索, 无则空串",
  "首次出现线索": "是否首次出现? 若是, 给出第几章; 否则填 null",
  "关键事件": ["第X章: 一句话事件", "最多4条"],
  "关系线索": ["与谁什么关系, 无则[]"],
  "弧光线索": "人物发展/情绪变化线索, 1 句"
}}
只输出 JSON。"""
            raw, pe, ev = _chat("你是小说人物分析师, 输入是某人物分章场景流。", user_s)
            obj = _parse_json(raw)
            if isinstance(obj, dict):
                summaries.append(obj)
                print(f"    {name} 块{bi}/{len(blocks)} (第{blk[0][0]}-{blk[-1][0]}章) OK 输入{pe}tok")
            else:
                print(f"    {name} 块{bi} [解析失败] {raw[:60]}")

        # ---- 阶段2: 合并终稿 ----
        if not summaries:
            print(f"  {name}: [失败] 所有分块解析失败")
            continue
        digest = json.dumps(summaries, ensure_ascii=False, indent=1)
        user_f = f"""以下是人物「{name}」按章节分块的关键信息摘要(JSON 数组, 每块覆盖若干章):
{digest}

全书其他主要人物名单(供判断别名): {other_str}

请合并为完整人物档案, 严格 JSON:
{{
  "name": "{name}",
  "aliases": ["同一个人物的其他名字/称呼, 如无则[]"],
  "身份": "一句话身份描述",
  "首次出现章": 1,
  "关键事件": ["第X章: 一句话事件", "最多8条, 覆盖不同章节"],
  "关系": ["与谁什么关系"],
  "弧光": "人物发展轨迹, 1-2 句"
}}
**硬性要求**: 只能基于输入摘要, 严禁使用你对这本小说的记忆; 首次出现章取摘要中最早的章号。
只输出 JSON。"""
        raw, pe, ev = _chat("你是一位小说人物分析师, 负责合并分章摘要为完整档案。", user_f)
        obj = _parse_json(raw)
        if isinstance(obj, dict):
            obj = {"name": obj.get("name", name), **obj}
            obj = _sanitize_characters([obj], chapters_max)
            if obj:
                chars.append(obj[0])
            print(f"  {name}: OK (合并 {len(summaries)} 块, 输入{pe}tok 输出{ev}tok)")
        else:
            print(f"  {name}: [合并失败] {raw[:120]}")
    return chars


def gen_settings(by_ch, batch=5, chapter_limit=None):
    """分批提取设定(每 batch 章一批, 每批 ~5000 tok), 合并去重。"""
    chapters_max = max(sorted(by_ch.keys())) if by_ch else 0
    chs = sorted(by_ch)
    if chapter_limit:
        chs = [c for c in chs if c <= chapter_limit]
        chapters_max = chapter_limit
    all_settings = {}
    system = ("你是一位小说世界观设定提取助手。输入是某小说若干章的场景事件流, "
              "请提取出现的世界观设定/专有名词/力量体系, 输出设定条目数组。")
    for i in range(0, len(chs), batch):
        grp = chs[i:i + batch]
        parts = []
        for cn in grp:
            parts.append(f"===== 第{cn}章 =====")
            parts.append(actinfo_stream(by_ch[cn], chapter_no=cn))
        stream = "\n".join(parts)
        user = f"""以下是第 {grp[0]}-{grp[-1]} 章的场景事件流:
{stream}

请从上面事件流中提取本批出现的世界观设定/专有名词/力量体系, 输出 JSON 数组, 每个元素是一个对象, 字段:
- name: 设定名(字符串)
- type: 地点 | 组织 | 概念 | 物品 | 力量 | 人物身份
- description: 1-2 句描述(必须来自上面的输入信息)
- first_seen: 首次出现的章号(整数, 范围 1-{chapters_max})
- related: 相关设定名列表(字符串数组)
**硬性要求**: 只基于上面的输入提取真实设定, 严禁输出模板占位符或示例对象; 章号必须在第 1-{chapters_max} 章内; 不编造。
只输出 JSON 数组。"""
        def extract_settings(obj):
            """从模型返回中稳定提取设定条目列表(容错 数组/单对象/包装结构)。"""
            if isinstance(obj, list):
                return [x for x in obj if isinstance(x, dict)]
            if isinstance(obj, dict):
                # 含 name/名称/设定名 键 = 单条设定对象, 直接收下
                if any(k in obj for k in ("name", "名称", "设定名")):
                    return [obj]
                # 否则视为包装结构 {settings:[...]}, 取第一个 list 值
                lst = [v for v in obj.values() if isinstance(v, list)]
                return lst[0] if lst else []
            return []

        def get_name(s):
            for k in ("name", "名称", "设定名"):
                if k in s and s[k]:
                    return str(s[k])
            return ""

        raw, pe, ev = _chat(system, user)
        obj = _parse_json(raw)
        items = extract_settings(obj)
        if items:
            added = 0
            for s in items:
                if not isinstance(s, dict):
                    continue
                nm = str(get_name(s) or "").strip()
                if nm and nm not in all_settings:
                    all_settings[nm] = s
                    added += 1
            print(f"  批次第{grp[0]}-{grp[-1]}章: +{added}条 (累计{len(all_settings)})")
        else:
            # 失败重试一次
            raw2, pe2, ev2 = _chat(system, user)
            obj2 = _parse_json(raw2)
            items = extract_settings(obj2)
            added = 0
            for s in (items or []):
                if not isinstance(s, dict):
                    continue
                nm = str(get_name(s) or "").strip()
                if nm and nm not in all_settings:
                    all_settings[nm] = s
                    added += 1
            print(f"  批次第{grp[0]}-{grp[-1]}章(重试): +{added}条 (累计{len(all_settings)})")
    return list(all_settings.values())


def gen_summary(outlines):
    """全书总结: 用全部章纲的主线汇总。"""
    lines = [f"第{cn}章: {o.get('主线', o.get('summary', ''))}" for cn, o in sorted(outlines.items())]
    stream = "\n".join(lines)
    system = ("你是一位小说全书总结助手。输入是各章节的主线摘要, 请输出全书总结。")
    user = f"""以下是全书各章主线:
{stream}

请输出全书总结, 严格 JSON:
{{
  "总述": "全书到目前为止的完整故事概述, 3-5 句",
  "主线脉络": ["按顺序的 5-8 个大的情节阶段"],
  "当前状态": "故事进行到第{max(outlines.keys()) if outlines else '?'}章的悬念/未解问题"
}}
只输出 JSON。"""
    raw, pe, ev = _chat(system, user)
    return _parse_json(raw), raw, pe, ev


def main():
    global DB, MODEL
    ap = argparse.ArgumentParser()
    ap.add_argument("--chapters", type=int, default=10)
    ap.add_argument("--db", default=None)
    ap.add_argument("--outline", type=int, default=None, help="生成指定章节章纲")
    ap.add_argument("--all-outlines", action="store_true", help="批量生成全部章纲")
    ap.add_argument("--characters", action="store_true", help="生成人物档案")
    ap.add_argument("--settings", action="store_true", help="提取世界观设定")
    ap.add_argument("--summary", action="store_true", help="全书总结(需先有章纲)")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--out-dir", default=None, help="产物输出目录(默认 outputs/<书>_<日期>/stage2);用于本地/云端分流对比")
    args = ap.parse_args()
    MODEL = args.model
    # db 优先显式参数, 否则用 config.DB_PATH(已按小说名隔离)
    DB = args.db or C.DB_PATH
    OUT_DIR = args.out_dir or C.STAGE2_DIR
    os.makedirs(OUT_DIR, exist_ok=True)

    by_ch = load_scenes()
    print(f"已加载 {len(by_ch)} 章场景")

    if args.outline:
        obj, raw, pe, ev = gen_chapter_outline(args.outline, by_ch)
        print(f"== 第{args.outline}章章纲 == (输入{pe}tok 输出{ev}tok)")
        print(json.dumps(obj, ensure_ascii=False, indent=2) if obj else f"[解析失败] {raw[:300]}")

    if args.all_outlines:
        print("== 批量章纲 ==")
        outlines = gen_outlines_all(by_ch)
        with open(os.path.join(OUT_DIR, "outlines.json"), "w", encoding="utf-8") as f:
            json.dump(outlines, f, ensure_ascii=False, indent=2)
        print(f"已存 {OUT_DIR}/outlines.json ({len(outlines)} 章)")

    if args.characters:
        print("== 人物档案(按人物分条) ==")
        chars = gen_characters_chunked(by_ch)
        with open(os.path.join(OUT_DIR, "characters.json"), "w", encoding="utf-8") as f:
            json.dump(chars, f, ensure_ascii=False, indent=2)
        print(f"已存 {OUT_DIR}/characters.json ({len(chars)} 人)")

    if args.settings:
        print("== 设定提取(分批) ==")
        sets = gen_settings(by_ch)
        with open(os.path.join(OUT_DIR, "settings.json"), "w", encoding="utf-8") as f:
            json.dump(sets, f, ensure_ascii=False, indent=2)
        print(f"已存 {OUT_DIR}/settings.json ({len(sets)} 条)")

    if args.summary:
        print("== 全书总结 ==")
        op = os.path.join(OUT_DIR, "outlines.json")
        if os.path.exists(op):
            with open(op, encoding="utf-8") as f:
                outlines = json.load(f)
        else:
            print("未找到 outlines.json, 先生成章纲")
            return
        obj, raw, pe, ev = gen_summary(outlines)
        print(json.dumps(obj, ensure_ascii=False, indent=2) if obj else f"[解析失败] {raw[:300]}")
        if obj:
            with open(os.path.join(OUT_DIR, "summary.json"), "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
            print(f"已存 {OUT_DIR}/summary.json")


if __name__ == "__main__":
    main()
