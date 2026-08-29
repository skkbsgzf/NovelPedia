# -*- coding: utf-8 -*-
"""
style_sampler.py —— 文风采样器(独立于线索图谱流程)

设计(对应讨论结论):
  - 文风分析不依赖线索图谱, 独立采集
  - 从全文**随机采样 10%** 的文本
  - 采样构成按**字数比 7:3**: 长叙述段落 70% + 短句 30%
  - 带随机性: 每次运行 seed 不同, 采样子集不同(探索不同文风面)
  - 采样结果喂给 LLM 做**一次性文风统合**(长文风分析, 非逐场景)

用法:
  from style_sampler import sample_style_text, analyze_style
  corpus = sample_style_text(db_path, chapters, ratio=0.1, seed=None)
  result = analyze_style(corpus, base, model)   # -> {fact, examples, chain}
"""
import os
import sys
import json
import sqlite3
import random
import re

sys.path.insert(0, os.path.dirname(__file__))
import llm_client
import config as C
from logbook import get_logbook as _get_logbook

# ======================================================================
# 通用词过滤（复用公版词库 generic_lexicon.json，与 setting_agent 同一份资产）
# ======================================================================
_GENERIC_SET = None
_GENERIC_PAT = None


def _load_generic():
    """惰性加载公版通用词库（categories 扁平词表 + patterns 正则）。"""
    global _GENERIC_SET, _GENERIC_PAT
    if _GENERIC_SET is not None:
        return
    _GENERIC_SET = set()
    _GENERIC_PAT = []
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generic_lexicon.json")
        if os.path.exists(p):
            d = json.load(open(p, encoding="utf-8"))
            for lst in (d.get("categories") or {}).values():
                for w in lst:
                    if w:
                        _GENERIC_SET.add(str(w).strip())
            for pat in (d.get("patterns") or []):
                try:
                    _GENERIC_PAT.append(re.compile(pat))
                except Exception:
                    pass
    except Exception:
        pass


def _is_generic(w):
    _load_generic()
    if w in _GENERIC_SET:
        return True
    for pat in _GENERIC_PAT:
        if pat.match(w):
            return True
    return False


# 高频虚词/停用词（分词后无意义成分）
_STOPWORDS = set("了 的 是 在 有 和 就 不 都 一 个 也 还 很 我 你 他 她 它 这 那 啊 吧 呢 吗 么 呀 哦 嗯 又 再 才 只 但 并 而 或 与 及 并且 因此 所以 因为 由于 如果 虽然 然后 接着 随后 这时 此时 于是".split())


# ======================================================================
# 段落/短句分类（三桶化: F1 修复——保留 21-39 字对白主体）
# ======================================================================
def _classify(paras):
    """按形态分三类（F1 修复：不再丢弃 21-39 字中间段，它们是对白主体）:
      long_narr   = 长叙述段落(≥ 2 句 且 平均句长较长 / 或单段字数 ≥ 阈值)
      mid_dialogue= 中间段(21-39 字, 多为对白/动作短段)
      short_sent  = 独立短句(长度 ≤ 20 字, 多为对话/留白/情绪点)
    返回 (long_list, mid_list, short_list), 元素为 (para_id, chapter_no, text)。"""
    long_narr, mid_dialogue, short_sent = [], [], []
    for pid, cn, text in paras:
        text = (text or "").strip()
        if not text:
            continue
        n = len(text)
        sentences = [s for s in re.split(r'[。！？!?]', text) if s.strip()]
        if n >= 40 or len(sentences) >= 3:
            long_narr.append((pid, cn, text))
        elif n <= 20:
            short_sent.append((pid, cn, text))
        else:
            mid_dialogue.append((pid, cn, text))  # 21-39 字: 对白主体, 不再丢弃
    return long_narr, mid_dialogue, short_sent


# ======================================================================
# 按字数比采样(7:3)
# ======================================================================
def _sample_by_chars(items, target_chars, rng):
    """从 items 中随机采样, 直到累计字数接近 target_chars。
    返回 (采样列表, 实际字数)。"""
    if not items:
        return [], 0
    pool = list(items)
    rng.shuffle(pool)
    picked, total = [], 0
    for it in pool:
        if total >= target_chars:
            break
        picked.append(it)
        total += len(it[2])
    return picked, total


