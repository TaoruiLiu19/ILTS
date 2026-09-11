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
from PySide6.QtCore import Qt, Signal, QRectF, QSize, QEvent
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont, QFontMetrics

from ui.widgets.scoped_scroll import ScopedScrollArea
from services.clock import get_today
from services.node_status import compute_node_status
from services.node_template import LASHING, BUFFER_HINT
from ui.theme import (
    AREA_COLORS, AREA_TEXT, AREA_FG, NODE_STATUS_COLORS, get_role_color,
    GRAY_SOFT, HAIRLINE, TEXT_SECONDARY, TEXT_PRIMARY, TEXT_TERTIARY,
    ACCENT, ORANGE, RED, GREEN, YELLOW, WAITING_YELLOW
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
BOTTLENECK_RATIO = 0.5    # 瓶颈判定：节点时长占全流程 ≥ 50%

AREA_ORDER = ("DOME", "SEA", "OVERSEA")


def _parse(s):
    if isinstance(s, date):
        return s
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))


class _GanttCanvas(QWidget):
    def __init__(self, nodes, today, export_port, over_count=0, buffer_days=4, parent=None):
        super().__init__(parent)
        self._nodes = nodes
        self._today = today
        self._export_port = export_port
        self._over_count = over_count or 0
        self._buffer_days = buffer_days or 4
        self._row_tags = {}         # node_id -> [(text, color), ...]
        self._dep_waiting = {}      # node_key -> 上游文案（§10.5 依赖标黄）
        self._bottleneck_id = None
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
        # 甘特联动：悬停/点击（由 GanttGrid 注入回调）
        self._hover_node_id = None
        self._hover_cb = None       # func(node_dict | None)
        self._activate_cb = None    # func(node_dict)
        self._compute()
        self.setMinimumSize(self._w, self._h)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)

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

        self._compute_row_tags()

    def _compute_row_tags(self):
        """瓶颈 / 风险 / 吊装预警 高亮标签（优化方案 §3.4、§2.1）"""
        tags = {}
        nodes = self._nodes
        if not nodes:
            self._row_tags = tags
            return

        # ── 瓶颈：单节点时长占全流程比例 > 阈值 → 标「最长段」 ──
        starts = [_parse(n["plan_start"]) for n in nodes]
        ends = [_parse(n["plan_end"]) for n in nodes]
        total_span = (max(ends) - min(starts)).days
        longest = None
        for n in nodes:
            span = (_parse(n["plan_end"]) - _parse(n["plan_start"])).days
            if span > 0 and (longest is None or span > longest[1]):
                longest = (n, span)
        if (longest and total_span > 0
                and longest[1] / total_span >= BOTTLENECK_RATIO):
            self._bottleneck_id = longest[0]["node_id"]
            tags.setdefault(longest[0]["node_id"], []).append(("最长段", ACCENT))

        for n in nodes:
            nid = n["node_id"]
            st = compute_node_status(n, self._today)
            n_tags = tags.get(nid, [])

            # ── 吊装预警：货物台账存在超限项 → LASHING（装箱/加固）标红 ──
            if n.get("node_key") == LASHING and self._over_count > 0:
                n_tags.append(("吊装预警", RED))

            # ── §10.5 依赖提醒：上游单证未完成 → 该节点标黄「待上游」 ──
            waiting = (self._dep_waiting or {}).get(n.get("node_key"))
            if waiting:
                n_tags.append((f"待上游：{waiting}", WAITING_YELLOW))

            # ── 风险：逾期 / 缓冲消耗 ──
            if st == "Overdue":
                plan_end = _parse(n["plan_end"])
                late = (self._today - plan_end).days
                if n.get("node_key") in BUFFER_HINT:
                    used = min(late, self._buffer_days)
                    n_tags.append((f"已耗缓冲{used}天", ORANGE))
                n_tags.append((f"逾期{late}天", RED))

            if n_tags:
                tags[nid] = n_tags
        self._row_tags = tags

    def set_dependency_waiting(self, mapping):
        """§10.5 依赖提醒：{node_key: '《MBL 主提单》'} → 对应节点标黄「待上游」。

        数据来自 services.doc_dependency，由看板在加载批次时注入。
        """
        self._dep_waiting = dict(mapping or {})
        self._compute_row_tags()
        self.update()

    def _build_sea_labels(self):
        """
        海运压缩列表头日期刻度 —— 全局单调、同一天只出现一次：
          · 首列若与前段（境内末列）同一天 → 该刻度让位给前段，不再重复；
          · 末列若与后段（境外首列）同一天 → 让位给境外段（10/26 只在境外出现一次）；
          · 「今日」所在列显示当日日期（与首/末同天则合并）；
          · 其余列显示 …；从左到右保证严格递增、不倒退、不重复。
        """
        sea_base = self._areas["SEA"][0]
        n = len(self._sea_ranges)
        labels = ["…"] * n
        if n == 0:
            self._sea_labels = labels
            return

        sea_start = self._sea_start
        sea_end = self._sea_end
        col_dates = self._col_dates

        # 左右相邻的真实日期列（用于跨段边界去重）
        prev_date = None
        if sea_base - 1 >= 0 and col_dates[sea_base - 1] is not None:
            prev_date = col_dates[sea_base - 1]
        next_date = None
        if sea_base + n < len(col_dates) and col_dates[sea_base + n] is not None:
            next_date = col_dates[sea_base + n]

        ticks = [None] * n          # date 对象
        if sea_start is not None and (prev_date is None or prev_date != sea_start):
            ticks[0] = sea_start
        if sea_end is not None and (next_date is None or next_date != sea_end):
            ticks[n - 1] = sea_end

        # 「今日」锚点（位于段内时）
        if (self._today_idx is not None
                and self._col_area[self._today_idx] == "SEA"):
            tc = self._today_idx - sea_base
            if 0 <= tc < n:
                today = self._today
                if ticks[tc] is None:
                    ticks[tc] = today
                elif ticks[tc] != today and today > ticks[tc]:
                    ticks[tc] = today      # 今日比端点更“新”，端点刻度让位

        # 从左到右仅保留严格递增的刻度（杜绝倒退/重复）
        last = None
        for i in range(n):
            d = ticks[i]
            if d is None:
                continue
            if last is not None and d <= last:
                ticks[i] = None
                continue
            last = d

        def _fmt(d):
            return f"{d.month}/{d.day}"

        for i in range(n):
            if ticks[i] is not None:
                labels[i] = _fmt(ticks[i])
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
        self._draw_hover(p)

    # ── 悬停联动 ──

    def _node_at(self, y):
        """由画布 y 坐标取行对应节点（表头/横幅区返回 None）"""
        if not self._nodes:
            return None
        node_top = self._node_top()
        if y < node_top:
            return None
        idx = int((y - node_top) // ROW_HEIGHT)
        if 0 <= idx < len(self._nodes):
            return self._nodes[idx]
        return None

    def _notify_hover(self, node):
        nid = node["node_id"] if node else None
        old = self._hover_node_id
        if nid != old:
            # 只重绘 移出旧行 + 移入新行 两个局部区域，避免悬停移动时整幅大画布重绘（性能）
            self._hover_node_id = nid
            for target in (old, nid):
                if target is None:
                    continue
                for row, n in enumerate(self._nodes):
                    if n["node_id"] == target:
                        y = self._node_top() + row * ROW_HEIGHT
                        self.update(0, y, self._w, ROW_HEIGHT)
                        break
            if self._hover_cb:
                self._hover_cb(node)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        if event.buttons() == Qt.NoButton:
            self._notify_hover(self._node_at(event.position().y()))

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if event.button() == Qt.LeftButton:
            node = self._node_at(event.position().y())
            if node:
                self._notify_hover(node)
                if self._activate_cb:
                    self._activate_cb(node)

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._notify_hover(None)

    def _draw_hover(self, p):
        """悬停行淡蓝遮罩 + 上下强调线（位于最上层）"""
        if not self._hover_node_id:
            return
        for row, n in enumerate(self._nodes):
            if n["node_id"] != self._hover_node_id:
                continue
            y = self._node_top() + row * ROW_HEIGHT
            fill = QColor(ACCENT)
            fill.setAlpha(16)
            p.fillRect(QRectF(0, y, self._w, ROW_HEIGHT), fill)
            p.setPen(QPen(QColor(ACCENT), 2))
            p.drawLine(0, y, self._w, y)
            p.drawLine(0, y + ROW_HEIGHT - 1, self._w, y + ROW_HEIGHT - 1)
            p.setPen(Qt.NoPen)
            return

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
            row_tags = self._row_tags.get(n["node_id"], [])
            if row_tags:
                # 左缘强调条：一眼标出瓶颈 / 风险 / 吊装预警行
                p.fillRect(QRectF(0, y, 3, ROW_HEIGHT), QColor(row_tags[0][1]))
            role_color = get_role_color(n["role_label"])
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(QColor(role_color)))
            p.drawRoundedRect(QRectF(10, y + (ROW_HEIGHT - 9) / 2, 9, 9), 3, 3)

            name = f"{n['node_id']}.{n['node_name']}"
            if row_tags:
                # 两行：上行节点名，下行风险/瓶颈标签
                f = QFont("Microsoft YaHei UI", 9)
                f.setBold(True)
                p.setFont(f)
                p.setPen(QColor(TEXT_PRIMARY))
                fm = QFontMetrics(f)
                name_el = fm.elidedText(name, Qt.ElideRight, NAME_W - 32)
                p.drawText(QRectF(25, y + 2, NAME_W - 30, 20),
                           Qt.AlignVCenter | Qt.AlignLeft, name_el)

                tf = QFont("Microsoft YaHei UI", 7)
                tf.setBold(True)
                p.setFont(tf)
                fm = QFontMetrics(tf)
                cx = 25.0
                for text, color in row_tags:
                    tw = fm.horizontalAdvance(text) + 6
                    if cx + tw > NAME_W - 4:
                        break
                    p.setPen(QColor(color))
                    p.drawText(QRectF(cx, y + 20, tw, ROW_HEIGHT - 22),
                               Qt.AlignVCenter | Qt.AlignLeft, text)
                    cx += tw
            else:
                f = QFont("Microsoft YaHei UI", 8)
                p.setFont(f)
                p.setPen(QColor(TEXT_PRIMARY))
                fm = QFontMetrics(f)
                name = f"{n['node_id']}.{n['node_name']}"
                name = fm.elidedText(name, Qt.ElideRight, NAME_W - 30)
                p.drawText(QRectF(23, y, NAME_W - 28, ROW_HEIGHT),
                           Qt.AlignVCenter | Qt.AlignLeft, name)

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


