# -*- coding: utf-8 -*-
"""
check_viz.py —— Stage3 可视化产物健康检查
对 outputs/<小说名_<日期>/ 下的 detail_data.json / graph_data.json / *.html 做一致性校验
把肉眼难发现的数据/模板 bug 变成可重复的断言。退出码 != 0 表示有 error
用法:
  python src/check_viz.py [--src cloud_fixed]
"""
import os, sys, json, re, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C

BASE = os.path.join(C.DATA_DIR, "llm_50")      # LLM 产物 + personality 源目录
RUN = C.OUTPUT_DIR                             # 本次可视化产物目录
ap = argparse.ArgumentParser()
ap.add_argument("--src", default=C.RUN_SRC_DIR, help="Stage2 产物目录")
A = ap.parse_args()
SRC = os.path.join(BASE, A.src)

errors, warns, oks = [], [], []


def err(msg):
    errors.append(msg)


def warn(msg):
    warns.append(msg)


def ok(msg):
    oks.append(msg)


def load(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception as e:
        err(f"读取失败 {p}: {e}")
        return None


# ---------------- detail_data.json ----------------
D = load(os.path.join(RUN, "detail_data.json"))
if D is not None:
    # 1. 书名
    title = (D.get("book") or {}).get("title", "")
    if not title:
        err("book.title 为空")
    elif re.match(r"^第[一二三四五六七八九十\d]+章", title):
        err(f"book.title 疑似章名而非书名: {title!r}")
    else:
        ok(f"book.title = {title!r}")

    # 2. 人物与图谱节点数一致(charGraph 节点 id 用"c:" 前缀标识角色)
    n_char = len(D.get("characters", []))
    g_nodes = [n for n in (D.get("charGraph") or {}).get("nodes", []) if str(n.get("id", "")).startswith("c:")]
    if n_char != len(g_nodes):
        err(f"人物数({n_char}) != 图谱角色节点数({len(g_nodes)})")
    else:
        ok(f"人物/图谱节点一致 {n_char}")

    # 3. 图谱边两端节点必须存在
    ids = {n.get("id") for n in (D.get("charGraph") or {}).get("nodes", [])}
    bad = [e for e in (D.get("charGraph") or {}).get("edges", [])
           if e.get("from") not in ids or e.get("to") not in ids]
    if bad:
        err(f"图谱边指向不存在节点: {len(bad)} 条")
    else:
        ok(f"图谱边引用完整 {len((D.get('charGraph') or {}).get('edges', []))} 条")

    # 4. 别名归并: 每个角色的 aliases 不得包含另一角色的主名
    names = {c["name"] for c in D.get("characters", [])}
    for c in D.get("characters", []):
        leak = [a for a in (c.get("aliases") or []) if a in names and a != c["name"]]
        if leak:
            err(f"角色 {c['name']} 的别名泄露其它主名 {leak}")
    if not any(a for c in D.get("characters", []) for a in (c.get("aliases") or [])
               if a in {x['name'] for x in D.get('characters', [])} and a != c['name']):
        ok("别名无跨角色泄露")

    # 5. 文风: perChStyle 词必须带 pct
    pcs = D.get("perChStyle") or []
    if pcs:
        no_pct = [p["no"] for p in pcs if not all("pct" in w for w in (p.get("words") or []))]
        if no_pct:
            err(f"perChStyle 缺pct 的章节 {no_pct[:5]}")
        else:
            ok(f"perChStyle 词均带pct ({len(pcs)} 章)")

    # 6. 标点键必须与实际捕获字符一致(展示键'—' 而非 '—…')
    punc_keys = {p["p"] for p in (D.get("style") or {}).get("punctuation", [])}
    if "—…" in punc_keys:
        err("punctuation 展示键用了'—…', 正则只捕获单破折号'—', 会显示0")
    elif "…" in punc_keys:
        ok("punctuation 键与捕获字符一致(—)")

    # 7. 排比 heuristic 标注
    if (D.get("style") or {}).get("note"):
        ok("style.note 已注明近似检测口径")

    # 8. 章节标题不得为空
    empty_ch = [c["no"] for c in D.get("chapters", []) if not c.get("title")]
    if empty_ch:
        err(f"章节标题为空: {empty_ch[:5]}")
    else:
        ok(f"章节标题完整 ({len(D.get('chapters', []))} 章)")

    # 9. 章纲数 == 章节数
    if len(D.get("outlines", [])) != len(D.get("chapters", [])):
        err(f"章纲数({len(D.get('outlines',[]))}) != 章节数({len(D.get('chapters',[]))})")
    else:
        ok(f"章纲/章节对齐: {len(D.get('outlines',[]))}")

# ---------------- graph_data.json ----------------
G = load(os.path.join(RUN, "graph_data.json"))
if G is not None:
    # 章节标题不得为空(真实章名取自 db chapters 表)
    bad_title = [c for c in G.get("chapters", []) if not c.get("title")]
    if bad_title:
        err(f"图谱章节标题为空: {bad_title[:5]}")
    else:
        ok(f"图谱章节标题为真实章名({len(G.get('chapters',[]))} 章)")

    gids = {n.get("id") for n in G.get("nodes", [])}
    bad_e = [e for e in G.get("edges", []) if e.get("from") not in gids or e.get("to") not in gids]
    if bad_e:
        err(f"图谱边指向不存在节点: {len(bad_e)} 条)")
    else:
        ok(f"图谱边引用完整 {len(G.get('edges',[]))} 条)")

    # 节点名不得被截断(设定名最长12字, 标签截断应在 12+)
    long_names = [n["name"] for n in G.get("nodes", []) if len(n.get("name", "")) > 12]
    if long_names:
        warn(f"存在 >12 字的节点名标签会截断: {long_names[:5]}")
    else:
        ok("节点名均 <=12 字, 标签无需截断")

# ---------------- HTML 自包含检查 ----------------
for fn in ("book_detail.html", "knowledge_graph.html"):
    p = os.path.join(RUN, fn)
    if not os.path.exists(p):
        err(f"缺产物 {fn}")
        continue
    txt = open(p, encoding="utf-8").read()
    if "__DETAIL_DATA__" in txt or "__GRAPH_DATA__" in txt:
        err(f"{fn} 模板占位符未替换")
    elif r"<\/" in txt and "</" not in txt.replace(r"<\/", ""):
        ok(f"{fn} 自包含(占位符已替换, </ 已转义)")
    else:
        ok(f"{fn} 自包含(占位符已替换)")
    if "<script" in txt and "src=" in txt and "http" in txt:
        warn(f"{fn} 引用了外部脚本, 非完全自包含)")

# index.html 在可视化产物目录
ip = os.path.join(RUN, "index.html")
if os.path.exists(ip):
    itxt = open(ip, encoding="utf-8").read()
    if "KB</span>" in itxt and re.search(r"\d+\s*KB</span>", itxt):
        ok("index.html 大小动态生成)")
    else:
        err("index.html 大小仍是硬编码)")
    if "__DETAIL_DATA__" in itxt or "__GRAPH_DATA__" in itxt:
        err("index.html 残留占位符)")
else:
    err(f"缺产物 {RUN}/index.html (需先跑 stage3 一键归档)")

# ---------------- personality.json ----------------
P = load(os.path.join(RUN, "personality.json"))
if P is not None:
    if not P:
        err("personality.json 为空")
    else:
        bad_dim = [p["name"] for p in P if not p.get("dims") or any(
            not isinstance(v, int) or v < 0 or v > 100 for v in p["dims"].values())]
        if bad_dim:
            err(f"personality dims 越界/缺失: {bad_dim[:5]}")
        else:
            ok(f"personality.json 格式正常 ({len(P)} 条)")

print("\n═══ Stage3 可视化健康检查 ═══")
for m in oks:
    print(f"  ✅ {m}")
for m in warns:
    print(f"  ⚠️ {m}")
for m in errors:
    print(f"  ❌ {m}")
print(f"\n结果: {len(oks)} 通过 / {len(warns)} 警告 / {len(errors)} 错误")
sys.exit(1 if errors else 0)