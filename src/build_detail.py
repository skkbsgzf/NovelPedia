"""
build_detail.py ——构建「拆书详情页」全量数据(detail_data.json)

数据源
  - data/stage1_v2_50.db  : paragraphs(原文50章 / chapters(章标题 / scenes(场景)
  - data/llm_50/cloud_fixed/ : characters / settings / outlines
  - outputs/<小说名_<日期>/personality.json : 性格六维向量

产出 detail_data.json:
  book        : 书名/总章数文风分析范围
  chapters    : [{no,title}] 全局筛选器
  timeline    : [{no, events:[{text, who}]}] 时间轴由人物关键事件归并
  outlines    : [{no,title,summary,scenes,chars,foreshadow}] 章纲
  tags        : {ch: {newChars:[], newSettings:[], newScenes:[]}} 三色浮点
  characters  : 人物画像(含dims 六维)
  charGraph   : 人物关系图谱 nodes/edges (显式关系 + 场景共现)
  settings    : 设定(含category 分类: 世界观势力/物品/其他)
  style       : 文风统计(词频/句长/标点/特殊句式/修辞)
  perChStyle  : [{no, chars, avgLen, words: top25, sentence, punctuation, special, rhetoric}] 每章文风(支撑章节范围筛选

产物: detail_data.json + book_detail.html (模板注入自包含
"""
import os, re, json, sqlite3, sys, argparse
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C
import vizutil

BASE = C.STAGE2_DIR   # stage2 产物(characters/settings/outlines/personality)在 outputs/<书>_<日期>/stage2/

ap = argparse.ArgumentParser()
ap.add_argument("--src", default=C.RUN_SRC_DIR, help="Stage2 产物目录（local|cloud|cloud_fixed）")
ap.add_argument("--db", default=C.DB_PATH, help="stage1 数据库路径（按小说隔离）")
ap.add_argument("--book-name", default="", help="书名; 留空则从正文前言自动推断")
ap.add_argument("--out", default="", help="detail_data.json 输出路径; 留空用默认")
A = ap.parse_args()
# 新结构: stage2 产物直接在 STAGE2_DIR 下(--src 不再拼子目录)
# 兼容旧路径: 若显式传 --src 且该路径存在, 则用之
if A.src and A.src != C.RUN_SRC_DIR and os.path.isdir(A.src):
    SRC = A.src
else:
    SRC = BASE
DB = A.db
OUT = os.path.join(C.OUTPUT_DIR, "detail_data.json") if not A.out else A.out
BOOK_NAME = A.book_name or C.NOVEL_NAME

# ---------------- 1. 读db ----------------
db = sqlite3.connect(DB)
cur = db.cursor()
chapters = [{"no": r[0], "title": r[1]} for r in cur.execute(
    "SELECT chapter_no, title FROM chapters ORDER BY chapter_no")]
N_CH = len(chapters)
# 原文按章
para_by_ch = defaultdict(list)
for ch, text in cur.execute("SELECT chapter_no, text FROM paragraphs"):
    para_by_ch[ch].append(text)
full_text = "\n".join(t for ch in range(1, N_CH + 1) for t in para_by_ch.get(ch, []))

# 书名推断: 优先命令行参数, 否则取正文前言(preamble)里第一条短标题（如"诡秘之主"）
if not BOOK_NAME:
    pre = [t.strip() for ch, t in cur.execute(
        "SELECT chapter_no, text FROM paragraphs WHERE chapter_no=0 ORDER BY para_id")
        if t and t.strip()]
    for t in pre:
        t = t.replace("\ufeff", "").strip()
        if 2 <= len(t) <= 10 and not re.search(r"章|作者|：|:", t):
            BOOK_NAME = t
            break
    if not BOOK_NAME and chapters:
        BOOK_NAME = chapters[0]["title"].split(" ")[0]
db.close()

# ---------------- 2. 读stage2 产物 ----------------
def load(p, default=None):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return default if default is not None else {}

raw_chars = load(os.path.join(SRC, "characters.json"), [])
sets_ = load(os.path.join(SRC, "settings.json"), [])
outlines = load(os.path.join(SRC, "outlines.json"), {})
personality = load(os.path.join(C.OUTPUT_DIR, "personality.json"), [])

# 同人多条目归并: 按 aliases 无向图求连通分量, 得到去重人物主表
# (原数据把 邓恩/邓恩·史密斯/马车夫、阿尔杰/阿尔杰·威尔逊 等拆成多条)
chars, name2canon = vizutil.merge_alias_components(raw_chars)
personality = vizutil.merge_personality(personality, name2canon)
pdim = {p.get("name"): p.get("dims", {}) for p in personality}

