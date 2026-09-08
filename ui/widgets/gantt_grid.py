"""
甘特网格控件 — 行=节点，列=日期。
自绘 canvas：三区底色、两条区域分隔竖线、今日金色边框、泳道角色标签。

列模型：
  · 国内/国外为逐日真实日期列
  · 海运段压缩为不超过 SEA_MAX_COLS 个虚拟列（过长时），
    表头以日期刻度显示「开始 次日 … 今日 … 结束」，今日落在海运段时高亮定位
  · 节点按 plan_start~plan_end 闭区间着色（海运节点铺满压缩段）
"""

from datetime import date, timedelta

from PySide6.QtWidgets import QWidget, QScrollArea, QSizePolicy
from PySide6.QtCore import Qt, QRectF, QSize
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont, QFontMetrics

from services.clock import get_today
from services.node_status import compute_node_status
from ui.theme import (
    AREA_COLORS, AREA_TEXT, AREA_FG, NODE_STATUS_COLORS, get_role_color,
    GRAY_SOFT, HAIRLINE, TEXT_SECONDARY, TEXT_PRIMARY, TEXT_TERTIARY
)

NAME_W = 170
COL_WIDTH = 45
ROW_HEIGHT = 45
HEADER_H = 24
BANNER_H = 32
SEA_MAX_COLS = 5          # 海运压缩后最多占用的列数
GOLD = "#E8A50C"          # 今日标注 · 金色
GOLD_BG = "#FFF4D6"       # 今日表头 · 浅金底
DIVIDER = "#AEB5C3"       # 三条区域之间的分隔竖线

AREA_ORDER = ("DOME", "SEA", "OVERSEA")


def _parse(s):
    if isinstance(s, date):
        return s
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))


