# -*- coding: utf-8 -*-
"""
segment.py —— 场景(分镜桶)切分
以 8-15 段为一个场景块。规则信号断块;可选 bge-m3 向量突变兜底。
输出 scenes: [{scene_id, chapter_no, volume_no, start_para, end_para, paras:[...]}]
坐标(para_id)由程序生成,绝不交给模型。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import config as C


def _is_dialogue(text: str) -> bool:
    return any(p in text for p in C.PUNCT_DIALOG)


def _rule_break_indices(paras):
    """返回建议断块位置(在这些段落【之前】断开)。"""
    breaks = set()
    dial_cnt = 0
    for i, p in enumerate(paras):
        t = p["text"]
        # 对话块收尾:连续>=3段对话后,遇到非对话段 -> 在对话块后断
        if _is_dialogue(t):
            dial_cnt += 1
        else:
            if dial_cnt >= 3:
                breaks.add(i)  # 在此非对话段之前断开(即对话块结束)
            dial_cnt = 0
        # 时间/地点/场景切换状语开头 -> 新场景
        head2 = t[:2]
        head3 = t[:3]
        if any(t.startswith(sig) for sig in C.TIME_SIGNALS) or \
           any(t.startswith(sig) for sig in C.PLACE_SIGNALS) or \
           any(t.startswith(sig) for sig in C.SCENE_SWITCH_SIGNALS):
            if i > 0:
                breaks.add(i)
    return breaks


def _is_switch_start(paras):
    """块首段是否命中场景切换信号(强边界: 合并时只能向后, 不得并入前块)。"""
    if not paras:
        return False
    t = paras[0]["text"]
    return any(t.startswith(sig) for sig in C.SCENE_SWITCH_SIGNALS)


def _vector_break_indices(paras, embed_batch_fn):
    """相邻段余弦相似度骤降处 -> 隐性边界(TextTiling 思路)。

    embed_batch_fn(texts) -> list[vec]  —— 必须是【批量】函数。
    历史踩坑:早期逐段调用 /api/embeddings(2181ms/段),2732 段要 103 分钟;
    改批量 /api/embed 后 51ms/段,全书约 2.3 分钟。
    """
    if embed_batch_fn is None or len(paras) < 3:
        return set()
    vecs = embed_batch_fn([p["text"] for p in paras])
    breaks = set()
    for i in range(1, len(paras)):
        a, b = vecs[i - 1], vecs[i]
        if not a or not b:
            continue  # 该段向量化失败,不作边界判断(交给规则信号)
        if _cosine(a, b) < C.VECTOR_DROP_THRESHOLD:
            breaks.add(i)
    return breaks


def _cosine(a, b):
    import math
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _vector_break_from_cache(paras, vec_map):
    """用预先算好的 para_id->vec 映射判定隐性边界(不发请求)。"""
    if not vec_map or len(paras) < 3:
        return set()
    breaks = set()
    for i in range(1, len(paras)):
        a = vec_map.get(paras[i - 1]["para_id"])
        b = vec_map.get(paras[i]["para_id"])
        if not a or not b:
            continue
        if _cosine(a, b) < C.VECTOR_DROP_THRESHOLD:
            breaks.add(i)
    return breaks


def _split_chapter(paras, vec_map=None):
    """把一个章节的段落列表切成若干场景块(段落列表的列表)。

    融合策略(最终方案,严格加法):
      1) 先按规则信号切出"规则块",每块起点=规则边界(受保护);
      2) 纯规则模式: 自由合并(还原 229 块规范基线);
      3) 向量模式: 对每个规则块内部做语义细分(仅 >MAX 才切,且保证子块>=MIN),
         再用方向感知合并消除碎片——全程不消除任何规则边界。
      结果:向量模式是规则模式的严格超集, lost 恒为 0, 对比可直接读。
    """
    if not paras:
        return []
    rule_b = sorted(_rule_break_indices(paras))
    blocks = []
    start = 0
    for b in rule_b:
        if b > start:
            blocks.append([paras[start:b], True])
            start = b
    if start < len(paras):
        blocks.append([paras[start:], True])

    if vec_map is None:
        return [p for p, _ in _rule_merge_freely(blocks)]

    split = []
    for blk, prot in blocks:
        split.extend(_split_oversize(blk, prot, vec_map))
    return [p for p, _ in _merge_fragments_additive(split)]


def _split_oversize(blk, prot, vec_map=None):
    """递归切开超过 MAX 的块,块起点(受保护语义边界)保持不动。

    有向量时在"语义最弱处"下刀(相邻段相似度最低),无向量时退化为机械切。
    候选断点约束在 [MIN, len-MIN) 内,保证切出的两侧都不产生新碎片。
    返回 [[sub_paras, sub_prot], ...]: 首子块继承原块 prot(规则边界),
    后续子块 prot=False(内部细分点,可自由合并)。
    """
    if len(blk) <= C.SEG_MAX_PARA:
        return [[blk, prot]]
    lo, hi = C.SEG_MIN_PARA, len(blk) - C.SEG_MIN_PARA
    if hi <= lo:
        cut = len(blk) // 2
    elif vec_map:
        best, best_sim = None, 2.0
        for i in range(lo, hi):
            a = vec_map.get(blk[i - 1]["para_id"])
            b = vec_map.get(blk[i]["para_id"])
            if not a or not b:
                continue
            s = _cosine(a, b)
            if s < best_sim:
                best, best_sim = i, s
        cut = best if best is not None else min(C.SEG_MAX_PARA, len(blk) // 2)
    else:
        cut = C.SEG_MAX_PARA
    return (_split_oversize(blk[:cut], prot, vec_map)
            + _split_oversize(blk[cut:], False, vec_map))


def _rule_merge_freely(blocks):
    """纯规则模式的尺寸规整(不保护任何边界,还原原始 229 块规范行为)。

    与向量模式用不同策略是故意的:规则模式允许自由跨边界合并小规则块,
    产出稳定的"规则模式基线";向量模式则在该基线之上只做加法(见下)。
    """
    res = [list(b) for b in blocks]
    changed = True
    while changed:
        changed = False
        n = len(res)
        for i in range(n):
            if len(res[i][0]) < C.SEG_MIN_PARA:
                # 场景切换块: 强边界, 只允许向后并入(保持切换起点), 禁止并入前块
                if _is_switch_start(res[i][0]):
                    if i + 1 < n and len(res[i][0]) + len(res[i + 1][0]) <= C.SEG_MAX_PARA:
                        res[i + 1][0] = res[i][0] + res[i + 1][0]
                        del res[i]
                        changed = True
                        break
                    continue  # 无法向后并入: 保留极小切换块, 不破边界
                if i > 0 and len(res[i - 1][0]) + len(res[i][0]) <= C.SEG_MAX_PARA:
                    res[i - 1][0] = res[i - 1][0] + res[i][0]
                    del res[i]
                    changed = True
                    break
                elif i + 1 < n:
                    res[i + 1][0] = res[i][0] + res[i + 1][0]
                    del res[i]
                    changed = True
                    break
                elif i > 0:
                    # 章末碎片+前块已满: 强行并入前块(超限由下方机械切兜底)
                    res[i - 1][0] = res[i - 1][0] + res[i][0]
                    del res[i]
                    changed = True
                    break
    out = []
    for blk, _ in res:
        out.extend(_split_oversize(blk, False, None))
    return out


def _merge_fragments_additive(blocks):
    """方向感知碎片合并(严格加法): 绝不消除一个受保护起点(=规则边界)。

    - 受保护碎片(规则块本身过短): 只能向后并入,合并后起点=本块起点仍为受保护;
    - 非受保护碎片(内部细分产生的): 优先向前并入,否则向后并入。
    迭代到不动点;最后对仍超长块机械切(保持起点 prot)。
    """
    res = [list(b) for b in blocks]
    changed = True
    while changed:
        changed = False
        n = len(res)
        for i in range(n):
            paras, prot = res[i]
            if len(paras) >= C.SEG_MIN_PARA:
                continue
            if prot:
                # 受保护起点: 仅向后并入,合并后起点=本块(受保护)
                if i + 1 < n and len(res[i + 1][0]) + len(paras) <= C.SEG_MAX_PARA:
                    res[i + 1][0] = paras + res[i + 1][0]
                    res[i + 1][1] = True
                    del res[i]
                    changed = True
                    break
                # 无法向后: 保留受保护极小规则块(宁留碎片不破边界)
                continue
            if i > 0 and len(res[i - 1][0]) + len(paras) <= C.SEG_MAX_PARA:
                res[i - 1][0] = res[i - 1][0] + paras
                del res[i]
                changed = True
                break
            if i + 1 < n and len(res[i + 1][0]) + len(paras) <= C.SEG_MAX_PARA:
                res[i + 1][0] = paras + res[i + 1][0]
                del res[i]
                changed = True
                break
    out = []
    for paras, prot in res:
        out.extend(_split_oversize(paras, prot, None))
    return out


def segment(paragraphs, chapters, embed_batch_fn=None, max_chapters=None):
    """场景切分。

    embed_batch_fn(texts)->list[vec] 为 None 时纯规则切分;
    传入时先一次性批量向量化全部段落(内部自动分批),再在章内查表判定隐性边界,
    避免逐章/逐段发请求。
    """
    scenes = []
    sid = 0

    # 按章预分组,避免 O(章数 × 段数) 的重复全量扫描
    by_chapter = {}
    for p in paragraphs:
        by_chapter.setdefault(p["chapter_no"], []).append(p)

    # 一次性批量向量化(仅 vector 模式)
    vec_map = None
    if embed_batch_fn is not None:
        # 前言(chapter_no==0)不进入场景切分,跳过其向量化以省算力
        targets = [p for p in paragraphs
                   if p["chapter_no"] > 0
                   and (not max_chapters or p["chapter_no"] <= max_chapters)]
        vecs = embed_batch_fn([p["text"] for p in targets])
        vec_map = {p["para_id"]: v for p, v in zip(targets, vecs) if v}

    for ch in chapters:
        if max_chapters and ch["chapter_no"] > max_chapters:
            continue
        cn = ch["chapter_no"]
        vn = ch["volume_no"]
        paras = by_chapter.get(cn) or []
        if not paras:
            continue
        blocks = _split_chapter(paras, vec_map)
        for bi, blk in enumerate(blocks):
            sid += 1
            scenes.append({
                "scene_id": sid,
                "chapter_no": cn,
                "volume_no": vn,
                "event_seq": bi + 1,
                "start_para": blk[0]["para_id"],
                "end_para": blk[-1]["para_id"],
                "paras": blk,
            })
    return scenes


if __name__ == "__main__":
    from ingest import ingest
    ps, chs = ingest(max_chapters=C.SAMPLE_CHAPTERS)
    scs = segment(ps, chs, max_chapters=C.SAMPLE_CHAPTERS)
    print(f"章节={len(chs)}  场景块={len(scs)}")
    for s in scs[:3]:
        print(f"scene#{s['scene_id']} 章{s['chapter_no']} 段{s['start_para']}-{s['end_para']} "
              f"({len(s['paras'])}段) | {s['paras'][0]['text'][:30]}")
