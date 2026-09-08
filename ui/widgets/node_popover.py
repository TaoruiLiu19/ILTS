"""
甘特节点悬浮速览卡 — 悬停甘特某行时，就地展示该节点的单证清单速览
（只读，不抢焦点，不拦截点击；文件面板默认收起时主反馈通道）
"""

from PySide6.QtWidgets import QFrame, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea
from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QGuiApplication

from services.clock import get_today
from services.node_status import compute_node_status
from ui.theme import (
    GREEN, RED, ORANGE, GRAY, TEXT_PRIMARY, TEXT_SECONDARY, TEXT_TERTIARY,
    HAIRLINE, ACCENT, ACCENT_SOFT, NODE_STATUS_TEXT
)

_MAX_ROWS = 7


def _parse_due(s):
    return s[5:] if s and len(s) >= 10 else (s or "—")


class NodePopover(QFrame):
    """无边框悬浮卡：标题（节点信息）+ 单证速览列表"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("popover")
        self.setWindowFlags(Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self._today = get_today()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(6)

        self._title = QLabel("")
        self._title.setStyleSheet(
            f"font-size: 13px; font-weight: 600; color: {TEXT_PRIMARY};")
        outer.addWidget(self._title)

        self._sub = QLabel("")
        self._sub.setStyleSheet(f"font-size: 11px; color: {TEXT_SECONDARY};")
        outer.addWidget(self._sub)

        self._count = QLabel("")
        self._count.setStyleSheet(f"font-size: 11px; color: {TEXT_TERTIARY};")
        outer.addWidget(self._count)

        self._list = QScrollArea()
        self._list.setWidgetResizable(True)
        self._list.setFixedHeight(_MAX_ROWS * 22 + 6)
        self._list.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self._body = QFrame()
        self._body.setStyleSheet("background: transparent;")
        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(0, 0, 0, 0)
        self._body_lay.setSpacing(0)
        self._body_lay.addStretch()
        self._list.setWidget(self._body)
        outer.addWidget(self._list)

        self.hide()

    # ── 数据填充 ──

    def show_node(self, node, files, global_pos, today=None):
        if today is not None:
            self._today = today
        self._today = today or self._today

        self._title.setText(f"节点{node['node_id']} · {node['node_name']}")
        role = node.get("role_label") or ""
        st = compute_node_status(node, self._today)
        st_text = NODE_STATUS_TEXT.get(st, "")
        self._sub.setText(f"{role}  ·  {node['plan_start']} ~ {node['plan_end']}"
                          + (f"  ·  {st_text}" if st_text else ""))

        rows = list(files)
        required = sum(1 for f in rows if f.get("doc_type") == "required")
        pending = sum(1 for f in rows if f.get("status") != "submitted")
        if rows:
            self._count.setText(f"绑定单证 {len(rows)} 份（必填 {required} · 未提交 {pending}）")
        else:
            self._count.setText("该节点未绑定单证")

        self._clear_rows()
        shown = 0
        for f in rows:
            if shown >= _MAX_ROWS:
                more = QLabel(f"… 另有 {len(rows) - shown} 份，详见「单证清单」")
                more.setStyleSheet(f"font-size: 11px; color: {TEXT_TERTIARY};")
                self._body_lay.insertWidget(self._body_lay.count() - 1, more)
                break
            self._body_lay.insertWidget(self._body_lay.count() - 1, self._row_widget(f))
            shown += 1
        if not rows:
            empty = QLabel("（无）")
            empty.setStyleSheet(f"font-size: 12px; color: {GRAY};")
            self._body_lay.insertWidget(self._body_lay.count() - 1, empty)

        self.adjustSize()
        self._place(global_pos)
        self.show()
        self.raise_()

    def _clear_rows(self):
        while self._body_lay.count():
            item = self._body_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._body_lay.addStretch()

    def _row_widget(self, f):
        from services.clock import get_today
        row = QFrame()
        row.setFixedHeight(22)
        row.setStyleSheet(f"border-bottom: 1px solid {HAIRLINE}; background: transparent;")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(8)

        submitted = f.get("status") == "submitted"
        required = f.get("doc_type") == "required"

        state, color = ("已交", GREEN) if submitted else ("必填", RED) if required else ("可选", GRAY)
        tag = QLabel(state)
        tag.setFixedWidth(34)
        tag.setStyleSheet(f"font-size: 10px; font-weight: 600; color: {color};")
        lay.addWidget(tag)

        name = QLabel(f"《{f['doc_name']}》")
        name.setStyleSheet(f"font-size: 12px; color: {TEXT_PRIMARY};")
        lay.addWidget(name, stretch=1)

        if f.get("due_date"):
            due_text = f"建议 {_parse_due(f['due_date'])}"
            overdue = (not submitted) and f["due_date"] < get_today().strftime("%Y-%m-%d")
            due_lbl = QLabel(due_text)
            due_lbl.setStyleSheet(
                f"font-size: 11px; color: {RED if overdue else TEXT_TERTIARY};"
                + (" font-weight: 600;" if overdue else ""))
        else:
            due_lbl = QLabel("全程")
            due_lbl.setStyleSheet(f"font-size: 11px; color: {TEXT_TERTIARY};")
        lay.addWidget(due_lbl)
        return row

    def _place(self, global_pos):
        """把悬浮卡放到全局坐标附近，并夹在屏幕内"""
        scr = QGuiApplication.primaryScreen()
        if not scr:
            self.move(global_pos + QPoint(16, 12))
            return
        geo = scr.availableGeometry()
        x = min(global_pos.x() + 16, geo.right() - self.width() - 8)
        y = min(global_pos.y() + 12, geo.bottom() - self.height() - 8)
        self.move(max(geo.left() + 4, x), max(geo.top() + 4, y))

    def hide_card(self):
        self.hide()
