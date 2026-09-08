"""
页面二：主看板 — 折叠卡片 + 甘特网格 + 文件清单
"""

from datetime import date

from services.clock import get_today

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QScrollArea
)
from PySide6.QtCore import Qt, Signal, QSize

import db
from config import get_country, get_port
from services.node_status import compute_node_status, get_current_node, compute_all_status
from services.file_checklist import count_files
from ui.theme import (
    GLOBAL_QSS, card_shadow, ACCENT, GREEN, RED, ORANGE, TEXT_PRIMARY,
    TEXT_SECONDARY, TEXT_TERTIARY, BORDER, HAIRLINE, ACCENT_SOFT, GRAY_SOFT
)
from ui.icons import icon, pixmap
from ui.widgets.mini_bar import MiniBar
from ui.widgets.gantt_grid import GanttGrid
from ui.widgets.file_panel import FilePanel


def _parse(s):
    if isinstance(s, date):
        return s
    if not s:
        return None
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))


class ProjectCard(QFrame):
    """折叠/展开的项目卡片"""

    def __init__(self, project, parent=None):
        super().__init__(parent)
        self._project = project
        self._expanded = False
        self._today = get_today()
        self._nodes = db.get_nodes(project["project_id"])
        self._files = db.get_files(project["project_id"])
        self.setObjectName("card")
        card_shadow(self, blur=18, dy=4, alpha=18)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header_frame = QFrame()
        header_frame.setFixedHeight(104)
        header_layout = QHBoxLayout(header_frame)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(0)

        # 状态竖条
        statuses = compute_all_status(self._nodes, self._today)
        has_overdue = any(s == "Overdue" for s in statuses.values())
        has_active = any(s == "Active" for s in statuses.values())
        bar_color = RED if has_overdue else (ORANGE if has_active else GREEN)

        bar = QFrame()
        bar.setFixedWidth(4)
        bar.setStyleSheet(f"background: {bar_color};")
        header_layout.addWidget(bar)

        # 左区
        left = QFrame()
        left.setFixedWidth(236)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(20, 16, 12, 16)
        left_layout.setSpacing(3)

        country_tmpl = get_country(self._project["country"])
        flag = country_tmpl.get("flag", "")
        name_label = QLabel(f"{flag}  {self._project['project_name']}")
        name_label.setStyleSheet(f"font-size: 15px; font-weight: 600; color: {TEXT_PRIMARY};")
        left_layout.addWidget(name_label)

        id_label = QLabel(f"ID · {self._project['project_id'][:12]}")
        id_label.setStyleSheet(f"font-size: 11px; color: {TEXT_TERTIARY};")
        left_layout.addWidget(id_label)

        port = get_port(self._project.get("export_port"))
        port_text = f"出口港 · {port['name']}" if port else "通用方案"
        port_label = QLabel(port_text)
        port_label.setStyleSheet(f"font-size: 11px; color: {TEXT_SECONDARY};")
        left_layout.addWidget(port_label)
        left_layout.addStretch()
        header_layout.addWidget(left)

        # 中区
        mid = QFrame()
        mid_layout = QVBoxLayout(mid)
        mid_layout.setContentsMargins(12, 16, 12, 16)
        mid_layout.setSpacing(8)

        current = get_current_node(self._nodes, self._today)
        if current:
            cur_text = f"当前 · 节点{current['node_id']} {current['node_name']}"
        else:
            cur_text = "全部完成"
        cur_label = QLabel(cur_text)
        cur_label.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY}; font-weight: 500;")
        mid_layout.addWidget(cur_label)

        mini_bar = MiniBar(self._nodes, self._today, self)
        mid_layout.addWidget(mini_bar)
        mid_layout.addStretch()
        header_layout.addWidget(mid, stretch=2)

        # 右区
        right = QFrame()
        right.setFixedWidth(240)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(12, 16, 20, 16)
        right_layout.setSpacing(4)

        etd = self._project["etd"]
        days_to_etd = (_parse(etd) - self._today).days
        etd_text = f"距 ETD {days_to_etd} 天" if days_to_etd >= 0 else f"已离港 {-days_to_etd} 天"
        etd_label = QLabel(etd_text)
        etd_label.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
        right_layout.addWidget(etd_label)

        done_count = sum(1 for s in statuses.values() if s == "Done")
        total_nodes = len(self._nodes)
        fc = count_files(self._files)
        progress_label = QLabel(f"进度 {done_count}/{total_nodes} 节点 · 缺单证 {fc['pending']}")
        progress_label.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
        right_layout.addWidget(progress_label)
        right_layout.addStretch()

        self.expand_btn = QPushButton("展开")
        self.expand_btn.setObjectName("ghost")
        self.expand_btn.setCursor(Qt.PointingHandCursor)
        self.expand_btn.setIcon(icon("chevron_down", ACCENT, 14))
        self.expand_btn.setIconSize(QSize(14, 14))
        self.expand_btn.clicked.connect(self._toggle_expand)
        right_layout.addWidget(self.expand_btn, 0, Qt.AlignRight)
        header_layout.addWidget(right)

        layout.addWidget(header_frame)

        # 展开区
        self.expand_area = QFrame()
        self.expand_area.setVisible(False)
        self.expand_area.setStyleSheet(
            f"background: #FAFAFC; border-top: 1px solid {BORDER};"
            f" border-bottom-left-radius: 16px; border-bottom-right-radius: 16px;"
        )
        expand_layout = QVBoxLayout(self.expand_area)
        expand_layout.setContentsMargins(16, 16, 16, 16)
        expand_layout.setSpacing(12)
        layout.addWidget(self.expand_area)

    def _toggle_expand(self):
        self._expanded = not self._expanded
        if self._expanded:
            self.expand_btn.setText("收起")
            self.expand_btn.setIcon(icon("chevron_up", ACCENT, 14))
            self._build_expanded()
            self.expand_area.setVisible(True)
        else:
            self.expand_btn.setText("展开")
            self.expand_btn.setIcon(icon("chevron_down", ACCENT, 14))
            self.expand_area.setVisible(False)

    def _build_expanded(self):
        layout = self.expand_area.layout()
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        statuses = compute_all_status(self._nodes, self._today)
        overdue = sum(1 for s in statuses.values() if s == "Overdue")
        active = sum(1 for s in statuses.values() if s == "Active")
        fc = count_files(self._files)

        # 统计字牌
        stat_row = QHBoxLayout()
        stat_row.setSpacing(10)
        stat_row.addWidget(self._stat_chip("进行中", active, ACCENT, ACCENT_SOFT))
        stat_row.addWidget(self._stat_chip("逾期", overdue, RED, "#FDEBEA"))
        stat_row.addWidget(self._stat_chip("缺单证", fc["pending"], ORANGE, "#FFF3E4"))
        stat_row.addStretch()
        layout.addLayout(stat_row)

        # 甘特
        gantt_label = QLabel("时间轴")
        gantt_label.setObjectName("section")
        layout.addWidget(gantt_label)

        gantt = GanttGrid(self._nodes, self._today, export_port=self._project.get("export_port"))
        gantt.setFixedHeight(gantt.auto_height())
        layout.addWidget(gantt)

        # 文件清单
        file_label = QLabel("单证清单")
        file_label.setObjectName("section")
        layout.addWidget(file_label)

        file_panel = FilePanel(self._files, self._nodes, self._today)
        file_panel.setFixedHeight(360)
        file_panel.file_toggled.connect(lambda fid, checked: self._toggle_file(fid, checked))
        layout.addWidget(file_panel)

    def _stat_chip(self, caption, value, color, bg):
        box = QFrame()
        box.setFixedHeight(36)
        box.setStyleSheet(f"background: {bg}; border-radius: 10px;")
        h = QHBoxLayout(box)
        h.setContentsMargins(14, 0, 14, 0)
        h.setSpacing(8)
        cap = QLabel(caption)
        cap.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
        h.addWidget(cap)
        val = QLabel(str(value))
        val.setStyleSheet(f"font-size: 16px; font-weight: 600; color: {color};")
        h.addWidget(val)
        return box

    def _toggle_file(self, file_id, checked):
        from db import today_str
        if checked:
            db.update_file(file_id, status="submitted", submitted_date=today_str())
        else:
            db.update_file(file_id, status="pending", submitted_date=None)
        self._files = db.get_files(self._project["project_id"])
        if self._expanded:
            self._build_expanded()


