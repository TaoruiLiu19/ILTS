"""§10.3 依赖视图：列表 + 简单连线图（纯 Qt 绘制，无第三方依赖）。

两种视图：
  · 列表（视图）：缩进树 + 状态标签 + 「待上游：《MBL》」文案。
  · 连线图：左侧为无上游的起点，按缩进层级排布方框，用折线箭头连接（QPainter）。

数据由 `services.doc_dependency.dependency_view(batch_id)` 提供，本文件只负责画。
"""

from PySide6.QtCore import Qt, QSize, QRectF, QPointF, Signal
from PySide6.QtGui import (QColor, QFont, QPainter, QPen, QBrush, QFontMetrics,
                           QPolygonF)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QTabWidget,
    QScrollArea, QSizePolicy,
)

from services import doc_dependency
from ui.theme import (ACCENT, GREEN, ORANGE, RED, TEXT_PRIMARY, TEXT_SECONDARY,
                      TEXT_TERTIARY, HAIRLINE)

# 状态 → (中文, 颜色)
STATE_META = {
    "submitted": ("已提交", GREEN),
    "waiting_upstream": ("待上游", "#8E8E93"),
    "ready": ("可提交", ACCENT),
    "absent": ("本批次无此单证", TEXT_TERTIARY),
}
GREY = "#8E8E93"
DASH = "#D1D1D6"


def _state_meta(state):
    return STATE_META.get(state, (state or "", TEXT_TERTIARY))


