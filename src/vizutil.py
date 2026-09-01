# -*- coding: utf-8 -*-
"""
vizutil.py —— Stage3 可视化共享工具

Stage2 的 LLM 直出存在"同人多条目"问题（同一人物被拆成 周明瑞 / 克莱恩 / 克莱恩·莫雷蒂 三条）。
本模块用**数据自带的 aliases 字段构建无向别名图，求连通分量并归并**，得到去重后的人物主表。
这样 人物 Tab / 力导向图谱 / 性格六维 三处拿到的是同一份干净数据，不会出现
「邓恩」和「邓恩·史密斯」两个节点、也不会把「马车夫」当成独立人物。

用法:
    chars, settings, outlines = load_stage2(src_dir)
    chars, p2c                  = merge_alias_components(chars)   # p2c: 原名 -> 规范名
"""
import os
import json
import re


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_stage2(src_dir):
    """读取 Stage2 产物 (characters/settings/outlines)，返回原始三件套。"""
    chars = _load_json(os.path.join(src_dir, "characters.json"))
    settings = _load_json(os.path.join(src_dir, "settings.json"))
    outlines = _load_json(os.path.join(src_dir, "outlines.json"))
    if isinstance(outlines, list):  # 兼容 [ {no:...}, ... ] 与 { "1": {...} }
        outlines = {o["no"]: o for o in outlines}
    return chars, settings, outlines


# 已知误并别名对：剧情性伪装共指（阿蒙多次伪装"愚者"，但两人是独立角色），
# 双向别名验证挡不住这类"伪装身份"，用黑名单显式排除。
ENTITY_BLACKLIST = {("阿蒙", "愚者")}


