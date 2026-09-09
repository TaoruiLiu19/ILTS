"""
主看板辅助对话框（优化方案 D1/D2）：
  · CargoDialog —— 货物台账查看 / 编辑 / 装箱(箱号·封号)登记
  · VesselDialog —— 班轮信息维护 + 船位手动登记（可联动重排境外段）
  · TodayTodoDialog —— 今日待办弹窗（级别分组 · 白卡条目）
"""

from datetime import date

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QTableWidget, QTableWidgetItem, QHeaderView, QCheckBox, QDateEdit,
    QGroupBox, QFormLayout, QMessageBox, QListWidget, QDoubleSpinBox,
    QScrollArea, QFrame, QWidget
)
from PySide6.QtCore import Qt, QDate
from PySide6.QtGui import QColor

import db
from config import get_port
from services.cargo_check import normalize_item, item_over_types, summary
from services.scheduler import apply_shift, undo_last_shift, ShiftError
from services.vessel_status import fetch_latest_status
from ui.theme import TEXT_SECONDARY, TEXT_TERTIARY, GRAY_SOFT

_HEADERS = ["#", "货名", "数量", "单位", "长(m)", "宽(m)", "高(m)", "毛重(kg)",
            "包装", "唛头/备注", "箱号", "封号", "超限"]


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _fmt_num(v):
    if v is None or v == "":
        return ""
    try:
        s = f"{float(v):.3f}".rstrip("0").rstrip(".")
        return "" if s in ("", "0") else s
    except (TypeError, ValueError):
        return ""