class DependencyCanvas(QWidget):
    """连线图画布：方框 + 折线箭头。只读展示，无交互依赖。"""

    BOX_W = 150
    BOX_H = 34
    COL_GAP = 62          # 列间距（留给箭头）
    ROW_GAP = 16
    PAD = 14

    def __init__(self, model=None, parent=None):
        super().__init__(parent)
        self._model = model or {"tree": [], "edges": []}
        self._layout = {}
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setMinimumWidth(360)
        self._relayout()

    def set_model(self, model):
        self._model = model or {"tree": [], "edges": []}
        self._relayout()
        self.update()

    # ── 布局 ──

    def _relayout(self):
        tree = self._model.get("tree") or []
        self._layout = {}
        y = self.PAD
        for node in tree:
            depth = node.get("depth", 0)
            x = self.PAD + depth * (self.BOX_W + self.COL_GAP)
            self._layout[node["key"]] = (x, y, node)
            y += self.BOX_H + self.ROW_GAP
        w = max([self.PAD * 2 + self.BOX_W] +
                [x + self.BOX_W + self.PAD for x, _y, _n in self._layout.values()])
        h = max(self.PAD * 2 + self.BOX_H, y - self.ROW_GAP + self.PAD)
        self.setFixedSize(QSize(max(360, w), max(120, h)))
        self._tooltip = self._build_tooltip()

    def _build_tooltip(self):
        lines = ["单证依赖链（§10.3）：箭头 = 上游 → 下游", ""]
        for node in self._model.get("tree") or []:
            label, _c = _state_meta(node.get("state"))
            prefix = "  " * node.get("depth", 0) + ("└ " if node.get("depth") else "")
            lines.append(f"{prefix}{node['name']} · {label}")
            if node.get("waiting_text"):
                lines.append(f"{'  ' * (node.get('depth', 0) + 1)}{node['waiting_text']}")
        return "\n".join(lines)

    def sizeHint(self):
        return self.size()

    # ── 绘制 ──

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QBrush(QColor("#FFFFFF")))

        # 先画连线（在方框下层）
        for e in self._model.get("edges") or []:
            up, down = self._layout.get(e["up"]), self._layout.get(e["down"])
            if not up or not down:
                continue
            self._draw_edge(p, up, down)

        for key, (x, y, node) in self._layout.items():
            self._draw_box(p, x, y, node)

        p.setPen(QPen(QColor(TEXT_TERTIARY)))
        p.setFont(QFont("Microsoft YaHei", 8))
        p.drawText(self.PAD, self.height() - 4,
                   "箭头方向：上游 → 下游；灰色虚线框 = 本批次无此单证；灰色 = 待上游")
        p.end()

    def _draw_box(self, p, x, y, node):
        _label, color = _state_meta(node.get("state"))
        rect = QRectF(x, y, self.BOX_W, self.BOX_H)
        waiting = node.get("state") == "waiting_upstream"
        absent = node.get("state") == "absent"
        fill = QColor("#F7F7F9" if waiting or absent else "#FFFFFF")
        border = QColor(DASH if absent else color)
        pen = QPen(border, 1)
        if absent:
            pen.setStyle(Qt.DashLine)
        elif waiting:
            pen.setStyle(Qt.SolidLine)
        p.setPen(pen)
        p.setBrush(QBrush(fill))
        p.drawRoundedRect(rect, 7, 7)

        p.setPen(QPen(QColor(GREY if (waiting or absent) else TEXT_PRIMARY)))
        p.setFont(QFont("Microsoft YaHei", 9, QFont.DemiBold))
        fm = QFontMetrics(p.font())
        name = fm.elidedText(f"{node['name']}", Qt.ElideRight, self.BOX_W - 12)
        p.drawText(rect.adjusted(7, 0, -7, 0), Qt.AlignVCenter | Qt.AlignLeft, name)

        if waiting:
            p.setPen(QPen(QColor(RED)))
            p.setFont(QFont("Microsoft YaHei", 8))
            p.drawText(rect.adjusted(0, 0, -6, 0), Qt.AlignVCenter | Qt.AlignRight, "待上游")

    def _draw_edge(self, p, up, down):
        ux, uy, unode = up
        dx, dy, dnode = down
        waiting = (unode.get("state") == "waiting_upstream"
                   or dnode.get("state") == "waiting_upstream")
        color = QColor("#AEAEB2" if waiting else "#C7C7CC")
        pen = QPen(color, 1.3)
        pen.setStyle(Qt.SolidLine)
        p.setPen(pen)
        x1 = ux + self.BOX_W
        y1 = uy + self.BOX_H / 2
        x2 = dx
        y2 = dy + self.BOX_H / 2
        mid = x1 + max(12, (x2 - x1) / 2)
        path = [QPointF(x1, y1), QPointF(mid, y1),
                QPointF(mid, y2), QPointF(x2, y2)]
        for i in range(len(path) - 1):
            p.drawLine(path[i], path[i + 1])
        # 箭头
        p.setBrush(QBrush(color))
        p.setPen(Qt.NoPen)
        arrow = QPolygonF([QPointF(x2 - 6, y2 - 4), QPointF(x2, y2),
                           QPointF(x2 - 6, y2 + 4)])
        p.drawPolygon(arrow)


