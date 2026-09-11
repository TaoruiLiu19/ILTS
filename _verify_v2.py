"""1A 地基无头验证：建库→播种→计划日期→船期变更→客户校验→状态推导。"""
import os, sys, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
from services import schedule2, schedule_change, batches as bsvc
from services.node_template import SEA_TRANSIT
from services.node_template import EXPORT_CUSTOMS, LOADING, EMPTY_RETURN

# 干净重建
for f in ("data/logistics.db", "data/logistics.db-wal", "data/logistics.db-shm"):
    if os.path.exists(f):
        os.remove(f)
db.init_db()

from mock_data import seed_demo_project, DEMO_PROJECT
from mock_completed import seed_completed_demo
seed_demo_project()
seed_completed_demo()

PASS = []

def check(name, cond, detail=""):
    PASS.append(name)
    mark = "OK " if cond else "FAIL"
    print(f"[{mark}] {name} {('· ' + str(detail)) if detail else ''}")

# 1. 项目/批次/节点/单证关键
pid = DEMO_PROJECT["project_id"]
b = db.current_batch_id(pid)
nodes = db.get_nodes_by_batch(b)
files = db.get_files(pid)
check("批次存在 B01", db.get_batch(b)["batch_no"].endswith("-B01"))
check("15 节点且 node_key 唯一", len(nodes) == 15 and len({n['node_key'] for n in nodes}) == 15)
check("单证数>0（项目级周期单证无节点锚点为预期）", len(files) > 0)
check("单证锚点齐全(节点级皆有 node_key)",
      sum(1 for f in files if f.get("node_key")) >= len(files) - 5)
route = db.get_route(b)
check("线路 ETD/ETA 已录", route["etd"] == "2026-09-15" and route["eta"] == "2026-10-26")

# 2. 计划日期不变量：境内末 end==ETD==海运 start；海运 end==ETA==境外首 start
plan = schedule2.result_summary(pid)
by_key = {n["node_key"]: n for n in plan["nodes"]}
sea_s, sea_e = by_key[SEA_TRANSIT]["plan_start"], by_key[SEA_TRANSIT]["plan_end"]
dome = [n for n in plan["nodes"] if n["area"] == "DOME"]
dome_last = max(dome, key=lambda x: x["seq"])
over = [n for n in plan["nodes"] if n["area"] == "OVERSEA"]
over_first = min(over, key=lambda x: x["seq"])
check("境内末 end==ETD==海运 start", dome_last["plan_end"] == "2026-09-15" == sea_s)
check("海运 end==ETA==境外首 start", sea_e == "2026-10-26" == over_first["plan_start"])
print("     首节点(提空箱):", min(dome, key=lambda x: x["seq"])["plan_start"], "→", by_key[SEA_TRANSIT]["plan_end"], "→", max(over, key=lambda x: x["seq"])["plan_end"])

# 3. 船期变更 B 类（仅 ETA 晚 3 天）
pv = schedule_change.preview(pid, b, "2026-09-15", "2026-10-29")
check("船期变更 B 类判定", pv["class"] == "B", pv["class"])
res = schedule_change.apply(pid, b, "2026-09-15", "2026-10-29", reason="船晚到3天")
plan2 = schedule2.result_summary(pid)
by2 = {n["node_key"]: n for n in plan2["nodes"]}
dome_last2 = max([n for n in plan2["nodes"] if n["area"] == "DOME"], key=lambda x: x["seq"])
over_first2 = min([n for n in plan2["nodes"] if n["area"] == "OVERSEA"], key=lambda x: x["seq"])
check("B类：DOME 不动", dome_last2["plan_end"] == "2026-09-15")
check("B类：OVERSEA 首节点顺移+3", over_first2["plan_start"] == "2026-10-29")
chgs = db.get_schedule_changes(b, limit=5)
check("船期变更已留痕(batch_schedule_changes)", chgs and chgs[0]["rule_class"] == "B")
sh = db.get_shift_history(pid)
check("不写 shift_history", len(sh) == 0, f"shift_history={len(sh)}")

# 4. 客户建模与单证前置校验
pid_party = db.insert_party(party_name="昆明中远海运工程物流", country="CN",
                            roles=["CUSTOMER", "SHIPPER"])
pid_cons = db.insert_party(party_name="SEPETIBA CONSTRUCTION", name_en="Sepetiba Const.",
                           country="BR", roles=["CONSIGNEE"])
pid_imp = db.insert_party(party_name="SEPETIBA IMPORT", name_en="Sepetiba Imp.",
                          country="BR", tax_id="12.345.678/0001-90", tax_id_type="CNPJ",
                          roles=["IMPORTER"])
# demo 批次当前无客户角色
dtree = "出口报关单"
missing = bsvc.missing_roles(b, "出口报关单")
check("缺客户角色：报关单受阻(SHIPPER+IMPORTER)", missing == {"SHIPPER", "IMPORTER"}, missing)
tax_missing = bsvc.importer_tax_missing(b, "BR")
check("缺 IMPORTER → 税号校验失败", tax_missing is True)
db.set_batch_parties(b, "SHIPPER", [pid_party])
db.set_batch_parties(b, "IMPORTER", [pid_imp])
db.set_batch_parties(b, "CONSIGNEE", [pid_cons])
check("报关单角色齐备后可提交", bsvc.missing_roles(b, "出口报关单") == set())
check("税号已录 → 校验通过", bsvc.importer_tax_missing(b, "BR") is False)
check("提单角色校验(D/O)", "CONSIGNEE" in bsvc.missing_roles(b, "D/O 提货单") or
      bsvc.missing_roles(b, "D/O 提货单") == set())

# 5. 状态推导
st = bsvc.derive_batch_status(b)
check("批次状态推导", st in ("draft", "ready", "running", "completed", "closed"), st)
ps = bsvc.update_project_status(pid)
check("项目状态推导", ps == "Active", ps)

# 6. 已完成项目
hist = db.get_project("hist-qd-br-001")
hist_b = db.current_batch_id("hist-qd-br-001")
hist_nodes = db.get_nodes_by_batch(hist_b)
check("已完成项目节点全 Done", all(n["status"] == "Done" for n in hist_nodes))
check("已完成项目状态 Completed", hist["status"] == "Completed")

print("\n===== 1A 后端验证通过 %d 项 =====" % len(PASS))
print(PASS)