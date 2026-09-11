"""
Mock 已完成项目：青岛→巴西Sepetiba 光伏组件运输（历史单）
用于测试"已完成项目"功能（已完成列表 + 只读甘特）。

与 DEMO_PROJECT 同航线，但起点更早、已全部完成：节点 status=Done、
单证全部 submitted、项目 status=Completed 且记录 actual_completion_date。
批次化适配：1 项目 1 批次（B01）。
"""

from datetime import date, timedelta
from mock_data import build_project

# 已完成项目（起点 2026-04-10，规划 42 天，实际 50 天完成）
COMPLETED_PROJECT = {
    "project_id": "hist-qd-br-001",
    "project_no": "P-HIST001",
    "project_name": "青岛→巴西Sepetiba 光伏组件运输（历史批次）",
    "country": "BR",
    "export_port": "QD",
    "status": "Completed",
    "etd": "2026-04-10",
    "eta": "2026-05-26",
    "buffer_days": 4,
    "actual_completion_date": "2026-05-30",
    "vessel_name": "COSCO BRAZIL",
    "node_remark": {
        "LASHING": "件杂货船自吊机，吊装能力≥100t，臂长≥36m",
        "SEA_TRANSIT": "航线青岛→上海→香港→马六甲→CapeTown→巴西 Sepetiba，直航 36-40 天",
    },
}


def build_completed_project():
    return build_project(COMPLETED_PROJECT, complete=True)


def seed_completed_demo():
    """把已完成演示项目写入 SQLite（若该项目尚未存在）。"""
    import db
    if db.get_project(COMPLETED_PROJECT["project_id"]) is not None:
        return
    data = build_completed_project()
    print(f"[Seed] 已完成演示项目已灌入: {COMPLETED_PROJECT['project_name']}")
    print(f"  节点数: {data['nodes']} | 单证数: {data['files']} | 完成日: {COMPLETED_PROJECT['actual_completion_date']}")


if __name__ == "__main__":
    import db
    db.init_db()
    seed_completed_demo()
    print("seed completed demo done")