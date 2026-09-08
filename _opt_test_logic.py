"""
优化方案验收 · 纯逻辑自测（S1–S7）
覆盖：货物台账/超限、全环节推迟/提前、四守卫、ETA 联动、单证 due 联动、
delay_days 净位移、位移历史与撤销、船位手动登记（ManualProvider）。

运行：python _opt_test_logic.py    （离屏，不启动 GUI）
"""

import os
import sys
import shutil
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
from services.clock import set_simulated_today, get_today_str
from services.scheduler import apply_shift, undo_last_shift, ShiftError
from services.cargo_check import normalize_item, item_over_types, summary
from services.vessel_status import fetch_latest_status, register_manual_position
from mock_data import DEMO_PROJECT, DEMO_NODES, get_demo_schedule
from services.file_checklist import bootstrap

# ── 隔离数据库 ──
_TMP = tempfile.mkdtemp(prefix="opt_test_")
db.DB_PATH = os.path.join(_TMP, "t.db")
db._conn = None
db.init_db()

FAILED = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")
    if not cond:
        FAILED.append(msg)


def d(s):
    return date(*map(int, s.split("-")))


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
        nodes.append({
            "node_id": n["node_id"], "node_name": n["node_name"],
            "role_label": n["role_label"], "seq": n["seq"], "area": n["area"],
            "default_duration": n["duration"], "duration": n["duration"],
            "plan_start": s, "plan_end": e, "remark": n.get("remark", "")})
    db.insert_nodes(pid, nodes)
    db.insert_files(pid, bootstrap(DEMO_PROJECT["country"],
                                   DEMO_PROJECT["export_port"], plan))
    return pid


def nd(pid):
    return {n["node_id"]: n for n in db.get_nodes(pid)}


print("== 预置 ==")
set_simulated_today(date(2026, 9, 5))   # 固定模拟日：节点1进行中、节点2起未开始
pid = seed_demo()
check(pid == DEMO_PROJECT["project_id"], "seed demo project")

# ── S1 货物台账 ──
print("\n== S1 货物台账 + 超限判定 ==")
items = [
    normalize_item({"item_name": "光伏组件（叠层）", "qty": 20, "unit": "套",
                    "dim_l": 2.3, "dim_w": 1.1, "dim_h": 0.4,
                    "weight_kg": 1200, "packaging": "木箱"}),
    normalize_item({"item_name": "主变压器", "qty": 1, "unit": "台",
                    "dim_l": 6.0, "dim_w": 2.5, "dim_h": 3.2,
                    "weight_kg": 118000, "packaging": "裸装"}),
]
db.insert_cargo_items(pid, items)
cargo = db.get_cargo_items(pid)
check(len(cargo) == 2, "S1 台账入库 2 条")
heavy = next(c for c in cargo if c["item_name"] == "主变压器")
check(heavy["over_flag"] == 1 and "超重" in item_over_types(heavy), "S1 >100t 自动判超重")
normal = next(c for c in cargo if c["item_name"].startswith("光伏"))
check(normal["over_flag"] == 0, "S1 普通项不误报")
check(summary(cargo)["over"] == 1, "S1 超限统计 = 1")

# ── S2 境外节点⑥ 推迟 3 天 ──
print("\n== S2 境外节点⑥ 推迟 3 天 ==")
before, proj0 = nd(pid), db.get_project(pid)
apply_shift(pid, 6, 3)
after = nd(pid)
check(all(after[i] == before[i] for i in range(1, 6)), "S2 境内 1-5 与海运不变")
check(all((d(after[i]["plan_start"]) - d(before[i]["plan_start"])).days == 3
          for i in range(6, 13)), "S2 节点6-12 顺延 +3")
proj1 = db.get_project(pid)
check(proj1["eta"] == proj0["eta"] and proj1["etd"] == proj0["etd"], "S2 ETA/ETD 不变")

# ── S3 境内节点② 提前 2 天 ──
print("\n== S3 境内节点② 提前 2 天 ==")
before, proj_b = nd(pid), db.get_project(pid)
apply_shift(pid, 2, -2)
after = nd(pid)
for i in (2, 3, 4):
    check((d(before[i]["plan_start"]) - d(after[i]["plan_start"])).days == 2,
          f"S3 节点{i} 提前 -2")
check(after[1] == before[1], "S3 节点1 不受影响（前段留窗口）")
check(after[5]["plan_start"] == after[4]["plan_end"], "S3 海运 start 联动境内末")
check(d(after[5]["plan_start"]) == d(before[5]["plan_start"]) - timedelta(days=2),
      "S3 海运 start(ETD) 提前 2 天")
check(after[5]["plan_end"] == before[5]["plan_end"], "S3 海运 end/ETA 不动")
check(db.get_project(pid)["etd"] == after[5]["plan_start"], "S3 项目 ETD 联动更新")
check(all(after[i] == before[i] for i in range(6, 13)), "S3 境外 6-12 不动")

# ── S4 海运节点⑤ 推迟 5 天 ──
print("\n== S4 海运节点⑤ 推迟 5 天 ==")
before, proj_b = nd(pid), db.get_project(pid)
apply_shift(pid, 5, 5)
after = nd(pid)
check(after[5]["plan_start"] == before[5]["plan_start"], "S4 海运 start(ETD) 不变")
check((d(after[5]["plan_end"]) - d(before[5]["plan_end"])).days == 5, "S4 海运 end/ETA +5")
check(int(after[5]["duration"]) == (d(after[5]["plan_end"]) - d(after[5]["plan_start"])).days,
      "S4 海运 duration = eta−etd 重算")
