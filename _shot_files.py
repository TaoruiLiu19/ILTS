import os, sys, shutil, tempfile
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
from services.clock import set_simulated_today
from datetime import date
set_simulated_today(date(2026, 9, 9))
_TMP = tempfile.mkdtemp(prefix="shotfiles_")
db.DB_PATH = os.path.join(_TMP, "t.db"); db._conn = None; db.init_db()
from mock_data import DEMO_PROJECT, DEMO_NODES, get_demo_schedule
from services.file_checklist import seed as seed_files
pid = DEMO_PROJECT["project_id"]
plan = get_demo_schedule()
db.insert_project({**{k: DEMO_PROJECT[k] for k in
                      ("project_id","project_name","country","export_port","etd","eta","buffer_days")}})
nodes=[]
for n in DEMO_NODES:
    s,e=plan[n["node_id"]]
    nodes.append({"node_id":n["node_id"],"node_name":n["node_name"],"role_label":n["role_label"],
                  "seq":n["seq"],"area":n["area"],"default_duration":n["duration"],
                  "duration":n["duration"],"plan_start":s,"plan_end":e,"remark":n.get("remark","")})
db.insert_nodes(pid,nodes)
seed_files(pid,DEMO_PROJECT["country"],DEMO_PROJECT["export_port"],plan)
from PySide6.QtWidgets import QApplication
app=QApplication(sys.argv)
from ui.pages.dashboard_page import DashboardPage
dp=DashboardPage(); dp.resize(1500,1000); dp.show(); dp.refresh(); app.processEvents()
cards=[dp.list_layout.itemAt(i).widget() for i in range(dp.list_layout.count())
       if dp.list_layout.itemAt(i).widget() and dp.list_layout.itemAt(i).widget().__class__.__name__=="ProjectCard"]
card=cards[0]                     # 摘要卡（保留断言用：卡片不再折叠）
wb = dp.open_workbench(pid, None, "single")
app.processEvents()
ok = bool(wb.grab().save("_shot_files.png"))
print("PASS  saved _shot_files.png（甘特工作台截图）" if ok else "FAIL  _shot_files.png 保存失败")
# 显式结束：离屏 Qt 进程不做 exec()，收尾必须自己走完，杜绝「打印完却不退出」
app.quit()
shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(0 if ok else 1)