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
from ui.pages.report_page import ReportPage
from services.reminder import compute_reminders, count_total
from services import reminder as reminder_svc


NAV_ITEMS = [
    ("home",      "启动页", "home"),
    ("dashboard", "主看板", "dashboard"),
    ("new_project", "新建项目", "new"),
    ("completed", "已完成", "completed"),
    ("report",   "生成报告", "doc"),
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

        version = QLabel("v6.7 · Demo")
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

        # ── 线路管理（P0-1/2：线路模板 + 批次线路） ──
        self.route_btn = QPushButton("线路管理")
        self.route_btn.setObjectName("secondary")
        self.route_btn.setCursor(Qt.PointingHandCursor)
        self.route_btn.setFixedHeight(28)
        self.route_btn.setToolTip("线路模板管理 · 批次线路选择")
        self.route_btn.clicked.connect(self._open_route_manager)
        tb_layout.addWidget(self.route_btn)

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
        self.todo_badge.setToolTip("点击查看今日待办；右键或使用托盘菜单进入「提醒中心」")
        self.todo_badge.mousePressEvent = lambda e: (
            self._show_reminder_center()
            if e.button() == Qt.RightButton else self._show_today_todo())
        tb_layout.addWidget(self.todo_badge)

        right_layout.addWidget(topbar)

        # 页面栈
        self.stack = QStackedWidget()

        self.home_page = HomePage()
        self.home_page.navigate.connect(self._navigate)
        self.home_page.open_workbench.connect(self._open_workbench_from_home)
        self.home_page.open_todo.connect(self._show_today_todo)
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

        self.report_page = ReportPage()
        self.report_page.navigate.connect(self._navigate)
        self.stack.addWidget(self.report_page)

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

        # §12.4 提醒中心（按批次分组：免箱期/到货通知/申报截止/依赖等待）
        action_center = QAction("提醒中心", self)
        action_center.triggered.connect(self._show_reminder_center)
        menu.addAction(action_center)

        menu.addSeparator()

        action_quit = QAction("退出", self)
        action_quit.triggered.connect(self._quit)
        menu.addAction(action_quit)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self._show_normal() if reason == QSystemTrayIcon.DoubleClick else None)
        self.tray.show()

    def _open_workbench_from_home(self, project_id, batch_id, mode="single"):
        """启动页点批次行/「全批次总览」→ 直接打开甘特工作台（不切页）。"""
        self.dashboard_page.open_workbench(project_id, batch_id or None, mode or "single")

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
        elif key == "report":
            self.stack.setCurrentWidget(self.report_page)
            self.report_page.refresh()
            self.page_title.setText("生成报告")

        for k, (btn, glyph) in self.nav_buttons.items():
            active = k == key
            btn.setChecked(active)
            btn.setIcon(icon(glyph, ACCENT if active else TEXT_SECONDARY, 18))

        self._update_todo_badge()

    def _update_todo_badge(self):
        """§10.5 徽标口径 = **启用中批次待办合计**（draft/ready/running）。

        旧实现按项目取 `db.get_files(proj_id)`（无 batch_id → 只取当前批次）逐级求和，
        既漏掉同项目其他启用批次，也会把已关闭批次的单证算进去。现改为按批次聚合：
        每个启用批次各算一次提醒（含免箱期/到货通知/申报截止/依赖等待），再合计。
        """
        today = get_today()
        from services.node_status import sync_active_projects
        sync_active_projects()      # 待办口径一致：先落定「必填齐+已过期末」自动完成
        data = reminder_svc.compute_reminders_three_level(today=today, enabled_only=True)
        total = data["badge"]["total"]
        n_batch = data["totals"].get("batches") or 0

        if total == 0:
            color = GREEN
            text = "今日待办 0"
        else:
            color = RED
            # §10.5 口径 = 启用中批次待办合计（逐批次相加）。多批次时同时给出批次数，
            # 让"70 项"这种数字有上下文，不至于看着像重复提醒。
            text = f"今日待办 {total}" + (f"（{n_batch} 批次）" if n_batch > 1 else "")
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

    def _open_route_manager(self):
        from ui.route_manager import RouteManagerDialog
        RouteManagerDialog(self).exec()

    def _show_today_todo(self):
        """今日待办弹窗：按启用中批次聚合（§10.5 同一口径）。"""
        today = get_today()
        from services.node_status import sync_active_projects
        sync_active_projects()
        all_reminders = {"P0": [], "P1": [], "P2": []}
        batch_ids = []
        for proj in db.get_projects_by_status("Active"):
            for b in db.get_batches(proj["project_id"]):
                if b["status"] not in reminder_svc.ENABLED_STATES:
                    continue
                batch_ids.append(b["batch_id"])
                for r in reminder_svc.compute_reminders_for_batch(b["batch_id"], today):
                    all_reminders.setdefault(r["level"], []).append(r)

        total = count_total(all_reminders)

        if total > 0:
            from ui.dialogs import TodayTodoDialog
            TodayTodoDialog(all_reminders, today, self,
                            batch_ids=batch_ids).exec()
            self.tray.showMessage("今日待办", f"共 {total} 项待办", QSystemTrayIcon.Information)

    def _show_reminder_center(self):
        """§12.4 提醒中心（按批次分组：免箱期倒计时/到货通知/申报截止/依赖等待）。"""
        today = get_today()
        from services.node_status import sync_active_projects
        sync_active_projects()
        from ui.widgets.reminder_center import ReminderCenterDialog
        dlg = ReminderCenterDialog(today=today, parent=self)
        dlg.exec()
        self._update_todo_badge()

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