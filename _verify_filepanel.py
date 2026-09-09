"""离线验证 file_panel 修复：分组常展全可点 + 滚动保持 + 勾选不销毁控件。"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date
from PySide6.QtWidgets import QApplication

import db
from ui.widgets.file_panel import FilePanel, FileRow

app = QApplication.instance() or QApplication([])

# 构造 5 个节点 + 单证数据；保证各节点都有 required 单证
NODES = [
    {"node_id": 1, "seq": 1, "node_name": "出口报关", "plan_start": "2026-09-01", "plan_end": "2026-09-05", "status": "Active"},
    {"node_id": 2, "seq": 2, "node_name": "报检报验", "plan_start": "2026-09-05", "plan_end": "2026-09-08", "status": "Active"},
    {"node_id": 3, "seq": 3, "node_name": "集港",     "plan_start": "2026-09-08", "plan_end": "2026-09-12", "status": "Active"},
    {"node_id": 4, "seq": 4, "node_name": "装船",     "plan_start": "2026-09-12", "plan_end": "2026-09-15", "status": ""},
    {"node_id": 5, "seq": 5, "node_name": "离港",     "plan_start": "2026-09-15", "plan_end": "2026-09-18", "status": ""},
]

def mk(fid, nid, doc, req=True, status="pending"):
    return {"file_id": fid, "doc_type": "required" if req else "optional",
            "doc_name": doc, "status": status, "node_id": nid,
            "owner_dept": "商务部", "copies": 1, "due_date": "2026-09-10"}

FILES = ([mk(100, None, "项目整体单证", True)] +
         [mk(100 + n, n, f"节点{n} 单证A", True) for n in range(1, 6)] +
         [mk(200 + n, n, f"节点{n} 单证B", False) for n in range(1, 6)])

today = date(2026, 9, 9)
panel = FilePanel(FILES, NODES, today=today, readonly=False)

def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    return bool(cond)

ok = True

# 1) 所有分组 checkbox 均启用且可点
rows = [r for r, _ in panel._row_order]
boxes = [r.checkbox for r in rows]
ok &= check(f"共 {len(boxes)} 行，全部 checkbox 处于启用态", all(b.isEnabled() for b in boxes))

# 2) 节点1 单证先勾选再取消（提交→撤交）应可行
n1_row = [r for r, nid in panel._row_order if nid == 1][0]
box1 = n1_row.checkbox
box1.setChecked(True); ok &= check("节点1 单证可设为已提交", box1.isChecked())
box1.setChecked(False); ok &= check("节点1 单证可取消（撤交）", not box1.isChecked())

# 3) 节点4/5 单证可选中提交
for nid in (4, 5):
    r = [r for r, _n in panel._row_order if _n == nid][0]
    cb = r.checkbox
    cb.setChecked(True)
    ok &= check(f"节点{nid} 单证可提交", cb.isChecked())

# 3b) ★ 原地刷新不销毁控件：刷新前后对象身份不变（历史崩溃根因回归）
row0 = panel._row_order[0][0]
box0 = row0.checkbox
changed = [dict(f) for f in FILES]
changed[0]["status"] = "submitted"
panel.update_files(changed, NODES)
ok &= check("update_files 后行对象未被销毁（同一对象）",
            panel._row_order[0][0] is row0)
try:
    box0.isChecked()
    alive = True
except RuntimeError:
    alive = False
ok &= check("update_files 后勾选框仍存活（可安全访问）", alive)
ok &= check("原地刷新后勾选态跟随数据", box0.isChecked() is True)

# 4) 滚动位置保持：给足尺寸使 maximum 足够大，设置滚动值 → refresh → 恢复
panel.resize(400, 60)          # 视口高度极小，保证可滚动范围远超测试值
panel.show()
app.processEvents()
panel.verticalScrollBar().setValue(320)
saved_expect = panel.verticalScrollBar().value()
panel.refresh(FILES, NODES)
restored = panel.verticalScrollBar().value()
ok &= check(f"refresh 后滚动位置保留（期望 {saved_expect}，实际 {restored}）", restored == saved_expect and saved_expect == 320)

print("\n" + ("ALL PASS" if ok else "SOME FAIL"))
sys.exit(0 if ok else 1)