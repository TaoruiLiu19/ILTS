"""
优化方案验收 · UI 冒烟（S8 + 页面构建 + 卡片位移链路）
运行：QT_QPA_PLATFORM=offscreen python _opt_test_ui.py
产出：_opt_gantt.png（甘特高亮截图，供人工目检）
"""

import os
import sys
import shutil
import tempfile
from datetime import date

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
from services.clock import set_simulated_today

_TMP = tempfile.mkdtemp(prefix="opt_ui_")
db.DB_PATH = os.path.join(_TMP, "t.db")
db._conn = None
db.init_db()

FAILED = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")
    if not cond:
        FAILED.append(msg)


# ── seed（与 app.seed_demo 等价 + 货物/班轮） ──
from mock_data import DEMO_PROJECT, DEMO_NODES, get_demo_schedule
from services.file_checklist import bootstrap
from services.cargo_check import normalize_item
from services.scheduler import apply_shift

pid = DEMO_PROJECT["project_id"]
plan = get_demo_schedule()
db.insert_project({**{k: DEMO_PROJECT[k] for k in
                      ("project_id", "project_name", "country", "export_port",
                       "etd", "eta", "buffer_days")}})
nodes = []
for n in DEMO_NODES:
    s, e = plan[n["node_id"]]
    nodes.append({"node_id": n["node_id"], "node_name": n["node_name"],
                  "role_label": n["role_label"], "seq": n["seq"], "area": n["area"],
                  "default_duration": n["duration"], "duration": n["duration"],
                  "plan_start": s, "plan_end": e, "remark": n.get("remark", "")})
db.insert_nodes(pid, nodes)
db.insert_files(pid, bootstrap(DEMO_PROJECT["country"], DEMO_PROJECT["export_port"], plan))
db.insert_cargo_items(pid, [
    normalize_item({"item_name": "光伏组件（叠层）", "qty": 20, "unit": "套",
                    "dim_l": 2.3, "dim_w": 1.1, "dim_h": 0.4, "weight_kg": 1200}),
    normalize_item({"item_name": "主变压器", "qty": 1, "unit": "台",
                    "dim_l": 6.0, "dim_w": 2.5, "dim_h": 3.2,
                    "weight_kg": 118000, "packaging": "裸装"}),
])
db.upsert_vessel(pid, vessel_name="COSCO SHIPPING UNIVERSE", voyage="081W")

from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)

from ui.theme import GLOBAL_QSS, APP_FONT, APP_FONT_SIZE, ensure_check_asset
from PySide6.QtGui import QFont
app.setFont(QFont(APP_FONT, APP_FONT_SIZE))
ensure_check_asset()
app.setStyleSheet(GLOBAL_QSS)

# ── 页面构建 ──
print("== 页面构建 ==")
from ui.pages.new_project_page import NewProjectPage
from ui.pages.dashboard_page import DashboardPage
from ui.pages.completed_page import CompletedPage
from ui.dialogs import CargoDialog, VesselDialog

np_page = NewProjectPage()
np_page.resize(1280, 900)
np_page.show()
app.processEvents()
check(np_page._cargo_rows == [] and len(np_page._node_widgets) == 12,
      "新建项目页构建（12 节点行 + 空台账）")
np_page._fill_samples()
check(len(np_page._cargo_rows) == 2, "货物台账预填 2 行示例")
np_page._add_cargo_row()
check(len(np_page._cargo_rows) == 3, "台账可增行")
np_page._remove_cargo_row(np_page._cargo_rows[-1])
check(len(np_page._cargo_rows) == 2, "台账可删行")
np_page._update_cargo_summary()
check("142" in np_page.cargo_summary.text() and "超限 1 项" in np_page.cargo_summary.text(),
      "台账汇总显示 142t 合计 + 超限 1 项")

dp_page = DashboardPage()
dp_page.resize(1440, 900)
dp_page.show()
dp_page.refresh()
app.processEvents()
cards = [dp_page.list_layout.itemAt(i).widget()
         for i in range(dp_page.list_layout.count())
         if dp_page.list_layout.itemAt(i).widget() is not None
         and dp_page.list_layout.itemAt(i).widget().__class__.__name__ == "ProjectCard"]
check(len(cards) == 1, "主看板渲染 1 张项目卡")
card = cards[0]
check("超限" in card.cargo_label.text(), "卡片头显示货物/超限微统计")
check("COSCO" in card.vessel_label.text(), "卡片头显示船名")

print("\n== 动态调整面板 + 卡片位移 ==")
card._toggle_expand()
app.processEvents()
check(card.expand_area.isVisible(), "展开卡片")
card._step_spin.setValue(1)
card._shift_node(6, 1)          # 境外节点⑥ 推迟 1 天（成功路径，不弹窗）
app.processEvents()
check(card._op_note and "推迟" in card._op_note.text(), "位移操作提示已刷新")
from services.clock import get_today
eta = db.get_project(pid)["eta"]
check(eta == DEMO_PROJECT["eta"], "节点⑥+1 不影响 ETA")

print("\n== 对话框构建 ==")
cd = CargoDialog(pid)
check(cd.table.rowCount() == 2, "货物台账对话框加载 2 行")
cd._add_row()
cd._del_selected() if cd.table.selectedItems() else None
cd.deleteLater()

vd = VesselDialog(pid)
check(vd.vessel_name.text() == "COSCO SHIPPING UNIVERSE", "班轮对话框预填船名")
check(vd.history_list.count() >= 1, "船位历史列表可显示")
vd.deleteLater()

print("\n== S8 甘特高亮（今日置为节点10结束次日） ==")
n10 = next(n for n in db.get_nodes(pid) if n["node_id"] == 10)
end10 = n10["plan_end"]
y, m, d = (int(x) for x in end10.split("-"))
set_simulated_today(date(y, m, d) + __import__("datetime").timedelta(days=1))
from services.clock import get_today as gt
from ui.widgets.gantt_grid import GanttGrid
today = gt()
gantt = GanttGrid(db.get_nodes(pid), today,
                  export_port=DEMO_PROJECT["export_port"],
                  over_count=1, buffer_days=4)
app.processEvents()
tags = gantt._canvas._row_tags
check(any("吊装预警" in t for t, _ in tags.get(3, [])), "节点3 吊装预警标签（超限件）")
check(any("最长段" in t for t, _ in tags.get(5, [])), "海运5 标「最长段」（瓶颈）")
check(any("已耗缓冲" in t for t, _ in tags.get(9, [])), "节点9 逾期提示已耗缓冲")
check(any("已耗缓冲" in t for t, _ in tags.get(10, [])), "节点10 逾期提示已耗缓冲")
gantt.resize(1240, 660)
gantt.show()
app.processEvents()
img = gantt.grab()
img.save("_opt_gantt.png")
print(f"  saved _opt_gantt.png {img.width()}x{img.height()}")

print("\n" + ("✅ ALL UI PASS" if not FAILED else f"❌ {len(FAILED)} FAILED"))
for f in FAILED:
    print("  -", f)
app.quit()
shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(0 if not FAILED else 1)
