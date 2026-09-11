"""新建页定向校验：字段存在性 / 预览 / 复制模板 / 保存落库(批次+路由+node_key)。"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
from mock_data import seed_demo_project
from mock_completed import seed_completed_demo
db.init_db()
seed_demo_project()
seed_completed_demo()
from services.clock import get_today
db.set_setting("last_alert_date", get_today().strftime("%Y-%m-%d"))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont
from ui.theme import GLOBAL_QSS, APP_FONT, APP_FONT_SIZE
app = QApplication(sys.argv)
app.setFont(QFont(APP_FONT, APP_FONT_SIZE))
app.setStyleSheet(GLOBAL_QSS)

from ui.pages.new_project_page import NewProjectPage

PASS = []
def check(name, cond, detail=""):
    PASS.append(name)
    print(f"[{'OK' if cond else 'FAIL'}] {name} {('· '+str(detail)) if detail else ''}")

pg = NewProjectPage()

# 1. 新字段/控件存在
for attr in ("copy_combo","copy_btn","mode_combo","batch_no_edit","batch_name_edit",
             "booking_no_edit","mbl_no_edit","batch_etd_edit","batch_eta_edit"):
    check(f"存在控件 {attr}", hasattr(pg, attr))

# 2. 预览为绿色成功文案（非错误）
pg._update_preview()
txt = pg.preview_label.text()
check("预览生成成功文案", "将生成" in txt and "海运" in txt, txt.strip())
mode = pg.mode_combo.currentData()
check("线路模式默认 SEA", mode == "SEA", mode)

# 2b. 各节点行显示建议起止日期
pg._update_preview()
node_dates = [pg._node_widgets[i][2].text() for i in sorted(pg._node_widgets)]
check("所有节点行有建议日期", all(len(d) >= 11 for d in node_dates), f"首节点 {node_dates[0]}",
      ) if node_dates else check("节点行无日期", False)
# 节点1 提空箱日期 应早于 海运start；海运为唯一 SEA 段
sea_start = [d for i, d in enumerate(node_dates) if i == 5][0] if len(node_dates) > 5 else ""
check("海运(节点6)日期存在", bool(sea_start), sea_start)

# 3. 复制模板：选 demo 项目并加载
demo = db.get_project("demo-qd-br-001")
idx = pg.copy_combo.findData(demo["project_id"])
check("下拉含 demo 项目", idx >= 0)
pg.copy_combo.setCurrentIndex(idx)
pg._load_template()
dur0 = pg._node_widgets[1][1].value()  # 节点1 提空箱
check("模板回填节点时长", dur0 == 1, dur0)
check("模板回填国家 BR", pg.country_combo.currentData() == "BR")
check("名称加副本后缀", "副本" in pg.name_edit.text(), pg.name_edit.text())

# 3b. 货物复制路径：先填货物→存一个含货项目→再作模板加载
pg._add_cargo_row()
pg._add_cargo_row()
pg._cargo_rows[0].name_edit.setText("测试变压器")
pg._cargo_rows[0].weight_spin.setValue(88000)
pg._cargo_rows[1].name_edit.setText("测试光伏板")
pg._cargo_rows[1].weight_spin.setValue(5000)
pg.name_edit.setText("__货物源项目__")
pg.batch_no_edit.setText("SAMPLE-C01")
pg._save()
pg.refresh()  # 模拟回到页面，刷新复制下拉列表
c_src = [p for p in db.get_projects_by_status("Active") if "__货物源项目__" in p["project_name"]][0]
_src_bid = db.current_batch_id(c_src["project_id"])
# 清空当前页货物行再重新加载该源项目模板
for r in list(pg._cargo_rows):
    pg._remove_cargo_row(r)
pg.copy_combo.setCurrentIndex(pg.copy_combo.findData(c_src["project_id"]))
pg._load_template()
check("模板复制了货物台账", len(pg._cargo_rows) == 2, [r.name_edit.text() for r in pg._cargo_rows])

# 4. 保存落库：用未命名避开重复，改为写临时项目
pg.name_edit.setText("__冒烟批次项目__")
from services.clock import get_today
import datetime
pg.batch_etd_edit.setDate(pg.batch_etd_edit.date().addYears(1))  # 未来日期避免污染看板待办
pg.batch_eta_edit.setDate(pg.batch_etd_edit.date().addDays(46))
pg.batch_no_edit.setText("SMOKE-B99")
pg._save()  # navigate signal 忽略
# 找到刚创建项目
projs = [p for p in db.get_projects_by_status("Active") if "__冒烟批次项目__" in p["project_name"]]
check("保存创建了项目", len(projs) == 1, len(projs))
if projs:
    pid = projs[0]["project_id"]
    bid = db.current_batch_id(pid)
    b = db.get_batch(bid)
    check("批次号正确", b["batch_no"] == "SMOKE-B99", b["batch_no"])
    route = db.get_route(bid)
    check("线路路由已落库", route and route["mode_primary"] == "SEA" and route["etd"], 
          (route or {}).get("mode_primary"))
    nodes = db.get_nodes_by_batch(bid)
    check("节点含 node_key 且全非空", all(n["node_key"] for n in nodes), f"{len(nodes)} 节点")
    files = db.get_files(pid, bid)
    check("单证清单已落库", len(files) > 0, len(files))
    cargo = db.get_cargo_items(pid, bid)
    check("货物台账已复制", len(cargo) >= 1, len(cargo))

# 清理测试项目，保证可重复运行
conn = db.get_conn()
test_pids = [r["project_id"] for r in conn.execute(
    "SELECT project_id FROM projects WHERE project_name LIKE '__%' ").fetchall()]
if test_pids:
    ph = ",".join("?" * len(test_pids))
    for pid in test_pids:
        btds = conn.execute("SELECT batch_id FROM batches WHERE project_id=?", (pid,)).fetchall()
        for b in btds:
            bid = b["batch_id"]
            for t in ("nodes", "cargo_items", "files", "vessel", "batch_routes",
                      "batch_schedule_changes", "shift_history", "op_log", "file_submissions"):
                try: conn.execute(f"DELETE FROM {t} WHERE batch_id=?", (bid,))
                except Exception: pass
            conn.execute("DELETE FROM batches WHERE batch_id=?", (bid,))
    conn.execute(f"DELETE FROM projects WHERE project_id IN ({ph})", test_pids)
    conn.commit()
    print(f"[cleanup] 删除 {len(test_pids)} 个测试项目")

print("\n===== 新建页校验 %d 项 =====" % len(PASS))