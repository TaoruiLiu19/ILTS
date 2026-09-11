"""
报告功能验收自测（纯逻辑，离屏）
覆盖：op_log 埋点/白名单、单证同日收敛为一行最终态、位移当日净收敛、撤销分离、
日报/周报聚合、单证最新状态板块、周对比空数据降级、编号按日重置、风险实时判定。

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


def _ts(s):
    """§6.6：时间戳存 ISO 8601 +08:00；比较时归一到 'YYYY-MM-DD HH:MM'。"""
    return str(s or "").replace("T", " ")[:16]


def _is_iso08(s):
    import re as _re
    return bool(_re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?\+08:00$", str(s or "")))


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


print("== 2. 单证同日收敛为一行最终态（T14） ==")
doc = "商业发票"
oplog("file_submit", PID, node_id=1, subject=doc, detail="提交",
      created_at="2026-09-09 10:00")
oplog("file_withdraw", PID, node_id=1, subject=doc, detail="撤交",
      created_at="2026-09-09 11:00")
oplog("file_submit", PID, node_id=1, subject=doc, detail="提交",
      created_at="2026-09-09 15:00")
rows = db.get_op_log_range(PID, start="2026-09-09 00:00", end="2026-09-09 23:59")
mine = [r for r in rows if r["subject"] == doc]
check(len(mine) == 1, "同日同单证只留 1 行（提交/撤交不并存）")
check(mine[0]["kind"] == "file_submit", "最终态 = 提交（最后动作）")
check(_ts(mine[0]["created_at"]) == "2026-09-09 15:00", "时间戳 = 最后一次动作时间")
check(_is_iso08(mine[0]["created_at"]),
      f"§6.6 时间戳为 ISO 8601 +08:00（实际 {mine[0]['created_at']!r}）")
# 再撤交 → 仍是 1 行，但 kind 变为撤交
oplog("file_withdraw", PID, node_id=1, subject=doc, detail="撤交",
      created_at="2026-09-09 16:00")
mine = [r for r in db.get_op_log_range(PID) if r["subject"] == doc]
check(len(mine) == 1 and mine[0]["kind"] == "file_withdraw"
      and _ts(mine[0]["created_at"]) == "2026-09-09 16:00",
      "再撤交 → 仍 1 行，覆盖为「撤销 + 16:00」")
# 跨天保留：次日再提交 → 多一行
oplog("file_submit", PID, node_id=1, subject=doc, detail="提交",
      created_at="2026-09-10 09:00")
mine = [r for r in db.get_op_log_range(PID) if r["subject"] == doc]
check(len(mine) == 2, "跨天各留一行（9/9 撤销 + 9/10 提交）")
last = db.last_file_actions(PID).get(doc)
check(last and _ts(last["created_at"]) == "2026-09-10 09:00",
      "last_file_actions 取全局最后一次")


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
# 单证最新状态板块：每张单证一行，且反映最终态
states = m["file_states"]
check(len(states) == len(db.get_files(PID)), "单证最新状态覆盖全部单证")
blk = reporting.blocks(m)
check(any(b["t"] == "h" and "单证最新状态" in b["text"] for b in blk),
      "报告含「单证最新状态」板块")
inv = next(x for x in states if x["doc_name"] == doc)
check(_ts(inv["at"]) == "2026-09-10 09:00" and inv["action"] == "提交",
      "最新状态取全局最后一次动作（跨天）")
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


print("== 9. 下一个工作日待办（标题改名 + 新增「单证」列） ==")
# 找一个「次日为工作日」且次日有非 Done 节点的日报日期
from datetime import date as _D, timedelta as _TD
nodes_all = db.get_nodes(PID)
target = None
for off in range(0, 14):
    ref = _D(2026, 9, 1) + _TD(days=off)
    nxt = ref + _TD(days=1)
    if nxt.weekday() in (5, 6):
        continue
    if any(n["status"] != "Done" and n["plan_start"] == nxt.strftime("%Y-%m-%d")
           for n in nodes_all):
        target = ref
        break
check(target is not None, "找到可用于验证的日报日期")

md = reporting.build_report("daily", target)
td = md["todo"]
blk = reporting.blocks(md)
h_next = [b for b in blk if b["t"] == "h" and "下一个工作日待办" in b["text"]]
check(len(h_next) == 1, "日报板块标题为「下一个工作日待办」")
check(not any(b["t"] == "h" and "未来待办" in b["text"] for b in blk),
      "不再出现旧标题「未来待办」")
todo_tbl = [b for b in blk if b["t"] == "table"
            and b["header"][:1] == ["日期"] and "单证" in b["header"]]
check(bool(todo_tbl), "待办表格已包含「单证」列")
if todo_tbl:
    # §11 要求报告两级口径：待办/未完成清单必须带「批次」列。
    # 断言语义（必备列齐备 + 行与表头对齐），不再冻结精确列清单，
    # 以免 §11 后续追加「客户/柜号」列时误报。
    hdr = todo_tbl[0]["header"]
    check(hdr[0] == "日期" and "项目" in hdr and "节点" in hdr and "单证" in hdr,
          f"表头含 日期/项目/节点/单证（实际 {hdr}）")
    check("批次" in hdr, f"§11 待办表含「批次」列（实际 {hdr}）")
    check(all(len(r) == len(hdr) for r in todo_tbl[0]["rows"]),
          f"每行列数与表头一致（{len(hdr)} 列）")
check(all("docs" in it for it in td["items"]), "待办条目均带 docs 字段")

# 单证列内容 = 该节点未提交的必填单证
node_day = {}
for it in td["items"]:
    node_day[it["node"]] = it
sample = next((it for it in td["items"] if it["docs"] != "—"), None)
if sample:
    nid = int(sample["node"].split("节点")[1].split(" ")[0])
    want = sorted(f["doc_name"] for f in db.get_files(PID)
                  if f.get("node_id") == nid and f["doc_type"] == "required"
                  and f["status"] != "submitted")
    got = sorted(sample["docs"].split("、"))
    check(got == want, f"单证列 = 该节点未提交必填单证（{got}）")
else:
    check(False, "次日存在有单证待办的节点（用于验证单证列）")

# 已提交的必填单证不应出现在单证列
submitted_name = next((f["doc_name"] for f in db.get_files(PID)
                       if f["status"] == "submitted" and f["doc_type"] == "required"
                       and f.get("node_id") is not None), None)
if submitted_name:
    check(all(submitted_name not in it["docs"] for it in td["items"]),
          f"已提交单证「{submitted_name}」不出现在待办单证列")

mw2 = reporting.build_report("weekly", "2026-09-07")
check(any(b["t"] == "h" and "下周待办" in b["text"] for b in reporting.blocks(mw2)),
      "周报板块标题为「下周待办」")
check(all("docs" in it for it in mw2["todo"]["items"]), "周报待办条目同样带 docs 字段")


print("\n==== 结果 ====")
if FAILED:
    print(f"FAILED {len(FAILED)} 项: {FAILED}")
    sys.exit(1)
print("ALL PASS")