# -*- coding: utf-8 -*-
"""webnovel_lexicon.py —— 网文设定体系「图 RAG」层(公版·正向知识库)。

定位: 作用在整个 pipeline 的**可检索图**, 不是孤立词典。与两件事互补:
  - generic_lexicon.json(负向停用词): 通用词 -> 拦截不入图谱("及时扔掉不必要的设定")
  - 本文知识库(settings_graph.json / clue_graph.json): 本次跑书实例 -> 与公版图互链

图上有什么:
  node = 体系词条(如 炼气/斗帝/序列9/主神空间), 带 domain(域) + slot(槽位) + rank(等级序号, 仅 power_levels 有)
  edge = next_level(同体系相邻等级, 有向) / in_domain(域内归属) / aligned_with(跨体系对齐, 可跨书迁移)

四个核心能力:
  1. search(q)            高效匹配(精确>前缀>子串>可选语义), 给 pipeline 任何环节做"这是什么体系"的判定
  2. match_terms(names)   批量把本文词条映射到公版节点 -> {domain, slot, rank}, 写入 term.wn_* 字段互链
  3. suggest_missing()    公版序列 vs 本文已有 -> 找出缺档, 提示回原文补全("快速补全")
  4. neighbors(name)     图上 n 跳扩展, 供 RAG 检索时把体系上下文一起召回

数据: src/webnovel_lexicon.json(人工维护词表 + slot_meta + alignment_pairs), 图结构在加载时**派生**生成,
      改词表即改图, 不维护两份数据。
"""
import json
import os
import re

_LEX = None
_GRAPH = None
# 加载作用域(★默认不对外开放): enabled=False 时任何查询都返回空, pipeline 不受 global 影响;
# 只有显式 set_scope(enabled=True, domains=[...]) 才按域加载, 且结果仅作参考。
_SCOPE = {"enabled": False, "domains": None}  # {'nodes':[...], 'edges':[...], 'by_id':{id:node}, 'adj':{id:[(nb,type)]}}


# ======================================================================
# 加载与图构建
# ======================================================================
def load_webnovel_lexicon():
    """加载词表(模块级缓存)。文件缺失降级为空, 不阻塞主流程。"""
    global _LEX
    if _LEX is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webnovel_lexicon.json")
        try:
            with open(path, encoding="utf-8") as f:
                _LEX = json.load(f)
        except Exception as e:
            print(f"[warn] 网文体系知识库加载失败({e}), 图 RAG 降级为不可用")
            _LEX = {"domains": {}}
    return _LEX


def graph():
    """从词表派生图结构(懒构建 + 缓存)。边规则:
       - power_levels 内相邻等级 -> next_level(有向, 弱→强)
       - 节点 -> 所属 domain 中心 -> in_domain
       - alignment_pairs 同行(非空) -> aligned_with(全连接, 跨体系对齐)
    """
    global _GRAPH
    if _GRAPH is not None:
        return _GRAPH
    lex = load_webnovel_lexicon()
    nodes, edges, by_id = [], [], {}

    for domain, slots in (lex.get("domains") or {}).items():
        # 域中心节点
        dom_id = "domain:" + domain
        nodes.append({"id": dom_id, "domain": domain, "slot": "_domain", "rank": None, "center": True})
        by_id[dom_id] = nodes[-1]
        for slot, words in (slots or {}).items():
            if not isinstance(words, list):
                continue
            meta = (lex.get("slot_meta") or {}).get(slot, {})
            ordered = bool(meta.get("ordered"))
            seq = []
            for i, w in enumerate(words):
                if not (isinstance(w, str) and w.strip()):
                    continue
                w = w.strip()
                if w in by_id:
                    continue
                node = {"id": w, "domain": domain, "slot": slot,
                        "rank": (i + 1) if ordered else None, "center": False}
                nodes.append(node)
                by_id[w] = node
                seq.append(w)
                edges.append({"a": dom_id, "b": w, "type": "in_domain"})
            if ordered:
                for i in range(len(seq) - 1):
                    edges.append({"a": seq[i], "b": seq[i + 1], "type": "next_level"})

    # 跨体系对齐边
    for row in (lex.get("alignment_pairs") or []):
        row = [x for x in row if isinstance(x, str) and x.strip() and x.strip() in by_id]
        for i in range(len(row)):
            for j in range(i + 1, len(row)):
                edges.append({"a": row[i], "b": row[j], "type": "aligned_with"})

    adj = {}
    for e in edges:
        adj.setdefault(e["a"], []).append((e["b"], e["type"]))
        adj.setdefault(e["b"], []).append((e["a"], e["type"]))

    _GRAPH = {"nodes": nodes, "edges": edges, "by_id": by_id, "adj": adj}
    return _GRAPH


