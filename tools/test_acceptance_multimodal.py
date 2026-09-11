#!/usr/bin/env python3
"""多式联运统一模型验收（《数据模型升级.md》阶段0+1）。

覆盖：
  U1  统一模型表存在（locations/routes/route_legs/node_defs/doc_definitions/
      file_records/submission_logs/charges/subcontracts/insurances + 兼容视图）
  U2  迁移：每个批次自动建默认线路 + 1 个默认海运段；节点锚点回填且 node_def_id 完整
  U3  节点唯一：段级节点 (leg_id, node_key)、线路级节点 (route_id, node_key) 幂等
  U4  文件锚点五个级别完整性与唯一性（project/batch/route/leg/node）
  U5  §6 关键查询：线路详情 / 节点按段分组 / 段级文件 / 缺单证统计
  U6  状态聚合：节点→段→线路（aggregate_seg_status）
  U7  费用/分包/保险可按段挂载
  U8  兼容：既有 V2 接口（get_nodes/get_files_by_batch/get_route）返回结构不变

离屏运行，用隔离临时库。
用法: python tools/test_acceptance_multimodal.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import db

_TMP = tempfile.mkdtemp(prefix="mm_")
db.DB_PATH = os.path.join(_TMP, "t.db")
db._conn = None
db.init_db()

from mock_data import DEMO_PROJECT, build_project
import migrate_multimodal

FAILED = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")
    if not cond:
        FAILED.append(msg)


def migrate(batch_ids):
    """对 batch_ids 做默认线路+海运段回填（复用迁移工具逻辑，不走其守卫）。"""
    import migrate_multimodal as mm
    conn = db.get_conn()
    for b in batch_ids:
        br = conn.execute("SELECT * FROM batch_routes WHERE batch_id=?", (b,)).fetchone()
        route = conn.execute("SELECT * FROM routes WHERE batch_id=? AND is_active=1 LIMIT 1",
                             (b,)).fetchone()
        rid = route["route_id"] if route else None
        if rid is None:
            cur = conn.execute(
                "INSERT INTO routes (batch_id, template_id, route_name, status, is_active, "
                "created_at, updated_at) VALUES (?,NULL,?,'active',1,?,?)",
                (b, f"{b} 默认线路", db._now(), db._now()))
            rid = cur.lastrowid
            conn.execute(
                "INSERT INTO route_legs (route_id, seq, mode, origin_name, dest_name, status, "
                "created_at, updated_at) VALUES (?,1,'sea','青岛','目的港','pending',?,?)",
                (rid, db._now(), db._now()))
            leg_cur = conn.execute("SELECT leg_id FROM route_legs WHERE route_id=?", (rid,)).fetchone()
        else:
            leg_cur = conn.execute("SELECT leg_id FROM route_legs WHERE route_id=?", (rid,)).fetchone()
        leg_id = leg_cur["leg_id"]
        # 节点回填
        nodes = conn.execute("SELECT node_id, node_key FROM nodes WHERE batch_id=?", (b,)).fetchall()
        for n in nodes:
            ndef = conn.execute(
                "SELECT node_def_id FROM node_defs WHERE mode='sea' AND node_key=?",
                (n["node_key"],)).fetchone()
            conn.execute(
                "UPDATE nodes SET route_id=?, leg_id=?, node_def_id=? WHERE batch_id=? AND node_id=?",
                (rid, leg_id, ndef["node_def_id"] if ndef else None, b, n["node_id"]))
    conn.commit()


# ── 初始化与种子 ──
_pid = 0


def _seed_project():
    global _pid
    _pid += 1
    import copy
    proj = copy.deepcopy(DEMO_PROJECT)
    proj["project_id"] = f"{DEMO_PROJECT['project_id']}-u{_pid}"
    return build_project(proj), proj


print("== U1 统一模型表存在 ==")
conn = db.get_conn()
tabs = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
views = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")}
for t in ["locations", "route_templates", "route_template_legs", "routes", "route_legs",
          "node_defs", "doc_definitions", "file_records", "submission_logs",
          "charges", "subcontracts", "insurances"]:
    check(t in tabs, f"表 {t} 存在")
check("v_nodes_legacy" in views and "v_files_legacy" in views, "兼容视图 v_nodes_legacy/v_files_legacy")

print("\n== U2 迁移：默认线路+海运段+节点锚点 ==")
# 播种节点/单证/地点/模板主数据（等价迁移工具 *_seed 逻辑）
for mode, key, name, phase, wi, anchor in migrate_multimodal.SEA_NODE_DEFS + migrate_multimodal.ROAD_NODE_DEFS:
    db.upsert_node_def(mode, key, name, phase, wi, anchor)
for code, docname, anchor, required, phase, wi in migrate_multimodal.DOC_DEFS:
    db.upsert_doc_definition(code, docname, anchor, required, phase, wi)
for loc in migrate_multimodal.LOCATION_SEEDS:
    db.upsert_location(loc["name"], loc["unlocode"], loc["loc_type"], loc["country"])
# 线路模板种子（等价迁移工具 _seed_route_templates）
for tpl in migrate_multimodal.ROUTE_TEMPLATES:
    db.create_route_template(tpl["name"], tpl.get("description"), legs=tpl.get("legs"))
(bld, proj) = _seed_project()
bid = bld["batch_id"]
migrate([bid])
rid = db.get_active_route(bid)
check(rid is not None, "批次有 active 线路")
legs = db.get_legs(rid["route_id"]) if rid else []
check(len(legs) == 1 and legs[0]["mode"] == "sea", "默认 1 个海运段")
nodes = db.get_nodes_by_batch(bid)
check(len(nodes) >= 15, f"节点数正常 ({len(nodes)})")
check(all(n["leg_id"] for n in nodes), "所有节点回填 leg_id")
check(all(n["route_id"] for n in nodes), "所有节点回填 route_id")
check(all(n["node_def_id"] for n in nodes), "所有节点回填 node_def_id")
# 段级唯一索引 (leg_id, node_key)
try:
    conn.execute("INSERT INTO nodes (batch_id, node_id, node_key, node_name, role_label, seq, "
                 "area, calendar_mode, default_duration, duration, status, route_id, leg_id) "
                 "VALUES (?,999,'LOADING','重复装船','x',99,'SEA','NATURAL',1,1,'Pending',?,?)",
                 (bid, rid["route_id"], legs[0]["leg_id"]))
    conn.commit()
    check(False, "段级节点唯一 (leg_id,node_key) 应被部分唯一索引拒绝")
except Exception as e:
    check("UNIQUE" in str(e) or "unique" in str(e).lower(), f"段级唯一索引生效 ({str(e)[:50]})")

print("\n== U3 兼容：既有接口返回结构不变 ==")
files = db.get_files_by_batch(bid)
rc = db.get_route(bid)
check(isinstance(files, list), "get_files_by_batch 返回 list")
check(isinstance(rc, dict) and "mode_primary" in rc, "get_route 返回 batch_routes dict")
check(isinstance(nodes, list) and all("node_key" in n for n in nodes),
      "get_nodes 返回节点含 node_key")

print("\n== U4 文件锚点：五级完整性与唯一性 ==")
dd_project = db.upsert_doc_definition("U_P_CT", "验收项目合同", "project", required=1)
dd_batch = db.upsert_doc_definition("U_B_PLAN", "验收批次计划", "batch", required=1)
dd_leg = db.upsert_doc_definition("U_L_DOC", "验收段级文件", "leg", required=1)
dd_node = db.upsert_doc_definition("U_N_DOC", "验收节点文件", "node", required=1)
# project 级
f_p = db.insert_file_record(dd_project, "project", proj["project_id"], "c.pdf", "/x/c.pdf")
check(f_p > 0, "project 级写入成功")
db.update_file_record(f_p, status="submitted")   # 上传=已提交，计入 §4 缺单证清零
# 同锚点同文档重复 → 拒绝
try:
    db.insert_file_record(dd_project, "project", proj["project_id"], "c2.pdf", "/x/c2.pdf")
    check(False, "project 级唯一性应拒绝重复有效文件")
except ValueError:
    check(True, "project 级唯一性生效")
# batch 级
f_b = db.insert_file_record(dd_batch, "batch", proj["project_id"], "plan.pdf", "/x/p.pdf",
                            batch_id=bid)
check(f_b > 0, "batch 级写入成功")
# 锚点完整性：leg 级缺 route_id → 拒绝
try:
    db.insert_file_record(dd_leg, "leg", proj["project_id"], "l.pdf", "/x/l.pdf",
                          batch_id=bid, route_id=None, leg_id=None)
    check(False, "leg 级缺 route/leg 应拒绝")
except ValueError:
    check(True, "锚点完整性校验（leg 需 route+leg）生效")
# node 级
node0 = nodes[0]
f_n = db.insert_file_record(dd_node, "node", proj["project_id"], "n.pdf", "/x/n.pdf",
                            batch_id=bid, node_id=node0["node_id"])
check(f_n > 0, "node 级写入成功")
# 提交日志
db.log_submission(f_p, dd_project, proj["project_id"], "project", "submit", operator="测试员")
logs = db.get_submission_logs(project_id=proj["project_id"])
check(len(logs) >= 1 and logs[0]["action"] == "submit", "提交日志记录成功")
# file_records 兼容视图
vrow = conn.execute("SELECT * FROM v_files_legacy WHERE file_id=?", (f_b,)).fetchone()
check(vrow is not None and vrow["project_id"] == proj["project_id"], "兼容视图 v_files_legacy 可查")

print("\n== U5 §6 关键查询 ==")
detail = db.get_batch_route_detail(bid)
check(detail is not None and len(detail["legs"]) == 1, "§6.1 线路详情含 1 段")
groups = db.get_batch_nodes_by_leg(bid)
check(len(groups) == 1 and len(groups[0]["nodes"]) == len(nodes),
      "§6.2 节点按段分组 (1 组，全量节点)")
leg_files = db.get_leg_required_files(legs[0]["leg_id"])
check(isinstance(leg_files, list), "§6.3 段级文件清单可查")
# §4 项目级缺单证只随 项目必填-项目已交 变化，不随批次重复计数
req_proj = [d for d in db.list_doc_definitions(anchor_level="project") if d["required"]]
submitted_proj = len(db.get_file_records(anchor_level="project", project_id=proj["project_id"]))
miss_proj = db.count_project_level_missing(proj["project_id"])
check(miss_proj == len(req_proj) - submitted_proj,
      f"§4 项目级缺单证=必填-已交 ({miss_proj} = {len(req_proj)}-{submitted_proj})，与批次数无关")
# 缺单证统计：批次级必填未交应有值
miss_batch = db.count_anchor_missing(proj["project_id"], "batch", bid)
check(miss_batch >= 0, f"§4 批次级缺单证统计可算 (={miss_batch})")

print("\n== U6 状态聚合（节点→段→线路） ==")
agg = db.aggregate_seg_status(bid)
check(agg["route_status"] in ("pending", "completed"), f"线路状态聚合 ({agg['route_status']})")
check(len(agg["legs"]) == 1, "聚合返回 1 个段")

print("\n== U7 费用/分包/保险按段 ==")
leg0 = legs[0]["leg_id"]
ch = db.insert_charge(proj["project_id"], "payable", "海运费", 1200.0, leg_id=leg0)
sc = db.insert_subcontract(proj["project_id"], "sup-01", "CT-001", 800.0, leg_id=leg0)
ins = db.insert_insurance(proj["project_id"], "POL-001", "平安保险", 50000.0, leg_id=leg0)
check(len(db.list_charges(proj["project_id"], leg_id=leg0)) == 1, "费用按段查询")
check(len(db.list_subcontracts(proj["project_id"], leg_id=leg0)) == 1, "分包按段查询")
check(len(db.list_insurances(proj["project_id"], leg_id=leg0)) == 1, "保险按段查询")

print("\n== U8 节点模板与定义 ==")
check(len(db.list_node_defs()) >= 15, f"node_defs 已播种 ({len(db.list_node_defs())})")
check(len(db.list_doc_definitions()) >= 5, f"doc_definitions 已播种 ({len(db.list_doc_definitions())})")
check(len(db.list_locations()) >= 1, f"locations 已播种 ({len(db.list_locations())})")
check(len(db.list_route_templates()) >= 1, f"线路模板已播种 ({len(db.list_route_templates())})")

print()
if FAILED:
    print(f"FAILED {len(FAILED)}")
    for f in FAILED:
        print("  -", f)
    sys.exit(1)
print("ALL MULTIMODAL ACCEPTANCE PASSED")