def sample_style_text(db_path=None, chapters=None, ratio=0.1, seed=None,
                      long_ratio=0.6, mid_ratio=0.25):
    """从全文随机采样 ratio 比例文本用于文风分析。
    构成(按字数): 长叙述段落 long_ratio(默认0.6) + 中间段 mid_ratio(默认0.25, 对白主体)
                 + 短句 余量(默认0.15)。三桶都保留, 不再丢弃对白。
    返回 dict: {sampled: [(para_id, chapter, text)...], chars, long_chars,
                mid_chars, short_chars}"""
    db_path = db_path or C.DB_PATH
    chapters = chapters or C.CHAPTERS
    rng = random.Random(seed)   # seed=None -> 每次运行随机
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT para_id, chapter_no, text FROM paragraphs "
        "WHERE chapter_no<=? ORDER BY chapter_no, para_id", (chapters,)).fetchall()
    conn.close()
    total_chars = sum(len(r[2] or "") for r in rows)
    if total_chars == 0:
        return {"sampled": [], "chars": 0, "long_chars": 0,
                "mid_chars": 0, "short_chars": 0}

    long_narr, mid_dialogue, short_sent = _classify(rows)
    target = max(200, int(total_chars * ratio))
    long_target = int(target * long_ratio)
    mid_target = int(target * mid_ratio)
    short_target = target - long_target - mid_target

    picked_long, long_chars = _sample_by_chars(long_narr, long_target, rng)
    picked_mid, mid_chars = _sample_by_chars(mid_dialogue, mid_target, rng)
    picked_short, short_chars = _sample_by_chars(short_sent, short_target, rng)
    # 若某一类不足, 用剩余类按序补足
    deficit = max(0, long_target - long_chars)
    if deficit:
        rest = [x for x in mid_dialogue if x not in picked_mid] + \
               [x for x in short_sent if x not in picked_short]
        more, c = _sample_by_chars(rest, deficit, rng)
        picked_long.extend(more)
        long_chars += c
    sampled = picked_long + picked_mid + picked_short
    sampled.sort(key=lambda x: (x[1], x[0]))
    return {"sampled": sampled, "chars": long_chars + mid_chars + short_chars,
            "long_chars": long_chars, "mid_chars": mid_chars, "short_chars": short_chars,
            "seed": seed}


# ======================================================================
# 文风统合分析(一次性, 基于采样文本)
# ======================================================================
STYLE_SYSTEM = (
    "你是**资深文学编辑**。基于随机采样的原文文本, 分析这部小说的**文风体系**。"
    "只输出 JSON。")

STYLE_USER = """以下是从小说全文中**随机采样**的文本片段(含长叙述段落与短句, 已标注章节):
{corpus}

请统合分析作者的**文风体系**(不是逐句点评):
{{
  "fact": "一段话总结文风特征(叙述节奏/句式偏好/意象体系/对话风格/氛围营造)",
  "examples": ["最能代表文风的原文片段(引用, 最多3个)", ...],
  "chain": "分析思路(基于哪些采样文本得出, 3-4句)"
}}
要求: 具体、可引用原文, 禁止"淋漓尽致/身临其境"这类空泛词。只输出 JSON。"""


def analyze_style(corpus, base, model):
    """对采样文本做一次性文风统合。corpus 是 sample_style_text 的返回。
    返回 {fact, examples, chain} 或 None。"""
    if not corpus or not corpus.get("sampled"):
        return None
    lines = []
    for pid, cn, text in corpus["sampled"]:
        t = text.strip()
        if t:
            lines.append("【第%s章】%s" % (cn, t[:120]))
    # 采样文本过长则截断(控制 token)
    chunk = "\n".join(lines[:60])
    if len(chunk) > 8000:
        chunk = chunk[:8000]
    user = STYLE_USER.format(corpus=chunk)
    try:
        raw = llm_client.chat(STYLE_SYSTEM, user, json_mode=True,
                              num_predict=700, temperature=0.4)[0]
        d = json.loads(raw)
        if not isinstance(d, dict) or not d.get("fact"):
            return None
        return {
            "fact": str(d["fact"]).strip(),
            "examples": [str(e).strip() for e in (d.get("examples") or [])][:3],
            "chain": str(d.get("chain", "")).strip(),
            "sampled_chars": corpus.get("chars", 0),
            "seed": corpus.get("seed"),
        }
    except Exception:
        return None


