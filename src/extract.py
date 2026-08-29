# -*- coding: utf-8 -*-
"""
extract.py —— 调 Ollama(qwen3:8b)做两轮抽取,落库前做 JSON 校验与重试
坐标层(para_id)由程序回填,不依赖模型。
"""
import sys
import os
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(__file__))
import config as C
import prompts as P
import llm_client


def _ollama_chat(base, model, messages, temperature, num_ctx,
                 num_predict=C.NUM_PREDICT, timeout=C.REQUEST_TIMEOUT):
    """调用 LLM(默认本地 Ollama, 可经 llm_client 切云端),返回模型文本。

    关键参数说明:
      - think=False : qwen3 默认开启思维链,会吐 <think> 长推理,既慢又污染 JSON。
                      抽取任务不需要深推理,必须显式关闭(云端走 json_mode 等价处理)。
      - format=json : 强制合法 JSON 输出。
      - num_predict : 限制单次输出上限,防止模型跑飞导致超时。
    重试与后端路由统一在 llm_client 内处理; base 仅本地 Ollama 生效(main.py --base 透传)。
    """
    system = next((m["content"] for m in messages if m.get("role") == "system"), "")
    user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    # 后端路由完全交给 llm_client(读 LLM_BACKEND/LLM_BASE_URL/LLM_MODEL 环境变量)。
    # 不强制传 base: 否则会覆盖 env 的云端 base_url 导致请求打到 localhost(404)。
    return llm_client.chat(
        system, user, model=model, num_ctx=num_ctx, temperature=temperature,
        json_mode=True, num_predict=num_predict, timeout=timeout,
    )[0]


def _parse_json(text):
    """鲁棒解析:去 ```json 围栏,截取首个 { 到末个 }。"""
    if text is None:
        return None
    t = text.strip()
    if t.startswith("```"):
        # 去首行围栏
        t = t.split("\n", 1)[-1]
    if t.endswith("```"):
        t = t[: t.rfind("```")]
    t = t.strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    s = t.find("{")
    e = t.rfind("}")
    if s != -1 and e != -1 and e > s:
        try:
            return json.loads(t[s:e + 1])
        except Exception:
            return None
    return None


def _number_text(paras, prev_tail=""):
    lines = []
    if prev_tail:
        lines.append(f"[重叠上下文] {prev_tail}")
    for p in paras:
        lines.append(f"[{p['para_id']}] {p['text']}")
    return "\n".join(lines)


# ======================================================================
# 读者注意力 · 动态场景切分(阶段1)
# 让 LLM 模拟读者阅读, 按"信息完满"动态分组, 避免数据块过小/过大。
# ======================================================================
ATTN_SPLIT_SYSTEM = (
    "你是一位小说拆书师。输入是一章的段落流(每段带[编号])。"
    "请像读者一样沉浸阅读, 用你的注意力把段落动态分组为若干'场景'。"
    "只输出 JSON。")

ATTN_SPLIT_USER = """下面是一章小说的段落流, 每段前缀[编号]。

{stream}

【任务】模拟读者阅读时的注意力分配, 把段落动态分组为场景:
- 逐段读, 当信息积累到"一个完整场景"的程度就封条为一个场景。
- **硬性约束**: 每个场景 8-15 段。信息太单薄就归入当前场景继续积累; 信息太多就提前封条。
  (个别场景允许 6-18 段, 但不要出现 <6 段或 >18 段的场景)
- **场景边界信号**: 地点/时间切换、人物主要进出、一个情节单元收尾、叙事视角/话题转换。

【输出】严格 JSON 数组, 每个元素一个场景的段落范围:
[
  {{"start": <起始段落编号>, "end": <结束段落编号>}},
  ...
]

要求:
- start/end 必须用输入的[编号], 是段落坐标。
- 所有场景按阅读顺序排列, 首尾相接, 覆盖全部段落, 不漏段不重段。
- 场景段数尽量落在 8-15 区间, 避免过小(<6段)或过大(>18段)的碎片。
- 若某章段落较多, 场景数相应多些; 段落少则场景数少些。
只输出 JSON 数组。"""