class CargoDialog(QDialog):
    """货物台账对话框：逐行编辑 + 装箱后登记箱号/封号"""

    def __init__(self, project_id, parent=None):
        super().__init__(parent)
        self.setWindowTitle("货物台账")
        self.setMinimumSize(1200, 500)
        self._project_id = project_id
        self._row_keys = []          # 每行对应的原始 item_id（None = 新增行）
        self._loading = False
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(10)

        proj = db.get_project(self._project_id)
        port = get_port(proj.get("export_port")) if proj else None
        port_txt = f" · 出口港 {port['name']}" if port else ""
        hint = QLabel(
            "逐行登记每件货物的尺寸 / 毛重（超重 >100t 或单边 >36m 自动判超限，"
            "节点③将标红「吊装预警」）；装箱后可在对应行补记箱号 / 封号。"
            + port_txt)
        hint.setWordWrap(True)
        hint.setStyleSheet(f"font-size: 12px; color: {TEXT_TERTIARY};")
        lay.addWidget(hint)

        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.itemChanged.connect(self._on_item_changed)
        lay.addWidget(self.table, stretch=1)

        ops = QHBoxLayout()
        add_btn = QPushButton("＋ 添加行")
        add_btn.setObjectName("secondary")
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.clicked.connect(self._add_row)
        ops.addWidget(add_btn)

        del_btn = QPushButton("移除选中行")
        del_btn.setObjectName("ghost")
        del_btn.setCursor(Qt.PointingHandCursor)
        del_btn.clicked.connect(self._del_selected)
        ops.addWidget(del_btn)
        ops.addStretch()

        self.sum_label = QLabel("")
        self.sum_label.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
        ops.addWidget(self.sum_label)
        lay.addLayout(ops)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton("取消")
        cancel.setObjectName("secondary")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        save = QPushButton("保存台账")
        save.setObjectName("primary")
        save.setCursor(Qt.PointingHandCursor)
        save.clicked.connect(self._save)
        btns.addWidget(save)
        lay.addLayout(btns)

        self._load_rows()

    # ── 行数据 ──

    def _load_rows(self):
        self._loading = True
        try:
            items = db.get_cargo_items(self._project_id)
            self._row_keys = []
            self.table.setRowCount(0)
            for it in items:
                self._row_keys.append(it.get("item_id"))
                r = self.table.rowCount()
                self.table.insertRow(r)
                self._fill_row(r, it)
            if not items:
                self._add_row()
        finally:
            self._loading = False
        self._refresh_summary()

    def _add_row(self):
        self._row_keys.append(None)
        r = self.table.rowCount()
        self.table.insertRow(r)
        self._fill_row(r, {})
        self.table.scrollToBottom()

    def _del_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        self._loading = True
        try:
            for r in rows:
                self.table.removeRow(r)
                self._row_keys.pop(r)
        finally:
            self._loading = False
        self._refresh_summary()

    def _fill_row(self, r, it):
        vals = [
            str(it.get("seq", r + 1)), it.get("item_name", ""),
            str(it.get("qty", 1)), it.get("unit", "") or "台",
            _fmt_num(it.get("dim_l")), _fmt_num(it.get("dim_w")), _fmt_num(it.get("dim_h")),
            _fmt_num(it.get("weight_kg")), it.get("packaging", "") or "",
            it.get("marks", "") or "", it.get("container_no", "") or "",
            it.get("seal_no", "") or "",
        ]
        for c, v in enumerate(vals):
            cell = QTableWidgetItem(v)
            if c == 0:
                cell.setFlags(Qt.ItemIsEnabled)
                cell.setTextAlignment(Qt.AlignCenter)
            elif c in (2, 4, 5, 6, 7):
                cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(r, c, cell)
        self._set_over_cell(r)

    def _row_item(self, r):
        def txt(c):
            it = self.table.item(r, c)
            return it.text().strip() if it else ""

        def num(c):
            try:
                return float(txt(c))
            except ValueError:
                return None

        return {
            "item_name": txt(1),
            "qty": int(_f(txt(2))) if txt(2) else 1,
            "unit": txt(3) or None,
            "dim_l": num(4), "dim_w": num(5), "dim_h": num(6),
            "weight_kg": num(7),
            "packaging": txt(8) or None,
            "marks": txt(9) or None,
            "container_no": txt(10) or None,
            "seal_no": txt(11) or None,
        }

    def _set_over_cell(self, r):
        types = item_over_types(self._row_item(r))
        cell = QTableWidgetItem("⚠ " + "、".join(types) if types else "—")
        cell.setFlags(Qt.ItemIsEnabled)
        cell.setForeground(QColor("#C0392B") if types else QColor("#B8B8BE"))
        self.table.setItem(r, 12, cell)

    def _on_item_changed(self, item):
        if self._loading or item.column() == 12 or item.column() == 0:
            return
        self._set_over_cell(item.row())
        self._refresh_summary()

    def _refresh_summary(self):
        items = [self._row_item(r) for r in range(self.table.rowCount())
                 if self._row_item(r).get("item_name")]
        if not items:
            self.sum_label.setText("共 0 项（请至少登记 1 项并填写货名）")
            return
        s = summary(items)
        self.sum_label.setText(
            f"共 {s['count']} 项 · 毛重 {s['total_weight_kg'] / 1000:.1f} t"
            + (f" · 超限 {s['over']} 项（触发吊装预警）" if s["over"] else ""))

    def _save(self):
        rows = []
        for r in range(self.table.rowCount()):
            item = self._row_item(r)
            if not item.get("item_name"):
                continue
            if not item.get("qty") or item["qty"] <= 0:
                item["qty"] = 1
            rows.append(normalize_item(item))
        if not rows:
            QMessageBox.information(self, "货物台账", "请至少登记 1 项货物（需填写货名）")
            return
        for i, it in enumerate(rows, start=1):
            it["seq"] = i
        db.delete_cargo_items(self._project_id)
        db.insert_cargo_items(self._project_id, rows)
        self.accept()


