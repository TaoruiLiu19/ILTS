"""离屏回归：1)右栏标签/开合状态在内容变更后保持 2)海运压缩表头单调去重 3)单证齐+过期末→自动完成"""
import os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tempfile, shutil
from datetime import date

import db
_TMP = tempfile.mkdtemp(prefix="fix_")
db.DB_PATH = os.path.join(_TMP, "t.db"); db._conn = None; db.init_db()

from services.clock import set_simulated_today
from services.file_checklist import seed as seed_files
from mock_data import DEMO_PROJECT, DEMO_NODES, get_demo_schedule

pid = DEMO_PROJECT["project_id"]
plan = get_demo_schedule()
db.insert_project({k: DEMO_PROJECT[k] for k in
                   ("project_id", "project_name", "country", "export_port",
                    "etd", "eta", "buffer_days")})
for n in DEMO_NODES:
    s, e = plan[n["node_id"]]
    db.insert_nodes(pid, [{"node_id": n["node_id"], "node_name": n["node_name"],
        "role_label": n["role_label"], "seq": n["seq"], "area": n["area"],
        "default_duration": n["duration"], "duration": n["duration"],
        "plan_start": s, "plan_end": e, "remark": n.get("remark", "")}])
seed_files(pid, DEMO_PROJECT["country"], DEMO_PROJECT["export_port"], plan)

FAILED = []
def check(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c:
        FAILED.append(m)

from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)
from ui.theme import GLOBAL_QSS, APP_FONT, APP_FONT_SIZE, ensure_check_asset
from PySide6.QtGui import QFont
app.setFont(QFont(APP_FONT, APP_FONT_SIZE))
ensure_check_asset()
app.setStyleSheet(GLOBAL_QSS)

from ui.widgets.collapsible import CollapsibleSection
from ui.pages.dashboard_page import DashboardPage
from services.node_status import sync_active_projects

# ── 1) 右栏标签状态与内容变更保持（右栏已迁到甘特工作台） ──
print("== 1) 工作台右栏默认标签 + 内容变更不改变标签/开合 ==")
dp = DashboardPage(); dp.resize(1440, 900); dp.show(); dp.refresh(); app.processEvents()
wb = dp.open_workbench(pid, None, "single"); app.processEvents()
check(wb._tab == "files", "默认停在「单证清单」标签")
check(wb._right_collapsed is False, "右栏默认展开")
check(wb._file_panel is not None, "单证面板已构建")

# 提交一张单证 → 标签与开合状态均不变，且行对象不被销毁
fp = wb._file_panel
row_before = fp._row_order[0][0]
fid1 = next(f["file_id"] for f in db.get_files(pid)
            if f.get("node_id") == 1 and f["doc_type"] == "required")
wb._toggle_file(fid1, True); app.processEvents()
check(wb._tab == "files", "提交单证后仍停在「单证清单」标签")
check(wb._right_collapsed is False, "提交单证后右栏保持展开")
check(wb._file_panel is fp and fp._row_order[0][0] is row_before,
      "提交单证后面板/行对象未被重建（卡死根因回归）")

# 收起右栏后再提交另一张 → 仍收起
wb._toggle_right(); app.processEvents()
check(wb._right_collapsed is True, "可收起右栏")
fid2 = next(f["file_id"] for f in db.get_files(pid)
            if f["doc_type"] == "required" and f["file_id"] != fid1)
wb._toggle_file(fid2, True); app.processEvents()
check(wb._right_collapsed is True, "收起状态提交单证后不被自动展开")
wb.close()

# ── 2) 海运压缩表头单调去重 ──
print("== 2) 海运压缩表头 单调/去重 ==")
from ui.widgets.gantt_grid import GanttGrid
nodes = db.get_nodes(pid)

def m_parse(t):
    m, d = t.split("/")
    return int(m) * 100 + int(d)

for d in (11, 15, 20, 25):
    set_simulated_today(date(2026, 9, d))
    from services.clock import get_today
    g = GanttGrid(nodes, get_today(), export_port="QD")
    labels = g._canvas._sea_labels
    real = [m_parse(t) for t in labels if t not in ("…", "")]
    mono = all(real[i] < real[i + 1] for i in range(len(real) - 1))
    uniq = len(real) == len(set(real))
    check(mono, f"今日9/{d} 海运表头严格递增 {labels}")
    check(uniq, f"今日9/{d} 海运表头无重复 {labels}")
    check(labels[-1] != "10/26", f"今日9/{d} 海运末列不再重复显示 10/26（让位境外）")

# ── 3) 单证齐 + 已过期末 → 自动完成 ──
print("== 3) 单证齐+过期末 → 自动完成 ==")
set_simulated_today(date(2026, 9, 10))          # 早于 ETA，已过节点1 plan_end(09-07)
req1 = [f for f in db.get_files(pid)
        if f.get("node_id") == 1 and f["doc_type"] == "required"]
for f in req1:
    db.update_file(f["file_id"], status="submitted", submitted_date="2026-09-06")
sync_active_projects()
st1 = next(n for n in db.get_nodes(pid) if n["node_id"] == 1)["status"]
check(st1 == "Done", "节点1 必填单证齐 + 已过结束日 → 自动 Done")
# 撤勾一张 → 回退
db.update_file(req1[0]["file_id"], status="pending", submitted_date=None)
sync_active_projects()
st1b = next(n for n in db.get_nodes(pid) if n["node_id"] == 1)["status"]
check(st1b == "Pending", "必填单证被撤勾 → 自动回退为 Pending")

print("✅ FIX OK" if not FAILED else f"❌ {len(FAILED)} FAILED")
for m in FAILED:
    print("  -", m)
shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(0 if not FAILED else 1)