class _GanttCanvas(QWidget):
    def __init__(self, nodes, today, export_port, parent=None):
        super().__init__(parent)
        self._nodes = nodes
        self._today = today
        self._export_port = export_port
        self._col_dates = []        # 每列对应日期（海运压缩列为 None）
        self._col_area = []         # 每列所属区域
        self._areas = {}            # area -> (start_idx, end_idx)
        self._today_idx = None      # 今日所在的全局列索引
        self._sea_labels = []       # 海运压缩列的表头天数刻度
        self._sea_ranges = []       # 海运压缩列的时间跨距 (delta_start, delta_end)
        self._sea_compressed = False
        self._port = None
        self._w = NAME_W
        self._h = 0
        self._banner_h = 0
        self._compute()
        self.setMinimumSize(self._w, self._h)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    # ── 列模型构建 ──
    def _compute(self):
        nodes = self._nodes
        if not nodes:
            return

        by_area = {a: [n for n in nodes if n["area"] == a] for a in AREA_ORDER}
        dome_nodes = by_area["DOME"]
        sea_nodes = by_area["SEA"]
        over_nodes = by_area["OVERSEA"]

        dome_start = min(_parse(n["plan_start"]) for n in dome_nodes) if dome_nodes else None
        dome_end = max(_parse(n["plan_end"]) for n in dome_nodes) if dome_nodes else None
        over_start = min(_parse(n["plan_start"]) for n in over_nodes) if over_nodes else None
        over_end = max(_parse(n["plan_end"]) for n in over_nodes) if over_nodes else None

        sea_start = sea_end = None
        e2s_day = 0     # 海运段总天数（含首尾的跨距天数，用于刻度/压缩）
        if sea_nodes:
            sea_start = min(_parse(n["plan_start"]) for n in sea_nodes)
            sea_end = max(_parse(n["plan_end"]) for n in sea_nodes)
            e2s_day = (sea_end - sea_start).days

        # 保存海运段起止日期，供压缩刻度显示日期用
        self._sea_start = sea_start
        self._sea_end = sea_end

        # 若海运>上限则压缩，否则海运逐日显示真实日期
        self._sea_compressed = e2s_day > SEA_MAX_COLS
        sea_steps = SEA_MAX_COLS if self._sea_compressed else (e2s_day + 1)

        col_dates = []
        col_area = []
        sea_labels = []
        sea_ranges = []

        # 国内段
        if dome_start is not None:
            d = dome_start
            while d <= dome_end:
                col_dates.append(d)
                col_area.append("DOME")
                d += timedelta(days=1)

        # 海运段
        if sea_nodes:
            if self._sea_compressed:
                # 均分为 sea_steps 个虚拟列，各自覆盖一段跨距
                for s in range(sea_steps):
                    s0 = s * e2s_day // sea_steps          # 该列覆盖的跨距起点
                    s1 = (s + 1) * e2s_day // sea_steps    # 跨距终点（不含）
                    col_dates.append(None)
                    col_area.append("SEA")
                    sea_ranges.append((s0, s1))
                self._sea_ranges = sea_ranges
            else:
                d = sea_start
                while d <= sea_end:
                    col_dates.append(d)
                    col_area.append("SEA")
                    d += timedelta(days=1)
                self._sea_ranges = []

        # 国外段
        if over_start is not None:
            d = over_start
            while d <= over_end:
                col_dates.append(d)
                col_area.append("OVERSEA")
                d += timedelta(days=1)

        self._col_dates = col_dates
        self._col_area = col_area

        # 每区域列范围
        for a in AREA_ORDER:
            idxs = [i for i, ar in enumerate(col_area) if ar == a]
            if idxs:
                self._areas[a] = (idxs[0], idxs[-1])

        # 今日所落列（真实日期列 or 海运压缩列）
        self._today_idx = None
        if self._today in col_dates:
            self._today_idx = col_dates.index(self._today)
        elif self._sea_compressed and sea_start is not None and sea_start <= self._today <= sea_end:
            delta = (self._today - sea_start).days
            sea_base = self._areas["SEA"][0]
            for i, (s0, s1) in enumerate(sea_ranges):
                if s0 <= delta < s1 or (s1 == e2s_day and s0 <= delta <= s1):
                    self._today_idx = sea_base + i
                    break

        # 海运压缩列表头刻度：开始日期、次日 … 今日 … 结束日期
        if self._sea_compressed:
            self._build_sea_labels()

        from config import get_port
        self._port = get_port(self._export_port)

        self._banner_h = BANNER_H if self._port else 0
        self._w = NAME_W + len(col_dates) * COL_WIDTH
        self._h = self._banner_h + HEADER_H * 2 + len(nodes) * ROW_HEIGHT

    def _build_sea_labels(self):
        """海运压缩列的表头日期刻度：首列开始日期、次列次日、今日列显示当日日期、末列结束日期、其余省略号"""
        sea_base = self._areas["SEA"][0]
        n = len(self._sea_ranges)
        sea_start = self._sea_start
        sea_end = self._sea_end
        labels = [""] * n
        today_rel = None
        if self._today_idx is not None and self._col_area[self._today_idx] == "SEA":
            today_rel = self._today_idx - sea_base

        def _fmt(d):
            return f"{d.month}/{d.day}"

        for i in range(n):
            if today_rel is not None and i == today_rel:
                label = _fmt(self._today)
            elif i == 0:
                label = _fmt(sea_start) if sea_start else ""
            elif i == n - 1:
                label = _fmt(sea_end) if sea_end else ""
            elif i == 1:
                label = _fmt(sea_start + timedelta(days=1)) if sea_start else ""
            else:
                label = "…"
            labels[i] = label
        self._sea_labels = labels

    def sizeHint(self):
        return QSize(self._w, self._h)

    def _region_top(self):
        return self._banner_h

    def _header_top(self):
        return self._banner_h + HEADER_H

    def _node_top(self):
        return self._banner_h + HEADER_H * 2

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor("#FFFFFF"))

        if not self._nodes:
            return

        self._draw_banner(p)
        self._draw_region(p)
        self._draw_header(p)
        self._draw_rows(p)
        self._draw_dividers(p)

    def _draw_banner(self, p):
        if not self._port:
            return
        p.fillRect(QRectF(0, 0, self._w, BANNER_H), QColor("#EAF3FF"))
        text = self._port.get("banner") or self._port.get("name") or ""
        f = QFont("Microsoft YaHei UI", 8)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#0A6BD6"))
        fm = QFontMetrics(f)
        text = f"港航方案 · {text}"
        text = fm.elidedText(text, Qt.ElideRight, self._w - 24)
        p.setClipRect(QRectF(0, 0, self._w, BANNER_H))
        p.drawText(QRectF(12, 0, self._w - 24, BANNER_H), Qt.AlignVCenter | Qt.AlignLeft, text)
        p.setClipping(False)

    def _draw_region(self, p):
        top = self._region_top()
        p.fillRect(QRectF(0, top, NAME_W, HEADER_H), QColor(GRAY_SOFT))
        p.setPen(QColor(TEXT_SECONDARY))
        f = QFont("Microsoft YaHei UI", 8)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(0, top, NAME_W, HEADER_H), Qt.AlignCenter, "节点 / 角色")

        for area in AREA_ORDER:
            if area not in self._areas:
                continue
            s, e = self._areas[area]
            x = NAME_W + s * COL_WIDTH
            w = (e - s + 1) * COL_WIDTH
            p.fillRect(QRectF(x, top, w, HEADER_H), QColor(AREA_COLORS[area]))
            f = QFont("Microsoft YaHei UI", 8)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor(AREA_FG[area]))
            p.drawText(QRectF(x, top, w, HEADER_H), Qt.AlignCenter, AREA_TEXT[area])

    def _draw_header(self, p):
        top = self._header_top()
        p.fillRect(QRectF(0, top, NAME_W, HEADER_H), QColor(GRAY_SOFT))

        for i, (d, area) in enumerate(zip(self._col_dates, self._col_area)):
            x = NAME_W + i * COL_WIDTH
            is_today = i == self._today_idx
            is_sea_virtual = area == "SEA" and self._sea_compressed

            if is_today:
                bg = GOLD_BG
            elif is_sea_virtual:
                bg = AREA_COLORS["SEA"]
            else:
                is_weekend = d.weekday() >= 5
                bg = "#FAFAFC" if is_weekend else "#FFFFFF"
            p.fillRect(QRectF(x, top, COL_WIDTH, HEADER_H), QColor(bg))

            f = QFont("Microsoft YaHei UI", 8)
            if is_today:
                f.setBold(True)
            p.setFont(f)
            p.setPen(QColor("#C07500") if is_today else TEXT_SECONDARY)

            if is_sea_virtual:
                rel = i - self._areas["SEA"][0]
                text = self._sea_labels[rel] if 0 <= rel < len(self._sea_labels) else ""
                if text:
                    p.drawText(QRectF(x, top, COL_WIDTH, HEADER_H), Qt.AlignCenter, text)
            else:
                p.drawText(QRectF(x, top, COL_WIDTH, HEADER_H), Qt.AlignCenter,
                           f"{d.month}/{d.day}")

        p.setPen(QColor(HAIRLINE))
        p.drawLine(0, top + HEADER_H - 1, self._w, top + HEADER_H - 1)

    def _draw_rows(self, p):
        nodes = self._nodes
        node_top = self._node_top()

        sea_start = min(_parse(n["plan_start"]) for n in nodes if n["area"] == "SEA") if self._sea_compressed else None

        for row, n in enumerate(nodes):
            y = node_top + row * ROW_HEIGHT
            n_area = n["area"]
            n_start = _parse(n["plan_start"])
            n_end = _parse(n["plan_end"])
            st = compute_node_status(n, self._today)

            # 名称列
            p.fillRect(QRectF(0, y, NAME_W, ROW_HEIGHT), QColor("#FFFFFF"))
            role_color = get_role_color(n["role_label"])
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(QColor(role_color)))
            p.drawRoundedRect(QRectF(8, y + (ROW_HEIGHT - 9) / 2, 9, 9), 3, 3)

            f = QFont("Microsoft YaHei UI", 8)
            p.setFont(f)
            p.setPen(QColor(TEXT_PRIMARY))
            fm = QFontMetrics(f)
            name = f"{n['node_id']}.{n['node_name']}"
            name = fm.elidedText(name, Qt.ElideRight, NAME_W - 30)
            p.drawText(QRectF(23, y, NAME_W - 28, ROW_HEIGHT), Qt.AlignVCenter | Qt.AlignLeft, name)

            # 节点在压缩海运段内的跨距区间（delta）
            if n_area == "SEA" and sea_start is not None:
                nd0 = (n_start - sea_start).days
                nd1 = (n_end - sea_start).days
            else:
                nd0 = nd1 = None

            # 日期格子
            for i, d in enumerate(self._col_dates):
                if d is None:
                    # 海运压缩列：判断节点跨距与该列区间是否相交
                    rel = i - self._areas["SEA"][0]
                    if n_area == "SEA" and 0 <= rel < len(self._sea_ranges):
                        s0, s1 = self._sea_ranges[rel]
                        covered = nd0 < s1 and nd1 >= s0
                    else:
                        covered = False
                else:
                    covered = n_start <= d <= n_end

                x = NAME_W + i * COL_WIDTH
                if covered:
                    bg = NODE_STATUS_COLORS.get(st, "#C7C7CC")
                    if st == "Pending":
                        bg = AREA_COLORS.get(n_area, "#F2F2F7")
                    p.fillRect(QRectF(x, y, COL_WIDTH, ROW_HEIGHT), QColor(bg))

                if i == self._today_idx:
                    p.setPen(QPen(QColor(GOLD), 2))
                    if covered:
                        p.setBrush(Qt.NoBrush)
                        p.drawRect(QRectF(x + 1, y + 1, COL_WIDTH - 2, ROW_HEIGHT - 2))
                    p.setPen(Qt.NoPen)

            p.setPen(QColor(HAIRLINE))
            p.drawLine(0, y + ROW_HEIGHT - 1, self._w, y + ROW_HEIGHT - 1)

    def _draw_dividers(self, p):
        present = [a for a in AREA_ORDER if a in self._areas]
        top = self._region_top()
        bottom = self._node_top() + len(self._nodes) * ROW_HEIGHT

        p.setPen(QPen(QColor(DIVIDER), 2))
        for i in range(len(present) - 1):
            nxt = present[i + 1]
            s = self._areas[nxt][0]
            x = NAME_W + s * COL_WIDTH
            p.drawLine(x, top, x, bottom)


class GanttGrid(QScrollArea):
    """甘特网格主控件"""

    def __init__(self, nodes, today=None, readonly=False, export_port=None, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._readonly = readonly
        self._canvas = _GanttCanvas(nodes, today or get_today(), export_port)
        self.setWidget(self._canvas)

    def auto_height(self):
        """返回画布完整高度（含横幅、表头与全部节点行），供外部按需设置显示高度"""
        return self._canvas._h