def split_attention(paras, base, model, num_ctx=None):
    """阶段1: 让 LLM 按读者注意力输出场景边界。返回 [(start_para, end_para), ...]。
    paras: [{para_id, text}...] 一章的段落。失败返回 None。"""
    stream = "\n".join(f"[{p['para_id']}] {p['text']}" for p in paras)
    ctx = num_ctx or C.NUM_CTX
    try:
        raw = _ollama_chat(base, model, [
            {"role": "system", "content": ATTN_SPLIT_SYSTEM},
            {"role": "user", "content": ATTN_SPLIT_USER.format(stream=stream)},
        ], C.TEMPERATURE, ctx, num_predict=2000)
        d = _parse_json(raw)
        if not isinstance(d, list):
            return None
        bounds = []
        for it in d:
            if isinstance(it, dict) and "start" in it and "end" in it:
                try:
                    s, e = int(it["start"]), int(it["end"])
                    if s <= e:
                        bounds.append((s, e))
                except Exception:
                    continue
        if not bounds:
            return None
        return _normalize_bounds(bounds, paras)
    except Exception:
        return None


def _normalize_bounds(bounds, paras):
    """把 LLM 给定的大致边界强制规整到 8-15 段区间(信息完满, 避免碎片/超大)。
    bounds: [(start,end)...] LLM 建议; 程序按 8-15 段区间重排。
    """
    if not paras:
        return []
    first_para = paras[0]["para_id"]
    last_para = paras[-1]["para_id"]
    MIN, MAX = 8, 15

    # 收集 LLM 建议的边界点(场景起点), 作为"强断点"候选
    borders = set()
    for s, e in bounds:
        if first_para < s <= last_para:
            borders.add(s)

    # 逐段扫描, 用 8-15 区间 + 边界点决定切分
    result = []
    cur = first_para
    paras_list = [p["para_id"] for p in paras]
    idx_map = {pid: i for i, pid in enumerate(paras_list)}  # para_id -> 下标
    n = len(paras_list)

    i = 0  # 下标
    while i < n:
        # 本段区间终点: 至少 MIN 段, 最多 MAX 段
        seg_start = paras_list[i]
        # 尝试在 [i+MIN, i+MAX] 内找最靠近的 LLM 边界点
        target = None
        for j in range(i + MIN, min(i + MAX + 1, n)):
            if paras_list[j] in borders:
                target = j
        if target is None:
            # 无边界点: 取 MAX 段
            target = min(i + MAX, n) - 1
        seg_end = paras_list[target]
        result.append((seg_start, seg_end))
        i = target + 1

    # 收尾: 最后一段若 < MIN 段且前一段合并后 <= MAX, 并入前一段
    if len(result) >= 2:
        last_len = result[-1][1] - result[-1][0] + 1
        prev_len = result[-2][1] - result[-2][0] + 1
        if last_len < MIN and prev_len + last_len <= MAX:
            result[-2] = (result[-2][0], result[-1][1])
            result.pop()
    # 确保覆盖到 last_para
    if result:
        result[-1] = (result[-1][0], last_para)
    return result


# ======================================================================
# 读者注意力 · 完整抽取(切分+抽取一体)入口
# 每章: 阶段1 切分边界 -> 阶段2 逐个区间用 v2 schema 抽取
# ======================================================================
def extract_attention_chapter(chapter_paras, chapter_no, volume_no=0,
                              base=C.OLLAMA_BASE, model=C.EXTRACT_MODEL):
    """对一章做"读者注意力"动态切分+抽取。返回 (records, failures)。
    records: 每个场景一条, 含 start_para/end_para/who/where/actinfo/notes。
    """
    records, failures = [], []
    if not chapter_paras:
        return records, failures
    bounds = split_attention(chapter_paras, base, model)
    if not bounds:
        return records, [{"chapter": chapter_no, "error": "注意力切分失败"}]

    for seq, (s, e) in enumerate(bounds, 1):
        # 截取该区间段落
        seg_paras = [p for p in chapter_paras if s <= p["para_id"] <= e]
        if not seg_paras:
            continue
        scene = {
            "scene_id": f"a{chapter_no}_{seq}",   # 临时 id, 落库时替换
            "chapter_no": chapter_no,
            "volume_no": volume_no,
            "event_seq": seq,
            "start_para": s,
            "end_para": e,
            "paras": seg_paras,
        }
        rec, status, err = extract_scene_v2(scene, base, model)
        if rec:
            rec["scene_id"] = -1  # 落库时分配
            records.append(rec)
        else:
            failures.append({"chapter": chapter_no, "para": f"{s}-{e}",
                             "status": status, "error": err})
    return records, failures


