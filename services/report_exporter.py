"""
报告导出层 —— Word(.docx) 优先，未安装 python-docx 自动降级纯文本(+牵引符)。

编号规则：RPT-YYYYMMDD-NNN，NNN 按自然日独立重置。
  · next_report_no(ref_date)  生成时使用（消耗一个序号）
  · peek_report_no(ref_date)  预览时可看「将得到的编号」，不消耗序号
导出文件写 data/reports/（已被 .gitignore 的 data/ 覆盖）。
"""

import os
from datetime import date, timedelta

import db

REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "reports")


def _seq_and_last():
    seq = int(db.get_setting("rpt_seq", "0") or "0")
    last = db.get_setting("rpt_last_date", "")
    return seq, last


def peek_report_no(ref_date):
    """预览用：不占用真实流水。返回将会得到的编号。"""
    key = ref_date.strftime("%Y%m%d")
    seq, last = _seq_and_last()
    if last != key:
        return f"RPT-{key}-001"
    return f"RPT-{key}-{seq + 1:03d}"


def next_report_no(ref_date):
    """生成用：推进按日重置的流水。返回本次编号。"""
    key = ref_date.strftime("%Y%m%d")
    seq, last = _seq_and_last()
    seq = seq + 1 if last == key else 1
    db.set_setting("rpt_seq", str(seq))
    db.set_setting("rpt_last_date", key)
    return f"RPT-{key}-{seq:03d}"


def default_filename(kind, ref_date):
    if kind == "daily":
        return f"ILTS_Daily_{ref_date.strftime('%Y-%m-%d')}"
    ws = ref_date - timedelta(days=ref_date.weekday())
    we = ws + timedelta(days=6)
    return f"ILTS_Weekly_{ws.strftime('%Y-%m-%d')}_to_{we.strftime('%Y-%m-%d')}"


def docx_available():
    try:
        import docx  # noqa: F401
        return True
    except Exception:
        return False


def _render_blocks_docx(doc, blocks):
    from docx import Document
    from docx.shared import Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    normal = doc.styles["Normal"]
    normal.font.name = "Arial"
    normal.font.size = Pt(10.5)
    normal._element.rPr.rFonts.set(__import__("docx").oxml.ns.qn("w:eastAsia"), "微软雅黑")

    for blk in blocks:
        t = blk["t"]
        if t == "title":
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            r = p.add_run(blk["text"])
            r.bold = True
            r.font.size = Pt(16)
            _set_cn(r, "微软雅黑")
        elif t == "h":
            p = doc.add_paragraph()
            r = p.add_run(blk["text"])
            r.bold = True
            r.font.size = Pt(13)
            r.font.color.rgb = __import__("docx").shared.RGBColor(0, 122, 255)
            _set_cn(r, "微软雅黑")
            p.paragraph_format.space_before = Pt(10)
        elif t == "sub":
            p = doc.add_paragraph()
            r = p.add_run(blk["text"])
            r.bold = True
            _set_cn(r, "微软雅黑")
        elif t == "note":
            p = doc.add_paragraph()
            r = p.add_run(blk["text"])
            r.font.color.rgb = __import__("docx").shared.RGBColor(0x8E, 0x8E, 0x93)
            r.font.size = Pt(10)
            _set_cn(r, "微软雅黑")
        elif t == "para":
            p = doc.add_paragraph(blk["text"])
            _set_cn(p.runs[0], "微软雅黑") if p.runs else None
        elif t == "table":
            header = blk["header"]
            if not header:
                continue
            rows = [header] + blk["rows"]
            table = doc.add_table(rows=len(rows), cols=len(header))
            table.style = "Table Grid"
            red_ids = blk.get("red") or set()
            for ri, row in enumerate(rows):
                for ci, cell in enumerate(row):
                    para = table.cell(ri, ci).paragraphs[0]
                    run = para.add_run(str(cell))
                    if ri == 0:
                        run.bold = True
                    if ri - 1 in red_ids and (ci in (4, 3)):
                        run.font.color.rgb = __import__("docx").shared.RGBColor(0xC0, 0x39, 0x2B)
                    _set_cn(run, "微软雅黑")
                    run.font.size = Pt(9.5)
    return doc


def _set_cn(run, font):
    from docx.oxml.ns import qn
    run.font.name = "Arial"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), font)


def export(model, blocks, out_dir=None):
    """根据是否安装 python-docx 决定导 docx 或 txt。返回 (path, kind)"""
    out_dir = out_dir or REPORTS_DIR
    os.makedirs(out_dir, exist_ok=True)
    ref = date(*[int(x) for x in model["ref_date"].split("-")])
    base = default_filename(model["kind"], ref)
    path = os.path.join(out_dir, base)

    if docx_available():
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        doc = Document()
        # 页眉：居中公司名称
        sec = doc.sections[0]
        from docx.shared import Pt
        hp = sec.header.paragraphs[0]
        hp.text = "昆明中远海运物流有限公司"
        hp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        for r in hp.runs:
            r.font.size = Pt(10)
        _render_blocks_docx(doc, blocks)
        full = path + ".docx"
        doc.save(full)
        return full, "docx"

    full = path + ".txt"
    lines = [_render_txt(blocks)]
    with open(full, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return full, "txt"


def _render_txt(blocks):
    """纯文本降级：红字用【逾期】/【缺失】标记代替，分节排印。"""
    out = []
    for blk in blocks:
        t = blk["t"]
        if t == "title":
            out.append("")
            out.append("=" * 44)
            out.append("      " + blk["text"])
            out.append("=" * 44)
        elif t == "h":
            out.append("")
            out.append("【" + blk["text"] + "】")
        elif t == "sub":
            out.append("— " + blk["text"] + " —")
        elif t == "note":
            out.append(blk["text"])
        elif t == "para":
            out.append(blk["text"])
        elif t == "table":
            header = blk["header"]
            if not header:
                continue
            red_ids = blk.get("red") or set()
            out.append("  " + "  |  ".join(header))
            for i, row in enumerate(blk["rows"]):
                mark = ""
                if i in red_ids:
                    mark = " 【缺失】"
                out.append("  " + "  |  ".join(str(c) for c in row) + mark)
    return "\n".join(out)


def open_report_folder(out_dir=None):
    import subprocess
    import sys
    d = out_dir or REPORTS_DIR
    os.makedirs(d, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(d)  # type: ignore
    else:
        subprocess.Popen(["open", d])