class GanttGrid(ScopedScrollArea):
    """甘特网格主控件；悬停/点击节点向外发信号（联动单证清单）"""

    nodeHovered = Signal(object)    # 悬停进入某行: node dict；移出(表头/离开): None
    nodeActivated = Signal(object)  # 左键单击某行: node dict

    def __init__(self, nodes, today=None, readonly=False, export_port=None,
                 over_count=0, buffer_days=4, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._readonly = readonly
        self._canvas = _GanttCanvas(nodes, today or get_today(), export_port,
                                    over_count=over_count, buffer_days=buffer_days)
        self._canvas._hover_cb = lambda n: self.nodeHovered.emit(n)
        if not readonly:
            self._canvas._activate_cb = lambda n: self.nodeActivated.emit(n)
        self.setWidget(self._canvas)

    def auto_height(self):
        """返回画布完整高度（含横幅、表头与全部节点行），供外部按需设置显示高度"""
        return self._canvas._h

    # 供联动方程序化高亮/清除（如反向联动）
    def set_hover_node(self, node):
        self._canvas._notify_hover(node)

    # §10.5 依赖提醒：{node_key: '《MBL 主提单》'} → 对应节点标黄「待上游」
    def set_dependency_waiting(self, mapping):
        self._canvas.set_dependency_waiting(mapping)