# ======================================================================
# A2 · 全量词频统计（jieba + POS 过滤 + 通用词过滤）—— 纯规则, 零 LLM
# ======================================================================
_WN = None


def _jieba_posseg():
    global _WN
    if _WN is None:
        import jieba.posseg as _p
        _WN = _p
    return _WN


# 词性白名单: a/ad 形容词家族, v/vd/vn 动词家族
_KEEP_POS = ("a", "ad", "v", "vd", "vn")
_POS_NAME = {"a": "形容词", "ad": "副形词", "v": "动词",
             "vd": "副动词", "vn": "名动词"}


def compute_word_freq(db_path=None, chapters=None, topk=100):
    """全文 jieba 分词 → 词性过滤(形容词/动词) → 通用词过滤 → 词频。
    返回 {schema, total_tokens, filtered_generic, freq: {pos: [{w, n}...]}}"""
    t0 = __import__("time").time()
    db_path = db_path or C.DB_PATH
    chapters = chapters or C.CHAPTERS
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT text FROM paragraphs WHERE chapter_no<=?", (chapters,)).fetchall()
    conn.close()
    ps = _jieba_posseg()
    freq = {p: {} for p in _KEEP_POS}
    total_tokens = 0
    filtered_generic = 0
    for (text,) in rows:
        if not text or not text.strip():
            continue
        for w, flag in ps.cut(text):
            if flag not in _KEEP_POS:
                continue
            w = w.strip()
            if len(w) < 2 or w in _STOPWORDS:
                continue
            total_tokens += 1
            if _is_generic(w):
                filtered_generic += 1
                continue
            d = freq[flag]
            d[w] = d.get(w, 0) + 1
    out = {p: sorted(({"w": w, "n": n} for w, n in d.items()),
                     key=lambda x: -x["n"])[:topk]
           for p, d in freq.items()}
    lb = _get_logbook()
    lb.info("style", "词频统计完成",
            total_tokens=total_tokens, filtered_generic=filtered_generic,
            top_per_pos={p: len(v) for p, v in out.items()},
            elapsed=round(__import__("time").time() - t0, 1))
    return {"schema": 1, "total_tokens": total_tokens,
            "filtered_generic": filtered_generic, "freq": out, "topk": topk}


# ======================================================================
# A3 · 全量拆句统计（句长/句式/衔接）—— 纯规则, 零 LLM
# ======================================================================
_LEN_BUCKETS = ["0-10", "11-20", "21-30", "31-40", "41+"]
_CONNECTORS = {
    "转折": ("但", "然而", "可是", "却", "不过", "反倒", "偏偏"),
    "因果": ("因此", "所以", "因为", "由于", "于是", "从而"),
    "时间": ("随后", "接着", "然后", "之后", "这时", "此时", "下一刻", "与此同时"),
    "递进": ("甚至", "而且", "况且", "何况"),
}