# ---------------- 3. 文风统计 ----------------
STOP = set("""的 了 是 我我们 你们 他们 她们 它它们 这 那 在 有 和 着 或一个没有 什么这个 那个 自己 时候知道 觉得 但是 可是 因为 所以如果 虽然 然后 现在 已经 还是 只是
不过 就是 但是 于是 又 也 很最 被 把 让 给 向 从 到 对 于 之 其 此 中 上呀 哦 嗯 么 不 没 别 要 会 能 可 想 着 再 次 """.split())

def _tokenize(text):
    """分词: 优先 jieba; 无 jieba 的环境用 2-4 字滑动窗口+ 子串去重兜底 (纯标准库)。"""
    try:
        import jieba
        return [w for w in jieba.cut(text) if len(w) >= 2 and w not in STOP
                and not re.search(r"[，。！？；：、“”‘’（）《》—…\s\dA-Za-z·]", w)]
    except Exception:
        out = []
        for n in (4, 3, 2):
            for i in range(len(text) - n + 1):
                w = text[i:i + n]
                if re.search(r"[，。！？；：、“”‘’（）《》—…\s\dA-Za-z·\ufeff]", w):
                    continue
                if any(c in STOP for c in w):
                    continue
                out.append(w)
        cnt = Counter(out)
        # 长度降序处理, 短词若被已保留的长词包含则丢弃("周明"/"明瑞" 之类碎片)
        kept = []
        for w, n in sorted(cnt.items(), key=lambda kv: (-len(kv[0]), -kv[1])):
            if n < 5 or any(w in k for k, _ in kept):
                continue
            kept.append((w, n))
        return [w for w, _ in kept]

def style_of(text):
    """对一段文本做规则风格统计(词频/句长/标点/句式/修辞)。
    全书与逐章共用同一实现, 保证范围筛选后可聚合。"""
    ws = _tokenize(text)
    wc = Counter(ws)
    word_freq = [{"word": w, "count": n, "pct": round(n / max(1, len(ws)) * 100, 2)}
                 for w, n in wc.most_common(80)]
    sentences = re.split(r"[。！？；]", text)
    sents = [s for s in sentences if len(s.strip()) >= 2]
    lens = [len(re.sub(r"[，、：—“”‘’（）《》…\s]", "", s)) for s in sents]
    total_s = max(1, len(lens))
    avg_len = round(sum(lens) / total_s, 1)
    short_pct = round(sum(1 for l in lens if l < 15) / total_s * 100, 1)
    long_pct = round(sum(1 for l in lens if l > 40) / total_s * 100, 1)
    dist = []
    for lo, hi in [(0, 10), (10, 20), (20, 30), (30, 40), (40, 60), (60, 999)]:
        n = sum(1 for l in lens if lo <= l < hi)
        dist.append({"range": f"{lo}-{hi if hi<999 else '+'}", "count": n})
    punc = Counter(re.findall(r"[，。！？；：—…、]", text))
    punctuation = [{"p": p, "count": punc.get(p, 0)} for p in ["，", "。", "！", "？", "；", "：", "—", "…"]]
    _ENUM = r"(?:一个|有的|没有|需要|有些|某些|各|每|这[^，。！？]{0,6}(?:，[^，。！？]{4,12}){2,})"
    _PARALLEL = r"(?:，[^，。！？]{4,12}){3,}[^，。！？]{0,12}[。！？]"
    special = {
        "反问": len(re.findall(r"难道|岂非|岂能|怎能|何必|何尝|不是.{0,12}吗", text)),
        "感叹": len(re.findall(r"！", text)),
        "排比": sum(1 for m in re.finditer(_PARALLEL, text) if not re.search(_ENUM, m.group())),
    }
    rhetoric = {
        "比喻": len(re.findall(r"像|仿佛|如同|宛如|好似|犹如", text)),
        "拟人": 0, "夸张": 0,
    }
    return {
        "wordFreq": word_freq,
        "sentence": {"avg": avg_len, "shortPct": short_pct, "longPct": long_pct,
                     "dist": dist, "total": len(lens)},
        "punctuation": punctuation,
        "special": special,
        "rhetoric": rhetoric,
    }

style_full = style_of(full_text)
style = {
    "scope": f"共抽取{len(chapters)} 章（第1-{len(chapters)} 章，全文 {len(full_text):,} 字）",
    "wordFreq": style_full["wordFreq"],
    "sentence": style_full["sentence"],
    "punctuation": style_full["punctuation"],
    "special": style_full["special"],
    "rhetoric": style_full["rhetoric"],
    "note": "词频基于 jieba 分词(去停用词)；句长按。！？；切分(去除标点后计字数)；反问感叹/排比/比喻为规则粗判（排比已剔除「一个…一个…」类枚举式，但非精确修辞学判定，数值仅作风格参考），拟人与夸张暂无可靠规则（待 LLM 抽样复核）。",
}