class DashboardPage(QWidget):
    navigate = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 24)
        layout.setSpacing(16)

        # 标题行
        title_row = QHBoxLayout()
        title = QLabel("主看板")
        title.setObjectName("title")
        title_row.addWidget(title)
        title_row.addStretch()

        new_btn = QPushButton("新建项目")
        new_btn.setObjectName("primary")
        new_btn.setCursor(Qt.PointingHandCursor)
        new_btn.setIcon(icon("new", "#FFFFFF", 16))
        new_btn.setIconSize(QSize(16, 16))
        new_btn.clicked.connect(lambda: self.navigate.emit("new_project"))
        title_row.addWidget(new_btn)
        layout.addLayout(title_row)

        # 统计条
        self.stat_row = QHBoxLayout()
        self.stat_row.setSpacing(12)
        layout.addLayout(self.stat_row)

        # 项目列表
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)

        self.list_widget = QWidget()
        self.list_layout = QVBoxLayout(self.list_widget)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(12)
        self.list_layout.addStretch()

        scroll.setWidget(self.list_widget)
        layout.addWidget(scroll, stretch=1)

    def _clear_stat(self):
        while self.stat_row.count():
            item = self.stat_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def refresh(self):
        today = get_today()
        projects = db.get_projects_by_status("Active")
        total_nodes = 0
        total_overdue = 0
        total_missing = 0
        total_today_end = 0

        for proj in projects:
            nodes = db.get_nodes(proj["project_id"])
            files = db.get_files(proj["project_id"])
            statuses = compute_all_status(nodes, today)
            total_nodes += len(nodes)
            total_overdue += sum(1 for s in statuses.values() if s == "Overdue")
            total_today_end += sum(1 for n in nodes if compute_node_status(n, today) == "Active"
                                   and _parse(n["plan_end"]) == today)
            fc = count_files(files)
            total_missing += fc["pending"]

        self._clear_stat()
        self.stat_row.addWidget(self._metric("进行中", len(projects), ACCENT))
        self.stat_row.addWidget(self._metric("今日截止", total_today_end, ORANGE))
        self.stat_row.addWidget(self._metric("逾期", total_overdue, RED if total_overdue else TEXT_TERTIARY))
        self.stat_row.addWidget(self._metric("缺单证", total_missing, RED if total_missing else TEXT_TERTIARY))
        self.stat_row.addStretch()

        # 清空列表
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not projects:
            empty_box = QVBoxLayout()
            empty_box.addStretch()
            empty_icon = QLabel()
            empty_icon.setFixedSize(56, 56)
            empty_icon.setPixmap(pixmap("box", "#D7D7DC", 56))
            empty_icon.setAlignment(Qt.AlignCenter)
            empty_box.addWidget(empty_icon, 0, Qt.AlignCenter)
            empty = QLabel("暂无进行中项目")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet(f"font-size: 15px; color: {TEXT_TERTIARY}; margin-top: 8px;")
            empty_box.addWidget(empty)
            hint = QLabel("点击右上角「新建项目」开始跟踪")
            hint.setAlignment(Qt.AlignCenter)
            hint.setStyleSheet(f"font-size: 13px; color: {TEXT_TERTIARY};")
            empty_box.addWidget(hint)
            empty_box.addStretch()
            self.list_layout.insertLayout(0, empty_box)
        else:
            for proj in projects:
                card = ProjectCard(proj, self)
                self.list_layout.insertWidget(self.list_layout.count() - 1, card)

    def _metric(self, caption, value, color):
        box = QFrame()
        box.setObjectName("card")
        box.setFixedSize(150, 74)
        v = QVBoxLayout(box)
        v.setContentsMargins(18, 14, 18, 14)
        v.setSpacing(0)
        num = QLabel(str(value))
        num.setStyleSheet(f"font-size: 26px; font-weight: 600; color: {color};")
        v.addWidget(num)
        cap = QLabel(caption)
        cap.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
        v.addWidget(cap)
        return box