class VesselDialog(QDialog):
    """班轮信息 + 船位手动登记对话框"""

    def __init__(self, project_id, parent=None):
        super().__init__(parent)
        self.setWindowTitle("班轮 · 船位动态")
        self.setMinimumWidth(600)
        self._project_id = project_id
        self._result_note = ""
        self._build()
        self._load()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 18)
        lay.setSpacing(10)

        # ── 班轮信息 ──
        group = QGroupBox("班轮信息（1 项目 1 船）")
        form = QFormLayout(group)
        form.setLabelAlignment(Qt.AlignRight)
        form.setVerticalSpacing(9)

        self.vessel_name = QLineEdit()
        self.vessel_name.setPlaceholderText("船名")
        form.addRow("船名", self.vessel_name)

        r1 = QHBoxLayout()
        self.voyage = QLineEdit()
        self.voyage.setPlaceholderText("航次")
        self.imo = QLineEdit()
        self.imo.setPlaceholderText("IMO")
        r1.addWidget(self.voyage)
        r1.addWidget(self.imo)
        form.addRow("航次 / IMO", r1)

        r2 = QHBoxLayout()
        self.carrier = QLineEdit()
        self.carrier.setPlaceholderText("承运人")
        self.mmsi = QLineEdit()
        self.mmsi.setPlaceholderText("MMSI")
        r2.addWidget(self.carrier)
        r2.addWidget(self.mmsi)
        form.addRow("承运人 / MMSI", r2)
        lay.addWidget(group)

        # ── 最近动态 ──
        self.latest_label = QLabel("暂无船位动态")
        self.latest_label.setWordWrap(True)
        self.latest_label.setStyleSheet(
            f"font-size: 12px; color: {TEXT_SECONDARY};"
            f" background: {GRAY_SOFT}; border-radius: 8px; padding: 8px 10px;")
        lay.addWidget(self.latest_label)

        # ── 登记船位 ──
        pos_group = QGroupBox("登记船位 / 实际 ETA（手动 · 离线）")
        pform = QFormLayout(pos_group)
        pform.setLabelAlignment(Qt.AlignRight)
        pform.setVerticalSpacing(8)

        geo = QHBoxLayout()
        self.lat_edit = QDoubleSpinBox()
        self.lat_edit.setRange(-90, 90)
        self.lat_edit.setDecimals(4)
        self.lat_edit.setValue(0)
        self.lat_edit.setSuffix("°")
        self.lon_edit = QDoubleSpinBox()
        self.lon_edit.setRange(-180, 180)
        self.lon_edit.setDecimals(4)
        self.lon_edit.setValue(0)
        self.lon_edit.setSuffix("°")
        geo.addWidget(self.lat_edit, 1)
        geo.addWidget(QLabel("纬度 / 经度"), 0)
        geo.addWidget(self.lon_edit, 1)
        pform.addRow("船位 (lat/lon)", geo)

        self.eta_check = QCheckBox("登记实际 ETA（到港）")
        pform.addRow("", self.eta_check)
        self.eta_edit = QDateEdit()
        self.eta_edit.setCalendarPopup(True)
        self.eta_edit.setDisplayFormat("yyyy-MM-dd")
        self.eta_edit.setEnabled(False)
        self.eta_check.toggled.connect(self.eta_edit.setEnabled)
        pform.addRow("实际 ETA", self.eta_edit)

        self.apply_check = QCheckBox("按实际 ETA 联动重排：海运 + 境外全段同步顺延/提前")
        self.apply_check.setEnabled(False)
        self.eta_check.toggled.connect(self.apply_check.setEnabled)
        pform.addRow("", self.apply_check)

        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("备注（如：过马六甲 / 抵 Sepetiba 锚地…）")
        pform.addRow("备注", self.note_edit)
        lay.addWidget(pos_group)

        # ── 历史 ──
        hist_lbl = QLabel("动态历史（本地留痕，可回放）")
        hist_lbl.setStyleSheet(f"font-size: 12px; color: {TEXT_TERTIARY};")
        lay.addWidget(hist_lbl)
        self.history_list = QListWidget()
        self.history_list.setMaximumHeight(110)
        lay.addWidget(self.history_list)

        btns = QHBoxLayout()
        btns.addStretch()
        undo = QPushButton("撤销上一步位移")
        undo.setObjectName("ghost")
        undo.setCursor(Qt.PointingHandCursor)
        undo.clicked.connect(self._undo_shift)
        btns.addWidget(undo)
        close = QPushButton("关闭")
        close.setObjectName("secondary")
        close.clicked.connect(self.accept)
        btns.addWidget(close)
        save = QPushButton("保存并登记")
        save.setObjectName("primary")
        save.setCursor(Qt.PointingHandCursor)
        save.clicked.connect(self._save)
        btns.addWidget(save)
        lay.addLayout(btns)

    def _load(self):
        vessel = db.get_vessel(self._project_id)
        if vessel:
            self.vessel_name.setText(vessel.get("vessel_name") or "")
            self.voyage.setText(vessel.get("voyage") or "")
            self.imo.setText(vessel.get("imo") or "")
            self.carrier.setText(vessel.get("carrier") or "")
            self.mmsi.setText(vessel.get("mmsi") or "")

        status = fetch_latest_status(vessel)
        if status:
            parts = []
            if status.get("lat") is not None:
                parts.append(f"位置 {status['lat']:.4f}°, {status['lon']:.4f}°")
            if status.get("actual_eta"):
                parts.append(f"实际 ETA {status['actual_eta']}")
            if status.get("note"):
                parts.append(status["note"])
            if status.get("created_at"):
                parts.append(f"登记于 {status['created_at']}")
            self.latest_label.setText("最近动态：" + (" · ".join(parts) if parts else "已登记"))

        project = db.get_project(self._project_id)
        if project:
            y, m, d = (int(x) for x in project["eta"].split("-"))
            self.eta_edit.setDate(QDate(y, m, d))

        positions = db.get_vessel_positions(self._project_id, limit=8)
        self.history_list.clear()
        for p in reversed(positions):
            text = p["created_at"]
            if p.get("lat") is not None:
                text += f"  ({p['lat']:.3f}°, {p['lon']:.3f}°)"
            if p.get("actual_eta"):
                text += f"  实际ETA {p['actual_eta']}"
            if p.get("note"):
                text += f"  {p['note']}"
            self.history_list.addItem(text)
        if not positions:
            self.history_list.addItem("（暂无登记；手工登记后在此留痕）")

    def _undo_shift(self):
        try:
            undo_last_shift(self._project_id)
        except ShiftError as e:
            QMessageBox.warning(self, "撤销被拦截", str(e))
            return
        self._result_note = "已撤销上一步位移"
        self.accept()

    def _save(self):
        project = db.get_project(self._project_id)

        name = self.vessel_name.text().strip()
        if name or any([self.voyage.text().strip(), self.imo.text().strip(),
                        self.carrier.text().strip(), self.mmsi.text().strip()]):
            db.upsert_vessel(self._project_id,
                             vessel_name=name or None,
                             voyage=self.voyage.text().strip() or None,
                             imo=self.imo.text().strip() or None,
                             carrier=self.carrier.text().strip() or None,
                             mmsi=self.mmsi.text().strip() or None)

        actual_eta = None
        if self.eta_check.isChecked():
            actual_eta = self.eta_edit.date().toString("yyyy-MM-dd")
        db.insert_vessel_position(
            self._project_id,
            lat=self.lat_edit.value(),
            lon=self.lon_edit.value(),
            actual_eta=actual_eta,
            note=self.note_edit.text().strip() or None)

        msg = "船位动态已登记留痕"
        if self.apply_check.isChecked() and actual_eta and project:
            ay, am, ad = (int(x) for x in actual_eta.split("-"))
            py, pm, pd = (int(x) for x in project["eta"].split("-"))
            delta = (date(ay, am, ad) - date(py, pm, pd)).days
            if delta == 0:
                msg += "；实际 ETA 与计划一致，未触发重排"
            else:
                try:
                    apply_shift(self._project_id, 5, delta)
                    msg += f"；已按实际 ETA {'推迟' if delta > 0 else '提前'} {abs(delta)} 天联动重排境外段"
                except ShiftError as e:
                    QMessageBox.warning(self, "ETA 联动被拦截", str(e))
        self._result_note = msg
        self.accept()

    def result_note(self):
        return self._result_note


