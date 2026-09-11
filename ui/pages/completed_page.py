"""
页面四：已完成列表 & 只读甘特
另含 §8/D14/T20「显示已取消」审计开关（只读查看已取消批次 op_log）。
注意：全部批次被取消的项目（D30/T32）状态为 `Cancelled`，**不进本页**。
"""

from datetime import date, datetime as dt

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView,
    QScrollArea, QFrame, QCheckBox
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor

import db
from config import get_country
from ui.theme import (
    GLOBAL_QSS, TEXT_PRIMARY, TEXT_SECONDARY, TEXT_TERTIARY, BORDER, card_shadow,
    CANCELLED, CANCELLED_SOFT
)
from ui.icons import pixmap
from ui.widgets.gantt_grid import GanttGrid


class CompletedPage(QWidget):
    navigate = Signal(str)

    @staticmethod
    def _route_dates(project_id):
        """1A 后 ETD/ETA 仅在批次线路里，项目级兼容字段可能为空。"""
        bid = db.current_batch_id(project_id)
        route = db.get_route(bid) if bid else None
        etd = (route or {}).get("etd")
        eta = (route or {}).get("eta")
        return etd or "", eta or ""

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

        # §8/D14/T20 审计开关：已取消批次默认全模块隐藏，勾选后可只读查看
        self.show_cancelled = QCheckBox("显示已取消")
        self.show_cancelled.setToolTip(
            "已取消批次默认隐藏；勾选后可只读查看其完整 op_log（审计，不可写入）")
        self.show_cancelled.toggled.connect(self._on_cancelled_toggled)
        search_row.addWidget(self.show_cancelled)
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

        # ── §8/D14/T20 已取消批次审计面板（默认隐藏，只读） ──
        self.audit_frame = QFrame()
        self.audit_frame.setObjectName("card")
        self.audit_frame.setVisible(False)
        card_shadow(self.audit_frame, blur=18, dy=4, alpha=16)
        audit_layout = QVBoxLayout(self.audit_frame)
        audit_layout.setContentsMargins(20, 16, 20, 16)
        audit_layout.setSpacing(8)

        audit_head = QHBoxLayout()
        audit_title = QLabel("已取消批次（只读审计）")
        audit_title.setObjectName("section")
        audit_title.setStyleSheet(f"color: {CANCELLED}; font-weight: 600;")
        audit_head.addWidget(audit_title)
        audit_head.addStretch()
        self.audit_count = QLabel("")
        self.audit_count.setStyleSheet(f"font-size: 12px; color: {TEXT_TERTIARY};")
        audit_head.addWidget(self.audit_count)
        audit_layout.addLayout(audit_head)

        self.audit_hint = QLabel(
            "取消批次默认隐藏（§8/D14）；取消期间禁止任何写入，历史报告文件不追溯修改（§11）。")
        self.audit_hint.setWordWrap(True)
        self.audit_hint.setStyleSheet(f"font-size: 11px; color: {TEXT_TERTIARY};")
        audit_layout.addWidget(self.audit_hint)

        audit_body = QHBoxLayout()
        audit_body.setSpacing(12)
        self.cancel_table = QTableWidget(0, 4)
        self.cancel_table.setHorizontalHeaderLabels(["批次", "项目", "取消原因", "取消时间"])
        self.cancel_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.cancel_table.verticalHeader().setVisible(False)
        self.cancel_table.verticalHeader().setDefaultSectionSize(28)
        self.cancel_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.cancel_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.cancel_table.setShowGrid(False)
        self.cancel_table.setMaximumHeight(150)
        self.cancel_table.itemSelectionChanged.connect(self._load_cancelled_log)
        audit_body.addWidget(self.cancel_table, 1)

        self.cancel_log = QTableWidget(0, 4)
        self.cancel_log.setHorizontalHeaderLabels(["时间", "动作", "对象", "内容"])
        self.cancel_log.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.cancel_log.setColumnWidth(0, 130)
        self.cancel_log.setColumnWidth(1, 90)
        self.cancel_log.setColumnWidth(2, 110)
        self.cancel_log.verticalHeader().setVisible(False)
        self.cancel_log.verticalHeader().setDefaultSectionSize(26)
        self.cancel_log.setEditTriggers(QTableWidget.NoEditTriggers)
        self.cancel_log.setShowGrid(False)
        self.cancel_log.setMaximumHeight(190)
        audit_body.addWidget(self.cancel_log, 1)
        audit_layout.addLayout(audit_body)

        layout.addWidget(self.audit_frame)

    # ── §8/D14/T20 已取消批次审计（只读） ──

    def _on_cancelled_toggled(self, on):
        self.audit_frame.setVisible(bool(on))
        self._refresh_cancelled()

    def _refresh_cancelled(self):
        self.cancel_table.setRowCount(0)
        self.cancel_log.setRowCount(0)
        if not self.show_cancelled.isChecked():
            self.audit_count.setText("")
            return
        rows = []
        for status in ("Active", "Completed", "Cancelled"):
            for p in db.get_projects_by_status(status):
                for b in db.get_batches(p["project_id"], include_cancelled=True):
                    if b.get("status") == "cancelled":
                        rows.append((p, b))
        self.audit_count.setText(f"共 {len(rows)} 个已取消批次")
        self._rows = rows
        for p, b in rows:
            r = self.cancel_table.rowCount()
            self.cancel_table.insertRow(r)
            at = ""
            for lg in db.get_op_log_range(p["project_id"], batch_id=b["batch_id"], limit=1000):
                if lg["kind"] in ("batch_cancel", "batch_restore"):
                    at = lg["created_at"][:16]
            label = f"{b.get('batch_no') or b['batch_id']}"
            if b.get("batch_name"):
                label += f" · {b['batch_name']}"
            vals = [label, p["project_name"], b.get("cancel_reason") or "—", at or "—"]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(str(v))
                if c == 0:
                    item.setForeground(QColor(CANCELLED))
                self.cancel_table.setItem(r, c, item)
        if rows:
            self.cancel_table.selectRow(0)

    def _load_cancelled_log(self):
        self.cancel_log.setRowCount(0)
        rows = {i.row() for i in self.cancel_table.selectedIndexes()}
        if not rows:
            return
        p, b = self._rows[sorted(rows)[0]]
        labels = {
            "batch_cancel": "取消批次", "batch_restore": "恢复批次",
            "batch_edit": "批次变更", "batch_create": "新建批次",
            "batch_status": "批次状态", "batch_complete": "确认完成",
            "batch_close": "关闭批次", "batch_copy": "复制批次",
            "batch_actual_override": "实际值覆盖", "route_change": "换线",
            "batch_schedule_change": "船期变更", "schedule_recompute": "计划重算",
            "node_shift": "推迟/提前", "node_unshift": "撤销位移",
            "node_done": "自动完成", "file_submit": "提交单证",
            "file_withdraw": "撤交单证", "project_create": "新建项目",
            "project_close": "项目完结", "vessel_position": "船位登记",
            "cargo_edit": "货物变更",
        }
        logs = db.get_op_log_range(p["project_id"], batch_id=b["batch_id"], limit=1000)
        if not logs:
            logs = [lg for lg in db.get_op_log_range(p["project_id"], limit=1000)
                    if not lg.get("batch_id")]
        for lg in logs:
            r = self.cancel_log.rowCount()
            self.cancel_log.insertRow(r)
            try:
                from services.oplog import display_detail
                detail = display_detail(lg.get("detail") or "")
            except Exception:
                detail = str(lg.get("detail") or "")
            for c, v in enumerate([lg["created_at"][:19].replace("T", " "),
                                   labels.get(lg["kind"], lg["kind"]),
                                   lg.get("subject") or "—", detail]):
                self.cancel_log.setItem(r, c, QTableWidgetItem(str(v)))

    def _refresh_table(self, keyword=""):
        projects = db.get_projects_by_status("Completed")
        if keyword:
            keyword_lower = keyword.lower()
            projects = [p for p in projects if keyword_lower in p["project_name"].lower()]

        self.table.setRowCount(len(projects))
        for i, proj in enumerate(projects):
            etd, eta = self._route_dates(proj["project_id"])
            self.table.setItem(i, 0, QTableWidgetItem(proj["project_name"]))
            country = get_country(proj["country"])
            self.table.setItem(i, 1, QTableWidgetItem(f"{country.get('flag','')} {country.get('name','')}"))
            self.table.setItem(i, 2, QTableWidgetItem(etd))
            self.table.setItem(i, 3, QTableWidgetItem(eta))
            self.table.setItem(i, 4, QTableWidgetItem(proj.get("actual_completion_date", "")))
            total_days = ""
            if etd and eta:
                d_etd = dt.strptime(etd, "%Y-%m-%d")
                d_eta = dt.strptime(eta, "%Y-%m-%d")
                total_days = f"{(d_eta - d_etd).days} 天"
            self.table.setItem(i, 5, QTableWidgetItem(total_days))

    def _show_detail(self, row, col):
        keyword = self.search_edit.text()
        projects = db.get_projects_by_status("Completed")
        if keyword:
            keyword_lower = keyword.lower()
            projects = [p for p in projects if keyword_lower in p["project_name"].lower()]
        if row >= len(projects):
            return

        proj = projects[row]
        bid = db.current_batch_id(proj["project_id"])
        route = db.get_route(bid) if bid else {}
        nodes = db.get_nodes_by_batch(bid) if bid else []
        etd = (route or {}).get("etd", "-")
        eta = (route or {}).get("eta", "-")

        self.detail_label.setText(
            f"{proj['project_name']} · ETD {etd} · ETA {eta} · 完成 {proj.get('actual_completion_date', '—')}"
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
        self._refresh_cancelled()
