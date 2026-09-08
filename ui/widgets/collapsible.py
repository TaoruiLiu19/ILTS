"""
可收纳区块组件 — 标题行 + 可折叠内容体
用于项目展开卡：除「时间轴·甘特」外的大块（动态调整 / 单证清单）默认收起、点击标题展开。
"""

from PySide6.QtWidgets import QFrame, QVBoxLayout, QPushButton, QWidget
from PySide6.QtCore import Qt, Signal, QSize

from ui.icons import icon
from ui.theme import ACCENT


class CollapsibleSection(QFrame):
    """标题可点击的折叠区块；expanded_changed 回调用于跨重建记忆状态"""

    expanded_changed = Signal(bool)

    def __init__(self, title, collapsed=True, parent=None):
        super().__init__(parent)
        self._expanded = not collapsed

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._head = QPushButton(title)
        self._head.setObjectName("collapsibleHead")
        self._head.setCursor(Qt.PointingHandCursor)
        self._head.setIconSize(QSize(14, 14))
        self._head.clicked.connect(self._toggle)
        outer.addWidget(self._head)

        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 2, 0, 0)
        self._body_layout.setSpacing(8)
        outer.addWidget(self._body)

        self._refresh()

    # ── 内容填充 ──

    def add_widget(self, w):
        self._body_layout.addWidget(w)

    def add_layout(self, layout):
        self._body_layout.addLayout(layout)

    def clear(self):
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    # ── 折叠状态 ──

    def _toggle(self):
        self.set_expanded(not self._expanded)

    def set_expanded(self, v):
        self._expanded = bool(v)
        self._body.setVisible(self._expanded)
        self._refresh()
        self.expanded_changed.emit(self._expanded)

    def is_expanded(self):
        return self._expanded

    def _refresh(self):
        glyph = "chevron_up" if self._expanded else "chevron_down"
        self._head.setIcon(icon(glyph, ACCENT, 14))
