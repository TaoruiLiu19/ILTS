"""模拟数据：青岛→巴西Sepetiba 光伏组件运输（批次化 / 15 节点 node_key 口径）。

`build_project()` 写库；app.py 启动时播种。
底部的 DEMO_NODES / get_demo_schedule 为旧 `_opt_*` 回归脚本提供的兼容层
（旧脚本用废弃的 12 节点 + 无批次接口，仅保证 import 与 seed 可用）。
"""

from services.node_template import template as _template
from services import schedule2
from services.file_checklist import bootstrap


# 进行中演示项目
DEMO_PROJECT = {
    "project_id": "demo-qd-br-001",
    "project_no": "P-DEMOQD600",
    "project_name": "青岛→巴西Sepetiba 光伏组件运输",
    "country": "BR",
    "export_port": "QD",
    "status": "Active",
    "etd": "2026-09-15",
    "eta": "2026-10-26",
    "buffer_days": 4,
    "vessel_name": "COSCO INTEGRITY",
    "create_date": "2026-09-10",
    "node_remark": {
        "LASHING": "件杂货船自吊机，吊装能力≥100t，臂长≥36m",
        "SEA_TRANSIT": "航线青岛→上海→香港→马六甲→Colombo→CapeTown→巴西 Sepetiba，直航 36-40 天",
    },
}


def build_project(project, complete=False):
    """构造并写库一个完整项目（项目 + 默认批次 + 线路 + 节点 + 单证）。
    complete=True：节点全 Done、单证全 submitted、批次 closed、项目 Completed。"""
    import db

    pid = project["project_id"]
    db.insert_project({**project, "etd": None, "eta": None})
    db.ensure_project_no(pid, project.get("project_no"))
    batch = db.create_default_batch(pid)

    route = {
        "mode_primary": "SEA",
        "mode_chain": '["SEA"]',
        "country": project["country"],
        "export_port": project["export_port"],
        "export_port": project["export_port"],
        "etd": project["etd"],
        "eta": project["eta"],
    }
    db.upsert_route(batch["batch_id"], **route)
    if project.get("vessel_name"):
        db.upsert_vessel(pid, vessel_name=project["vessel_name"], batch_id=batch["batch_id"])

    nodes = []
    for n in _template():
        n = dict(n)
        n["node_key"] = n["node_key"]
        n["remark"] = project.get("node_remark", {}).get(n["node_key"], "")
        nodes.append(n)

    plan = schedule2.compute_plan(project["etd"], project["eta"], nodes)
    for n in nodes:
        n["plan_start"], n["plan_end"] = plan[n["node_key"]]
        if complete:
            n["status"] = "Done"
            n["actual_completion_date"] = n["plan_end"]
        else:
            n["status"] = "Pending"
            n["actual_completion_date"] = None
    db.insert_nodes(pid, nodes, batch["batch_id"])

    files = bootstrap(project["country"], project["export_port"], plan)
    if complete:
        for f in files:
            f["status"] = "submitted"
            f["submitted_date"] = f.get("due_date") or project["eta"]
    db.insert_files(pid, files, batch["batch_id"])

    if complete:
        db.update_batch(batch["batch_id"],
                        status="closed",
                        actual_etd=project["etd"], actual_eta=project["eta"],
                        actual_delivery=project.get("actual_completion_date"),
                        empty_returned_at=project["eta"])
        db.update_project(pid, status="Completed",
                          actual_completion_date=project.get("actual_completion_date"))
    else:
        db.update_batch(batch["batch_id"], status="ready", booking_no="BOOK-DEMO-001",
                        mbl_no="MBL-COS-0001", hbl_nos="[]")
        db.update_project(pid, status="Active")

    return {"project_id": pid, "batch_id": batch["batch_id"],
            "nodes": len(nodes), "files": len(files)}


def seed_demo_project():
    import db
    if db.get_project(DEMO_PROJECT["project_id"]) is not None:
        return None
    r = build_project(DEMO_PROJECT)
    print(f"[Seed] Demo 项目已灌入: {DEMO_PROJECT['project_name']} | 节点 {r['nodes']} | 单证 {r['files']}")
    return r


# ── 旧 _opt_* 回归脚本兼容层 ──
def _demo_nodes():
    nodes = []
    for n in _template():
        n = dict(n)
        n.setdefault("node_key", n["node_key"])
        n.setdefault("default_duration", n.get("duration", 1))
        n.setdefault("calendar_mode", "NATURAL")
        n.setdefault("role_label", "")
        nodes.append(n)
    return nodes


DEMO_NODES = _demo_nodes()


def get_demo_schedule():
    """返回 {node_id: (plan_start, plan_end)}——旧脚本以 node_id(序) 作索引。"""
    by_key = schedule2.compute_plan(DEMO_PROJECT["etd"], DEMO_PROJECT["eta"], DEMO_NODES)
    return {n["node_id"]: by_key[n["node_key"]] for n in DEMO_NODES}


if __name__ == "__main__":
    import db
    db.init_db()
    seed_demo_project()
    print("seed demo done")