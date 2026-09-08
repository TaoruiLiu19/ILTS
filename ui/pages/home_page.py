"""
页面一：启动页 — 欢迎区 + 今日待办 + 三入口卡片
"""

from services.clock import get_today

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QScrollArea
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap

import db
from ui.theme import (
    GLOBAL_QSS, card_shadow, ACCENT, GREEN, RED, ORANGE,
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_TERTIARY, BORDER, HAIRLINE, GRAY_SOFT
)
from ui.icons import tile_pixmap, pixmap
from services.reminder import compute_reminders, count_total


class EntryCard(QFrame):
    clicked = Signal(str)

    def __init__(self, key, title, subtitle, count, glyph, color, bg, parent=None):
        super().__init__(parent)
        self._key = key
        self.setFixedHeight(120)
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("card")
        card_shadow(self, blur=22, dy=6, alpha=22)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        icon_lbl = QLabel()
        icon_lbl.setFixedSize(46, 46)
        icon_lbl.setPixmap(tile_pixmap(glyph, color, bg, 46))
        layout.addWidget(icon_lbl, 0, Qt.AlignTop)

        text_box = QVBoxLayout()
        text_box.setSpacing(4)
        self.title_lbl = QLabel(title)
        self.title_lbl.setStyleSheet(f"font-size: 16px; font-weight: 600; color: {TEXT_PRIMARY};")
        self.sub_lbl = QLabel(subtitle)
        self.sub_lbl.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
        self.sub_lbl.setWordWrap(True)
        text_box.addWidget(self.title_lbl)
        text_box.addWidget(self.sub_lbl)
        text_box.addStretch()
        layout.addLayout(text_box, stretch=1)

        self.count_lbl = QLabel(str(count))
        self.count_lbl.setStyleSheet(f"font-size: 34px; font-weight: 600; color: {color};")
        layout.addWidget(self.count_lbl, 0, Qt.AlignTop)

    def update_count(self, count, subtitle):
        self.count_lbl.setText(str(count))
        self.sub_lbl.setText(subtitle)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._key)


