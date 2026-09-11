"""
提醒 / 报告 的「多批次同名文件区分」校验（v6.10 穿透层）：
  A 提醒条目：批次信息齐备 + 条目 id 跨批次唯一 + 今日待办行显示批次
  B op_log 收敛：同一天在两个批次提交同名单证 → 各留一行（不再互相覆盖删行）
  C 位移收敛：两个批次的同序节点位移互不覆盖
  D 项目级单证：日志不挂批次；报告「单证最新状态」同项目只出现一次
  E 报告不串批次：两个批次各自拿到自己的动作时间

背景（修前的实测画像）：
  · 三批次 70 条提醒里只有 26 个唯一 id（每批次内部从 rmd-1 重新编号）；
  · 今日待办只显示项目名，三个批次的《出口报关单》长得一模一样；
  · oplog 单证收敛键不含 batch_id → 同日跨批次提交会互相覆盖，只剩 1 行；
  · 报告按「单证名」取全局最后一条动作 + 按批次循环 → B01 的单证显示成 B02 的提交时间。
"""

import os, sys, tempfile, shutil
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import db
from services.clock import set_simulated_today
from datetime import date
set_simulated_today(date(2026, 9, 9))

_TMP = tempfile.mkdtemp(prefix="rmdbatch_")
db.DB_PATH = os.path.join(_TMP, "t.db")
db._conn = None
db.init_db()
FAILED = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")
    if not cond:
        FAILED.append(msg)


from mock_data import DEMO_PROJECT, build_project        # noqa: E402
from services import batches as bsvc                     # noqa: E402
from services import oplog                               # noqa: E402
from services import reporting                           # noqa: E402
from services import reminder as rmd                     # noqa: E402

r = build_project(DEMO_PROJECT)
PID = r["project_id"]
B01 = r["batch_id"]
B02 = db.create_batch(PID, batch_name="B02·推迟5天")["batch_id"]
db.upsert_route(B02, export_port="QD", etd="2026-09-20", eta="2026-10-31")
bsvc.ensure_batch_nodes(PID, B02)
db.upsert_route(B01, customs_broker="中外运报关行")
db.upsert_route(B02, customs_broker="中外运报关行")
db.upsert_vessel(PID, vessel_name="COSCO INTEGRITY", voyage="V0123", batch_id=B01)
db.upsert_vessel(PID, vessel_name="COSCO INTEGRITY", voyage="V0123", batch_id=B02)
bsvc.sync_batch_status(B02)

# ══════════ A 提醒条目 ══════════
print("== A 提醒条目：批次信息齐备 + id 唯一 ==")
items = rmd.compute_reminders_for_project(PID)
ids = [it["id"] for it in items]
check(len(items) > 0, f"项目级提醒 {len(items)} 条")
check(all(it.get("batch_no") for it in items), "每条都带 batch_no（0 条缺失）")
check(len(set(ids)) == len(ids),
      f"条目 id 跨批次唯一（{len(ids)} 条 / {len(set(ids))} 唯一）")
check(all("B01" in i or "B02" in i for i in ids), f"id 含批次号，样例：{ids[:2]}")

# ══════════ B op_log 单证收敛按批次 ══════════
print("== B op_log：同日两批次同名单证各留一行 ==")
for bid in (B01, B02):
    f = next(x for x in db.get_files_by_batch(bid) if x["doc_name"] == "出口报关单")
    db.update_file(f["file_id"], status="submitted", submitted_date="2026-09-09")
    oplog.record("file_submit", PID, node_id=f["node_id"], subject="出口报关单",
                 detail="提交", batch_id=bid, node_key=f.get("node_key"),
                 created_at="2026-09-09 10:00")
rows = [x for x in db.get_op_log_range(PID) if x["kind"] == "file_submit"]
check(len(rows) == 2, f"两批次各留一行（共 {len(rows)} 行；修复前会互相覆盖只剩 1 行）")
check(len({x["batch_id"] for x in rows}) == 2, "两行的 batch_id 不同，未串批次")

