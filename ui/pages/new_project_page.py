"""
页面三：新建项目录入页 — 国家/出口港下拉 + 节点列表 + 保存
"""

from datetime import date
from uuid import uuid4

from services.clock import get_today

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QDateEdit, QComboBox, QSpinBox, QScrollArea,
    QFrame, QGroupBox, QFormLayout, QSizePolicy
)
from PySide6.QtCore import Qt, Signal, QDate, QSize
from PySide6.QtGui import QFont

import db
from config import COUNTRIES, PORTS, get_country, get_port
from services.scheduler import generate_schedule
from services.file_checklist import bootstrap
from ui.theme import (
    GLOBAL_QSS, ACCENT, RED, GREEN, TEXT_PRIMARY, TEXT_SECONDARY,
    TEXT_TERTIARY, HAIRLINE
)
from ui.icons import icon


class NewProjectPage(QWidget):
    navigate = Signal(str)
    toast = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._node_widgets = {}
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

        port_text = ""
        if port_code:
            port_text = f"，国内段已套用{get_port(port_code)['name']}线上平台备注"

        self.toast.emit(f'项目「{name}」已创建，生成 {len(nodes)} 节点与 {len(files)} 份单证清单{port_text}')
        self.navigate.emit("dashboard")

    def refresh(self):
        pass


def _parse_date(s):
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))