class HomePage(QWidget):
    navigate = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(32, 36, 32, 32)
        layout.setSpacing(22)

        # 欢迎区
        title = QLabel("国际物流全流程跟踪系统")
        title.setObjectName("title")
        layout.addWidget(title)

        sub = QLabel("面向货代 / 外贸操作员的项目全生命周期跟踪工具")
        sub.setObjectName("subtitle")
        layout.addWidget(sub)

        # 今日待办卡片
        self.reminder_bar = QFrame()
        self.reminder_bar.setObjectName("card")
        self.reminder_bar.setFixedHeight(56)
        self.reminder_bar.setCursor(Qt.PointingHandCursor)
        card_shadow(self.reminder_bar, blur=16, dy=4, alpha=16)
        r_layout = QHBoxLayout(self.reminder_bar)
        r_layout.setContentsMargins(18, 0, 18, 0)
        r_layout.setSpacing(12)

        r_icon = QLabel()
        r_icon.setFixedSize(24, 24)
        r_icon.setPixmap(pixmap("calendar", TEXT_SECONDARY, 24))
        r_layout.addWidget(r_icon)

        self.reminder_text = QLabel("正在加载今日待办...")
        self.reminder_text.setStyleSheet(f"font-size: 14px; color: {TEXT_SECONDARY};")
        r_layout.addWidget(self.reminder_text)
        r_layout.addStretch()

        self.reminder_tag = QLabel("0")
        self.reminder_tag.setFixedHeight(24)
        r_layout.addWidget(self.reminder_tag)

        self.reminder_bar.mousePressEvent = lambda e: self.navigate.emit("dashboard")
        layout.addWidget(self.reminder_bar)

        # 三入口卡片
        cards_layout = QHBoxLayout()
        cards_layout.setSpacing(18)

        self.card_active = EntryCard("dashboard", "进行中项目", "", 0,
                                     "box", GREEN, "#E8F8EC", self)
        self.card_active.clicked.connect(lambda k: self.navigate.emit(k))
        cards_layout.addWidget(self.card_active, stretch=1)

        self.card_completed = EntryCard("completed", "已完成项目", "", 0,
                                        "folder", TEXT_SECONDARY, "#F2F2F7", self)
        self.card_completed.clicked.connect(lambda k: self.navigate.emit(k))
        cards_layout.addWidget(self.card_completed, stretch=1)

        self.card_new = EntryCard("new_project", "新建项目", "创建新的物流跟踪", 0,
                                  "new", ACCENT, "#EAF3FF", self)
        self.card_new.clicked.connect(lambda k: self.navigate.emit(k))
        cards_layout.addWidget(self.card_new, stretch=1)

        layout.addLayout(cards_layout)

        # 今日待办详情
        self.todo_section = QLabel("今日待办")
        self.todo_section.setObjectName("section")
        layout.addWidget(self.todo_section)

        self.todo_list = QFrame()
        self.todo_list.setObjectName("card")
        self.todo_layout = QVBoxLayout(self.todo_list)
        self.todo_layout.setContentsMargins(20, 16, 20, 16)
        self.todo_layout.setSpacing(2)
        layout.addWidget(self.todo_list)

        layout.addStretch()

        scroll.setWidget(content)
        outer.addWidget(scroll)

    def refresh(self):
        active_count = db.count_projects("Active")
        completed_count = db.count_projects("Completed")

        self.card_active.update_count(active_count, f"当前跟踪 {active_count} 个项目")
        self.card_completed.update_count(completed_count, f"归档 {completed_count} 个项目")

        self._refresh_reminders()

    def _refresh_reminders(self):
        today = get_today()
        from services.node_status import sync_active_projects
        sync_active_projects()
        projects = db.get_projects_by_status("Active")
        total_reminders = 0
        todo_lines = []

        for proj in projects:
            nodes = db.get_nodes(proj["project_id"])
            files = db.get_files(proj["project_id"])
            reminders = compute_reminders(proj, nodes, files, today)
            total = count_total(reminders)
            if total > 0:
                total_reminders += total
                for level in ("P0", "P1", "P2"):
                    for r in reminders[level]:
                        todo_lines.append((level, r["project"], r["msg"]))

        if total_reminders == 0:
            self.reminder_text.setText("今日暂无待办，一切正常")
            self.reminder_tag.setText("0 项")
            self.reminder_tag.setStyleSheet(
                f"background: {GREEN}; color: #FFFFFF; border-radius: 12px;"
                f" padding: 2px 10px; font-size: 12px; font-weight: 600;"
            )
        else:
            self.reminder_text.setText(f"今日有 {total_reminders} 项待办需要处理")
            self.reminder_tag.setText(f"{total_reminders} 项")
            self.reminder_tag.setStyleSheet(
                f"background: {RED}; color: #FFFFFF; border-radius: 12px;"
                f" padding: 2px 10px; font-size: 12px; font-weight: 600;"
            )

        # 清空并重建待办列表
        while self.todo_layout.count():
            item = self.todo_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not todo_lines:
            empty = QLabel("暂无待办事项")
            empty.setStyleSheet(f"font-size: 14px; color: {TEXT_TERTIARY}; padding: 24px;")
            empty.setAlignment(Qt.AlignCenter)
            self.todo_layout.addWidget(empty)
        else:
            level_color = {"P0": RED, "P1": ORANGE, "P2": GREEN}
            for level, project, msg in todo_lines:
                row = QHBoxLayout()
                row.setSpacing(10)
                dot = QLabel()
                dot.setFixedSize(8, 8)
                dot.setStyleSheet(
                    f"background: {level_color[level]}; border-radius: 4px;"
                )
                row.addWidget(dot, 0, Qt.AlignVCenter)

                text = QLabel(f"{project} · {msg}")
                text.setStyleSheet(f"font-size: 13px; color: {TEXT_PRIMARY};")
                text.setWordWrap(True)
                row.addWidget(text, stretch=1)

                wrap = QWidget()
                wrap.setLayout(row)
                wrap.setStyleSheet(f"border-bottom: 1px solid {HAIRLINE}; padding: 6px 0;")
                self.todo_layout.addWidget(wrap)