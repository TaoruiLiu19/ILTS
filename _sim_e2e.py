"""端到端：设置模拟时间 → 主看板勾选单证 → op_log 时间戳跟随模拟日期"""
import os, sys, shutil, tempfile

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
_TMP = tempfile.mkdtemp(prefix="e2e_")
db.DB_PATH = os.path.join(_TMP, "t.db")
db._conn = None
db.init_db()

from datetime import date
from services.clock import set_simulated_today, reset
from mock_data import DEMO_PROJECT, DEMO_NODES, get_demo_schedule
from services.file_checklist import seed as seed_files

pid = DEMO_PROJECT["project_id"]
plan = get_demo_schedule()
db.insert_project({k: DEMO_PROJECT[k] for k in
                   ("project_id", "project_name", "country", "export_port",
                    "etd", "eta", "buffer_days")})
nodes = []
for n in DEMO_NODES:
    s, e = plan[n["node_id"]]
    nodes.append({"node_id": n["node_id"], "node_name": n["node_name"],
                  "role_label": n["role_label"], "seq": n["seq"], "area": n["area"],
                  "default_duration": n["duration"], "duration": n["duration"],
                  "plan_start": s, "plan_end": e, "remark": n.get("remark", "")})
db.insert_nodes(pid, nodes)
seed_files(pid, DEMO_PROJECT["country"], DEMO_PROJECT["export_port"], plan)

from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)
from ui.theme import GLOBAL_QSS, APP_FONT, APP_FONT_SIZE, ensure_check_asset
from PySide6.QtGui import QFont
app.setFont(QFont(APP_FONT, APP_FONT_SIZE))
ensure_check_asset()
app.setStyleSheet(GLOBAL_QSS)

from ui.pages.dashboard_page import DashboardPage

SIM = date(2026, 12, 25)
set_simulated_today(SIM)          # 模拟“测试时间”按钮的效果

page = DashboardPage()
page.resize(1440, 900)
page.show()
page.refresh()
app.processEvents()
card = [page.list_layout.itemAt(i).widget() for i in range(page.list_layout.count())
        if page.list_layout.itemAt(i).widget() is not None
        and page.list_layout.itemAt(i).widget().__class__.__name__ == "ProjectCard"][0]
wb = page.open_workbench(pid, None, "single")
app.processEvents()
check_card = card  # 卡片保留用于对照（摘要不再折叠）

fid = db.get_files(pid)[0]["file_id"]
doc = db.get_files(pid)[0]["doc_name"]
wb._toggle_file(fid, True)
app.processEvents()

rows = [r for r in db.get_op_log_range(pid) if r["kind"] == "file_submit"]
FAILED = []


def check(c, m):
    print(("PASS  " if c else "FAIL  ") + m)
    if not c:
        FAILED.append(m)


check(len(rows) == 1, "勾选后写入 1 条 file_submit")
check(rows and rows[0]["created_at"].startswith("2026-12-25"),
      f"op_log 时间戳跟随模拟日期（{rows[0]['created_at'] if rows else '—'}）")
check(rows and rows[0]["created_at"][:10] != date.today().strftime("%Y-%m-%d"),
      "时间戳不是真实今日（证明模拟生效）")
f = next(x for x in db.get_files(pid) if x["file_id"] == fid)
check(f["submitted_date"] == "2026-12-25",
      f"单证 submitted_date 也跟随模拟日期（{f['submitted_date']}）")

# 行内状态同步
row = wb._file_panel._rows.get(fid)
check(row is not None and row.status_label.text().startswith("已提交"),
      "界面行状态已同步为「已提交」")

reset()
print("\n" + ("✅ 端到端通过" if not FAILED else f"❌ {len(FAILED)} 项失败"))
shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(0 if not FAILED else 1)
