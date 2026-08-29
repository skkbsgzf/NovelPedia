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


def merge_alias_components(chars):
    """
    用 aliases 字段建无向图 → 连通分量归并。
    返回 (merged_chars, name2canon)，name2canon 记录每个原名归并到哪个规范名。
    规范名选取：别名图度数最高 → 串最长 → 首次出现章最早。
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

    # 边：name <-> 每个 alias（alias 必须也是已知人物名，否则是别名串不参与图）
    for c in chars:
        for a in c.get("aliases", []) or []:
            if a in idx:
                union(idx[c["name"]], idx[a])

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


def load_personality(per_path, name2canon):
    if not os.path.exists(per_path):
        return []
    return merge_personality(_load_json(per_path), name2canon)