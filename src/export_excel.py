# -*- coding: utf-8 -*-
"""
export_excel.py —— 把 stage1 数据库导出为 .xlsx（纯标准库，零依赖）。
xlsx 本质是 zip 包: [Content_Types].xml + workbook.xml + sheet xml。
用法:
  python src/export_excel.py <db_path> <xlsx_path>
  （每个表一个 sheet，自动跳过 FTS 内部表）
"""
import os
import sys
import json
import sqlite3
import zipfile

# ---------- XML 转义 ----------
def _x(s):
    if s is None:
        return ""
    s = str(s)
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def _cell_type(v):
    """返回 (xml, type) type: s=字符串 n=数字 b=布尔"""
    if v is None:
        return "", "s"
    if isinstance(v, bool):
        return "1" if v else "0", "b"
    if isinstance(v, (int, float)):
        return str(v), "n"
    return _x(v), "s"


def _sheet_xml(header, rows):
    """生成单个 sheet 的 XML。共享字符串表用 inline 字符串简化。"""
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
             '<sheetData>']
    # 表头
    parts.append('<row r="1">')
    for c, name in enumerate(header, 1):
        parts.append('<c r="%s%d" t="inlineStr"><is><t>%s</t></is></c>'
                     % (_col(c), 1, _x(name)))
    parts.append('</row>')
    # 数据行
    for r, row in enumerate(rows, 2):
        parts.append('<row r="%d">' % r)
        for c, v in enumerate(row, 1):
            xml, t = _cell_type(v)
            if t == "s" and xml == "":
                continue  # 空字符串跳过
            if t == "s":
                parts.append('<c r="%s%d" t="inlineStr"><is><t>%s</t></is></c>'
                             % (_col(c), r, xml))
            else:
                parts.append('<c r="%s%d" t="%s"><v>%s</v></c>'
                             % (_col(c), r, t, xml))
        parts.append('</row>')
    parts.append('</sheetData></worksheet>')
    return "".join(parts)


def _col(n):
    """1 -> A, 27 -> AA"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def export(db_path, xlsx_path, max_rows=20000):
    """导出 db 全部业务表到 xlsx（多 sheet）。"""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    tables = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%' "
        "AND name NOT LIKE '%_data' AND name NOT LIKE '%_idx' "
        "AND name NOT LIKE '%_content' AND name NOT LIKE '%_docsize' "
        "AND name NOT LIKE '%_config' ORDER BY name")]
    # 元信息表排最后, 业务表优先
    tables = [t for t in tables if t != "meta"] + (["meta"] if "meta" in tables else [])

    sheets = []
    for t in tables:
        try:
            cols = [r[1] for r in cur.execute("PRAGMA table_info(%s)" % t)]
        except Exception:
            continue
        rows = cur.execute("SELECT * FROM %s LIMIT %d" % (t, max_rows)).fetchall()
        # JSON 列压缩展示(避免单元格过长)
        out_rows = []
        for r in rows:
            out_rows.append([_compact(v) for v in r])
        sheets.append((t, cols, out_rows))
    conn.close()

    # ---------- 组装 xlsx zip ----------
    N = len(sheets)
    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                     + "".join('<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % i
                               for i in range(1, N + 1))
                     + '</Types>')

    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                 '</Relationships>')

    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets>'
                + "".join('<sheet name="%s" sheetId="%d" r:id="rId%d"/>'
                          % (_sheet_name(t), i, i) for i, (t, _, _) in enumerate(sheets, 1))
                + '</sheets></workbook>')

    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               + "".join('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>'
                         % (i, i) for i in range(1, N + 1))
               + '</Relationships>')

    os.makedirs(os.path.dirname(xlsx_path), exist_ok=True)
    with zipfile.ZipFile(xlsx_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        for i, (t, cols, rows) in enumerate(sheets, 1):
            z.writestr("xl/worksheets/sheet%d.xml" % i, _sheet_xml(cols, rows))

    print("已导出 %s: %d 个 sheet" % (xlsx_path, N))
    for t, cols, rows in sheets:
        print("  - %s: %d 列 × %d 行" % (t, len(cols), len(rows)))
    return xlsx_path


def _compact(v):
    """JSON/长文本压缩: 只保留前 200 字符。"""
    if v is None:
        return ""
    if isinstance(v, str) and len(v) > 200:
        return v[:200] + "…"
    return v


def _sheet_name(t):
    """sheet 名: 去掉下划线, 限 31 字符。"""
    n = t.replace("_", " ").title()
    return n[:31]


if __name__ == "__main__":
    db = sys.argv[1]
    xlsx = sys.argv[2] if len(sys.argv) > 2 else db.replace(".db", ".xlsx")
    export(db, xlsx)