# ---------------- 4. 时间轴 人物关键事件归并到章 ----------------
def ch_of(text):
    m = re.search(r"第\s*(\d+)\s*(?:-|—|至|~)?\s*(\d+)?\s*章", text)
    if not m:
        return None
    return int(m.group(1))

timeline = defaultdict(list)
for c in chars:
    nm = c.get("name", "")
    for ev in (c.get("关键事件") or []):
        cn = ch_of(ev)
        if cn and 1 <= cn <= len(chapters):
            body = re.sub(r"^第\s*[\d\-—至~]+\s*章[:：]?\s*", "", ev)
            timeline[cn].append({"text": body, "who": nm})
timeline = [{"no": cn, "events": timeline.get(cn, [])} for cn in range(1, len(chapters) + 1)]

# ---------------- 5. 章纲 + 三色浮点标签 ----------------
new_chars = defaultdict(list)      # 绿 本章引入人物
for c in chars:
    fs = c.get("首次出现章")
    if fs and 1 <= fs <= len(chapters):
        new_chars[fs].append(c.get("name", ""))

new_sets = defaultdict(list)       # 蓝 本章展开设定
for s in sets_:
    fs = s.get("first_seen")
    if fs and 1 <= fs <= len(chapters):
        new_sets[fs].append(s.get("name", ""))

seen_scenes = set()
new_scenes = defaultdict(list)     # 橙 本章新场景
for cn in range(1, len(chapters) + 1):
    ol = outlines.get(str(cn)) or outlines.get(cn) or {}
    for sc in (ol.get("场景") or []):
        sc = str(sc).strip()
        if sc and sc not in seen_scenes:
            seen_scenes.add(sc)
            new_scenes[cn].append(sc)

tags = {}
for cn in range(1, len(chapters) + 1):
    tags[cn] = {
        "newChars": new_chars.get(cn, []),
        "newSettings": new_sets.get(cn, []),
        "newScenes": new_scenes.get(cn, []),
    }

outline_list = []
for cn in range(1, len(chapters) + 1):
    ol = outlines.get(str(cn)) or outlines.get(cn) or {}
    outline_list.append({
        "no": cn,
        "title": chapters[cn - 1]["title"],
        "summary": ol.get("主线") or "",
        "scenes": ol.get("场景") or [],
        "chars": ol.get("出场人物") or [],
        "foreshadow": ol.get("伏笔备注") or "",
    })

# ---------------- 6. 人物画像 ----------------
char_list = []
for c in chars:
    char_list.append({
        "name": c.get("name", ""),
        "aliases": c.get("aliases") or [],
        "身份": c.get("身份") or "",
        "首次出现章": c.get("首次出现章"),
        "关键事件": c.get("关键事件") or [],
        "关系": c.get("关系") or [],
        "弧光": c.get("弧光") or "",
        "dims": pdim.get(c.get("name", ""), {}),
    })

# ---------------- 7. 设定分类 ----------------
TYPE_CATEGORY = {
    "组织": "势力", "物品": "物品",
    "概念": "世界观", "力量": "世界观", "地点": "世界观",
    "人物身份": "世界观",
}
CAT_ORDER = ["世界观", "势力", "物品", "其他"]
settings_list = []
for s in sets_:
    cat = TYPE_CATEGORY.get(s.get("type"), "其他")
    settings_list.append({
        "name": s.get("name", ""), "type": s.get("type", ""),
        "category": cat, "description": s.get("description") or "",
        "first_seen": s.get("first_seen"), "related": s.get("related") or [],
        "note": s.get("note") or "",
    })

# ---------------- 7b. 人物关系图谱 (显式关系 + 场景共现) ----------------
# 别名 -> 主名
alias2main = {}
for c in chars:
    main = str(c.get("name", "")).strip()
    alias2main[main] = main
    for al in (c.get("aliases") or []):
        alias2main[str(al).strip()] = main

char_ids = {c["name"]: f"c:{c['name']}" for c in char_list}
# 角色章节归属: 按 scenes.who_json 反查(供前端按章筛选图谱节点)
char_chapters = defaultdict(set)
_db3 = sqlite3.connect(DB)
for _cn, _wj in _db3.execute(
        "SELECT chapter_no, who_json FROM scenes WHERE who_json IS NOT NULL"):
    try:
        _who = json.loads(_wj or "[]")
    except Exception:
        _who = []
    for _w in _who:
        _nm = str((_w or {}).get("name", "")).strip()
        if _nm:
            _canon = alias2main.get(_nm, _nm)
            if _canon in char_ids:
                char_chapters[_canon].add(_cn)