def compute_sentence_stats(db_path=None, chapters=None):
    """全量拆句统计: 句长五桶 / 长短比 / 句式占比 / 衔接句首词。
    返回 {schema, n_sent, len_dist, long_short_ratio, types, connector_top}"""
    t0 = __import__("time").time()
    db_path = db_path or C.DB_PATH
    chapters = chapters or C.CHAPTERS
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT text FROM paragraphs WHERE chapter_no<=?", (chapters,)).fetchall()
    conn.close()
    len_dist = {b: 0 for b in _LEN_BUCKETS}
    types = {"陈述": 0, "疑问": 0, "感叹": 0, "反问": 0}
    connector = {}
    n_long = n_short = n_sent = 0
    for (text,) in rows:
        if not text:
            continue
        # 按句末标点拆句(保留标点归属)
        for s in re.split(r'(?<=[。！？!?])', text):
            s = s.strip()
            if not s:
                continue
            n_sent += 1
            L = len(s)
            if L <= 10:
                bucket = "0-10"
            elif L <= 20:
                bucket = "11-20"
            elif L <= 30:
                bucket = "21-30"
            elif L <= 40:
                bucket = "31-40"
            else:
                bucket = "41+"
            # 长短比口径(与方案一致): 长句 >25 字 / 短句 ≤10 字, 独立于分桶
            if L <= 10:
                n_short += 1
            elif L > 25:
                n_long += 1
            len_dist[bucket] += 1
            # 句式: 按句末标点
            if s.endswith("？") or s.endswith("?"):
                if any(k in s for k in ("难道", "岂", "吗", "么", "怎能", "如何能")):
                    types["反问"] += 1
                else:
                    types["疑问"] += 1
            elif s.endswith("！") or s.endswith("!"):
                types["感叹"] += 1
            else:
                types["陈述"] += 1
            # 衔接句首词
            head = s[:2]
            for cat, words in _CONNECTORS.items():
                for w in words:
                    if s.startswith(w):
                        connector[w] = connector.get(w, 0) + 1
                        break
    total = max(sum(types.values()), 1)
    types = {k: round(v / total, 3) for k, v in types.items()}
    connector_top = sorted(({"w": w, "cat": c, "n": n}
                            for w, n in connector.items()
                            for c, ws in _CONNECTORS.items() if w in ws),
                           key=lambda x: -x["n"])[:12]
    long_short_ratio = round(n_long / max(n_short, 1), 2)
    lb = _get_logbook()
    lb.info("style", "句统计完成", n_sent=n_sent,
            long_short_ratio=long_short_ratio,
            len_dist=len_dist, elapsed=round(__import__("time").time() - t0, 1))
    return {"schema": 1, "n_sent": n_sent, "len_dist": len_dist,
            "long_short_ratio": long_short_ratio, "types": types,
            "connector_top": connector_top}


# ======================================================================
# B · analyze_style_v2 —— 四维组合式聚合（词/句/段/篇）
# ======================================================================
# 情感词典（内置轻量版: 情绪曲线只需趋势, 不需要精确分类）
_POS_WORDS = set("微笑温暖幸福希望喜悦平静安宁爱信任坚定光明胜利欢呼拥抱治愈守护温柔甜蜜欣慰安心期待感激骄傲释然救活".replace(" ", ""))
_NEG_WORDS = set("恐惧绝望愤怒悲伤痛苦死亡黑暗寒冷尖叫尸体崩溃疯狂恶毒恨谎言背叛失去惨叫哭杀死腐烂腐朽恐怖诡异惊骇颤抖冷汗窒息阴森诅咒灾难毁灭血".replace(" ", ""))

# 修辞规则特征
_RHETORIC_PATTERNS = {
    "比喻": re.compile(r"像|如同|仿佛|宛如|似的|般"),
    "夸张": re.compile(r"万丈|千里|永恒|永远|彻底|毁灭性|不可能|前所未有|天下第一"),
    "设问": re.compile(r"[？?]"),
}
_FOIL_ENV = re.compile(r"夜色|风|雨|光|黑暗|晨|黄昏|街道|房间|屋|血|灰雾|灯")


def _sentiment(text):
    """规则情感打分 [-1,1]。"""
    if not text:
        return 0.0
    pos = sum(1 for w in _POS_WORDS if w in text)
    neg = sum(1 for w in _NEG_WORDS if w in text)
    return round((pos - neg) / max(pos + neg, 1), 3)


