"""统一时钟回归：模拟时间生效时，所有入库时间戳（op_log/船位/位移历史/报告）跟随模拟日期"""
import os, sys, tempfile
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
_TMP = tempfile.mkdtemp(prefix="clock_")
db.DB_PATH = os.path.join(_TMP, "t.db")
db._conn = None
db.init_db()

from services.clock import (set_simulated_today, reset, get_today, get_now,
                            get_today_str, get_now_str)
from services import reporting
from services.oplog import record
from services.scheduler import apply_shift
from mock_data import DEMO_PROJECT, DEMO_NODES, get_demo_schedule
from services.file_checklist import bootstrap

FAILED = []


def check(c, m):
    print(("PASS  " if c else "FAIL  ") + m)
    if not c:
        FAILED.append(m)


# ── seed ──
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
db.insert_files(pid, bootstrap(DEMO_PROJECT["country"], DEMO_PROJECT["export_port"], plan))

print("== 1. 未模拟：走真实时间 ==")
reset()
real_today = date.today()
check(get_today() == real_today, f"get_today() = 真实今日 {real_today}")
check(abs((get_now() - datetime.now()).total_seconds()) < 2, "get_now() = 真实当下")
rec = record("file_submit", pid, node_id=1, subject="时钟测试单证", detail="提交")
row = [r for r in db.get_op_log_range(pid) if r["subject"] == "时钟测试单证"][0]
check(row["created_at"].startswith(real_today.strftime("%Y-%m-%d")),
      f"未模拟时 op_log 记真实日期（{row['created_at']}）")

print("\n== 2. 模拟时间生效：入库时间戳跟随模拟日期 ==")
sim = date(2026, 12, 25)          # 故意取与真实今日不同的日期，确保断言真正生效
set_simulated_today(sim)
check(get_today() == sim, "get_today() = 模拟日期")
check(get_now().date() == sim, "get_now().date() = 模拟日期")
check(get_today_str() == "2026-12-25", "get_today_str() = 2026-12-25")
check(get_now_str().startswith("2026-12-25"), f"get_now_str() = {get_now_str()}")
check(get_now_str()[:10] != date.today().strftime("%Y-%m-%d"),
      "模拟日期与真实今日不同（断言有效）")

record("file_submit", pid, node_id=1, subject="模拟单证", detail="提交")
row = [r for r in db.get_op_log_range(pid) if r["subject"] == "模拟单证"][0]
check(row["created_at"].startswith("2026-12-25"),
      f"op_log.created_at 跟随模拟日期（{row['created_at']}）")
# 时分应与真实当下一致（只替换日期，不冻结时间）
check(row["created_at"][11:13] == datetime.now().strftime("%H"),
      "时分仍取真实时钟（仅日期被替换）")

db.insert_vessel_position(pid, lat=1.0, lon=2.0, actual_eta="2026-10-01", note="时钟测试")
vp = db.get_vessel_positions(pid, limit=1)[0]
check(vp["created_at"].startswith("2026-12-25"),
      f"vessel_positions.created_at 跟随模拟日期（{vp['created_at']}）")

apply_shift(pid, 6, 1)
sh = db.get_shift_history(pid, limit=1)[0]
check(sh["created_at"].startswith("2026-12-25"),
      f"shift_history.created_at 跟随模拟日期（{sh['created_at'][:16]}）")
check(len(sh["created_at"].split(".")[-1]) == 6, "位移历史仍保留微秒（同秒可区分）")

print("\n== 3. 报告生成时间同样跟随 ==")
m = reporting.build_report("daily", sim)
check(m["generated_at"].startswith("2026-12-25"),
      f"报告生成时间跟随模拟日期（{m['generated_at']}）")
check(m["data_cutoff"] == m["generated_at"], "数据截止时间与生成时间一致")

print("\n== 4. 恢复真实时间 ==")
reset()
check(get_today() == date.today(), "reset() 后恢复真实今日")
record("file_submit", pid, node_id=1, subject="恢复后单证", detail="提交")
row = [r for r in db.get_op_log_range(pid) if r["subject"] == "恢复后单证"][0]
check(row["created_at"].startswith(date.today().strftime("%Y-%m-%d")),
      f"恢复后 op_log 记回真实日期（{row['created_at']}）")

print("\n" + ("✅ 全部通过" if not FAILED else f"❌ {len(FAILED)} 项失败"))
sys.exit(0 if not FAILED else 1)