# ======================================================================
# 0) 加载作用域控制(global 库默认不开放)
# ======================================================================
def set_scope(domains=None, enabled=None):
    """设置加载作用域(经 config_schema 注册表持久化, 与 CLI --genre 统一)。
      enabled=False(默认): 全库关闭, 所有查询返回空 —— pipeline 完全不使用 global 知识
      domains=['修真仙侠']: 只开放指定域(可多个)
      domains=None + enabled=True: 全域开放(谨慎, 仅调试用)
    """
    import config_schema as _CS
    if enabled is not None:
        _SCOPE["enabled"] = bool(enabled)
    if domains is not None:
        _SCOPE["domains"] = set(domains) if domains else None
    _CS.set("knowledge.scope", {"enabled": _SCOPE["enabled"],
                                "domains": sorted(_SCOPE["domains"]) if _SCOPE["domains"] else None})
    return get_scope()


def get_scope():
    return {"enabled": _SCOPE["enabled"],
            "domains": sorted(_SCOPE["domains"]) if _SCOPE["domains"] else None}


def peek(name):
    """★探测(不受作用域限制): 只看某个词是否属于某个已知体系, 不意味着加载该体系。
    仅供 KnowledgeRouter 的"观察阶段"使用 —— 观察 ≠ 加载, 这样默认关闭的策略才不失效。"""
    if not name:
        return None
    node = graph()["by_id"].get(str(name).strip())
    if not node or node.get("center"):
        return None
    return {"domain": node["domain"], "slot": node["slot"], "rank": node["rank"]}


def domains_of(names):
    """批量探测: 返回 {域名: 命中词条数}(观察用, 不受作用域限制)。"""
    hits = {}
    for n in names or []:
        r = peek(n)
        if r:
            hits[r["domain"]] = hits.get(r["domain"], 0) + 1
    return hits


def _in_scope(domain):
    if not _SCOPE["enabled"]:
        return False
    if _SCOPE["domains"] is None:
        return True
    return domain in _SCOPE["domains"]


def resolve_genre(genre):
    """类别别名 -> 标准域名。如 '宫斗'->'宫廷权谋', '修仙'->'修真仙侠'。未命中原样返回。"""
    if not genre:
        return None
    g = str(genre).strip()
    lex = load_webnovel_lexicon()
    aliases = lex.get("genre_aliases") or {}
    if g in aliases:
        return aliases[g]
    if g in (lex.get("domains") or {}):
        return g
    # 宽松匹配: 别名中包含
    for k, v in aliases.items():
        if k in g or g in k:
            return v
    return g


def detect_genre(sample_names, topk=3):
    """用本书样本词条"先搜索查类别": 临时全域检索, 统计各域命中数, 返回候选域。
    ★ 只做类别判定, 不改变当前作用域 —— 判定完仍需显式 set_scope 才会真正加载。"""
    hits = {}
    for n in sample_names or []:
        node = graph()["by_id"].get(str(n).strip())
        if node and not node.get("center"):
            hits[node["domain"]] = hits.get(node["domain"], 0) + 1
    ranked = sorted(hits.items(), key=lambda x: -x[1])
    return [{"domain": d, "hits": c} for d, c in ranked[:topk]]