def _rhetoric_scan(text):
    """规则修辞识别, 返回 {修辞: 命中次数}。"""
    out = {}
    for name, pat in _RHETORIC_PATTERNS.items():
        n = len(pat.findall(text))
        if n:
            out[name] = n
    # 排比: ≥2 句同句首(句首2字相同)
    sents = [s.strip() for s in re.split(r'[。！？!?]', text) if len(s.strip()) >= 6]
    from collections import Counter
    heads = Counter(s[:2] for s in sents)
    dup = sum(1 for _, c in heads.items() if c >= 2)
    if dup >= 2:
        out["排比"] = dup
    return out


# 认知/情态动词（L3 过滤: 任何小说都高频, 无风格辨识度）
_COGNITIVE_VERBS = set("知道 看到 看着 没有 不会 开始 可能 应该 需要 发现 起来 下来 进去 出来 认为 觉得 感觉 希望 想要 似乎 仿佛 好像 就是 已经 正在 突然 忽然 终于 原来 其实 真的 完全 非常 一直 往往 常常 总是 一般 根本 明显 不同 特殊 必须 能够 可以 准备 继续 回到 离开 看向 望向 听见 声音".split())


def _split_style_words(freq, content_words):
    """词频拆三份: 风格词(freq) / 设定相关词(content_words) / 认知动词(丢弃)。
    freq = {pos: [{"w","n"}...]}  → 返回 (style_freq, content_by_pos)"""
    style_freq = {}
    content_by_pos = {}
    for pos, lst in (freq or {}).items():
        style, content = [], []
        for x in lst:
            w = x["w"]
            if w in content_words:
                content.append(x)
            elif pos in ("v", "vn") and w in _COGNITIVE_VERBS:
                continue  # 认知动词: 直接丢弃
            else:
                style.append(x)
        if style:
            style_freq[pos] = style
        if content:
            content_by_pos[pos] = content
    return style_freq, content_by_pos


