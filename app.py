"""
国际物流全流程跟踪系统 · Demo 启动入口
使用全局 Python 3.12 + PySide6
"""

import sys
import os

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont

import db
from ui.theme import GLOBAL_QSS, APP_FONT, APP_FONT_SIZE, ensure_check_asset
from ui.main_window import MainWindow

# 复用已有的 mock_data
from mock_data import DEMO_PROJECT, DEMO_NODES, get_demo_schedule
from services.file_checklist import bootstrap
from config import get_port


def seed_demo():
    """如果数据库为空，灌入 Mock 演示数据"""
    if db.count_projects("Active") > 0:
        return

    project_id = DEMO_PROJECT["project_id"]
    plan = get_demo_schedule()

    # 写入项目
    db.insert_project({
        "project_id": project_id,
        "project_name": DEMO_PROJECT["project_name"],
        "country": DEMO_PROJECT["country"],
        "export_port": DEMO_PROJECT["export_port"],
        "etd": DEMO_PROJECT["etd"],
        "eta": DEMO_PROJECT["eta"],
        "buffer_days": DEMO_PROJECT["buffer_days"],
    })

    # 写入节点
    nodes = []
    for n in DEMO_NODES:
        s, e = plan[n["node_id"]]
        remark = n.get("remark", "")
        port = get_port(DEMO_PROJECT["export_port"])
        if port and n["node_id"] in port.get("platform_notes", {}):
            platform = port["platform_notes"][n["node_id"]]
            remark = f"{remark} | {platform}" if remark else platform
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
            "remark": remark,
        })
    db.insert_nodes(project_id, nodes)

    # 写入单证
    files = bootstrap(DEMO_PROJECT["country"], DEMO_PROJECT["export_port"], plan)
    db.insert_files(project_id, files)

    print(f"[Seed] Demo 项目已灌入: {DEMO_PROJECT['project_name']}")
    print(f"  节点数: {len(nodes)} | 单证数: {len(files)}")


def main():
    # 初始化数据库
    db.init_db()

    # 灌入演示数据
    seed_demo()

    # 启动 GUI
    app = QApplication(sys.argv)
    app.setFont(QFont(APP_FONT, APP_FONT_SIZE))
    ensure_check_asset()
    app.setStyleSheet(GLOBAL_QSS)

    window = MainWindow()
    window.show()

    print("[App] 国际物流全流程跟踪系统 Demo 已启动")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