class TodayTodoDialog(QDialog):
    """今日待办弹窗：级别分组（色点+计数）+ 白卡条目，苹果极简风"""

    # (级别, 组名, 组色)
    _GROUPS = (("P0", "需处理", "#FF3B30"),
               ("P1", "进行中提醒", "#FF9500"),
               ("P2", "今日启动", "#007AFF"))

    def __init__(self, all_reminders, today, parent=None):
        super().__init__(parent)
        self.setWindowTitle("今日待办")
        self.setMinimumWidth(500)
        self.setMaximumWidth(560)
        self._all = all_reminders
        self._today = today
        self._build()

    def _build(self):
        from ui.icons import icon
        from ui.theme import (ACCENT, CARD, BORDER, TEXT_PRIMARY,
                              TEXT_SECONDARY, TEXT_TERTIARY)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.setSpacing(12)

        # ── 头部：日历图标 + 标题 + 日期 ──
        head = QHBoxLayout()
        head.setSpacing(10)
        icon_lbl = QLabel()
        icon_lbl.setPixmap(icon("calendar", ACCENT, 26).pixmap(26, 26))
        head.addWidget(icon_lbl)
        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title = QLabel("今日待办")
        title.setStyleSheet(
            f"font-size: 19px; font-weight: 600; color: {TEXT_PRIMARY};")
        title_col.addWidget(title)
        date_lbl = QLabel(self._today.strftime("%Y-%m-%d"))
        date_lbl.setStyleSheet(f"font-size: 12px; color: {TEXT_TERTIARY};")
        title_col.addWidget(date_lbl)
        head.addLayout(title_col)
        head.addStretch()
        lay.addLayout(head)

        # ── 滚动内容区 ──
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
            "QScrollBar:vertical { width: 8px; background: transparent; }"
            "QScrollBar::handle:vertical { background: #D7D7DC;"
            " border-radius: 4px; min-height: 20px; }"
            "QScrollBar::add-line, QScrollBar::sub-line { height: 0; }")
        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(2, 0, 8, 0)
        body_lay.setSpacing(8)

        has_any = False
        for level, gname, gcolor in self._GROUPS:
            items = self._all.get(level, [])
            if not items:
                continue
            has_any = True
            gh = QHBoxLayout()
            gh.setSpacing(7)
            dot = QLabel()
            dot.setFixedSize(9, 9)
            dot.setStyleSheet(f"background: {gcolor}; border-radius: 4px;")
            gh.addWidget(dot)
            gl = QLabel(gname)
            gl.setStyleSheet(
                f"font-size: 13px; font-weight: 600; color: {TEXT_SECONDARY};")
            gh.addWidget(gl)
            cnt = QLabel(str(len(items)))
            cnt.setStyleSheet(
                f"background: {gcolor}; color: #FFFFFF; font-size: 11px;"
                f" font-weight: 600; padding: 1px 8px; border-radius: 8px;")
            gh.addWidget(cnt)
            gh.addStretch()
            body_lay.addSpacing(4)
            body_lay.addLayout(gh)
            for r in items:
                body_lay.addWidget(self._row_card(r["project"], r["msg"]))

        if not has_any:
            empty_lbl = QLabel("今日暂无待办，一切正常")
            empty_lbl.setAlignment(Qt.AlignCenter)
            empty_lbl.setStyleSheet(
                f"font-size: 14px; color: {TEXT_TERTIARY}; padding: 34px 0;")
            body_lay.addWidget(empty_lbl)

        body_lay.addStretch()
        scroll.setWidget(body)
        scroll.setMaximumHeight(360)
        lay.addWidget(scroll, 1)

        # ── 底部：关闭 ──
        foot = QHBoxLayout()
        close_btn = QPushButton("关闭")
        close_btn.setStyleSheet(
            f"background: {ACCENT}; color: #FFFFFF; border: none;"
            f" border-radius: 10px; padding: 8px 28px;"
            f" font-size: 13px; font-weight: 600;")
        close_btn.clicked.connect(self.accept)
        foot.addStretch()
        foot.addWidget(close_btn)
        lay.addLayout(foot)

    def _row_card(self, project, msg):
        from ui.theme import CARD, BORDER, TEXT_PRIMARY, TEXT_SECONDARY
        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background: {CARD}; border: 1px solid {BORDER};"
            f" border-radius: 12px; }}")
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 9, 14, 9)
        v.setSpacing(3)
        pj = QLabel(project)
        pj.setStyleSheet(
            f"font-size: 12px; font-weight: 600; color: {TEXT_SECONDARY};")
        v.addWidget(pj)
        msg_lbl = QLabel(msg)
        msg_lbl.setWordWrap(True)
        msg_lbl.setStyleSheet(f"font-size: 13px; color: {TEXT_PRIMARY};")
        v.addWidget(msg_lbl)
        return card
