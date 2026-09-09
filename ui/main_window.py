"""
主窗口 — 浅色侧栏导航 + 几何图标 + QStackedWidget + 顶栏(今日待办徽章)
"""

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QStackedWidget,
    QPushButton, QLabel, QFrame, QSystemTrayIcon, QMenu,
    QMessageBox, QCalendarWidget, QDialog
)
from PySide6.QtCore import Qt, QSize, QDate
from PySide6.QtGui import QIcon, QAction, QPixmap

import db
from services.clock import get_today, is_simulated, reset, set_simulated_today
from ui.theme import GLOBAL_QSS, APP_FONT, APP_FONT_SIZE, ACCENT, TEXT_SECONDARY, GREEN, RED, ACCENT_SOFT
from ui.icons import icon, pixmap
from ui.pages.home_page import HomePage
from ui.pages.dashboard_page import DashboardPage
from ui.pages.new_project_page import NewProjectPage
from ui.pages.completed_page import CompletedPage
from services.reminder import compute_reminders, count_total


NAV_ITEMS = [
    ("home",      "启动页", "home"),
    ("dashboard", "主看板", "dashboard"),
    ("new_project", "新建项目", "new"),
    ("completed", "已完成", "completed"),
]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("国际物流全流程跟踪系统")
        self.setMinimumSize(1280, 800)
        self.resize(1440, 900)

        self._build_ui()
        self._build_tray()
        self._check_first_alert()

    def _build_ui(self):
        central = QWidget()
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── 侧栏 ──
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(224)
        sb_layout = QVBoxLayout(sidebar)
        sb_layout.setContentsMargins(12, 24, 12, 20)
        sb_layout.setSpacing(4)

        # Logo
        logo_row = QHBoxLayout()
        logo_row.setSpacing(10)
        logo_icon = QLabel()
        logo_icon.setFixedSize(36, 36)
        logo_icon.setPixmap(_logo_pixmap(36))
        logo_row.addWidget(logo_icon)

        logo_text_box = QVBoxLayout()
        logo_text_box.setSpacing(0)
        logo_text = QLabel("物流跟踪")
        logo_text.setObjectName("logoText")
        logo_sub = QLabel("全流程可视化")
        logo_sub.setObjectName("logoSub")
        logo_text_box.addWidget(logo_text)
        logo_text_box.addWidget(logo_sub)
        logo_row.addLayout(logo_text_box)
        logo_row.addStretch()
        sb_layout.addLayout(logo_row)
        sb_layout.addSpacing(20)

        # 导航
        self.nav_buttons = {}
        for key, label, glyph in NAV_ITEMS:
            btn = QPushButton(label)
            btn.setObjectName("nav")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setIconSize(QSize(18, 18))
            btn.setIcon(icon(glyph, TEXT_SECONDARY, 18))
            btn.clicked.connect(lambda checked, k=key: self._navigate(k))
            sb_layout.addWidget(btn)
            self.nav_buttons[key] = (btn, glyph)

        sb_layout.addStretch()

        version = QLabel("v6.4 · Demo")
        version.setObjectName("logoSub")
        version.setAlignment(Qt.AlignCenter)
        sb_layout.addWidget(version)

        main_layout.addWidget(sidebar)

        # ── 右侧 ──
        right = QFrame()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # 顶栏
        topbar = QFrame()
        topbar.setObjectName("topbar")
        topbar.setFixedHeight(56)
        tb_layout = QHBoxLayout(topbar)
        tb_layout.setContentsMargins(28, 0, 28, 0)

        self.page_title = QLabel("启动页")
        self.page_title.setObjectName("topbarTitle")
        tb_layout.addWidget(self.page_title)
        tb_layout.addStretch()

        # ── 时钟设置（测试用，交付前删除） ──
        self.clock_btn = QPushButton()
        self.clock_btn.setObjectName("clockBtn")
        self.clock_btn.setCursor(Qt.PointingHandCursor)
        self.clock_btn.setFixedHeight(28)
        self.clock_btn.clicked.connect(self._change_time)
        tb_layout.addWidget(self.clock_btn)

        self.todo_badge = QLabel()
        self.todo_badge.setCursor(Qt.PointingHandCursor)
        self.todo_badge.setFixedHeight(28)
        self.todo_badge.mousePressEvent = lambda e: self._show_today_todo()
        tb_layout.addWidget(self.todo_badge)

        right_layout.addWidget(topbar)

        # 页面栈
        self.stack = QStackedWidget()

        self.home_page = HomePage()
        self.home_page.navigate.connect(self._navigate)
        self.stack.addWidget(self.home_page)

        self.dashboard_page = DashboardPage()
        self.dashboard_page.navigate.connect(self._navigate)
        self.stack.addWidget(self.dashboard_page)

        self.new_project_page = NewProjectPage()
        self.new_project_page.navigate.connect(self._navigate)
        self.new_project_page.toast.connect(self._show_toast)
        self.stack.addWidget(self.new_project_page)

        self.completed_page = CompletedPage()
        self.completed_page.navigate.connect(self._navigate)
        self.stack.addWidget(self.completed_page)

        right_layout.addWidget(self.stack, stretch=1)

        main_layout.addWidget(right, stretch=1)

        self.setCentralWidget(central)

        self._update_clock_btn()
        self._navigate("home")

    def _build_tray(self):
        self.tray = QSystemTrayIcon(self)
        self.tray.setToolTip("国际物流全流程跟踪系统")
        self.tray.setIcon(icon("route", ACCENT, 20))

        menu = QMenu()
        action_show = QAction("打开主界面", self)
        action_show.triggered.connect(self._show_normal)
        menu.addAction(action_show)

        action_todo = QAction("今日待办", self)
        action_todo.triggered.connect(self._show_today_todo)
        menu.addAction(action_todo)

        menu.addSeparator()

        action_quit = QAction("退出", self)
        action_quit.triggered.connect(self._quit)
        menu.addAction(action_quit)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self._show_normal() if reason == QSystemTrayIcon.DoubleClick else None)
        self.tray.show()

    def _navigate(self, key):
        self._current_page = key
        if key == "home":
            self.stack.setCurrentWidget(self.home_page)
            self.home_page.refresh()
            self.page_title.setText("启动页")
        elif key == "dashboard":
            self.stack.setCurrentWidget(self.dashboard_page)
            self.dashboard_page.refresh()
            self.page_title.setText("主看板")
        elif key == "new_project":
            self.stack.setCurrentWidget(self.new_project_page)
            self.new_project_page.refresh()
            self.page_title.setText("新建项目")
        elif key == "completed":
            self.stack.setCurrentWidget(self.completed_page)
            self.completed_page.refresh()
            self.page_title.setText("已完成项目")

        for k, (btn, glyph) in self.nav_buttons.items():
            active = k == key
            btn.setChecked(active)
            btn.setIcon(icon(glyph, ACCENT if active else TEXT_SECONDARY, 18))

        self._update_todo_badge()

    def _update_todo_badge(self):
        today = get_today()
        from services.node_status import sync_active_projects
        sync_active_projects()      # 待办口径一致：先落定「必填齐+已过期末」自动完成
        projects = db.get_projects_by_status("Active")
        total = 0
        for proj in projects:
            nodes = db.get_nodes(proj["project_id"])
            files = db.get_files(proj["project_id"])
            reminders = compute_reminders(proj, nodes, files, today)
            total += count_total(reminders)

        if total == 0:
            color = GREEN
            text = "今日待办 0"
        else:
            color = RED
            text = f"今日待办 {total}"
        self.todo_badge.setText(f"  {text}  ")
        self.todo_badge.setStyleSheet(
            f"background: {color}; color: #FFFFFF; border-radius: 14px;"
            f" padding: 2px 4px; font-size: 12px; font-weight: 600;"
        )

    def _check_first_alert(self):
        today = get_today().strftime("%Y-%m-%d")
        last = db.get_setting("last_alert_date")
        if last != today:
            self._show_today_todo()
            db.set_setting("last_alert_date", today)

    def _update_clock_btn(self):
        """刷新模拟时钟按钮文案与配色（测试用）"""
        if is_simulated():
            self.clock_btn.setText(f"模拟时间 {get_today().strftime('%Y-%m-%d')} ⚠")
            self.clock_btn.setStyleSheet(
                f"background: {RED}; color: #FFFFFF; border-radius: 14px;"
                f" padding: 2px 14px; font-size: 12px; font-weight: 600; border: none;"
            )
            self.clock_btn.setToolTip("当前为模拟日期（测试用）。点击可调整或恢复真实时间。")
        else:
            self.clock_btn.setText("测试时间")
            self.clock_btn.setStyleSheet(
                f"background: {ACCENT_SOFT}; color: {ACCENT}; border-radius: 14px;"
                f" padding: 2px 14px; font-size: 12px; font-weight: 600; border: none;"
            )
            self.clock_btn.setToolTip("仅在测试/演示时用于调整'今日'日期。交付前会移除。")

    def _change_time(self):
        """弹出日历选择模拟日期；勾选'恢复真实时间'则复位。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("设置今日日期（测试用）")
        dlg.setMinimumWidth(340)
        lay = QVBoxLayout(dlg)
        msg = QLabel(
            "选择模拟日期后，全应用的『今日』将按此日期计算，\n"
            "用于提前演示节点与单证预警。勾选下方选项可立即恢复真实系统时间。"
        )
        msg.setWordWrap(True)
        msg.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 12px;")
        lay.addWidget(msg)

        cal = QCalendarWidget()
        cal.setGridVisible(True)
        t = get_today()
        cal.setSelectedDate(QDate(t.year, t.month, t.day))
        lay.addWidget(cal)

        use_real = QPushButton("恢复真实系统时间")
        use_real.setStyleSheet(f"background: {ACCENT_SOFT}; color: {ACCENT}; border: none; border-radius: 8px; padding: 8px; font-weight: 600;")

        def on_recover():
            reset()
            self._apply_clock_change()
            dlg.accept()

        use_real.clicked.connect(on_recover)
        lay.addWidget(use_real)

        ok_row = QHBoxLayout()
        ok = QPushButton("应用")
        ok.setStyleSheet(f"background: {ACCENT}; color: #FFFFFF; border: none; border-radius: 8px; padding: 8px 20px; font-weight: 600;")
        ok.clicked.connect(lambda: (set_simulated_today(cal.selectedDate().toPython()), self._apply_clock_change(), dlg.accept()))
        cancel = QPushButton("取消")
        cancel.clicked.connect(dlg.reject)
        ok_row.addStretch()
        ok_row.addWidget(ok)
        ok_row.addWidget(cancel)
        lay.addLayout(ok_row)

        dlg.exec()

    def _apply_clock_change(self):
        """切换时间后刷新全部依赖'今日'的界面与预警"""
        self._update_clock_btn()
        self._navigate(self._current_page)

    def _show_today_todo(self):
        today = get_today()
        from services.node_status import sync_active_projects
        sync_active_projects()
        projects = db.get_projects_by_status("Active")
        all_reminders = {"P0": [], "P1": [], "P2": []}
        for proj in projects:
            nodes = db.get_nodes(proj["project_id"])
            files = db.get_files(proj["project_id"])
            reminders = compute_reminders(proj, nodes, files, today)
            for level in all_reminders:
                all_reminders[level].extend(reminders[level])

        total = count_total(all_reminders)

        if total > 0:
            from ui.dialogs import TodayTodoDialog
            TodayTodoDialog(all_reminders, today, self).exec()
            self.tray.showMessage("今日待办", f"共 {total} 项待办", QSystemTrayIcon.Information)

    def _show_toast(self, msg):
        self.tray.showMessage("操作完成", msg, QSystemTrayIcon.Information)
        self._navigate("dashboard")

    def _show_normal(self):
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def closeEvent(self, event):
        """关闭窗口隐藏到托盘"""
        event.ignore()
        self.hide()
        self.tray.showMessage("国际物流跟踪系统", "程序已最小化到托盘，点击托盘图标恢复", QSystemTrayIcon.Information)

    def _quit(self):
        reply = QMessageBox.question(self, "退出确认", "确定要退出国际物流全流程跟踪系统吗？",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply == QMessageBox.Yes:
            self.tray.hide()
            from PySide6.QtWidgets import QApplication
            QApplication.quit()


def _logo_pixmap(size):
    """蓝色圆角方块 + 白色几何路线标记"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    from PySide6.QtGui import QPainter, QBrush, QColor
    from PySide6.QtCore import QRectF
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setBrush(QBrush(QColor(ACCENT)))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.28, size * 0.28)
    # 在蓝底上叠白色 route 图形
    white = pixmap("route", "#FFFFFF", int(size * 0.82))
    p.drawPixmap(int(size * 0.09), int(size * 0.09), white)
    p.end()
    return pm