# -*- coding: utf-8 -*-
"""
ingest.py —— 读取小说 -> 段落编号 + 卷/章识别
支持输入格式:
  .txt  纯文本(章节行 "第X章 ..." 切章, 行=段落)
  .epub 电子书(zip 包, 按 spine 顺序读 chapter_*.xhtml, <h1> 章标题 / <p> 段落)
纯规则,不调用模型。输出:
  paragraphs: [{para_id, volume_no, chapter_no, text}]
  chapters:   [{chapter_no, volume_no, title, start_para, end_para}]
"""
import re
import sys
import os
import zipfile
import html as _html

sys.path.insert(0, os.path.dirname(__file__))
import config as C

VOL_RE = re.compile(r"^第[一二三四五六七八九十百千0-9]+卷")
CH_RE = re.compile(r"^第[一二三四五六七八九十百千0-9]+章")


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gb18030", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


# ============================================================
# epub 解析
# ============================================================
def _xhtml_text(xhtml: str) -> str:
    """提取 xhtml body 文本，块级元素(p/h/div/li/br)之间换行，去掉其余标签。"""
    m = re.search(r"<body[^>]*>(.*?)</body>", xhtml, re.I | re.S)
    if not m:
        return ""
    body = m.group(1)
    # 块级标签前后加换行
    body = re.sub(r"<(p|h[1-6]|div|li|br\s*/?)", "\n<", body, flags=re.I)
    body = re.sub(r"</(p|h[1-6]|div|li)>", "</\\1>\n", body, flags=re.I)
    body = re.sub(r"<[^>]+>", "", body)
    body = _html.unescape(body)
    # 清理残留的空标签记号(如 "<>")
    body = re.sub(r"^<>", "", body, flags=re.M)
    return body


def _epub_ingest(book_path: str, max_chapters: int = None):
    """从 epub 读取 (paragraphs, chapters)，结构与 txt 一致。"""
    z = zipfile.ZipFile(book_path)
    opf_name = None
    for n in z.namelist():
        if n.endswith(".opf"):
            opf_name = n
            break
    if opf_name is None:
        raise ValueError("epub 缺少 content.opf")
    opf = z.read(opf_name).decode("utf-8", errors="replace")
    spine = re.findall(r'<itemref[^>]*idref="([^"]+)"', opf)
    if not spine:  # 兼容单引号
        spine = re.findall(r"<itemref[^>]*idref='([^']+)'", opf)

    chapters = []          # [{chapter_no, title, paras:[text]}]
    pending = None
    for item in spine:
        if not item.startswith("chapter_"):
            continue
        try:
            xhtml = z.read("OEBPS/" + item).decode("utf-8", errors="replace")
        except KeyError:
            continue
        text = _xhtml_text(xhtml)
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        # 章标题：第一行若是 第X章 则是标题；否则整文件并入上一章
        title = None
        paras = []
        for l in lines:
            if title is None and CH_RE.match(l):
                title = l
            elif title is not None:
                paras.append(l)
            # 标题之前的内容(如引子)丢弃
        if title is None:
            if pending is not None:
                pending["paras"].extend(lines)
            continue
        if pending is not None:
            chapters.append(pending)
        pending = {"title": title, "paras": paras}
        if max_chapters and len(chapters) >= max_chapters:
            break
    if pending is not None and (not max_chapters or len(chapters) < max_chapters):
        chapters.append(pending)

    # 组装 paragraphs / chapters
    paragraphs = []
    ch_out = []
    para_id = 0
    for no, c in enumerate(chapters, 1):
        start = para_id + 1
        for t in c["paras"]:
            para_id += 1
            paragraphs.append({"para_id": para_id, "volume_no": 0,
                               "chapter_no": no, "text": t})
        ch_out.append({"chapter_no": no, "volume_no": 0,
                       "title": c["title"], "start_para": start,
                       "end_para": para_id})
    return paragraphs, ch_out


# ============================================================
# txt 解析
# ============================================================
def _txt_ingest(book_path: str, max_chapters: int = None):
    raw = open(book_path, "rb").read()
    txt = _decode(raw)
    lines = txt.split("\n")

    volumes = []          # 卷标题(仅用于 volume_no 映射)
    chapters = []
    paragraphs = []
    volume_no = 0
    chapter_no = 0
    para_id = 0
    pending = None        # 正在累积的章节边界信息
    last_appended = False  # 上一章是否已入列(防止 break 后重复收尾)

    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        mv = VOL_RE.match(s)
        mc = CH_RE.match(s)
        if mv:
            volume_no += 1
            volumes.append(s)
            continue
        if mc:
            # 收尾上一章并入列
            if pending is not None:
                pending["end_para"] = para_id
                chapters.append(pending)
                last_appended = True
            else:
                last_appended = False
            chapter_no += 1
            if max_chapters and chapter_no > max_chapters:
                # 已越过目标章节数,停止后续读取(当前 pending 已在上一轮入列,不再收尾)
                break
            pending = {
                "chapter_no": chapter_no,
                "volume_no": volume_no,
                "title": s,
                "start_para": para_id + 1,  # 下一行才是正文第一段
            }
            last_appended = False
            continue
        # 普通段落
        para_id += 1
        paragraphs.append({
            "para_id": para_id,
            "volume_no": volume_no,
            "chapter_no": chapter_no,
            "text": s,
        })
    # 收尾最后一章(仅在循环正常结束、pending 尚未入列时)
    if pending is not None and not last_appended:
        pending["end_para"] = para_id
        chapters.append(pending)

    return paragraphs, chapters


def ingest(book_path: str = C.BOOK_PATH, max_chapters: int = None):
    """按扩展名分流: .epub 走 epub 解析, 其余走 txt 解析。"""
    if book_path.lower().endswith(".epub"):
        return _epub_ingest(book_path, max_chapters)
    return _txt_ingest(book_path, max_chapters)


if __name__ == "__main__":
    ps, chs = ingest(max_chapters=C.SAMPLE_CHAPTERS)
    print(f"段落数={len(ps)}  章节数={len(chs)}")
    for c in chs[:5]:
        print(c)
    if ps:
        print("首段:", ps[0]["text"][:60])