# ======================================================================
# 1) 高效匹配检索
# ======================================================================
def lookup_term(name):
    """精确查询单个词条 -> {domain, slot, rank} 或 None。
    ★ 未开放(默认)或该域不在作用域内时一律返回 None —— global 库不污染本文抽取。"""
    if not name or not _SCOPE["enabled"]:
        return None
    n = str(name).strip()
    node = graph()["by_id"].get(n)
    if not node or not _in_scope(node["domain"]):
        return None
    return {"domain": node["domain"], "slot": node["slot"],
            "rank": node["rank"], "term": node["id"]}


def search(query, topk=10, embed_fn=None):
    """分层匹配: 精确 -> 前缀 -> 子串 -> (可选) embedding 语义。返回 [{term,domain,slot,rank,score,how}]。
    embed_fn(texts)->list[vec], 传入则子串无果时用语义兜底(复用 rag.py 的 bge-m3 批量接口)。"""
    q = str(query or "").strip()
    if not q:
        return []
    g = graph()
    by_id, out = g["by_id"], []
    seen = set()

    def push(t, score, how):
        if t in seen or not by_id.get(t):
            return
        # 作用域过滤(默认关闭 -> 不返回任何结果)
        if not _in_scope(by_id[t].get("domain")):
            return
        seen.add(t)
        n = by_id[t]
        out.append({"term": t, "domain": n["domain"], "slot": n["slot"],
                    "rank": n["rank"], "score": score, "how": how})

    # L1 精确
    push(q, 1.0, "exact")
    # L2 前缀
    for t in by_id:
        if t.startswith("domain:"):
            continue
        if t != q and (t.startswith(q) or q.startswith(t)):
            push(t, 0.8, "prefix")
        if len(out) >= topk * 3:
            break
    # L3 子串
    for t in by_id:
        if t.startswith("domain:"):
            continue
        if t not in seen and (q in t or t in q):
            push(t, 0.6, "substring")
        if len(out) >= topk * 3:
            break
    # L4 编辑距离<=1(纯标准库, 覆盖'练气/炼气'这类形近/谐音错字; 无需 embedding)
    if len(out) < topk and len(q) >= 2:
        for t in by_id:
            if t.startswith("domain:") or t in seen:
                continue
            if abs(len(t) - len(q)) <= 1 and _edit_le(q, t, 1):
                push(t, 0.45, "edit1")
            if len(out) >= topk * 3:
                break
    out.sort(key=lambda x: -x["score"])
    return out[:topk]


def _edit_le(a, b, max_d):
    """编辑距离是否 <= max_d(早停优化, 只需判定不需精确值)。"""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > max_d:
        return False
    # 短串上的 O(n*m) DP, 词表词长 <=6, 成本可忽略
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1,
                         prev[j - 1] + (a[i - 1] != b[j - 1]))
        if min(cur) > max_d:
            return False
        prev = cur
    return prev[lb] <= max_d


# ======================================================================
# 2) 本文词条 -> 公版图 批量映射(双库关联)
# ======================================================================
def match_terms(names):
    """批量映射: {name: {domain,slot,rank}} (仅返回命中项)。
    pipeline 用它给 term 打 wn_domain/wn_slot/wn_rank 字段, 完成"公版图 ↔ 本文知识库"互链。"""
    res = {}
    for n in names or []:
        r = lookup_term(n)
        if r:
            res[n] = r
    return res


def annotate_terms(terms, fields=("wn_domain", "wn_slot", "wn_rank")):
    """同 match_terms, 就地打标。★注意: 结果全部存入 wn_* 字段, 仅作"网文体系参考",
    绝不覆盖本文抽到的 definition/category —— 与原文冲突时以本次阅读到的为准。"""
    """就地给 settings_graph 的 terms 列表打标(返回被标注的数量)。
    幂等: 重复调用只覆盖, 不产生副作用。"""
    fd, fs, fr = fields
    n = 0
    for t in terms or []:
        r = lookup_term(t.get("name"))
        if r:
            t[fd], t[fs], t[fr] = r["domain"], r["slot"], r["rank"]
            n += 1
        else:
            # 未命中 -> 清空旧标记, 保证重跑后状态一致
            t.pop(fd, None); t.pop(fs, None); t.pop(fr, None)
    return n


