"""
微型三区色块条 — 折叠卡片中部显示项目进度概览（支持数据刷新，不重复建布局）
"""

from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QFrame
from PySide6.QtCore import Qt

from ui.theme import AREA_COLORS, AREA_FG, TEXT_SECONDARY, AREA_TEXT


class MiniBar(QWidget):
    """微型三区色块条，显示境内/海运/境外三段进度"""

    def __init__(self, nodes, today=None, parent=None):
        super().__init__(parent)
        self.setFixedHeight(34)
        self._nodes = nodes
        self._today = today
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(4)
        self._rebuild()

    def _clear(self):
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _rebuild(self):
        self._clear()

        for area in ("DOME", "SEA", "OVERSEA"):
            area_nodes = [n for n in self._nodes if n["area"] == area]
            if not area_nodes:
                continue
            total = len(area_nodes)
            done = 0
            for n in area_nodes:
                from services.node_status import compute_node_status
                if compute_node_status(n, self._today) == "Done":
                    done += 1

            seg = QFrame()
            seg.setFixedHeight(34)
            seg.setMinimumWidth(72)
            seg.setStyleSheet(
                f"background: {AREA_COLORS[area]}; border-radius: 10px;"
            )
            seg_layout = QHBoxLayout(seg)
            seg_layout.setContentsMargins(12, 0, 12, 0)
            seg_layout.setSpacing(6)

            name = QLabel(AREA_TEXT[area])
            name.setStyleSheet(f"font-size: 11px; color: {AREA_FG[area]}; font-weight: 600;")
            seg_layout.addWidget(name)
            seg_layout.addStretch()

            label = QLabel(f"{done}/{total}")
            label.setStyleSheet(f"font-size: 11px; color: {TEXT_SECONDARY}; font-weight: 600;")
            seg_layout.addWidget(label)
            self._layout.addWidget(seg, stretch=total)

        self._layout.addStretch()

    def update_data(self, nodes, today=None):
        self._nodes = nodes
        if today is not None:
            self._today = today
        self._rebuild()
