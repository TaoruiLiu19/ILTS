"""
页面四：已完成列表 & 只读甘特
"""

from datetime import date, datetime as dt

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView,
    QScrollArea, QFrame
)
from PySide6.QtCore import Qt, Signal

import db
from config import get_country
from ui.theme import (
    GLOBAL_QSS, TEXT_PRIMARY, TEXT_SECONDARY, TEXT_TERTIARY, BORDER, card_shadow
)
from ui.icons import pixmap
from ui.widgets.gantt_grid import GanttGrid


class CompletedPage(QWidget):
    navigate = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 24)
        layout.setSpacing(16)

        title = QLabel("已完成项目")
        title.setObjectName("title")
        layout.addWidget(title)

        # 搜索
        search_row = QHBoxLayout()
        search_row.setSpacing(10)

        search_icon = QLabel()
        search_icon.setFixedSize(20, 20)
        search_icon.setPixmap(pixmap("search", TEXT_TERTIARY, 20))
        search_row.addWidget(search_icon)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("输入项目名称模糊搜索")
        self.search_edit.textChanged.connect(self._refresh_table)
        self.search_edit.setMaximumWidth(360)
        search_row.addWidget(self.search_edit)
        search_row.addStretch()
        layout.addLayout(search_row)

        # 表格
        self.table = QTableWidget(0, 6)
        headers = ["项目名称", "目的国", "ETD", "ETA", "完成日期", "总耗时"]
        self.table.setHorizontalHeaderLabels(headers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self.table.horizontalHeader().resizeSection(1, 110)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(48)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.cellDoubleClicked.connect(self._show_detail)
        layout.addWidget(self.table, stretch=1)

        # 详情区
        self.detail_frame = QFrame()
        self.detail_frame.setObjectName("card")
        self.detail_frame.setVisible(False)
        card_shadow(self.detail_frame, blur=18, dy=4, alpha=16)
        detail_layout = QVBoxLayout(self.detail_frame)
        detail_layout.setContentsMargins(20, 16, 20, 16)
        detail_layout.setSpacing(10)

        banner = QLabel("项目详情 · 只读视图")
        banner.setObjectName("section")
        detail_layout.addWidget(banner)

        self.detail_label = QLabel("")
        self.detail_label.setStyleSheet(f"font-size: 13px; color: {TEXT_SECONDARY};")
        detail_layout.addWidget(self.detail_label)

        self.gantt_scroll = QScrollArea()
        self.gantt_scroll.setWidgetResizable(True)
        self.gantt_scroll.setFixedHeight(330)
        detail_layout.addWidget(self.gantt_scroll)

        layout.addWidget(self.detail_frame)

    def _refresh_table(self, keyword=""):
        projects = db.get_projects_by_status("Completed")
        if keyword:
            keyword_lower = keyword.lower()
            projects = [p for p in projects if keyword_lower in p["project_name"].lower()]

        self.table.setRowCount(len(projects))
        for i, proj in enumerate(projects):
            self.table.setItem(i, 0, QTableWidgetItem(proj["project_name"]))
            country = get_country(proj["country"])
            self.table.setItem(i, 1, QTableWidgetItem(f"{country.get('flag','')} {country.get('name','')}"))
            self.table.setItem(i, 2, QTableWidgetItem(proj["etd"]))
            self.table.setItem(i, 3, QTableWidgetItem(proj["eta"]))
            self.table.setItem(i, 4, QTableWidgetItem(proj.get("actual_completion_date", "")))
            etd = dt.strptime(proj["etd"], "%Y-%m-%d")
            eta = dt.strptime(proj["eta"], "%Y-%m-%d")
            total_days = (eta - etd).days
            self.table.setItem(i, 5, QTableWidgetItem(f"{total_days} 天"))

    def _show_detail(self, row, col):
        keyword = self.search_edit.text()
        projects = db.get_projects_by_status("Completed")
        if keyword:
            keyword_lower = keyword.lower()
            projects = [p for p in projects if keyword_lower in p["project_name"].lower()]
        if row >= len(projects):
            return

        proj = projects[row]
        nodes = db.get_nodes(proj["project_id"])

        self.detail_label.setText(
            f"{proj['project_name']} · ETD {proj['etd']} · ETA {proj['eta']} · 完成 {proj.get('actual_completion_date', '—')}"
        )

        layout = self.detail_frame.layout()
        while self.gantt_scroll.widget():
            self.gantt_scroll.takeWidget()
            if self.gantt_scroll.widget():
                self.gantt_scroll.widget().deleteLater()

        gantt = GanttGrid(nodes, readonly=True, export_port=proj.get("export_port"))
        self.gantt_scroll.setWidget(gantt)

        self.detail_frame.setVisible(True)

    def refresh(self):
        self._refresh_table()