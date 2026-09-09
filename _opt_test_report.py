"""
报告功能验收自测（纯逻辑，离屏）
覆盖：op_log 埋点/白名单、文件审计覆盖、位移当日净收敛、撤销分离、
日报/周报聚合、周对比空数据降级、编号按日重置、风险实时判定。

运行：python -X utf8 _opt_test_report.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
from services.clock import set_simulated_today
from services.oplog import record as oplog
from services import reporting
from services import report_exporter
from services.scheduler import apply_shift, undo_last_shift
from mock_data import DEMO_PROJECT, DEMO_NODES, get_demo_schedule
from services.file_checklist import bootstrap

_TMP = tempfile.mkdtemp(prefix="opt_report_")
db.DB_PATH = os.path.join(_TMP, "r.db")
db._conn = None
db.init_db()

FAILED = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")
    if not cond:
        FAILED.append(msg)


def seed_demo():
    pid = DEMO_PROJECT["project_id"]
    plan = get_demo_schedule()
    db.insert_project({
        "project_id": pid, "project_name": DEMO_PROJECT["project_name"],
        "country": DEMO_PROJECT["country"], "export_port": DEMO_PROJECT["export_port"],
        "etd": DEMO_PROJECT["etd"], "eta": DEMO_PROJECT["eta"],
        "buffer_days": DEMO_PROJECT["buffer_days"]})
    nodes = []
    for n in DEMO_NODES:
        s, e = plan[n["node_id"]]
        nodes.append({"node_id": n["node_id"], "node_name": n["node_name"],
                      "role_label": n["role_label"], "seq": n["seq"], "area": n["area"],
                      "default_duration": n["duration"], "duration": n["duration"],
                      "plan_start": s, "plan_end": e})
    db.insert_nodes(pid, nodes)
    files = bootstrap(DEMO_PROJECT["country"], DEMO_PROJECT["export_port"], plan)
    db.insert_files(pid, files)
    return pid


PID = seed_demo()
set_simulated_today("2026-09-09")


print("== 1. 白名单：浏览/未知动作不落库 ==")
before = len(db.get_op_log_all())
oplog("view_detail", PID, subject="节点1", detail="查看详情")
oplog("whatever", PID)
check(len(db.get_op_log_all()) == before, "未知 kind 不产生日志（T16）")


print("== 2. 文件审计覆盖（T14） ==")
doc = "商业发票"
oplog("file_submit", PID, node_id=1, subject=doc, detail="提交",
      created_at="2026-09-09 10:00")
oplog("file_withdraw", PID, node_id=1, subject=doc, detail="撤交",
      created_at="2026-09-09 11:00")
oplog("file_submit", PID, node_id=1, subject=doc, detail="提交",
      created_at="2026-09-09 15:00")
valid = db.get_op_log_range(PID, start="2026-09-09 00:00", end="2026-09-09 23:59")
submits = [r for r in valid if r["kind"] == "file_submit"]
f_with = [r for r in db.get_op_log_all(valid_only=False)
          if r["kind"] in ("file_submit", "file_withdraw")]
check(len(submits) == 1 and sum(bool(r["valid"]) for r in submits) == 1,
      "同日同单证仅保留最新有效提交（1 行）")
check(sum(1 for r in f_with) >= 3, "审计底稿保留 3 行（含撤交/被覆盖）")
withdrawn = any(r["kind"] == "file_withdraw" and r["valid"] for r in f_with)
check(submits[0]["created_at"].startswith("2026-09-09 15"), "最终动态显示 15 时提交")


print("== 3. 位移当日净收敛 + 撤销分离（T15） ==")
# 直接测试 record() 收敛：+3 再 -3 → 净 0 不记
oplog("node_shift", PID, node_id=6, subject="节点6", delta=3,
      detail="推迟 3 天", created_at="2026-09-09 09:00")
oplog("node_shift", PID, node_id=6, subject="节点6", delta=-3,
      detail="提前 3 天", created_at="2026-09-09 14:00")
shifts = [r for r in db.get_op_log_range(PID) if r["kind"] == "node_shift"]
check(len(shifts) == 0, "当日净 0 位移不记（收敛）")

oplog("node_shift", PID, node_id=6, subject="节点6", delta=3,
      detail="推迟 3 天", created_at="2026-09-09 09:00")
shifts = [r for r in db.get_op_log_range(PID) if r["kind"] == "node_shift"]
check(len(shifts) == 1, "当日净 +3 记一条")


print("== 4. build_report 日报 / 周报 ==")
m = reporting.build_report("daily", "2026-09-09")
check(m["kind"] == "daily" and m["overview"]["project_count"] >= 1, "日报概览 (T1 顶部)")
check("oper" not in m["title"], "标题不含非法前缀")
mw = reporting.build_report("weekly", "2026-09-07")
check(mw["weekly_compare"] is None or isinstance(mw["weekly_compare"], dict),
      "周报对比字段存在（仅首份时 None）")


print("== 5. 周对比：前一周无数据 → 首次降级（T27） ==")
check(mw["weekly_compare"] is None, "前一周无数据 → 本周无对比（显示首份）")
blk = reporting.blocks(mw)
any_note = any(b["t"] == "note" and "首份" in b["text"] for b in blk)
check(any_note, "blocks() 输出『前一周无数据，本次为首份周报』")


print("== 6. 编号按日重置（T22） ==")
report_exporter.db = db
db.set_setting("rpt_seq", "0"); db.set_setting("rpt_last_date", "")
d1a = report_exporter.next_report_no(__import__("datetime").date(2026, 9, 9))
d1b = report_exporter.next_report_no(__import__("datetime").date(2026, 9, 9))
d9 = report_exporter.peek_report_no(__import__("datetime").date(2026, 9, 9))
check(d1a == "RPT-20260909-001" and d1b == "RPT-20260909-002",
      "9/9 连续两份为 001/002")
check(d9 == "RPT-20260909-003", "peek 不消耗流水，返回 003")
report_exporter.next_report_no(__import__("datetime").date(2026, 9, 10))
back = report_exporter.next_report_no(__import__("datetime").date(2026, 9, 9))
check(back == "RPT-20260909-001", "跳 9/10 再回 9/9 → 从 001 起")


print("== 7. 风险实时判定（T25） ==")
db.insert_cargo_items(PID, [{"item_name": "光伏组件", "qty": 1, "unit": "件",
                             "dim_l": 12.0, "dim_w": 3.0, "dim_h": 3.5,
                             "weight_kg": 101500}])
m = reporting.build_report("weekly", "2026-09-07")
check(any("超限" in r for r in m["risks"]), "台账 101.5t → 周报出现超限风险（T25）")
# 改回 99t → 风险消失
db.delete_cargo_items(PID)
db.insert_cargo_items(PID, [{"item_name": "光伏组件", "qty": 1, "unit": "件",
                             "dim_l": 12.0, "dim_w": 3.0, "dim_h": 3.5,
                             "weight_kg": 99000}])
m2 = reporting.build_report("weekly", "2026-09-07")
check(not any("超限" in r for r in m2["risks"]), "改回 99t → 风险消失（T25）")


print("== 8. 导出自测（txt 路径） ==")
blocks = reporting.blocks(m2)
# 强制走 txt（隔离环境无 python-docx 无碍）
path, kind = report_exporter.export(m2, blocks, out_dir=os.path.join(_TMP, "reports"))
check(kind in ("docx", "txt") and os.path.exists(path), "导出产出文件（T8）")


print("\n==== 结果 ====")
if FAILED:
    print(f"FAILED {len(FAILED)} 项: {FAILED}")
    sys.exit(1)
print("ALL PASS")