"""
Mock 已完成项目：青岛→巴西Sepetiba 光伏组件运输（历史单）
用于测试"已完成项目"功能（已完成列表 + 只读甘特）。

与 DEMO_PROJECT 同航线，但起点更早、已全部完成：节点 status=Done、
单证全部 submitted、项目 status=Completed 且记录 actual_completion_date。
"""

from datetime import date, timedelta

from mock_data import DEMO_NODES, generate_schedule
from services.file_checklist import bootstrap

# 已完成项目（起点 2026-04-01，规划 42 天，实际 46 天完成）
COMPLETED_PROJECT = {
    "project_id": "hist-qd-br-001",
    "project_name": "青岛→巴西Sepetiba 光伏组件运输（历史批次）",
    "country": "BR",
    "export_port": "QD",
    "etd": "2026-04-10",
    "eta": "2026-05-26",
    "buffer_days": 4,
    "actual_completion_date": "2026-05-30",
}

_COMPLETED_DURATIONS = {1: 3, 2: 1, 3: 1, 4: 1, 5: 46, 6: 1, 7: 1, 8: 3, 9: 1, 10: 1, 11: 2, 12: 1}


def _fmt(dt):
    return dt.strftime("%Y-%m-%d")


def build_completed_project():
    """返回完整已完成项目数据 dict，可直接经 db 写库。"""
    proj = dict(COMPLETED_PROJECT)
    plan = generate_schedule(proj["etd"], proj["eta"], _COMPLETED_DURATIONS)

    nodes = []
    for n in DEMO_NODES:
        s, e = plan[n["node_id"]]
        # 全部节点已完成，记录各自实际完成日（= 计划结束日）
        nodes.append({
            "node_id": n["node_id"],
            "node_name": n["node_name"],
            "role_label": n["role_label"],
            "seq": n["seq"],
            "area": n["area"],
            "default_duration": n["duration"],
            "duration": n["duration"],
            "plan_start": s,
            "plan_end": e,
            "remark": n.get("remark", ""),
            "status": "Done",
            "actual_completion_date": e,
        })

    # 单证全部提交
    files_cfg = bootstrap(proj["country"], proj["export_port"], plan)
    files = []
    for f in files_cfg:
        files.append({
            **f,
            "status": "submitted",
            "submitted_date": f.get("due_date") or proj["eta"],
        })

    return {"project": proj, "nodes": nodes, "files": files}


def seed_completed_demo():
    """把已完成演示项目写入 SQLite（若该项目尚未存在）。"""
    import db

    if db.get_project(COMPLETED_PROJECT["project_id"]) is not None:
        return

    data = build_completed_project()
    proj = data["project"]

    # insert_project 会写死 status=Active，这里通过原生 SQL 写入 Completed
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects (project_id, project_name, country, export_port, status, "
        "etd, eta, buffer_days, actual_completion_date, create_date) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (proj["project_id"], proj["project_name"], proj["country"], proj["export_port"],
         "Completed", proj["etd"], proj["eta"], proj["buffer_days"],
         proj["actual_completion_date"], "2026-04-01")
    )

    # 节点：显式 status=Done + 实际完成日
    conn = db.get_conn()
    for n in data["nodes"]:
        conn.execute(
            "INSERT INTO nodes (project_id, node_id, node_name, role_label, seq, area, "
            "default_duration, duration, plan_start, plan_end, status, "
            "actual_completion_date, remark) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (proj["project_id"], n["node_id"], n["node_name"], n["role_label"], n["seq"],
             n["area"], n["default_duration"], n["duration"], n["plan_start"],
             n["plan_end"], "Done", n["actual_completion_date"], n["remark"])
        )

    # 单证全部 submitted
    for f in data["files"]:
        conn.execute(
            "INSERT INTO files (project_id, node_id, doc_name, doc_type, owner_dept, copies, "
            "due_node_id, due_type, remind_before_days, status, due_date, "
            "submitted_date, note, is_default) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 1)",
            (proj["project_id"], f.get("node_id"), f["doc_name"], f["doc_type"],
             f.get("owner_dept"), f.get("copies"), f.get("due_node_id"),
             f.get("due_type"), f.get("remind_before_days", 3), "submitted",
             f.get("due_date"), f.get("submitted_date"), f.get("note", ""))
        )

    conn.commit()
    print(f"[Seed] 已完成演示项目已灌入: {proj['project_name']}")
    print(f"  节点数: {len(data['nodes'])} | 单证数: {len(data['files'])} | 完成日: {proj['actual_completion_date']}")


if __name__ == "__main__":
    import db
    db.init_db()
    seed_completed_demo()
    print("seed completed demo done")