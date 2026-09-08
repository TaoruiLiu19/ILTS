"""
单证清单面板 — 分组展示项目级+各节点单证，支持提交/撤销
"""

from datetime import date

from services.clock import get_today

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QFrame, QScrollArea, QCheckBox, QGroupBox
)
from PySide6.QtCore import Qt, Signal

from services.node_status import compute_node_status
from ui.theme import (
    GREEN, RED, ORANGE, GRAY, TEXT_PRIMARY, TEXT_SECONDARY, TEXT_TERTIARY, HAIRLINE
)


def _parse(s):
    if isinstance(s, date):
        return s
    if not s:
        return None
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))


class FileRow(QFrame):
    toggled = Signal(int, bool)

    def __init__(self, file_data, node_status=None, today=None, readonly=False, parent=None):
        super().__init__(parent)
        self._data = file_data
        self._readonly = readonly
        self._node_status = node_status
        self._today = today or get_today()
        self._build()

    def _build(self):
        self.setFixedHeight(44)
        self.setStyleSheet(f"FileRow {{ border-bottom: 1px solid {HAIRLINE}; background: transparent; }}")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(12)

        f = self._data
        is_submitted = f.get("status") == "submitted"

        self.checkbox = QCheckBox()
        self.checkbox.setChecked(is_submitted)
        self.checkbox.setEnabled(not self._readonly)
        self.checkbox.toggled.connect(self._on_toggle)
        layout.addWidget(self.checkbox)

        # 必需/可选标签
        required = f["doc_type"] == "required"
        type_lbl = QLabel("必填" if required else "可选")
        type_lbl.setFixedSize(37, 20)
        type_lbl.setAlignment(Qt.AlignCenter)
        type_color = RED if required else TEXT_TERTIARY
        type_bg = "#FDEBE8" if required else "#F2F2F7"
        type_lbl.setStyleSheet(
            f"color: {type_color}; font-size: 10px; font-weight: 600;"
            f" background: {type_bg}; border-radius: 5px;"
        )
        layout.addWidget(type_lbl)

        name_label = QLabel(f["doc_name"])
        name_label.setStyleSheet(f"font-size: 13px; color: {TEXT_PRIMARY};")
        layout.addWidget(name_label)

        # 部门 × 份数
        dept = f.get("owner_dept") or ""
        copies = f.get("copies")
        dept_text = dept
        if copies:
            dept_text += f" ×{copies}"
        if dept_text:
            dept_label = QLabel(dept_text)
            dept_label.setStyleSheet(f"font-size: 11px; color: {TEXT_SECONDARY};")
            layout.addWidget(dept_label)

        layout.addStretch()

        # 建议日
        due = f.get("due_date")
        due_label = QLabel(f"建议 {due[5:]}" if due else "全程")
        due_label.setStyleSheet(f"font-size: 11px; color: {TEXT_TERTIARY};")
        layout.addWidget(due_label)

        # 状态
        status_text, status_color = self._status(is_submitted, due, required)
        status_label = QLabel(status_text)
        status_label.setFixedHeight(22)
        status_label.setStyleSheet(
            f"color: {status_color}; font-size: 11px; font-weight: 600;"
        )
        layout.addWidget(status_label)

    def _status(self, is_submitted, due, required):
        f = self._data
        if is_submitted:
            txt = "已提交"
            if f.get("submitted_date"):
                txt += f" · {f['submitted_date'][5:]}"
            return txt, GREEN

        due_date = _parse(due) if due else None
        if self._node_status in ("Active", "Overdue") and required:
            return "缺失", RED
        if due_date and self._today > due_date:
            return "超期", RED
        if due_date and 0 <= (due_date - self._today).days <= 3:
            return "临近", ORANGE
        return "待准备", GRAY

    def _on_toggle(self, checked):
        self.toggled.emit(self._data.get("file_id", -1), checked)


class FilePanel(QScrollArea):
    file_toggled = Signal(int, bool)

    def __init__(self, files, nodes=None, today=None, readonly=False, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self._files = files
        self._nodes = nodes or []
        self._today = today or get_today()
        self._readonly = readonly
        self._build()

    def _build(self):
        container = QWidget()
        container.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        node_map = {n["node_id"]: n for n in self._nodes}

        project_files = [f for f in self._files if f.get("node_id") is None]
        if project_files:
            layout.addWidget(self._make_group("项目级 · 全程常备", project_files, None))

        for nid in sorted(set(f["node_id"] for f in self._files if f.get("node_id") is not None)):
            node_files = [f for f in self._files if f.get("node_id") == nid]
            node_obj = node_map.get(nid)
            node_st = compute_node_status(node_obj, self._today) if node_obj else None
            node_name = node_obj["node_name"] if node_obj else f"节点{nid}"
            layout.addWidget(self._make_group(f"节点{nid} · {node_name}", node_files, node_st))

        container.setLayout(layout)
        self.setWidget(container)

    def _make_group(self, title, file_list, node_status):
        group = QGroupBox(title)
        glayout = QVBoxLayout(group)
        glayout.setContentsMargins(8, 12, 8, 4)
        glayout.setSpacing(0)

        for f in file_list:
            row = FileRow(f, node_status, self._today, self._readonly)
            row.toggled.connect(self.file_toggled.emit)
            glayout.addWidget(row)

        return group

    def refresh(self, files, nodes=None):
        self._files = files
        if nodes:
            self._nodes = nodes
        self._build()