check(all((d(after[i]["plan_start"]) - d(before[i]["plan_start"])).days == 5
          for i in range(6, 13)), "S4 境外全段 6-12 顺延 +5")
check(all(after[i] == before[i] for i in range(1, 5)), "S4 境内 1-4 不动")
check(db.get_project(pid)["eta"] == after[5]["plan_end"], "S4 项目 ETA = 原 ETA+5")

# ── S6 位移后单证 due 跟随重算 ──
print("\n== S6 单证 due 跟随重算 ==")
files = db.get_files(pid)
f7 = [f for f in files if f.get("node_id") == 7 and f.get("due_type") == "node_start"]
f5 = [f for f in files if f.get("node_id") == 5 and f.get("due_type") == "node_start"]
check(bool(f7) and f7[0]["due_date"] == nd(pid)[7]["plan_start"],
      "S6 节点7 单证 due == 新 node_start")
check(bool(f5) and f5[0]["due_date"] == nd(pid)[5]["plan_start"],
      "S6 海运正本提单 due == 新海运 start")

# ── S7 delay_days 净位移 / shift_history ──
print("\n== S7 净位移与历史 ==")
n5, n6 = nd(pid)[5], nd(pid)[6]
check(n5["delay_days"] == -2 + 5, "S7 海运节点⑤ 净位移 = +3（-2 提前后 +5 推迟）")
check(n6["delay_days"] == 3 + 5, "S7 境外节点⑥ 净位移 = +8")
check(n5["is_delayed"] == 1, "S7 is_delayed 已标记")
hist = db.get_shift_history(pid)
check(len(hist) >= 3, "S7 shift_history 留痕 ≥3 组")
check(any(h["delta"] < 0 for h in hist), "S7 历史含负值（提前）记录")

# ── 撤销上一步（撤销 S4 海运 +5） ──
print("\n== 撤销上一步（应还原 S4 海运+5） ==")
before = nd(pid)
eta_before = db.get_project(pid)["eta"]
undo_last_shift(pid)
after = nd(pid)
check((d(after[5]["plan_end"]) - d(before[5]["plan_end"])).days == -5,
      "撤销后海运 end 还原 -5")
check(db.get_project(pid)["eta"] == (d(eta_before) - timedelta(days=5)).strftime("%Y-%m-%d"),
      "撤销后项目 ETA 还原")
check(after[6]["delay_days"] == 3, "撤销后节点⑥ 净位移回到 +3（仅剩 S2）")
check(after[5]["delay_days"] == -2, "撤销后海运节点⑤ 净位移回到 -2（仅剩 S3）")

# ── S5 守卫 ──
print("\n== S5 四守卫 ==")
try:
    apply_shift(pid, 2, -1)     # 节点2 当前 start == 今日 → 提前越过今日
    check(False, "S5-① 提前越过今日未拦截")
except ShiftError:
    check(True, "S5-① 提前到今日之前被拦截")

db.update_node(pid, 9, status="Done", actual_completion_date=get_today_str())
try:
    apply_shift(pid, 6, 1)      # 窗口 6-12 含已完成节点9
    check(False, "S5-② Done 在位移窗口未拦截")
except ShiftError:
    check(True, "S5-② 位移窗口含已完成节点被拦截")
db.update_node(pid, 9, status="Pending", actual_completion_date=None)

db.update_node(pid, 8, status="Done", actual_completion_date=get_today_str())
try:
    apply_shift(pid, 9, -1)     # 节点9 start == 已完成节点8 end
    check(False, "S5-③ 提前撞 Done 未拦截")
except ShiftError as e:
    check("已完成" in str(e), "S5-③ 提前撞已完成节点被拦截")
db.update_node(pid, 8, status="Pending", actual_completion_date=None)

try:
    undo_last_shift("proj-not-exist")
    check(False, "S5-④ 空历史撤销未拦截")
except ShiftError:
    check(True, "S5-④ 无历史撤销有明确提示")

# ── 船位手动登记（承接层） ──
print("\n== 船位手动登记 / ManualProvider ==")
db.upsert_vessel(pid, vessel_name="COSCO SHIPPING UNIVERSE", voyage="081W",
                 imo="9860058", carrier="中远海运")
vessel = db.get_vessel(pid)
check(vessel and vessel["vessel_name"] == "COSCO SHIPPING UNIVERSE", "班轮信息落库")
register_manual_position(pid, lat=-22.9811, lon=-43.2434,
                         actual_eta="2026-11-06", note="抵 Sepetiba 锚地")
st = fetch_latest_status(vessel)
check(st and abs(st["lat"] - (-22.9811)) < 1e-6 and st["actual_eta"] == "2026-11-06",
      "ManualProvider 返回最近人工登记船位")
check(bool(db.get_vessel_positions(pid, limit=1)), "船位历史本地留痕可回放")

print("\n" + ("✅ ALL LOGIC PASS" if not FAILED else f"❌ {len(FAILED)} FAILED"))
for f in FAILED:
    print("  -", f)
shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(0 if not FAILED else 1)