def analyze_style_v2(deps, base=None, model=None):
    """四维组合式聚合。deps 提供各维度既有产物, 本函数只做聚合+统计+一次 LLM 综合。
    deps = {scenes_meta, word_freq, sentence_stats, char_facts, resumes,
            clue_graph, settings_graph, total_chars}
    返回 schema=2 的 style_analysis dict 或 None(数据缺失时)。
    与旧 analyze_style 的关系: 这是 v2 主路径; 缺 scenes_meta 等数据时由调用方降级旧版。"""
    import time as _t
    t0 = _t.time()
    lb = _get_logbook()
    lb.section("style", "文风四维聚合(analyze_style_v2)")

    scenes = deps.get("scenes_meta") or []
    wf = deps.get("word_freq") or {}
    ss = deps.get("sentence_stats") or {}
    cf = deps.get("char_facts") or {}
    rs = deps.get("resumes") or []
    cg = deps.get("clue_graph") or {}
    sg = deps.get("settings_graph") or {}
    total_chars = deps.get("total_chars") or 0

    # ---- B1 情绪曲线: 场景 raw_text 规则打分 → 按章聚合 ----
    ch_emo = {}
    for m in scenes:
        s = _sentiment(m.get("raw_text", ""))
        ch = m.get("chapter_no")
        if ch is None:
            continue
        arr = ch_emo.setdefault(ch, [])
        arr.append(s)
    arc = [{"ch": ch, "sentiment": round(sum(v) / len(v), 3),
            "n_scenes": len(v)}
           for ch, v in sorted(ch_emo.items())]
    lb.info("style", "B1 情绪曲线", chapters=len(arc))

    # ---- B2 修辞: 规则扫描场景原文 ----
    rhe = {}
    for m in scenes:
        for name, n in _rhetoric_scan(m.get("raw_text", "")).items():
            r = rhe.setdefault(name, {"n": 0, "scenes": []})
            r["n"] += n
            if len(r["scenes"]) < 3:
                r["scenes"].append({"ch": m.get("chapter_no"), "scene": m.get("scene_id")})
    total_sc = max(len(scenes), 1)
    rhetoric = {k: {"n": v["n"], "per_scene": round(v["n"] / total_sc, 3),
                    "examples": v["scenes"]}
                for k, v in rhe.items()}
    # 烘托: 场景含环境词 + 有人物
    foil = [{"ch": m.get("chapter_no"), "scene": m.get("scene_id"),
             "where": m.get("where", "")}
            for m in scenes if len(m.get("who") or []) >= 1
            and _FOIL_ENV.search(m.get("raw_text", "") or "")]
    lb.info("style", "B2 修辞", rhetoric=len(rhetoric), foil=len(foil))

    # ---- B3 蒙太奇: 相邻场景地点变化 = 切镜 ----
    montage = {}
    prev_ch = prev_where = None
    for m in scenes:
        ch, wh = m.get("chapter_no"), m.get("where", "")
        if prev_ch is not None and ch == prev_ch and wh and prev_where and wh != prev_where:
            c = montage.setdefault(ch, {"cuts": 0})
            c["cuts"] += 1
        prev_ch, prev_where = ch, wh
    montage = [{"ch": k, "cuts": v["cuts"]} for k, v in sorted(montage.items())]
    montage.sort(key=lambda x: -x["cuts"])
    lb.info("style", "B3 蒙太奇", chapters=len(montage), max_cuts=montage[0]["cuts"] if montage else 0)

    # ---- B4 对白/描写占比: 取经 sayings/doings ----
    say_chars = sum(len(s.split(": ", 1)[-1]) for v in cf.values() for s in (v.get("sayings") or []))
    do_chars = sum(len(d.split(": ", 1)[-1]) for v in cf.values() for d in (v.get("doings") or []))
    denom = max(total_chars, say_chars + do_chars, 1)
    dialogue_pct = round(say_chars / denom, 3)
    action_pct = round(do_chars / denom, 3)
    lb.info("style", "B4 对白/描写", say_chars=say_chars, do_chars=do_chars,
            dialogue_pct=dialogue_pct, action_pct=action_pct)

    # ---- B5 反转点: 取经 clue_graph.conclusions ----
    cons = cg.get("conclusions") or []
    reversals = [{"conclusion_id": c.get("id"), "ch": c.get("chapter_no"),
                  "cluster_id": c.get("cluster_id"),
                  "fact": str(c.get("fact", ""))[:80]}
                 for c in cons if c.get("chapter_no")]
    lb.info("style", "B5 反转点", n=len(reversals))

    # ---- B6 角色塑造: 取经 resumes relations/doubts + sayings 密度 ----
    def _top(rs, key, n=3):
        ranked = sorted([r for r in rs if r.get(key)], key=lambda r: -len(r[key]))
        return [{"name": r["name"], "n": len(r[key])} for r in ranked[:n]]
    characterization = {
        "relation_hub": _top(rs, "relations"),
        "doubt_carrier": _top(rs, "doubts"),
    }
    lb.info("style", "B6 角色塑造", hubs=len(characterization["relation_hub"]))

    # ---- B7 意象体系: 取经 settings_graph.terms 分类 ----
    cat_terms = {}
    for t in (sg.get("terms") or []):
        c = t.get("category") or "其他"
        cat_terms.setdefault(c, []).append(t.get("name", ""))
    imagery = {k: v[:20] for k, v in cat_terms.items()}
    lb.info("style", "B7 意象体系", cats=len(imagery))

    # ---- B8 词/句块: L2 内容词过滤(设定词/实体名) + L3 认知动词过滤 ----
    _content = set()
    for _tt in (sg.get("terms") or []):
        _content.add(_tt.get("name", ""))
        for _al in (_tt.get("aliases") or []):
            if _al:
                _content.add(_al)
    for _ee in (cg.get("entities") or []):
        _content.add(_ee.get("name", ""))
    _content.discard("")
    style_freq, content_words = _split_style_words(wf.get("freq") or {}, _content)
    lb.info("style", "B8 词过滤", style_pos=len(style_freq),
            content_words=sum(len(v) for v in content_words.values()))
    word_block = {
        "freq": style_freq,
        "content_words": content_words,
        "total_tokens": wf.get("total_tokens", 0),
        "filtered_generic": wf.get("filtered_generic", 0),
        "imagery": imagery,
    }
    sentence_block = {
        "n_sent": ss.get("n_sent", 0),
        "len_dist": ss.get("len_dist", {}),
        "long_short_ratio": ss.get("long_short_ratio", 0),
        "types": ss.get("types", {}),
        "connector_top": ss.get("connector_top", []),
        "dialogue_pct": dialogue_pct,
        "action_pct": action_pct,
    }

    # ---- B9 综合描述: 唯一 1 次 LLM（强制引用证据） ----
    narrative = {}
    if base is not None or model is not None:
        summary = {
            "词频top(adj)": [x["w"] for x in (word_block["freq"].get("a") or [])[:8]],
            "词频top(v)": [x["w"] for x in (word_block["freq"].get("v") or [])[:8]],
            "长短比": sentence_block["long_short_ratio"],
            "句式": sentence_block["types"],
            "对白占比": dialogue_pct,
            "修辞": {k: v["n"] for k, v in rhetoric.items()},
            "情绪曲线要点": (arc[:3] if len(arc) <= 6 else
                          [arc[0], arc[len(arc)//3], arc[2*len(arc)//3], arc[-1]]),
            "反转点": [{"ch": r["ch"], "fact": r["fact"]} for r in reversals[:5]],
            "蒙太奇top3": montage[:3],
            "角色枢纽": characterization["relation_hub"][:3],
        }
        try:
            user = (
                "以下是这本书文风四维的**机器聚合结果**(词/句/段/篇, 全部来自已抽取产物):\n"
                + json.dumps(summary, ensure_ascii=False)[:3000] +
                "\n\n请写一段**文风叙事总结**(\"fact\"): 综合节奏/用词/修辞/情绪曲线/反转铺设, "
                "**必须引用具体章号或结论编号作为证据**, 禁止空泛词。"
                "再给\"chain\": 你依据哪些聚合数据得出这些结论, 2-3句。\n"
                '输出 JSON: {"fact": "...", "chain": "..."}')
            raw = llm_client.chat(STYLE_SYSTEM, user, json_mode=True,
                                  num_predict=600, temperature=0.4)[0]
            d = json.loads(raw)
            narrative = {"fact": str(d.get("fact", "")).strip(),
                         "chain": str(d.get("chain", "")).strip()}
        except Exception as e:
            lb.error("style", "B9 综合描述失败", err=str(e)[:200])

    out = {
        "schema": 2,
        "sampled_chars": total_chars,
        "word": word_block,
        "sentence": sentence_block,
        "para": {"rhetoric": rhetoric, "foil": foil[:10]},
        "chapter": {"arc": arc, "montage": montage[:20],
                    "reversals": reversals, "characterization": characterization},
        "narrative": narrative,
        "sources": ["scenes_meta", "word_freq", "sentence_stats",
                    "character_facts", "characters_resume", "clue_graph", "settings_graph"],
    }
    lb.info("style", "文风四维聚合完成", elapsed=round(_t.time() - t0, 1),
            narrative=bool(narrative))
    return out



    import argparse
    p = argparse.ArgumentParser(description="文风采样器(独立于线索图谱)")
    p.add_argument("--chapters", type=int, default=None)
    p.add_argument("--ratio", type=float, default=0.1, help="采样比例(默认0.1=10%)")
    p.add_argument("--seed", type=int, default=None, help="随机种子(留空则随机)")
    p.add_argument("--out", default=None, help="输出JSON路径")
    a = p.parse_args()
    corpus = sample_style_text(chapters=a.chapters, ratio=a.ratio, seed=a.seed)
    print("采样: %d 段, %d 字 (长句 %d / 短句 %d)" % (
        len(corpus["sampled"]), corpus["chars"],
        corpus["long_chars"], corpus["short_chars"]))
    res = analyze_style(corpus, None, None)
    if res:
        print("文风:", res["fact"][:200])
        out = a.out or os.path.join(C.STAGE1_DIR, "style_analysis.json")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print("已写入:", out)
    else:
        print("文风分析失败(检查 LLM 后端)")


if __name__ == "__main__":
    main()
