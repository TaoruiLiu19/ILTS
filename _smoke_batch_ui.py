"""1A 界面离屏冒烟：遍历 5 个页面并截图。运行前先灌注演示数据。"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
from mock_data import seed_demo_project
from mock_completed import seed_completed_demo

db.init_db()
seed_demo_project()
seed_completed_demo()
# 防止启动时今日待办弹窗阻塞离屏流程
from services.clock import get_today
db.set_setting("last_alert_date", get_today().strftime("%Y-%m-%d"))

from PySide6.QtWidgets import QApplication
from ui.theme import GLOBAL_QSS, APP_FONT, APP_FONT_SIZE, ensure_check_asset
from ui.main_window import MainWindow

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_smoke_out")
os.makedirs(OUT, exist_ok=True)

app = QApplication(sys.argv)
from PySide6.QtGui import QFont
app.setFont(QFont(APP_FONT, APP_FONT_SIZE))
ensure_check_asset()
app.setStyleSheet(GLOBAL_QSS)

win = MainWindow()
win.resize(1440, 900)
win.show()

PAGES = {
    "home": "启动页",
    "dashboard": "主看板",
    "new_project": "新建项目",
    "completed": "已完成",
    "report": "生成报告",
}

results = []
for key in PAGES:
    win._navigate(key)
    win.repaint()
    app.processEvents()
    pm = win.grab()
    path = os.path.join(OUT, f"page_{key}.png")
    ok = pm.save(path)
    results.append((key, ok, pm.width(), pm.height()))
    print(f"[shot] {key:12s} ok={ok} {pm.width()}x{pm.height()}")

# 看板右侧面板局部截图（单证清单当前批次）
try:
    dp = win.dashboard_page
    if hasattr(dp, "file_panel") and dp.file_panel is not None:
        fp = dp.file_panel
        fp.repaint(); app.processEvents()
        p = fp.grab()
        ok = p.save(os.path.join(OUT, "dashboard_file_panel.png"))
        print(f"[panel] 单证清单面板 ok={ok} {p.width()}x{p.height()}")
        results.append(("file_panel", ok, p.width(), p.height()))
except Exception as e:
    print("[panel] 单证面板截图失败:", e)

print("\n===== 冒烟完成 %d 项（含 completed 项目看板应显示只读甘特）=====" % len(results))
fail = [r for r in results if not r[1]]
print("截图失败项:", fail if fail else "无")
win.close()
app.processEvents()