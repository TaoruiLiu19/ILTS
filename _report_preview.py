"""用真实库预览日报，目检「下一个工作日待办」表格"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date, timedelta
import db
from services import reporting, report_exporter

ref = date.today()
# 找一个「次日有节点」的日期来展示效果
for off in range(0, 30):
    d = ref + timedelta(days=off)
    m = reporting.build_report("daily", d)
    if m["todo"]["items"]:
        ref = d
        break

m = reporting.build_report("daily", ref, report_no=report_exporter.peek_report_no(ref))
print(f"== 日报 {ref} ==")
for blk in reporting.blocks(m):
    t = blk["t"]
    if t in ("title", "h", "sub", "para", "note"):
        print(f"[{t}] {blk['text']}")
    elif t == "table":
        print(f"[table] {' | '.join(blk['header'])}")
        for r in blk["rows"][:8]:
            print("        " + " | ".join(str(c) for c in r))
        if len(blk["rows"]) > 8:
            print(f"        … 另有 {len(blk['rows']) - 8} 行")

print("\n== 周报章节 ==")
mw = reporting.build_report("weekly", ref)
for blk in reporting.blocks(mw):
    if blk["t"] == "h":
        print(" ", blk["text"])
    elif blk["t"] == "table" and blk["header"][:1] == ["日期"]:
        print(f"   [table] {' | '.join(blk['header'])}")
        for r in blk["rows"][:3]:
            print("           " + " | ".join(str(c) for c in r))