def merge_alias_components(chars, extra_edges=None, blacklist=ENTITY_BLACKLIST):
    """
    用 aliases 字段建无向图 → 连通分量归并。
    返回 (merged_chars, name2canon)，name2canon 记录每个原名归并到哪个规范名。
    规范名选取：别名图度数最高 → 串最长 → 首次出现章最早。

    extra_edges: 外部注入的 (name_a, name_b) 等价边列表。
                 供 build_detail 主流程把 entity_registry 的"双向互指"证据
                 (克莱恩⇄周明瑞⇄愚者⇄格尔曼…) 注入 stage2 稀疏 aliases 的归并。
    blacklist: 已知误并别名对集合，union 时跳过（默认 ENTITY_BLACKLIST）。
    """
    names = [c["name"] for c in chars]
    idx = {n: i for i, n in enumerate(names)}
    parent = list(range(len(names)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    def blocked(a, b):
        return blacklist and ((a, b) in blacklist or (b, a) in blacklist)

    # 边：name <-> 每个 alias（alias 必须也是已知人物名，否则是别名串不参与图）
    for c in chars:
        for a in c.get("aliases", []) or []:
            if a in idx and not blocked(c["name"], a):
                union(idx[c["name"]], idx[a])

    # 外部注入边（entity 双向互指证据）
    if extra_edges:
        for a, b in extra_edges:
            if a in idx and b in idx and not blocked(a, b):
                union(idx[a], idx[b])

    comps = {}
    for i, n in enumerate(names):
        r = find(i)
        comps.setdefault(r, []).append(i)

    name2canon = {}
    merged = []
    for members in comps.values():
        # 规范名评分
        def score(i):
            c = chars[i]
            deg = sum(1 for m in members if c["name"] in (chars[m].get("aliases") or []))
            return (deg, len(c["name"]), -(c.get("首次出现章") or 999))
        canon_i = max(members, key=score)
        canon = chars[canon_i]["name"]
        for i in members:
            name2canon[chars[i]["name"]] = canon
        entries = [chars[i] for i in members]
        merged.append(_merge_entries(entries, members.index(canon_i)))

    merged.sort(key=lambda c: c.get("首次出现章") or 999)
    return merged, name2canon


def _merge_entries(entries, canon_i):
    """把同一连通分量的多条人物记录合并成一条。"""
    canon = entries[canon_i]
    aliases, ident, arc, rels, evs, first = [], [], [], [], [], None
    seen = set()
    for e in entries:
        # 同一分量里其他条目的名字本身也是规范名的别名（如「马车夫」实为邓恩·史密斯）
        if e["name"] != canon["name"] and e["name"] not in seen:
            seen.add(e["name"])
            aliases.append(e["name"])
        for a in e.get("aliases", []) or []:
            if a != canon["name"] and a not in seen:
                seen.add(a)
                aliases.append(a)
        ident.append((e.get("身份") or "").strip())
        arc.append((e.get("弧光") or "").strip())
        for r in e.get("关系") or []:
            if r not in rels:
                rels.append(r)
        for ev in e.get("关键事件") or []:
            if ev not in evs:
                evs.append(ev)
        f = e.get("首次出现章")
        if f is not None:
            first = f if first is None else min(first, f)
    # 去重后取最长的身份/弧光描述（信息最全的那条）
    ident = max([x for x in ident if x], key=len) if any(ident) else ""
    arc = max([x for x in arc if x], key=len) if any(arc) else ""
    # 别名按长度降序排，让全名（如克莱恩·莫雷蒂）排在简称前面，身份链一目了然
    aliases.sort(key=lambda s: (-len(s), s))
    return {
        "name": canon["name"],
        "aliases": aliases,
        "身份": ident,
        "弧光": arc,
        "关系": rels,
        "关键事件": evs,
        "首次出现章": first,
    }


def merge_personality(per_list, name2canon):
    """把性格六维按规范名归并（同一分量多条取均值）。"""
    agg = {}
    for p in per_list:
        n = p.get("name")
        if not n:
            continue
        cn = name2canon.get(n, n)
        d = agg.setdefault(cn, {"name": cn, "dims": {}})
        for k, v in (p.get("dims") or {}).items():
            cur = d["dims"].get(k)
            d["dims"][k] = v if cur is None else round((cur + v) / 2, 1)
    out = [v for v in agg.values() if v["dims"]]
    out.sort(key=lambda v: v["name"])
    return out


# ---------------- entity_registry 证据注入 ----------------
# entity_registry.json 提供跨场景的 canonical+aliases 表（比 stage2 自带 aliases 全，
# 但混入场景共指噪音：眷者指向神、伪装身份、关系短语等）。策略：
#   1. 归并用边只取「双向互指」——a 是 b 的别名 且 b 是 a 的别名（真同人）。
#      单向边（如 伦纳德→克莱恩、佛尔思→休）全是共指噪音，丢弃。
#   2. 阿蒙⇄愚者 是剧情性伪装的双向噪音，黑名单显式排除。
#   3. 归并后 enrich：把 canonical 别名集挂到分量上（马甲如 夏洛克·莫里亚蒂
#      以别名形式展示），但跳过「属于其他独立人物的名字」和描述性长串。


def load_entity_alias_map(er_path):
    """entity_registry.json → {canonical: set(aliases)}（仅 category==人物，跨场景聚合）。
    文件缺失/损坏返回空 dict，调用方安全降级。"""
    alias_map = {}
    try:
        er = json.load(open(er_path, encoding="utf-8"))
    except Exception:
        return alias_map
    for lst in (er.values() if isinstance(er, dict) else []):
        if not isinstance(lst, list):
            continue
        for e in lst:
            if isinstance(e, dict) and e.get("category") == "人物":
                c = str(e.get("canonical") or "").strip()
                if c:
                    alias_map.setdefault(c, set()).update(
                        str(a).strip() for a in (e.get("aliases") or []) if str(a).strip())
    return alias_map


def _is_alias(name, canon, alias_map):
    """name 是否属于 canon 的别名集合（含自身）。canon 无条目视为空。"""
    if name == canon:
        return True
    return name in alias_map.get(canon, set())


def _mutual(name_a, name_b, alias_map):
    """a、b 在 entity 层互相把对方标为别名（双向互指 = 真同人证据）。"""
    return _is_alias(name_a, name_b, alias_map) and _is_alias(name_b, name_a, alias_map)


def entity_alias_edges(chars, alias_map, blacklist=ENTITY_BLACKLIST):
    """从 entity_registry 提取 raw 人物名之间的双向互指边（归并证据）。
    只返回两端都存在于 chars 的边；黑名单边跳过。"""
    names = [c["name"] for c in chars if isinstance(c, dict) and c.get("name")]
    n = len(names)
    edges = set()
    for i in range(n):
        for j in range(i + 1, n):
            a, b = names[i], names[j]
            if blacklist and ((a, b) in blacklist or (b, a) in blacklist):
                continue
            if _mutual(a, b, alias_map):
                edges.add((a, b))
    return edges


def enrich_aliases(chars, alias_map, skip_names=None):
    """把 entity canonical 的别名集并入对应分量的 aliases（仅展示补全，不再归并）。
    过滤规则：
      - 跳过属于其他独立人物的名字（避免把 伦纳德/正义 等挂进克莱恩别名）
      - 跳过描述性长串/含标点的串（'穿橄榄绿长裙的女士' 之类场景描述噪音）
    返回原地修改后的 chars。"""
    skip = set(skip_names or [])
    # 噪音串判据：描述性短语（含结构词/标点/括号等）。注意 ·(U+00B7) 是全名间隔号
    # （克莱恩·莫雷蒂/夏洛克·莫里亚蒂），属于合法全名，不能当噪音过滤。
    _NOISE = re.compile(r"[的之与和或（(）)『』「」‘’“”—…：:：]")
    for c in chars:
        cname = c["name"]
        # 本分量 canonical 名不拦截（其余分量名即 skip 集，禁止跨人物挂载）
        skip_here = skip - {cname}
        extra = []
        seen = set(c.get("aliases") or [])
        seen.add(cname)
        # 分量成员名（含已并别名）命中 canonical 时，取该 canonical 的别名集
        for member in [cname] + list(c.get("aliases") or []):
            for a in alias_map.get(member, set()):
                a = str(a).strip()
                if (a and a != cname and a not in seen and a not in skip_here
                        and len(a) <= 12 and not _NOISE.search(a)):
                    seen.add(a)
                    extra.append(a)
        if extra:
            c["aliases"] = c.get("aliases") or []
            c["aliases"].extend(extra)
            c["aliases"].sort(key=lambda s: (-len(s), s))
    return chars


def load_personality(per_path, name2canon):
    if not os.path.exists(per_path):
        return []
    return merge_personality(_load_json(per_path), name2canon)