def _locate_anchor(paras, anchor):
    """在场景段落中定位 anchor 短句所在 para_id。失败返回 None。"""
    if not anchor:
        return None
    key = anchor.strip()[:8]
    if not key:
        return None
    for p in paras:
        if key in p["text"]:
            return p["para_id"]
    # 宽松:原文含 key 的子串
    full = "\n".join(p["text"] for p in paras)
    idx = full.find(key)
    if idx >= 0:
        cnt = full[:idx].count("\n") + 1
        if cnt <= len(paras):
            return paras[cnt - 1]["para_id"]
    return None


def _resolve_beats(paras, beats):
    """给每个 beat 回填 start_para/end_para。

    程序侧硬约束(不依赖模型自觉):
      1) 丢弃 anchor 或 content 为空的残缺分镜;
      2) 定位后按 start_para 排序,强制坐标单调递增(模型常乱序);
      3) 同一起始段的重复分镜去重;
      4) 未定位到 anchor 的分镜用均匀切分兜底,不丢数据。
    """
    if not beats:
        return []

    # 1) 过滤残缺分镜
    valid = [
        b for b in beats
        if isinstance(b, dict)
        and str(b.get("anchor", "")).strip()
        and str(b.get("content", "")).strip()
    ]
    if not valid:
        return []

    n = len(paras)
    para_ids = [p["para_id"] for p in paras]

    # 2) 定位 anchor
    located = []
    for i, b in enumerate(valid):
        sp = _locate_anchor(paras, b.get("anchor", ""))
        located.append({
            "sp": sp,
            "order": i,
            "anchor": str(b.get("anchor", "")).strip(),
            "content": str(b.get("content", "")).strip(),
        })
    # 未定位的按模型给出的相对顺序均匀铺开
    per = max(1, n // max(1, len(located)))
    for i, it in enumerate(located):
        if it["sp"] is None:
            it["sp"] = para_ids[min(i * per, n - 1)]

    # 3) 按定位坐标排序(修正模型乱序),同起始段去重
    located.sort(key=lambda x: (x["sp"], x["order"]))
    dedup = []
    for it in located:
        if dedup and it["sp"] == dedup[-1]["sp"]:
            continue  # 同一起始段只保留第一条
        dedup.append(it)

    # 4) 强制单调递增并回填 end_para
    out = []
    for i, it in enumerate(dedup):
        sp = it["sp"]
        if out and sp <= out[-1]["start_para"]:
            sp = out[-1]["start_para"] + 1
        if sp > para_ids[-1]:
            break
        ep = dedup[i + 1]["sp"] - 1 if i + 1 < len(dedup) else para_ids[-1]
        if ep < sp:
            ep = sp
        out.append({
            "seq": len(out) + 1,
            "start_para": sp,
            "end_para": ep,
            "anchor": it["anchor"],
            "content": it["content"],
        })
    return out


def _clean_list(items, max_n, min_len=0):
    """清洗字符串数组:去空、去重、限长度下限、限条数上限。"""
    if not isinstance(items, list):
        return []
    out = []
    for x in items:
        s = str(x).strip() if x is not None else ""
        if not s or len(s) < min_len:
            continue
        if s in out:
            continue
        out.append(s)
        if len(out) >= max_n:
            break
    return out


def _clean_pov(pov, who):
    """清洗视角字段。

    模型有时会把提示词里的说明当答案(如"第三人称主语"),这类描述性回答无检索价值。
    命中描述词则回退为 who 中的"主事"人物,再不行填"未明示"。
    """
    s = str(pov or "").strip()
    bad_marks = ("人称", "视角", "主语", "叙事者", "narrator", "POV", "pov")
    if s and not any(m in s for m in bad_marks) and len(s) <= 20:
        return s
    for w in who:
        if w.get("role") == "主事":
            return w["name"]
    return who[0]["name"] if who else "未明示"


def _clean_who(who):
    """清洗人物数组:名称非空、role 归一到 主事/参与/提及。"""
    if not isinstance(who, list):
        return []
    ok_roles = {"主事", "参与", "提及"}
    out, seen = [], set()
    for w in who:
        if not isinstance(w, dict):
            continue
        name = str(w.get("name", "")).strip()
        if not name or name in seen:
            continue
        role = str(w.get("role", "")).strip()
        if role not in ok_roles:
            role = "参与"
        seen.add(name)
        out.append({"name": name, "role": role})
    return out


# ======================================================================
# v2 薄 schema: actinfo 清洗
# act   -> {type:"act"(可缺省), channel, who, content}
# event -> {type:"event", content, scope}
# ======================================================================
V2_CHANNELS = ("see", "hear", "feel", "do", "say", "recall", "reason")
V2_MAX_ACTINFO = 12


def _clean_actinfo(actinfo, who_names, src_len=0):
    """清洗 actinfo 数组(程序侧硬约束,不依赖模型自觉):
      - 非 dict 条目丢弃; content 必填非空;
      - event 条目: scope 清为非空列表(只留 * 或在 who 名单内的名字,否则兜底 ["*"]);
      - act 条目: channel 必须命中白名单,who 必填;
      - 条数上限按场景规模缩放: cap = clamp(src_len//60, 2, 12);
      - 连续同(类型, who, channel)条目自动合并(去冗余,省字段开销)。
    """
    if not isinstance(actinfo, list):
        return []
    out = []
    for x in actinfo:
        if not isinstance(x, dict):
            continue
        typ = str(x.get("type", "") or "act").strip()
        content = str(x.get("content", "") or "").strip()
        if not content:
            continue
        if typ == "event":
            scope = []
            for s in x.get("scope", []) or []:
                s = str(s).strip()
                if s and s not in scope and (s == "*" or s in who_names):
                    scope.append(s)
            if not scope:
                scope = ["*"]
            out.append({"type": "event", "content": content, "scope": scope})
        else:
            ch = str(x.get("channel", "") or "").strip()
            who = str(x.get("who", "") or "").strip()
            if ch not in V2_CHANNELS or not who:
                continue  # act 条目缺 channel 或执行者则丢弃
            out.append({"type": "act", "channel": ch, "who": who, "content": content})

    # 同一(类型, who, channel)最多保留 2 条,第 3 条起并入该组合最近一条
    # (既去冗余又不过度合并: 单条 content 不会长得离谱)
    cnt = {}
    merged = []
    for it in out:
        key = (it["type"], it.get("who"), it.get("channel"))
        if cnt.get(key, 0) >= 2:
            for j in range(len(merged) - 1, -1, -1):
                m = merged[j]
                if m["type"] == it["type"] and m.get("who") == it.get("who") \
                        and m.get("channel") == it.get("channel"):
                    if it["type"] == "event":
                        for s in it.get("scope", []):
                            if s not in m["scope"]:
                                m["scope"].append(s)
                    m["content"] += "；" + it["content"]
                    break
        else:
            merged.append(it)
            cnt[key] = cnt.get(key, 0) + 1

    cap = max(2, min(V2_MAX_ACTINFO, src_len // 60)) if src_len else V2_MAX_ACTINFO
    # 仍超上限: 继续合并相邻同 who 或同 channel, 直到 <= cap
    while len(merged) > cap:
        best = None
        for i in range(len(merged) - 1):
            a, b = merged[i], merged[i + 1]
            if a["type"] == b["type"] and \
                    (a.get("who") == b.get("who") or a.get("channel") == b.get("channel")):
                best = i
                break
        if best is None:
            break
        a, b = merged[best], merged.pop(best + 1)
        if b["type"] == "event":
            for s in b.get("scope", []):
                if s not in a["scope"]:
                    a["scope"].append(s)
        a["content"] += "；" + b["content"]
    return merged[:cap]


def extract_scene(scene, base, model, prev_tail=""):
    """对单个场景做两轮抽取,返回 (record, status, error)。"""
    text = _number_text(scene["paras"], prev_tail)
    try:
        r1_raw = _ollama_chat(base, model, [
            {"role": "system", "content": P.SYSTEM_PROMPT},
            {"role": "user", "content": P.build_round1_user(text)},
        ], C.TEMPERATURE, C.NUM_CTX)
        r1 = _parse_json(r1_raw)
        if not isinstance(r1, dict):
            return None, "round1_parse_fail", r1_raw

        r2_raw = _ollama_chat(base, model, [
            {"role": "system", "content": P.SYSTEM_PROMPT},
            {"role": "user", "content": P.build_round2_user(text, r1)},
        ], C.TEMPERATURE, C.NUM_CTX)
        r2 = _parse_json(r2_raw)
        if not isinstance(r2, dict):
            return None, "round2_parse_fail", r2_raw

        who_clean = _clean_who(r1.get("who", []))
        rec = {
            "scene_id": scene["scene_id"],
            "chapter_no": scene["chapter_no"],
            "volume_no": scene["volume_no"],
            "event_seq": scene["event_seq"],
            "start_para": scene["start_para"],
            "end_para": scene["end_para"],
            "who": who_clean,
            "what": str(r1.get("what", "") or "").strip(),
            "when": r1.get("when", {}) if isinstance(r1.get("when"), dict) else {},
            # where 必填:模型留空则记为"未明示",保持字段非空便于后续统计/聚合
            "where": str(r1.get("where", "") or "").strip() or "未明示",
            "why": str(r1.get("why", "") or "").strip(),
            "how": str(r1.get("how", "") or "").strip(),
            "pov": _clean_pov(r2.get("pov", ""), who_clean),
            "emotion": r2.get("emotion", {}) if isinstance(r2.get("emotion"), dict) else {},
            "plot_function": str(r2.get("plot_function", "") or "").strip(),
            # 文学层限量:模型倾向过度摘录,程序侧强制截断
            "rhetoric": _clean_list(r2.get("rhetoric", []), C.MAX_RHETORIC),
            "key_sentences": _clean_list(
                r2.get("key_sentences", []), C.MAX_KEY_SENTENCES,
                min_len=C.MIN_KEY_SENTENCE_LEN),
            "summary": str(r2.get("summary", "") or "").strip(),
            "keywords": _clean_list(r2.get("keywords", []), C.MAX_KEYWORDS),
            "beats": _resolve_beats(scene["paras"], r2.get("beats", [])),
        }
        return rec, "ok", None
    except Exception as e:
        return None, "error", str(e)


def extract_scene_single(scene, base, model, prev_tail=""):
    """单轮抽取: 5W1H + 叙事/文学 + 分镜一次完成。

    实测(2026-08-20, 2章小样): 11/11 成功, 单场景 24.8s vs 两轮 ~55s,
    提速 ~2.2x, 质量样例与两轮持平。产物结构完全一致。
    """
    text = _number_text(scene["paras"], prev_tail)
    try:
        r_raw = _ollama_chat(base, model, [
            {"role": "system", "content": P.SYSTEM_PROMPT},
            {"role": "user", "content": P.build_single_user(text)},
        ], C.TEMPERATURE, C.NUM_CTX)
        r = _parse_json(r_raw)
        if not isinstance(r, dict):
            return None, "single_round_parse_fail", r_raw

        who_clean = _clean_who(r.get("who", []))
        rec = {
            "scene_id": scene["scene_id"],
            "chapter_no": scene["chapter_no"],
            "volume_no": scene["volume_no"],
            "event_seq": scene["event_seq"],
            "start_para": scene["start_para"],
            "end_para": scene["end_para"],
            "who": who_clean,
            "what": str(r.get("what", "") or "").strip(),
            "when": r.get("when", {}) if isinstance(r.get("when"), dict) else {},
            "where": str(r.get("where", "") or "").strip() or "未明示",
            "why": str(r.get("why", "") or "").strip(),
            "how": str(r.get("how", "") or "").strip(),
            "pov": _clean_pov(r.get("pov", ""), who_clean),
            "emotion": r.get("emotion", {}) if isinstance(r.get("emotion"), dict) else {},
            "plot_function": str(r.get("plot_function", "") or "").strip(),
            "rhetoric": _clean_list(r.get("rhetoric", []), C.MAX_RHETORIC),
            "key_sentences": _clean_list(
                r.get("key_sentences", []), C.MAX_KEY_SENTENCES,
                min_len=C.MIN_KEY_SENTENCE_LEN),
            "summary": str(r.get("summary", "") or "").strip(),
            "keywords": _clean_list(r.get("keywords", []), C.MAX_KEYWORDS),
            "beats": _resolve_beats(scene["paras"], r.get("beats", [])),
        }
        return rec, "ok", None
    except Exception as e:
        return None, "error", str(e)


def extract_scene_v2(scene, base, model, prev_tail=""):
    """v2 薄 schema 单轮抽取: who/when/where + actinfo 有序列表 + notes。

    输出必须小于原文(读薄);actinfo 由 _clean_actinfo 硬清洗。
    """
    text = _number_text(scene["paras"], prev_tail)
    try:
        r_raw = _ollama_chat(base, model, [
            {"role": "system", "content": P.SYSTEM_PROMPT_V2},
            {"role": "user", "content": P.build_v2_user(text)},
        ], C.TEMPERATURE, C.NUM_CTX)
        r = _parse_json(r_raw)
        if not isinstance(r, dict):
            return None, "v2_parse_fail", r_raw

        who_clean = _clean_who(r.get("who", []))
        src_len = sum(len(p["text"]) for p in scene["paras"])
        actinfo = _clean_actinfo(r.get("actinfo", []),
                                 {w["name"] for w in who_clean},
                                 src_len=src_len)

        # 一致性兜底: actinfo 中出现但顶层 who 漏列的角色,补进 who(默认参与)
        names = {w["name"] for w in who_clean}
        for it in actinfo:
            if it["type"] == "act" and it["who"] not in names:
                who_clean.append({"name": it["who"], "role": "参与"})
                names.add(it["who"])

        rec = {
            "schema": "v2",
            "scene_id": scene["scene_id"],
            "chapter_no": scene["chapter_no"],
            "volume_no": scene["volume_no"],
            "event_seq": scene["event_seq"],
            "start_para": scene["start_para"],
            "end_para": scene["end_para"],
            "who": who_clean,
            "when": r.get("when", {}) if isinstance(r.get("when"), dict) else {},
            "where": str(r.get("where", "") or "").strip() or "未明示",
            "actinfo": actinfo,
            "notes": str(r.get("notes", "") or "").strip(),
        }
        return rec, "ok", None
    except Exception as e:
        return None, "error", str(e)


def extract_all(scenes, base=C.OLLAMA_BASE, model=C.EXTRACT_MODEL,
               num_parallel=C.OLLAMA_NUM_PARALLEL, cache_path=None,
               extract_mode="two"):
    """并发抽取全部场景。返回 (records, failures)。

    extract_mode: "two"(两轮: 5W1H 再叙事/文学) | "single"(单轮合并, ~2.2x 提速)
                  | "v2"(薄 schema 单轮: who/when/where + actinfo + notes)。
    缓存机制: cache_path 存在时,按 scene_id 落盘已抽记录。被外部回收/中断后
    重跑可命中缓存(秒回),只补未完成场景,多跑几次必然收敛。每个场景抽完即写盘,
    因此即便中途被 kill,已完成部分不丢失。仅信任"当前场景集合内"的 scene_id。
    """
    records = []
    failures = []
    cache = {}
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}
    # 统一以 str(scene_id) 作缓存键(json 会把 int 键转 str,需两侧一致
    cache = {str(k): v for k, v in cache.items()}
    current_ids = {str(sc["scene_id"]) for sc in scenes}
    cache = {k: v for k, v in cache.items() if k in current_ids}

    # 预备 overlap 上下文(上一场景末段)
    prev_tail = {}
    for i, sc in enumerate(scenes):
        prev_tail[str(sc["scene_id"])] = scenes[i - 1]["paras"][-1]["text"] if i > 0 else ""

    def _job(sc):
        if extract_mode == "v2" or extract_mode == "attention":
            # attention 模式: 场景已由读者注意力切分, 抽取仍用 v2 schema(actinfo 完整)
            return extract_scene_v2(sc, base, model,
                                    prev_tail.get(str(sc["scene_id"]), ""))
        if extract_mode == "single":
            return extract_scene_single(sc, base, model,
                                        prev_tail.get(str(sc["scene_id"]), ""))
        return extract_scene(sc, base, model,
                             prev_tail.get(str(sc["scene_id"]), ""))

    todo = [sc for sc in scenes if str(sc["scene_id"]) not in cache]
    print(f"  抽取缓存: 命中 {len(cache)} / 待抽 {len(todo)}")
    with ThreadPoolExecutor(max_workers=num_parallel) as ex:
        futs = {ex.submit(_job, sc): str(sc["scene_id"]) for sc in todo}
        for fut in as_completed(futs):
            sid = futs[fut]
            rec, status, err = fut.result()
            if rec is None:
                failures.append({"scene_id": sid, "status": status, "error": err})
            else:
                records.append(rec)
                cache[sid] = rec
                if cache_path:
                    try:
                        with open(cache_path, "w", encoding="utf-8") as f:
                            json.dump(cache, f, ensure_ascii=False)
                    except Exception:
                        pass
    # 合并缓存中已命中部分(按 str 键比对,避免重复/丢失)
    done_ids = {str(r["scene_id"]) for r in records}
    records.extend([cache[k] for k in cache if k not in done_ids])
    records.sort(key=lambda r: r["scene_id"])
    return records, failures