class _TreeRow(QFrame):
    """列表视图一行：缩进 + 方点 + 名称 + 状态 + 待上游文案。"""

    def __init__(self, node, depth, parent=None):
        super().__init__(parent)
        label, color = _state_meta(node.get("state"))
        self.setFixedHeight(32)
        self.setStyleSheet(f"_TreeRow {{ border-bottom: 1px solid {HAIRLINE}; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6 + depth * 18, 0, 8, 0)
        lay.setSpacing(8)

        dot = QLabel()
        dot.setFixedSize(8, 8)
        dot.setStyleSheet(f"background: {color}; border-radius: 4px;")
        lay.addWidget(dot)

        name = QLabel(node.get("name") or "")
        name.setStyleSheet(
            f"font-size: 12px; color: {GREY if node.get('state') == 'waiting_upstream' else TEXT_PRIMARY};")
        lay.addWidget(name)

        cat = QLabel(node.get("category") or "")
        cat.setStyleSheet(
            f"font-size: 10px; color: {TEXT_TERTIARY}; background: #F2F2F7;"
            " border-radius: 4px; padding: 1px 5px;")
        lay.addWidget(cat)

        if node.get("waiting_text"):
            wt = QLabel(node["waiting_text"])
            wt.setStyleSheet(
                f"font-size: 10px; color: {GREY}; background: #F2F2F7;"
                " border-radius: 4px; padding: 1px 5px;")
            lay.addWidget(wt)

        lay.addStretch()

        st = QLabel(label)
        st.setStyleSheet(f"font-size: 11px; font-weight: 600; color: {color};")
        lay.addWidget(st)


class DependencyView(QWidget):
    """§10.3 依赖视图（列表 + 简单连线图），可独立嵌入对话框或批次详情页。"""

    DEPENDENCY_IGNORED = Signal(str)

    def __init__(self, batch_id=None, model=None, parent=None):
        super().__init__(parent)
        self._batch_id = batch_id
        self._model = model or {}
        if not self._model and batch_id:
            self._model = doc_dependency.dependency_view(batch_id)
        self._build()

    # ── 构建 ──

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        self._head = QLabel("")
        self._head.setWordWrap(True)
        self._head.setStyleSheet(f"font-size: 11px; color: {TEXT_TERTIARY};")
        lay.addWidget(self._head)

        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        self._tabs.addTab(self._build_list_page(), "列表")
        self._tabs.addTab(self._build_graph_page(), "连线图")
        lay.addWidget(self._tabs, 1)
        self.refresh()

    def _build_list_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self._list_scroll = QScrollArea()
        self._list_scroll.setWidgetResizable(True)
        self._list_scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }")
        self._list_body = QWidget()
        self._list_lay = QVBoxLayout(self._list_body)
        self._list_lay.setContentsMargins(0, 0, 0, 0)
        self._list_lay.setSpacing(0)
        self._list_lay.addStretch()
        self._list_scroll.setWidget(self._list_body)
        v.addWidget(self._list_scroll, 1)
        return page

    def _build_graph_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self._graph_scroll = QScrollArea()
        self._graph_scroll.setWidgetResizable(True)
        self._graph_scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }")
        self._canvas = DependencyCanvas(self._model)
        self._graph_scroll.setWidget(self._canvas)
        v.addWidget(self._graph_scroll, 1)
        return page

    # ── 刷新 ──

    def set_batch(self, batch_id):
        self._batch_id = batch_id
        self._model = doc_dependency.dependency_view(batch_id) if batch_id else {}
        self.refresh()

    def set_model(self, model):
        self._model = model or {}
        self.refresh()

    def refresh(self):
        waiting = self._model.get("waiting") or []
        edges = self._model.get("edges") or []
        nodes = self._model.get("nodes") or []
        self._head.setText(
            f"§10.3 单证依赖链：共 {len(nodes)} 个环节 / {len(edges)} 条依赖边；"
            f"当前 {len(waiting)} 个环节处于「待上游」状态。"
            + ("（上游未完成时下游标灰，不阻断提交，仅提示）" if waiting
               else "（依赖均已满足）"))

        while self._list_lay.count():
            item = self._list_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        tree = self._model.get("tree") or []
        if not tree:
            empty = QLabel("本批次暂无依赖数据（依赖表为空或批次不存在）")
            empty.setStyleSheet(f"font-size: 12px; color: {TEXT_TERTIARY}; padding: 20px;")
            self._list_lay.addWidget(empty)
        else:
            for node in tree:
                self._list_lay.addWidget(_TreeRow(node, node.get("depth", 0)))
        self._list_lay.addStretch()

        self._canvas.set_model(self._model)

    def model(self):
        """供自测读取当前依赖模型。"""
        return self._model

    def waiting_texts(self):
        """供自测：当前所有「待上游」文案。"""
        return [n.get("waiting_text") for n in (self._model.get("tree") or [])
                if n.get("waiting_text")]


def dependency_view_for(batch_id):
    """便捷函数：直接取依赖视图模型（服务层口径）。"""
    return doc_dependency.dependency_view(batch_id)
