"""报告操作动态收敛测试：同一单证「同一天」只显示最后一条；跨天各留一条"""
import os, sys, tempfile
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
from services.clock import set_simulated_today
from datetime import date
set_simulated_today(date(2026, 9, 9))
from datetime import datetime
_TMP = tempfile.mkdtemp(prefix="conv_")
db.DB_PATH = os.path.join(_TMP, "t.db"); db._conn = None; db.init_db()

from mock_data import DEMO_PROJECT, DEMO_NODES, get_demo_schedule
from services.file_checklist import bootstrap
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

from services.oplog import record
from services.reporting import activity_daily, _converge_file_ops

FAILED=[]
def check(c,m):
    print(("PASS " if c else "FAIL ")+m)
    if not c: FAILED.append(m)

files=db.get_files(pid)
docA=files[1]["doc_name"]; docB=files[0]["doc_name"]; docA_nid=files[1].get("node_id")

# 同一表单当天反复提交/撤交 → 库里只应剩 1 行，且为最后动作
record("file_submit", pid, node_id=docA_nid, subject=docA, detail="提交", created_at="2026-09-09 09:00")
record("file_withdraw", pid, node_id=docA_nid, subject=docA, detail="撤交", created_at="2026-09-09 10:00")
record("file_submit", pid, node_id=docA_nid, subject=docA, detail="提交", created_at="2026-09-09 11:00")
# 另一表单只提交一次 → 保留
record("file_submit", pid, node_id=docB[0] or None, subject=docB, detail="提交", created_at="2026-09-09 12:00")
# 非文件操作（节点位移）→ 原样保留
record("node_shift", pid, node_id=1, subject="节点1 出口报关", detail="推迟",
       delta=3, created_at="2026-09-09 13:00")
# 跨天：次日再提交同一表单 → 新增一行（跨天不折叠）
record("file_submit", pid, node_id=docA_nid, subject=docA, detail="提交", created_at="2026-09-10 09:00")

print("--- 数据库实际留痕 ---")
for r in db.get_op_log_range(pid):
    print(f"  {r['created_at']} {r['kind']} {r['subject']}")
print("--- 收敛断言 ---")
check(len([r for r in db.get_op_log_range(pid)
           if r["subject"] == docA and r["created_at"].startswith("2026-09-09")]) == 1,
      f"表单{docA} 9/9 反复提交撤交 → 库里只 1 行")
check(len([r for r in db.get_op_log_range(pid) if r["subject"] == docA]) == 2,
      f"表单{docA} 跨天 → 9/9、9/10 各 1 行")

items = activity_daily([db.get_project(pid)], date(2026,9,9), {pid: "测试项目"})
print("--- activity_daily 输出（9/9） ---")
for it in items:
    print(f"  {it['created_at']} {it['kind']} {it['subject']}")
docs = [it for it in items if it["kind"] in ("file_submit","file_withdraw")]
byA = [it for it in docs if it["subject"]==docA]
check(len(byA)==1, f"表单{docA} 当日收敛为 1 条")
check(byA and byA[0]["kind"]=="file_submit" and byA[0]["created_at"]=="2026-09-09 11:00",
      "收敛保留当日最后一条（11:00 提交）")
byB = [it for it in docs if it["subject"]==docB]
check(len(byB)==1 and byB[0]["kind"]=="file_submit", "表单B 单次提交保留")
shift = [it for it in items if it["kind"]=="node_shift"]
check(len(shift)==1, "非文件操作不被收敛")

# 防御性收敛：喂入重复数据（模拟历史库）也只保留每（单证, 日）最后一条
dup = [
    {"project_id": pid, "project": "P", "kind": "file_submit", "kind_label": "提交单证",
     "subject": docA, "detail": "", "created_at": "2026-09-09 09:00",
     "date": "2026-09-09", "hour": "09"},
    {"project_id": pid, "project": "P", "kind": "file_withdraw", "kind_label": "撤交单证",
     "subject": docA, "detail": "", "created_at": "2026-09-09 14:00",
     "date": "2026-09-09", "hour": "14"},
    {"project_id": pid, "project": "P", "kind": "file_submit", "kind_label": "提交单证",
     "subject": docA, "detail": "", "created_at": "2026-09-10 09:00",
     "date": "2026-09-10", "hour": "09"},
]
conv = _converge_file_ops(dup)
check(len(conv) == 2 and {c["date"] for c in conv} == {"2026-09-09", "2026-09-10"},
      "防御性收敛：同日去重、跨天保留")

print("\n" + ("PASS ALL" if not FAILED else f"FAIL {len(FAILED)}"))
sys.exit(0 if not FAILED else 1)