# ======================================================================
# 3) 补档检测(快速补全)
# ======================================================================
def suggest_missing(book_terms, domain=None, max_suggest=20):
    """本文已抽到的等级 vs 公版序列 -> 找出**中间缺档**。
    例: 本文只有[炼气, 元婴], 公版修真序列 -> 缺[筑基, 金丹], 提示回原文找。
    book_terms: 本文词条名集合/列表; domain: 限定域, 不传则扫全部有序体系。"""
    g = graph()
    have = set(book_terms or [])
    sug = []
    for dom, slots in (load_webnovel_lexicon().get("domains") or {}).items():
        if domain and dom != domain:
            continue
        seq = [w for w in (slots.get("power_levels") or []) if isinstance(w, str)]
        if not seq:
            continue
        hits = [i for i, w in enumerate(seq) if w in have]
        if len(hits) < 2:
            continue  # 本文命中不足 2 档, 无法判断缺档
        lo, hi = min(hits), max(hits)
        missing = [w for i, w in enumerate(seq) if lo <= i <= hi and w not in have]
        if missing:
            sug.append({"domain": dom, "have": [seq[i] for i in hits],
                        "missing": missing[:max_suggest], "span": [seq[lo], seq[hi]]})
    return sug


# ======================================================================
# 4) 图上 n 跳扩展(供 RAG 召回体系上下文)
# ======================================================================
def neighbors(name, hops=1, edge_types=None):
    """图上 n 跳邻居, 用于 RAG 检索时把"同体系/相邻等级/跨体系对齐"一起召回。"""
    g = graph()
    start = str(name or "").strip()
    if start not in g["by_id"]:
        return []
    seen, frontier, out = {start}, [start], []
    for _ in range(max(1, hops)):
        nxt = []
        for cur in frontier:
            for nb, et in g["adj"].get(cur, []):
                if edge_types and et not in edge_types:
                    continue
                if nb in seen or nb.startswith("domain:"):
                    continue
                seen.add(nb)
                n = g["by_id"][nb]
                out.append({"term": nb, "domain": n["domain"], "slot": n["slot"],
                            "rank": n["rank"], "via": et})
                nxt.append(nb)
        frontier = nxt
        if not frontier:
            break
    return out


def stats():
    g = graph()
    types = {}
    for e in g["edges"]:
        types[e["type"]] = types.get(e["type"], 0) + 1
    return {"nodes": len(g["nodes"]),
            "terms": len([n for n in g["nodes"] if not n.get("center")]),
            "domains": len(load_webnovel_lexicon().get("domains", {})),
            "edges": len(g["edges"]), "edge_types": types}


if __name__ == "__main__":
    print("=== 图规模 ===")
    print(" ", stats())
    print()
    print("=== 精确匹配(正向/负向对照) ===")
    cases = [("炼气", "修真仙侠"), ("斗帝", "玄幻斗气"), ("序列9", "诡秘神秘学"),
             ("主神空间", "无限流"), ("收容物", "SCP收容"), ("迪化", "剧情桥段套路（跨题材）"),
             ("医院", None), ("觐见之梯", None), ("程实", None)]
    ok = True
    for n, exp in cases:
        r = lookup_term(n)
        got = r["domain"] if r else None
        if got != exp:
            ok = False
        print(f"  {'OK' if got == exp else 'FAIL':5s} {n} -> {got}")
    print()
    print("=== 检索 '练气'(错别字容错: 前缀/子串) ===")
    for r in search("练气", topk=5):
        print(f"  {r['score']} {r['how']:10s} {r['term']} [{r['domain']}]")
    print()
    print("=== 图上 1 跳邻居 '金丹'(含跨体系对齐) ===")
    for r in neighbors("金丹", hops=1)[:8]:
        print(f"  via={r['via']:14s} {r['term']} [{r['domain']}]")
    print()
    print("=== 补档检测: 本文只抽到[炼气, 元婴] ===")
    for s in suggest_missing(["炼气", "元婴"]):
        print(f"  域={s['domain']} 已有={s['have']} 缺失={s['missing']}")
    print()
    print("ALL PASS" if ok else "HAS FAILURES")
