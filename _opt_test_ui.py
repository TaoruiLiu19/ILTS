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
from services import node_template as nt

# ── 节点锚点（一律按 node_key 定位，禁止硬编码 node_id —— D20/§5.2） ──
_TMPL = nt.template()
DOME_IDS = [n["node_id"] for n in _TMPL if n["area"] == "DOME"]
OVERSEA_IDS = [n["node_id"] for n in _TMPL if n["area"] == "OVERSEA"]
FIRST_OVERSEA = nt.by_key(nt.ARRIVAL_NOTICE)["node_id"]   # 境外首节点
NODE_COUNT = len(_TMPL)                                   # 模板节点数（15）


def _d(s):
    return date(*map(int, s.split("-")))


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
from services.file_checklist import seed as seed_files
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
seed_files(pid, DEMO_PROJECT["country"], DEMO_PROJECT["export_port"], plan)
db.insert_cargo_items(pid, [
    normalize_item({"item_name": "光伏组件（叠层）", "qty": 20, "unit": "套",
                    "dim_l": 2.3, "dim_w": 1.1, "dim_h": 0.4, "weight_kg": 1200}),
    normalize_item({"item_name": "主变压器", "qty": 1, "unit": "台",
                    "dim_l": 6.0, "dim_w": 2.5, "dim_h": 3.2,
                    "weight_kg": 118000, "packaging": "裸装"}),
])
db.upsert_vessel(pid, vessel_name="COSCO SHIPPING UNIVERSE", voyage="081W")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
app = QApplication(sys.argv)

# 看门狗：位移被守卫拦截时产品会弹模态 QMessageBox，离屏无人点击会永久挂起。
# 兜底 120s 强制失败退出，保证脚本一定「跑完 + 有明确结论」。
WATCHDOG_MS = 120000


def _watchdog():
    print("❌ 看门狗超时（120s）：出现模态弹窗或阻塞 → 强制失败退出")
    os._exit(2)


QTimer.singleShot(WATCHDOG_MS, _watchdog)


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
check(np_page._cargo_rows == [] and len(np_page._node_widgets) == NODE_COUNT,
      f"新建项目页构建（{NODE_COUNT} 节点行 + 空台账）")
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

print("\n== 动态调整面板 + 工作台位移 ==")
wb = dp_page.open_workbench(pid, None, "single")
app.processEvents()
check(wb.isVisible(), "甘特工作台已打开（卡片不再折叠）")
wb._step_spin.setValue(1)
_oversea_before = {n["node_id"]: n["plan_start"] for n in db.get_nodes(pid)
                   if n["node_id"] in OVERSEA_IDS}
wb._shift_node(FIRST_OVERSEA, 1)   # 境外首节点 推迟 1 天（成功路径，不弹窗）
app.processEvents()
check(wb._op_note and "推迟" in wb._op_note.text(), "位移操作提示已刷新")
from services.clock import get_today
eta = db.get_project(pid)["eta"]
check(eta == DEMO_PROJECT["eta"], "境外节点+1 不影响 ETA")
_after = {n["node_id"]: n for n in db.get_nodes(pid)}
check(all((_d(_after[i]["plan_start"]) - _d(_oversea_before[i])).days == 1
          for i in OVERSEA_IDS), "境外全段顺延 +1（位移已生效）")

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

print("\n== S8 甘特高亮（今日置为最后一个缓冲提示节点结束日次日） ==")
# 缓冲消耗提示节点（§5.2 / node_template.BUFFER_HINT）：海关查验 + 堆存费
_buf_end = max(n["plan_end"] for n in db.get_nodes(pid)
               if n["node_key"] in nt.BUFFER_HINT)
y, m, d = (int(x) for x in _buf_end.split("-"))
set_simulated_today(date(y, m, d) + __import__("datetime").timedelta(days=1))
from services.clock import get_today as gt
from ui.widgets.gantt_grid import GanttGrid
today = gt()
gantt = GanttGrid(db.get_nodes(pid), today,
                  export_port=DEMO_PROJECT["export_port"],
                  over_count=1, buffer_days=4)
app.processEvents()
tags = gantt._canvas._row_tags


def tag_texts(node_key):
    """按 node_key 取该行标签文案（模板改序/改号不影响本断言）"""
    return [t for t, _ in tags.get(nt.by_key(node_key)["node_id"], [])]


check(any("吊装预警" in t for t in tag_texts(nt.LASHING)),
      "装箱/加固（吊装预警节点）标「吊装预警」（超限件）")
check(any("最长段" in t for t in tag_texts(nt.SEA_TRANSIT)),
      "海运节点标「最长段」（瓶颈）")
check(any("已耗缓冲" in t for t in tag_texts(nt.CUSTOMS_INSPECT)),
      "海关查验（缓冲提示节点）逾期提示已耗缓冲")
check(any("已耗缓冲" in t for t in tag_texts(nt.STORAGE_FEE)),
      "堆存费（缓冲提示节点）逾期提示已耗缓冲")
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
