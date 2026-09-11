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
from mock_data import seed_demo_project
from mock_completed import seed_completed_demo


def _oplog(*args, **kw):
    from services.oplog import record
    return record(*args, **kw)


def seed_demo():
    """如果数据库为空，灌入 Mock 演示数据（项目 + 默认批次 + 节点 + 单证）"""
    seed_demo_project()


def seed_oplog_demo():
    """为演示项目灌入几条「今日操作动态」日志（一次性，settings 守卫）。
    仅生成 op_log 时间线，不改动真实的节点/单证状态。"""
    if db.get_setting("oplog_demo_seeded"):
        return
    projects = db.get_projects_by_status("Active")
    if not projects:
        return
    pid = projects[0]["project_id"]
    proj = db.get_project(pid)
    batch_id = db.current_batch_id(pid)
    # 出口报关节点（EXPORT_CUSTOMS）必填单证「提交」
    files = db.get_files(pid)
    key = "EXPORT_CUSTOMS"
    node1_file = next((f for f in files
                       if f.get("node_key") == key and f["doc_type"] == "required"), None)
    if node1_file:
        _oplog("file_submit", pid, batch_id=batch_id, node_id=node1_file.get("node_id"),
               node_key=key, subject=node1_file["doc_name"],
               detail="提交", created_at=f"{proj['create_date']} 09:10:00")
    _oplog("vessel_position", pid, batch_id=batch_id, subject="船舶",
           detail="登记实际 ETA（演示）", created_at=f"{proj['create_date']} 10:20:00")
    db.set_setting("oplog_demo_seeded", "1")


def main():
    # 初始化数据库
    db.init_db()

    # 灌入演示数据
    seed_demo()
    seed_completed_demo()
    seed_oplog_demo()

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
