import sys, os
sys.path.insert(0, os.getcwd())
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont
import db
import app as appmod

db.init_db()
appmod.seed_demo()

app = QApplication(sys.argv)
from ui.theme import GLOBAL_QSS, APP_FONT, APP_FONT_SIZE, ensure_check_asset
app.setFont(QFont(APP_FONT, APP_FONT_SIZE))
ensure_check_asset()
app.setStyleSheet(GLOBAL_QSS)

from ui.widgets.gantt_grid import GanttGrid
projs = db.get_projects_by_status("Active")
if projs:
    nodes = db.get_nodes(projs[0]["project_id"])
    print("node count:", len(nodes))
    for n in nodes:
        print(n["node_id"], n["area"], n["plan_start"], "->", n["plan_end"])
    g = GanttGrid(nodes, export_port=projs[0].get("export_port"))
    g.resize(1000, 560)
    g.show()
    app.processEvents()
    img = g.grab()
    img.save("_gantt.png")
    print("saved", img.width(), "x", img.height())
print("DONE")