# 同批次当日重复勾选/撤交 → 仍收敛为一行（回归既有口径）
f1 = next(x for x in db.get_files_by_batch(B01) if x["doc_name"] == "出口报关单")
oplog.record("file_withdraw", PID, node_id=f1["node_id"], subject="出口报关单",
             detail="撤交", batch_id=B01, node_key=f1.get("node_key"),
             created_at="2026-09-09 11:00")
rows_b01 = [x for x in db.get_op_log_range(PID)
            if x["kind"] in ("file_submit", "file_withdraw") and x["batch_id"] == B01]
check(len(rows_b01) == 1 and rows_b01[0]["kind"] == "file_withdraw",
      "同批次内当日仍收敛为一行最终态（撤销）")

# ══════════ C 位移收敛按批次 ══════════
print("== C 位移：两批次同序节点互不覆盖 ==")
oplog.record("node_shift", PID, node_id=6, subject="节点6", delta=3, detail="推迟 3 天",
             batch_id=B01, created_at="2026-09-09 09:00")
oplog.record("node_shift", PID, node_id=6, subject="节点6", delta=-2, detail="提前 2 天",
             batch_id=B02, created_at="2026-09-09 09:30")
shifts = [x for x in db.get_op_log_range(PID) if x["kind"] == "node_shift"]
check(len(shifts) == 2, f"两个批次各一条位移留痕（{len(shifts)} 条）")
check(len({x["batch_id"] for x in shifts}) == 2, "位移留痕也按批次分开")

# ══════════ D 项目级单证 ══════════
print("== D 项目级单证：日志不挂批次 + 报告只出现一次 ==")
pf = db.get_project_files(PID)[0]
db.update_project_file(pf["file_id"], status="submitted", submitted_date="2026-09-09")
oplog.record("file_submit", PID, subject=pf["doc_name"], detail="提交",
             batch_id=None, scope="project", created_at="2026-09-09 12:00")
prows = [x for x in db.get_op_log_range(PID)
         if x["kind"] == "file_submit" and not x["batch_id"]]
check(len(prows) == 1 and prows[0]["subject"] == pf["doc_name"],
      f"项目级单证日志 1 条、batch_id 为空：{prows[0]['subject'] if prows else '—'}")

states = reporting.last_file_states(db.get_projects_by_status("Active"))
proj_rows = [s for s in states if s.get("scope") == "project"]
check(len(proj_rows) == len(db.get_project_files(PID)) == 3,
      f"报告中项目级单证只出现一次（{len(proj_rows)} 行 / 库中 3 份）")
check(all(s["batch"] == "项目级" for s in proj_rows), "项目级行的批次列显示「项目级」")
check(len(states) == len(db.get_files_by_batch(B01)) + len(db.get_files_by_batch(B02))
      + len(db.get_project_files(PID)),
      f"报告总行数 = 两批次单证 + 项目级一份（{len(states)}）")

# ══════════ E 报告不串批次 ══════════
print("== E 报告：各批次拿到自己的动作 ==")
b02 = [s for s in states if s["batch"].endswith("B02") and s["doc_name"] == "出口报关单"]
b01 = [s for s in states if s["batch"].endswith("B01") and s["doc_name"] == "出口报关单"]
check(len(b01) == 1 and len(b02) == 1, "两批次的《出口报关单》各有一行")
check(b01[0]["action"] == "撤销" and b02[0]["action"] == "提交",
      f"动作互不串批次：B01={b01[0]['action']} / B02={b02[0]['action']}")
check(b01[0]["at"] != b02[0]["at"], f"时间也各自独立：{b01[0]['at']} vs {b02[0]['at']}")

print("\n" + ("PASS ALL" if not FAILED else f"FAIL {len(FAILED)}"))
for f in FAILED:
    print(" -", f)
try:
    if db._conn is not None:
        db._conn.close()
        db._conn = None
except Exception:
    pass
shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(0 if not FAILED else 1)
