"""
页面三：新建项目录入页 — 国家/出口港下拉 + 节点列表 + 货物台账 + 班轮信息 + 保存
"""

from datetime import date
from uuid import uuid4

from services.clock import get_today

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QDateEdit, QComboBox, QSpinBox, QDoubleSpinBox,
    QScrollArea, QFrame, QGroupBox, QFormLayout
)
from PySide6.QtCore import Qt, Signal, QDate, QSize

import db
from config import COUNTRIES, PORTS, get_country, get_port
from services.scheduler import generate_schedule
from services.file_checklist import bootstrap
from services.cargo_check import normalize_item, item_over_types, summary
from ui.theme import (
    ACCENT, RED, GREEN, TEXT_PRIMARY, TEXT_SECONDARY,
    TEXT_TERTIARY, HAIRLINE, GRAY_SOFT
)
from ui.icons import icon

_UNITS = ["台", "套", "件", "箱", "捆", "卷", "批", "块", "吨", "米"]
_PACKS = ["", "木箱", "裸装", "绑扎", "铁架", "托盘", "卷装", "散装"]


class CargoRow(QFrame):
    """货物台账单行录入控件"""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(42)
        self.setStyleSheet(f"CargoRow {{ border-bottom: 1px solid {HAIRLINE}; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 3, 2, 3)
        lay.setSpacing(6)

        self.seq_label = QLabel("1")
        self.seq_label.setFixedWidth(26)
        self.seq_label.setAlignment(Qt.AlignCenter)
        self.seq_label.setStyleSheet(
            f"background: {GRAY_SOFT}; color: {TEXT_SECONDARY}; font-weight: 600;"
            f" border-radius: 8px; font-size: 11px;")
        lay.addWidget(self.seq_label)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("设备/货名")
        self.name_edit.setFixedWidth(150)
        self.name_edit.setMaxLength(40)
        self.name_edit.textChanged.connect(lambda *_: self._refresh_flag())
        lay.addWidget(self.name_edit)

        self.qty_spin = QSpinBox()
        self.qty_spin.setRange(1, 9999)
        self.qty_spin.setValue(1)
        self.qty_spin.setFixedWidth(56)
        lay.addWidget(self.qty_spin)

        self.unit_combo = QComboBox()
        self.unit_combo.addItems(_UNITS)
        self.unit_combo.setFixedWidth(54)
        lay.addWidget(self.unit_combo)

        self.dim_spins = []
        for _ in range(3):
            ds = QDoubleSpinBox()
            ds.setRange(0, 5000)
            ds.setDecimals(1)
            ds.setValue(0.0)
            ds.setSingleStep(0.1)
            ds.setFixedWidth(56)
            ds.setSuffix("m")
            ds.valueChanged.connect(lambda *_: self._refresh_flag())
            lay.addWidget(ds)
            self.dim_spins.append(ds)

        self.weight_spin = QDoubleSpinBox()
        self.weight_spin.setRange(0, 5_000_000)
        self.weight_spin.setDecimals(0)
        self.weight_spin.setSingleStep(500)
        self.weight_spin.setFixedWidth(96)
        self.weight_spin.setSuffix(" kg")
        self.weight_spin.setValue(0)
        self.weight_spin.valueChanged.connect(lambda *_: self._refresh_flag())
        lay.addWidget(self.weight_spin)

        self.pack_combo = QComboBox()
        self.pack_combo.addItems(_PACKS)
        self.pack_combo.setFixedWidth(70)
        lay.addWidget(self.pack_combo)

        self.marks_edit = QLineEdit()
        self.marks_edit.setPlaceholderText("唛头/备注")
        self.marks_edit.setMaxLength(60)
        lay.addWidget(self.marks_edit, stretch=1)

        self.flag_label = QLabel("")
        self.flag_label.setFixedWidth(86)
        self.flag_label.setStyleSheet(f"font-size: 10px; color: {RED}; font-weight: 600;")
        lay.addWidget(self.flag_label)

        del_btn = QPushButton("移除")
        del_btn.setObjectName("ghost")
        del_btn.setFixedWidth(48)
        del_btn.setCursor(Qt.PointingHandCursor)
        del_btn.clicked.connect(self._on_delete)
        lay.addWidget(del_btn)
        self._del_handler = None

    def _on_delete(self):
        if self._del_handler:
            self._del_handler(self)

    def on_delete(self, handler):
        self._del_handler = handler

    def _refresh_flag(self):
        over_types = item_over_types(self.to_item())
        if over_types:
            self.flag_label.setText("⚠ " + "、".join(over_types))
            self.flag_label.setStyleSheet(
                f"font-size: 10px; color: {RED}; font-weight: 600;")
        else:
            self.flag_label.setText("")
        self.changed.emit()

    def set_seq(self, i):
        self.seq_label.setText(str(i))

    def to_item(self):
        dims = [s.value() for s in self.dim_spins]
        return {
            "item_name": self.name_edit.text().strip(),
            "qty": self.qty_spin.value(),
            "unit": self.unit_combo.currentText(),
            "dim_l": dims[0] or None,
            "dim_w": dims[1] or None,
            "dim_h": dims[2] or None,
            "weight_kg": self.weight_spin.value() or None,
            "packaging": self.pack_combo.currentText() or None,
            "marks": self.marks_edit.text().strip() or None,
        }


class NewProjectPage(QWidget):
    navigate = Signal(str)
    toast = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._node_widgets = {}
        self._cargo_rows = []
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(32, 28, 32, 32)
        layout.setSpacing(16)

        title = QLabel("新建项目")
        title.setObjectName("title")
        layout.addWidget(title)

        sub = QLabel("填写基本信息，系统将自动生成节点排期与单证清单")
        sub.setObjectName("subtitle")
        layout.addWidget(sub)

        # ── 基本信息 ──
        form_group = QGroupBox("基本信息")
        form_layout = QFormLayout(form_group)
        form_layout.setLabelAlignment(Qt.AlignRight)
        form_layout.setContentsMargins(20, 20, 20, 20)
        form_layout.setHorizontalSpacing(18)
        form_layout.setVerticalSpacing(14)

        self.country_combo = QComboBox()
        for code, c in COUNTRIES.items():
            self.country_combo.addItem(f"{c['flag']} {c['name']}", code)
        self.country_combo.currentIndexChanged.connect(self._on_country_changed)
        form_layout.addRow("目的国", self.country_combo)

        self.port_combo = QComboBox()
        self.port_combo.addItem("通用方案", None)
        for code, p in PORTS.items():
            self.port_combo.addItem(p["name"], code)
        form_layout.addRow("出口港", self.port_combo)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("如：青岛 → 巴西 Sepetiba 光伏组件运输")
        self.name_edit.setMaxLength(50)
        form_layout.addRow("项目名称", self.name_edit)

        today = get_today()
        self.etd_edit = QDateEdit()
        self.etd_edit.setCalendarPopup(True)
        self.etd_edit.setDate(QDate(today.year, today.month, today.day))
        self.etd_edit.setDisplayFormat("yyyy-MM-dd")
        form_layout.addRow("ETD 离港", self.etd_edit)

        self.eta_edit = QDateEdit()
        self.eta_edit.setCalendarPopup(True)
        self.eta_edit.setDate(QDate(today.year, today.month, today.day).addDays(46))
        self.eta_edit.setDisplayFormat("yyyy-MM-dd")
        form_layout.addRow("ETA 到港", self.eta_edit)

        self.buffer_spin = QSpinBox()
        self.buffer_spin.setRange(0, 30)
        self.buffer_spin.setValue(4)
        form_layout.addRow("缓冲天数", self.buffer_spin)

        layout.addWidget(form_group)

        # ── 高级排程参数 ──
        self.schedule_group = QGroupBox("节点排期")
        self.schedule_layout = QVBoxLayout(self.schedule_group)
        self.schedule_layout.setContentsMargins(12, 16, 12, 12)
        self.schedule_layout.setSpacing(0)

        hint = QLabel("调整耗时后系统将重算所有节点起止日期与单证建议日")
        hint.setStyleSheet(f"font-size: 12px; color: {TEXT_TERTIARY}; padding: 0 8px 10px 8px;")
        self.schedule_layout.addWidget(hint)
        layout.addWidget(self.schedule_group)

        self._build_node_rows()

        # ── 货物台账（优化方案 D1 §2.4） ──
        cargo_group = QGroupBox("货物台账（可选）")
        cargo_layout = QVBoxLayout(cargo_group)
        cargo_layout.setContentsMargins(16, 18, 16, 16)
        cargo_layout.setSpacing(8)

        head_row = QHBoxLayout()
        cargo_hint = QLabel("登记每件设备的尺寸 / 毛重；超重（>100t）或超长（>36m）将触发节点3「捆扎固定」吊装预警")
        cargo_hint.setStyleSheet(f"font-size: 12px; color: {TEXT_TERTIARY};")
        cargo_hint.setWordWrap(True)
        head_row.addWidget(cargo_hint, stretch=1)

        fill_btn = QPushButton("预填示例")
        fill_btn.setObjectName("ghost")
        fill_btn.setCursor(Qt.PointingHandCursor)
        fill_btn.setToolTip("填入两件示例：光伏组件（普通）+ 主变压器（118t 超重）")
        fill_btn.clicked.connect(self._fill_samples)
        head_row.addWidget(fill_btn)

        add_btn = QPushButton("＋ 添加货项")
        add_btn.setObjectName("secondary")
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.setIcon(icon("new", ACCENT, 14))
        add_btn.setIconSize(QSize(14, 14))
        add_btn.clicked.connect(lambda: self._add_cargo_row())
        head_row.addWidget(add_btn)
        cargo_layout.addLayout(head_row)

        # 列头
        col_head = QLabel(
            f"{'#':<3}{'货名':<28}{'数量':<10}{'单位':<10}"
            f"{'长m':<11}{'宽m':<11}{'高m':<11}{'毛重kg':<18}{'包装':<14}备注")
        col_head.setStyleSheet(f"font-size: 11px; color: {TEXT_TERTIARY};")
        cargo_layout.addWidget(col_head)

        self.cargo_scroll = QScrollArea()
        self.cargo_scroll.setWidgetResizable(True)
        self.cargo_scroll.setFixedHeight(170)
        self.cargo_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self.cargo_body = QWidget()
        self.cargo_body_lay = QVBoxLayout(self.cargo_body)
        self.cargo_body_lay.setContentsMargins(0, 0, 0, 0)
        self.cargo_body_lay.setSpacing(0)
        self.cargo_body_lay.addStretch()
        self.cargo_scroll.setWidget(self.cargo_body)
        cargo_layout.addWidget(self.cargo_scroll)

        self.cargo_summary = QLabel("未登记货物")
        self.cargo_summary.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
        cargo_layout.addWidget(self.cargo_summary)
        layout.addWidget(cargo_group)

        # ── 班轮信息（优化方案 D1 §2.2，1 项目 1 船） ──
        vessel_group = QGroupBox("班轮信息（可选）")
        vessel_form = QFormLayout(vessel_group)
        vessel_form.setLabelAlignment(Qt.AlignRight)
        vessel_form.setContentsMargins(20, 20, 20, 16)
        vessel_form.setHorizontalSpacing(18)
        vessel_form.setVerticalSpacing(12)

        self.vessel_name = QLineEdit()
        self.vessel_name.setPlaceholderText("如：COSCO SHIPPING UNIVERSE")
        self.vessel_name.setMaxLength(60)
        vessel_form.addRow("船名", self.vessel_name)

        vrow = QHBoxLayout()
        self.voyage = QLineEdit()
        self.voyage.setPlaceholderText("航次")
        self.voyage.setMaxLength(30)
        self.imo = QLineEdit()
        self.imo.setPlaceholderText("IMO")
        self.imo.setMaxLength(12)
        self.carrier = QLineEdit()
        self.carrier.setPlaceholderText("承运人")
        self.carrier.setMaxLength(40)
        self.mmsi = QLineEdit()
        self.mmsi.setPlaceholderText("MMSI(预留AIS)")
        self.mmsi.setMaxLength(12)
        vrow.addWidget(self.voyage, 2)
        vrow.addWidget(self.imo, 2)
        vrow.addWidget(self.carrier, 3)
        vrow.addWidget(self.mmsi, 2)
        vessel_form.addRow("航次 / IMO / 承运人 / MMSI", vrow)

        vtip = QLabel("系统离线运行：船位动态由操作员在主看板手工登记，不接入实时 AIS")
        vtip.setStyleSheet(f"font-size: 11px; color: {TEXT_TERTIARY};")
        vessel_form.addRow("", vtip)
        layout.addWidget(vessel_group)

        # ── 预览 ──
        self.preview_label = QLabel("")
        layout.addWidget(self.preview_label)
        self._update_preview()

        # ── 按钮 ──
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("secondary")
        cancel_btn.clicked.connect(lambda: self.navigate.emit("home"))
        btn_row.addWidget(cancel_btn)

        save_btn = QPushButton("保存项目")
        save_btn.setObjectName("primary")
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.setIcon(icon("completed", "#FFFFFF", 16))
        save_btn.setIconSize(QSize(16, 16))
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)

        layout.addLayout(btn_row)
        layout.addStretch()

        scroll.setWidget(container)
        outer.addWidget(scroll)

    def _build_node_rows(self):
        for w in list(self._node_widgets.values()):
            w[0].deleteLater()
        self._node_widgets.clear()

        country_code = self.country_combo.currentData()
        tmpl = get_country(country_code)
        if not tmpl:
            return

        for node in tmpl["nodes"]:
            row = QFrame()
            row.setFixedHeight(46)
            row.setStyleSheet(f"border-bottom: 1px solid {HAIRLINE};")
            rlayout = QHBoxLayout(row)
            rlayout.setContentsMargins(8, 6, 8, 6)
            rlayout.setSpacing(12)

            seq_label = QLabel(f"{node['id']}")
            seq_label.setFixedSize(30, 30)
            seq_label.setAlignment(Qt.AlignCenter)
            seq_label.setStyleSheet(
                f"background: #F2F2F7; color: {TEXT_SECONDARY}; font-weight: 600;"
                f" border-radius: 9px; font-size: 12px;"
            )
            rlayout.addWidget(seq_label)

            name_label = QLabel(node["name"])
            name_label.setFixedWidth(190)
            name_label.setStyleSheet(f"font-size: 13px; color: {TEXT_PRIMARY};")
            rlayout.addWidget(name_label)

            role_label = QLabel(node["role"])
            role_label.setStyleSheet(f"font-size: 11px; color: {TEXT_SECONDARY};")
            rlayout.addWidget(role_label)

            rlayout.addStretch()

            dur_spin = QSpinBox()
            dur_spin.setRange(1, 999)
            dur_spin.setValue(node["duration"])
            dur_spin.setFixedWidth(76)
            dur_spin.valueChanged.connect(self._update_preview)
            rlayout.addWidget(dur_spin)

            unit = QLabel("天")
            unit.setStyleSheet(f"font-size: 13px; color: {TEXT_SECONDARY};")
            rlayout.addWidget(unit)

            self.schedule_layout.addWidget(row)
            self._node_widgets[node["id"]] = (row, dur_spin)

    # ── 货物台账行管理 ──

    def _add_cargo_row(self):
        row = CargoRow()
        row.on_delete(self._remove_cargo_row)
        row.changed.connect(self._update_cargo_summary)
        self.cargo_body_lay.insertWidget(self.cargo_body_lay.count() - 1, row)
        self._cargo_rows.append(row)
        self._renumber_cargo()
        self._update_cargo_summary()

    def _remove_cargo_row(self, row):
        if row in self._cargo_rows:
            self._cargo_rows.remove(row)
            row.deleteLater()
            self._renumber_cargo()
            self._update_cargo_summary()

    def _renumber_cargo(self):
        for i, row in enumerate(self._cargo_rows, start=1):
            row.set_seq(i)

    def _fill_samples(self):
        """预填：光伏组件（普通）+ 主变压器（118t 超重，触发吊装预警）"""
        if not self._cargo_rows:
            self._add_cargo_row()
            self._add_cargo_row()
        rows = self._cargo_rows
        rows[0].name_edit.setText("光伏组件（叠层）")
        rows[0].qty_spin.setValue(20)
        rows[0].unit_combo.setCurrentText("套")
        rows[0].dim_spins[0].setValue(2.3)
        rows[0].dim_spins[1].setValue(1.1)
        rows[0].dim_spins[2].setValue(0.4)
        rows[0].weight_spin.setValue(1200)
        rows[0].pack_combo.setCurrentText("木箱")
        rows[0].marks_edit.setText("QD-BR-SEP-01~20")
        if len(rows) > 1:
            rows[1].name_edit.setText("主变压器")
            rows[1].qty_spin.setValue(1)
            rows[1].unit_combo.setCurrentText("台")
            rows[1].dim_spins[0].setValue(6.0)
            rows[1].dim_spins[1].setValue(2.5)
            rows[1].dim_spins[2].setValue(3.2)
            rows[1].weight_spin.setValue(118000)
            rows[1].pack_combo.setCurrentText("裸装")
            rows[1].marks_edit.setText("重件 · 码头自吊机")
        self._renumber_cargo()
        self._update_cargo_summary()

    def _update_cargo_summary(self):
        items = [r.to_item() for r in self._cargo_rows if r.to_item().get("item_name")]
        if not items:
            self.cargo_summary.setText("未登记货物（可跳过）")
            self.cargo_summary.setStyleSheet(f"font-size: 12px; color: {TEXT_TERTIARY};")
            return
        s = summary(items)
        over_text = f" · 超限 {s['over']} 项（节点3吊装预警）" if s["over"] else ""
        self.cargo_summary.setText(
            f"共 {s['count']} 项 · 毛重合计 {s['total_weight_kg'] / 1000:.1f} t{over_text}")
        self.cargo_summary.setStyleSheet(
            f"font-size: 12px; color: {'#C0392B' if s['over'] else TEXT_SECONDARY}; font-weight: 500;")

    def _on_country_changed(self):
        self._build_node_rows()
        self._update_preview()

    def _update_preview(self):
        try:
            etd = self.etd_edit.date().toPython()
            eta = self.eta_edit.date().toPython()
            if eta <= etd:
                self._set_preview("ETA 必须晚于 ETD 至少 1 天", RED, "#FDEBEA")
                return

            country_code = self.country_combo.currentData()
            tmpl = get_country(country_code)
            nodes_cfg = []
            for node in tmpl["nodes"]:
                dur = self._node_widgets[node["id"]][1].value()
                nodes_cfg.append({"id": node["id"], "dur": dur, "area": node["area"]})

            plan = generate_schedule(etd.strftime("%Y-%m-%d"), eta.strftime("%Y-%m-%d"), nodes_cfg)
            first_date = plan[1][0]
            last_date = plan[max(plan.keys())][1]
            sea_node = next(n for n in tmpl["nodes"] if n["area"] == "SEA")
            sea_days = (_parse_date(plan[sea_node["id"]][1]) - _parse_date(plan[sea_node["id"]][0])).days

            port_code = self.port_combo.currentData()
            port = get_port(port_code)
            port_text = f" · 出口港 {port['name']}" if port else " · 通用方案"

            self._set_preview(
                f"将生成 {len(nodes_cfg)} 个节点 · {first_date} → {last_date} · 海运 {sea_days} 天{port_text}",
                GREEN, "#E8F8EC"
            )
        except Exception as e:
            self._set_preview(f"⚠ {e}", RED, "#FDEBEA")

    def _set_preview(self, text, color, bg):
        self.preview_label.setText(f"  {text}")
        self.preview_label.setStyleSheet(
            f"font-size: 12px; color: {color}; padding: 10px 2px;"
            f" background: {bg}; border-radius: 10px;"
        )

    def _save(self):
        name = self.name_edit.text().strip()
        if not name:
            self.name_edit.setStyleSheet("border: 2px solid #FF3B30;")
            self.toast.emit("项目名称不能为空")
            return
        self.name_edit.setStyleSheet("")

        etd = self.etd_edit.date().toPython()
        eta = self.eta_edit.date().toPython()
        if eta <= etd:
            self.toast.emit("ETA 必须晚于 ETD 至少 1 天")
            return

        country_code = self.country_combo.currentData()
        port_code = self.port_combo.currentData()
        buffer_days = self.buffer_spin.value()
        tmpl = get_country(country_code)

        nodes_cfg = []
        for node in tmpl["nodes"]:
            dur = self._node_widgets[node["id"]][1].value()
            nodes_cfg.append({"id": node["id"], "dur": dur, "area": node["area"]})

        plan = generate_schedule(etd.strftime("%Y-%m-%d"), eta.strftime("%Y-%m-%d"), nodes_cfg)

        project_id = f"proj-{uuid4().hex[:8]}"
        project = {
            "project_id": project_id,
            "project_name": name,
            "country": country_code,
            "export_port": port_code,
            "etd": etd.strftime("%Y-%m-%d"),
            "eta": eta.strftime("%Y-%m-%d"),
            "buffer_days": buffer_days,
        }
        db.insert_project(project)

        nodes = []
        for node in tmpl["nodes"]:
            s, e = plan[node["id"]]
            remark = node.get("remark", "")
            nodes.append({
                "node_id": node["id"],
                "node_name": node["name"],
                "role_label": node["role"],
                "seq": node["id"],
                "area": node["area"],
                "default_duration": node["duration"],
                "duration": self._node_widgets[node["id"]][1].value(),
                "plan_start": s,
                "plan_end": e,
                "remark": remark,
            })
        db.insert_nodes(project_id, nodes)

        files = bootstrap(country_code, port_code, plan)
        db.insert_files(project_id, files)

        # 货物台账
        cargo_count = 0
        over_count = 0
        raw_items = [r.to_item() for r in self._cargo_rows]
        valid = []
        for it in raw_items:
            if not it.get("item_name"):
                continue
            if not any([it.get("qty"), it.get("dim_l"), it.get("dim_w"),
                        it.get("dim_h"), it.get("weight_kg"), it.get("marks")]):
                continue
            valid.append(normalize_item(it))
        if valid:
            db.insert_cargo_items(project_id, valid)
            cargo_count = len(valid)
            over_count = summary(valid)["over"]

        # 班轮信息
        has_vessel = any([self.vessel_name.text().strip(), self.voyage.text().strip(),
                          self.imo.text().strip(), self.carrier.text().strip(),
                          self.mmsi.text().strip()])
        if has_vessel:
            db.upsert_vessel(project_id,
                             vessel_name=self.vessel_name.text().strip() or None,
                             voyage=self.voyage.text().strip() or None,
                             imo=self.imo.text().strip() or None,
                             carrier=self.carrier.text().strip() or None,
                             mmsi=self.mmsi.text().strip() or None)

        port_text = ""
        if port_code:
            port_text = f"，国内段已套用{get_port(port_code)['name']}线上平台备注"

        cargo_text = f"，货物 {cargo_count} 项" + ("⚠ 含超限件" if over_count else "")
        self.toast.emit(f'项目「{name}」已创建，生成 {len(nodes)} 节点与 {len(files)} 份单证清单'
                        f'{cargo_text}{port_text}')
        self.navigate.emit("dashboard")

    def refresh(self):
        pass


def _parse_date(s):
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))
