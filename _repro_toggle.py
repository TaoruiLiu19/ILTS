"""回归：主看板单证清单勾选/取消 —— 不再崩溃、不再卡死"""
import os, sys, time, shutil, tempfile, traceback

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
_TMP = tempfile.mkdtemp(prefix="repro_")
db.DB_PATH = os.path.join(_TMP, "t.db")
db._conn = None
db.init_db()

from mock_data import DEMO_PROJECT, DEMO_NODES, get_demo_schedule
from services.file_checklist import bootstrap

pid = DEMO_PROJECT["project_id"]
plan = get_demo_schedule()
db.insert_project({k: DEMO_PROJECT[k] for k in
                   ("project_id", "project_name", "country", "export_port", "etd", "eta", "buffer_days")})
nodes = []
for n in DEMO_NODES:
    s, e = plan[n["node_id"]]
    nodes.append({"node_id": n["node_id"], "node_name": n["node_name"],
                  "role_label": n["role_label"], "seq": n["seq"], "area": n["area"],
                  "default_duration": n["duration"], "duration": n["duration"],
                  "plan_start": s, "plan_end": e, "remark": n.get("remark", "")})
db.insert_nodes(pid, nodes)
db.insert_files(pid, bootstrap(DEMO_PROJECT["country"], DEMO_PROJECT["export_port"], plan))

from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)
from ui.theme import GLOBAL_QSS, APP_FONT, APP_FONT_SIZE, ensure_check_asset
from PySide6.QtGui import QFont
app.setFont(QFont(APP_FONT, APP_FONT_SIZE))
ensure_check_asset()
app.setStyleSheet(GLOBAL_QSS)

from ui.pages.dashboard_page import DashboardPage

page = DashboardPage()
page.resize(1440, 900)
page.show()
page.refresh()
app.processEvents()

card = [page.list_layout.itemAt(i).widget() for i in range(page.list_layout.count())
        if page.list_layout.itemAt(i).widget() is not None
        and page.list_layout.itemAt(i).widget().__class__.__name__ == "ProjectCard"][0]
card._toggle_expand()
app.processEvents()

FAILED = []


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAILED.append(msg)


fp = card._file_panel
rows = [r for r, _ in fp._row_order]
print(f"文件行数 = {len(rows)}  节点数 = {len(card._nodes)}")

print("\n== 逐个勾选（模拟用户点击 checkbox）==")
times = []
for i, row in enumerate(rows):
    t0 = time.perf_counter()
    try:
        row.checkbox.setChecked(True)
        app.processEvents()
    except Exception:
        print(f"  [{i}] 异常:")
        traceback.print_exc()
        FAILED.append(f"勾选第 {i} 行异常")
        break
    times.append((time.perf_counter() - t0) * 1000)
check(len(times) == len(rows), f"全部 {len(rows)} 行勾选无异常")
print(f"  单次耗时 最大 {max(times):.1f} ms / 平均 {sum(times)/len(times):.1f} ms")

print("\n== 逐个取消 ==")
times = []
for i, row in enumerate(rows):
    t0 = time.perf_counter()
    try:
        row.checkbox.setChecked(False)
        app.processEvents()
    except Exception:
        print(f"  [{i}] 异常:")
        traceback.print_exc()
        FAILED.append(f"取消第 {i} 行异常")
        break
    times.append((time.perf_counter() - t0) * 1000)
check(len(times) == len(rows), f"全部 {len(rows)} 行取消无异常")
print(f"  单次耗时 最大 {max(times):.1f} ms / 平均 {sum(times)/len(times):.1f} ms")

print("\n== 同一行连续快速切换 40 次 ==")
t0 = time.perf_counter()
try:
    for k in range(40):
        rows[0].checkbox.setChecked(k % 2 == 0)
        app.processEvents()
    ok = True
except Exception:
    ok = False
    traceback.print_exc()
check(ok, "连续快速切换无异常")
print(f"  总耗时 {(time.perf_counter()-t0)*1000:.0f} ms")

print("\n== op_log：同一单证同一天只留 1 行最终态 ==")
logs = db.get_op_log_range(pid)
docs = {}
for r in logs:
    if r["kind"] in ("file_submit", "file_withdraw"):
        docs.setdefault((r["subject"], r["created_at"][:10]), []).append(r)
check(all(len(v) == 1 for v in docs.values()),
      f"每（单证, 日期）只有 1 行（共 {len(docs)} 组）")
last = db.last_file_actions(pid)
check(len(last) == len({r["subject"] for r in logs if r["kind"].startswith("file_")}),
      "last_file_actions 每张单证只返回最后一条")

print("\n== 勾选后行内状态同步 ==")
target = rows[3]
target.checkbox.setChecked(True)
app.processEvents()
check(target.status_label.text().startswith("已提交"), "行状态文案变为「已提交」")
target.checkbox.setChecked(False)
app.processEvents()
check(not target.status_label.text().startswith("已提交"), "取消后行状态文案回退")

print("\n" + ("✅ 回归通过" if not FAILED else f"❌ {len(FAILED)} 项失败"))
for m in FAILED:
    print("  -", m)
shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(0 if not FAILED else 1)
