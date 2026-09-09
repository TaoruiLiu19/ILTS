import os, sys, tempfile
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
from services.clock import set_simulated_today
from datetime import date
set_simulated_today(date(2026, 5, 10))

_TMP = tempfile.mkdtemp(prefix="dash2col_")
db.DB_PATH = os.path.join(_TMP, "t.db")
db._conn = None
db.init_db()
FAILED = []

def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")
    if not cond: FAILED.append(msg)

from mock_data import DEMO_PROJECT, DEMO_NODES, get_demo_schedule
from services.file_checklist import bootstrap
from services.cargo_check import normalize_item
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
db.insert_files(pid,bootstrap(DEMO_PROJECT["country"],DEMO_PROJECT["export_port"],plan))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QPoint, QPointF
from PySide6.QtGui import QWheelEvent
app=QApplication(sys.argv)
from ui.pages.dashboard_page import DashboardPage
from ui.widgets.scoped_scroll import ScopedScrollArea

dp=DashboardPage(); dp.resize(1440,900); dp.show(); dp.refresh(); app.processEvents()
cards=[dp.list_layout.itemAt(i).widget() for i in range(dp.list_layout.count())
       if dp.list_layout.itemAt(i).widget() and dp.list_layout.itemAt(i).widget().__class__.__name__=="ProjectCard"]
check(len(cards)==1,"渲染 1 张项目卡")
card=cards[0]

print("== 展开 / 两栏构建 ==")
card._toggle_expand(); app.processEvents()
check(card.expand_area.isVisible(),"展开区可见")
check(card._gantt_grid is not None,"左栏甘特已建")
check(card._gantt_grid.height()==card._gantt_grid.auto_height(),"甘特完整高度显示")
check(card._gantt_grid.verticalScrollBarPolicy()==Qt.ScrollBarAlwaysOff,"甘特垂直滚动条已关闭（无滚轮）")
check(card._tabbar is not None and card._tabbar.count()==2,"右栏含 2 个标签")
check(card._shift_page is not None and card._files_page is not None,"两个面板已预构建")

print("== 标签切换 ==")
card._set_active_panel("shift", sync_tabbar=True); app.processEvents()
check(card._tab=="shift","切到动态调整")
check(card._tabbar.currentIndex()==0,"标签高亮同步")
card._set_active_panel("files", sync_tabbar=True); app.processEvents()
check(card._tab=="files" and card._tabbar.currentIndex()==1,"切到单证清单")
check(card._file_panel is not None,"单证面板可访问")

print("== 甘特点击联动 ==")
card._set_active_panel("shift", sync_tabbar=True)
n=db.get_nodes(pid)
card._on_gantt_activate(n[5]); app.processEvents()
check(card._tab=="files","点甘特节点自动切到单证标签")
check(card._focused_node==n[5]["node_id"],"节点已钉住")
card._on_gantt_activate(n[5]); app.processEvents()
check(card._focused_node is None,"再次点击同节点取消钉住")

print("== 右栏收纳 ==")
check(not card._right_collapsed and card._right_panel.width()>400,"默认展开（宽 >400）")
card._toggle_right(); app.processEvents()
check(card._right_collapsed,"已收起")
check(card._right_panel.width()<60,"收起时缩为窄条")
check(not card._tabbar.isVisible() and not card._panel_host.isVisible(),"标签与面板已隐藏")
card._toggle_right(); app.processEvents()
check(not card._right_collapsed and card._tabbar.isVisible(),"再次展开恢复")

print("== ScopedScrollArea 滚轮边界吞掉 ==")
from PySide6.QtCore import QPoint
class E:
    def __init__(s, dy): s.dy=dy; s.accepted=False
    def angleDelta(s): return QPoint(0, s.dy)
    def accept(s): s.accepted=True
    def ignore(s): pass
# 构造一个足够高的容器，滚动到顶/底后滚轮不应使值继续变化
sc=ScopedScrollArea(); sc.setWidgetResizable(True)
body=__import__("PySide6.QtWidgets",fromlist=["QWidget"]).QWidget()
lay=__import__("PySide6.QtWidgets",fromlist=["QVBoxLayout"]).QVBoxLayout(body)
for i in range(50):
    lay.addWidget(__import__("PySide6.QtWidgets",fromlist=["QCheckBox"]).QCheckBox(f"r{i}"))
sc.setWidget(body); sc.resize(300,120); sc.show(); app.processEvents()
vb=sc.verticalScrollBar()
vb.setValue(vb.maximum())                      # 滚到底
ev=E(-120)
sc.wheelEvent(ev)                               # 继续向下滚（负）
check(vb.value()==vb.maximum() and ev.accepted,"底部继续滚轮被吞（值不变且已接受）")
vb.setValue(vb.minimum())
ev2=E(120)
sc.wheelEvent(ev2)                              # 顶部反向滚（正）
check(vb.value()==vb.minimum() and ev2.accepted,"顶部滚轮被吞（值不变且已接受）")

print("== 单证勾选后不重建甘特（性能） ==")
card._set_active_panel("files")
g0 = card._gantt_grid
fid=db.get_files(pid)[0]["file_id"]
card._toggle_file(fid,True); app.processEvents()
check(card._tab=="files","勾选单证后仍停留在单证标签")
check(card._gantt_grid is g0,"单证勾选未重建甘特（避免卡顿）")
check(card._chip and card._chip["missing"].text() is not None,"统计字牌已轻量刷新")
card._toggle_file(fid,False); app.processEvents()
check(card._gantt_grid is g0,"撤交同样不重建甘特")
check(card._focused_node is None,"重建后未误钉节点")

print("\n"+("PASS ALL" if not FAILED else f"FAIL {len(FAILED)}"))
for f in FAILED: print(" -",f)
sys.exit(0 if not FAILED else 1)