_db3.close()

rel_edges, co_edges = [], []
# 显式关系: "对方名：描述"
for c in char_list:
    src = c["name"]
    for r in (c.get("关系") or []):
        m = re.match(r"^([^:：]{1,12})[:：]\s*(.*)$", str(r).strip())
        if not m:
            continue
        tgt_raw, desc = m.group(1).strip(), m.group(2).strip()
        tgt = alias2main.get(tgt_raw)
        if tgt and tgt != src and tgt in char_ids:
            rel_edges.append({"from": char_ids[src], "to": char_ids[tgt],
                              "label": desc[:24], "kind": "relation"})
# 场景共现: 同一场景 who 两两成边
db2 = sqlite3.connect(DB)
who_rows = db2.execute("SELECT who_json FROM scenes WHERE who_json IS NOT NULL").fetchall()
db2.close()
co_pair = defaultdict(int)
for (wj,) in who_rows:
    try:
        who = json.loads(wj or "[]")
    except Exception:
        who = []
    names = []
    for w in who:
        nm = str((w or {}).get("name", "")).strip()
        if nm:
            names.append(alias2main.get(nm, nm))
    names = [n for n in names if n in char_ids]
    names = list(dict.fromkeys(names))
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            co_pair[tuple(sorted((names[i], names[j])))] += 1
for (a, b), n in co_pair.items():
    if n >= 2:  # 至少共现 2 次才建边, 控密度
        co_edges.append({"from": char_ids[a], "to": char_ids[b],
                         "label": f"同场景×{n}", "kind": "cooccur"})
char_graph = {
    "nodes": [{"id": char_ids[c["name"]], "name": c["name"],
               "aliases": c["aliases"], "first": c["首次出现章"],
               "chapters": sorted(char_chapters.get(c["name"], [])),
               "scenes": 0, "color": "#4f7cff"} for c in char_list],
    "edges": rel_edges + co_edges,
}

# ---------------- 7c. 每章文风统计 (支撑章节范围筛选联动 ----------------
per_ch_style = []
for cn in range(1, len(chapters) + 1):
    text = "\n".join(para_by_ch.get(cn, []))
    n_chars = len(text)
    st = style_of(text)
    per_ch_style.append({"no": cn, "chars": n_chars, "avgLen": st["sentence"]["avg"],
                         "words": st["wordFreq"][:25],
                         "sentence": st["sentence"], "punctuation": st["punctuation"],
                         "special": st["special"], "rhetoric": st["rhetoric"]})

# ---------------- 8. 汇总写出----------------
data = {
    "book": {
        "title": BOOK_NAME or (chapters[0]["title"].split(" ")[0] if chapters else ""),
        "totalChapters": len(chapters),
        "styleScope": style["scope"],
    },
    "chapters": chapters,
    "timeline": timeline,
    "outlines": outline_list,
    "tags": tags,
    "characters": char_list,
    "charGraph": char_graph,
    "settings": settings_list,
    "style": style,
    "perChStyle": per_ch_style,
}

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False)

# ---------------- 9. 注入模板生成 book_detail.html ----------------
TPL = os.path.join(os.path.dirname(__file__), "detail_template.html")
HTML_OUT = os.path.join(os.path.dirname(OUT), "book_detail.html")
with open(TPL, encoding="utf-8") as f:
    html = f.read()
js_safe = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
html = html.replace("__DETAIL_DATA__", js_safe)
if "__DETAIL_DATA__" in html:
    print(f"⚠️ 模板占位符未完全替换!")
with open(HTML_OUT, "w", encoding="utf-8") as f:
    f.write(html)
print(f"已生成{HTML_OUT} ({os.path.getsize(HTML_OUT)/1024:.0f} KB)")

# 摘要输出
cat_count = Counter(s["category"] for s in settings_list)
print(f"  章节: {len(chapters)} | 时间轴事件 {sum(len(t['events']) for t in timeline)} | 章纲: {len(outline_list)}")
print(f"  人物: {len(char_list)} | 图谱: {len(char_graph['nodes'])} 节点 {len(char_graph['edges'])} 边)")
print(f"  设定: {len(settings_list)} | 分类: {dict(cat_count)}")
print(f"  文风: 词{len(style_full['wordFreq'])} 个| 句{style_full['sentence']['total']} 句| 均长 {style_full['sentence']['avg']} | 短句 {style_full['sentence']['shortPct']}% | 长句 {style_full['sentence']['longPct']}%")
print(f"  句式: {style_full['special']} | 比喻: {style_full['rhetoric']['比喻']}")
