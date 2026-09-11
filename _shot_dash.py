"""截图：新看板摘要卡 + 甘特工作台（单批次 / 多批次合并 / 空批次）
复制真实库到临时库再渲染，不动真实数据。"""
import os, sys, shutil, tempfile
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import db
_ROOT = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="shotdash_")
db.DB_PATH = os.path.join(_TMP, "t.db")
db._conn = None
shutil.copy2(os.path.join(_ROOT, "data", "logistics.db"), db.DB_PATH)
db.init_db()

from services import batches as bsvc
from services.clock import set_simulated_today
from datetime import date

from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)
from ui.theme import GLOBAL_QSS, ensure_check_asset
ensure_check_asset()
app.setStyleSheet(GLOBAL_QSS)
from ui.pages.dashboard_page import DashboardPage

if not db.get_projects_by_status("Active"):
    from mock_data import seed_demo_project
    seed_demo_project()

dp = DashboardPage(); dp.resize(1500, 1000); dp.show(); dp.refresh(); app.processEvents()
card = [dp.list_layout.itemAt(i).widget() for i in range(dp.list_layout.count())
        if dp.list_layout.itemAt(i).widget()
        and dp.list_layout.itemAt(i).widget().__class__.__name__ == "ProjectCard"][0]
PID = card._project["project_id"]
B01 = card._current_batch_id

ok1 = dp.grab().save("_shot_dash_collapsed.png")
print("摘要卡（无折叠）→ _shot_dash_collapsed.png", ok1,
      f"（卡片高 {card.height()}px，批次下拉 {card.batch_combo.width()}×"
      f"{card.batch_combo.height()}px）")

# 单批次工作台
wb = dp.open_workbench(PID, B01, "single"); app.processEvents()
wb.resize(1460, 940); app.processEvents()
g = wb._gantt_grid
print("  单批次甘特：整幅 %dpx / 可视 %dpx（窗口内纵向滚动）" % (g.auto_height(), g.height()))
ok2 = wb.grab().save("_shot_wb_single.png")
print("单批次工作台 → _shot_wb_single.png", ok2)

# 全批次总览（直接用库里现有批次，不做任何造数）
wb.set_mode("merged"); app.processEvents()
m = wb._overview.canvas()
print("  全批次总览：%d 批次 / %d 列 / 画布 %d×%d / 汇总 %s / 冲突 %s"
      % (len(m._rows), len(m._axis_cols), m._w, m._h,
         wb._overview_summary.text(), wb._conflict_count.text()))
ok3 = wb.grab().save("_shot_wb_overview.png")
print("全批次总览（周刻度）→ _shot_wb_overview.png", ok3)

wb.set_zoom("day"); app.processEvents()
m = wb._overview.canvas()
print("  日刻度：%d 列 / 画布宽 %dpx" % (len(m._axis_cols), m._w))
ok4 = wb.grab().save("_shot_wb_overview_day.png")
print("全批次总览（日刻度）→ _shot_wb_overview_day.png", ok4)
wb.set_zoom("week")

# 空批次工作台（新批次尚未排期）—— 仅在临时库副本里造
B03 = db.create_batch(PID)
db.update_project(PID, current_batch_id=B03["batch_id"])
card._load(); card._after_change()
wb.sync_batch(B03["batch_id"]); wb.set_mode("single"); app.processEvents()
ok5 = wb.grab().save("_shot_wb_empty.png")
print("空批次工作台 → _shot_wb_empty.png", ok5,
      f"（甘特 {wb._gantt_grid.auto_height()}px，补齐按钮 "
      f"{'有' if wb._fill_btn.isVisibleTo(wb) else '无'}）")

app.quit()
shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(0 if all([ok1, ok2, ok3, ok4, ok5]) else 1)
