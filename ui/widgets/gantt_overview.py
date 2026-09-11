"""
全批次总览画布 —— 真实日历轴（日/周两档缩放），**行 = 批次**，只表达"时间轴 + 提交状态"。

与单批次画布（`gantt_grid.py`，三区相对轴 + 海运压缩 + 可操作）的分工：
  · 本画布是**只读总览**：一行一个批次，横轴落到真实日历，便于横向比较各批次的排期；
  · 操作（勾单证 / 推迟提前 / 撤销）一律在单批次视图里做；
  · 因此这里不做勾选、不做位移按钮，点击某一行 = 跳到该批次的单批次视图。

行内画法：
  · 三段色块（境内 / 海运 / 境外，复用三区淡彩）= 该批次三段的真实日历跨度；
  · 左列 = 批次号 + 状态徽章 + 「票货 x/y · 项目级 a/b」+ 逾期标记；
  · 条首细色帽 = 单证提交状态（绿=全提交 / 橙=部分 / 灰=未提交）；
  · 红色虚线竖带 = 资源冲突窗口（跨行），涉及行淡红底 + 左列 ⚠。

画布本身"笨"：行数据由工作台算好（含状态统计、三段区间、冲突标记）后喂进来，便于单测。
"""

from datetime import date, timedelta

from PySide6.QtWidgets import QWidget, QScrollArea
from PySide6.QtCore import Qt, Signal, QRectF, QSize
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont, QFontMetrics

from services.clock import get_today
from ui.theme import (
    AREA_COLORS, AREA_TEXT, GRAY_SOFT, HAIRLINE, TEXT_SECONDARY, TEXT_PRIMARY,
    TEXT_TERTIARY, ACCENT, RED, GREEN, ORANGE
)

NAME_W = 300              # 左侧批次列宽（批次号 + 状态 + 提交进度两行）
COL_W_DAY = 45            # 日视图列宽
COL_W_WEEK = 64           # 周视图列宽
MONTH_H = 20              # 月份带高度
HEADER_H = 22             # 日期/周 表头高度
ROW_H = 56                # 行高（两行文字 + 三段色块）
BAR_H = 16                # 三段色块高度
GOLD = "#E8A50C"          # 今日线
CONFLICT_RGB = (255, 59, 48)
WEEKEND_BG = "#FAFAFC"

SEGMENTS = ("DOME", "SEA", "OVERSEA")     # 三段顺序（与单批次三区一致）
LEVEL_ALPHA = {"high": 26, "medium": 16, "low": 10}

KIND_CN = {
    "PORT_WINDOW": "港口窗口",
    "CUSTOMS_BROKER": "报关行",
    "VESSEL_VOYAGE": "船名航次",
    "FREE_TIME": "免堆/免箱",
    "DEST_STORAGE": "境外堆存",
}
STATUS_CN = {"draft": "草稿", "ready": "已就绪", "running": "进行中",
             "completed": "待确认完成", "closed": "已完成", "cancelled": "已取消"}


def _parse(s):
    if isinstance(s, date):
        return s
    if not s:
        return None
    try:
        y, m, d = str(s).split("-")
        return date(int(y), int(m), int(d))
    except Exception:
        return None


def doc_state_color(req_total, req_submitted):
    """提交状态色：全提交=绿 / 部分=橙 / 未提交=灰。"""
    if not req_total:
        return TEXT_TERTIARY
    if req_submitted >= req_total:
        return GREEN
    return ORANGE if req_submitted > 0 else "#C7C7CC"


def doc_state_text(req_total, req_submitted):
    """提交状态文案（用户口径：全提交了还是未提交）。"""
    if not req_total:
        return "无必填单证"
    if req_submitted >= req_total:
        return f"✓ 全提交 {req_submitted}/{req_total}"
    if req_submitted == 0:
        return f"未提交 0/{req_total}"
    return f"部分提交 {req_submitted}/{req_total}"


class _OverviewCanvas(QWidget):
    """全批次总览自绘画布（行=批次）。数据由工作台注入，更新走 update_data。"""

    def __init__(self, rows=None, today=None, zoom="week", conflicts=None, parent=None):
        super().__init__(parent)
        self._today = today or get_today()
        self._zoom = zoom if zoom in ("day", "week") else "week"
        self._rows = []
        self._conflicts = list(conflicts or [])
        self._axis_start = None
        self._axis_cols = []      # [(start, end, label, is_weekend)]
        self._today_col = None
        self._col_w = COL_W_WEEK
        self._hover_row = None
        self._hover_cb = None
        self._activate_cb = None
        self._w = NAME_W
        self._h = 0
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.update_data(rows, today, conflicts)

    # ── 数据 ──

    def update_data(self, rows, today=None, conflicts=None):
        """重算行/轴并就地更新尺寸（不替换控件自身，避免销毁信号发射者）。"""
        if today is not None:
            self._today = today
        if conflicts is not None:
            self._conflicts = list(conflicts)
        self._rows = list(rows or [])

        starts, ends = [], [self._today]
        for r in self._rows:
            for _area, (s, e) in (r.get("spans") or {}).items():
                if s:
                    starts.append(s)
                if e:
                    ends.append(e)
        if starts:
            s, e = min(starts), max(ends)
            if self._zoom == "week":
                s -= timedelta(days=s.weekday())
                e += timedelta(days=6 - e.weekday())
            self._axis_start = s
            self._axis_cols = self._build_cols(s, e)
        else:
            self._axis_start = None
            self._axis_cols = []
        self._col_w = COL_W_WEEK if self._zoom == "week" else COL_W_DAY
        self._today_col = self._col_of(self._today)

        self._w = NAME_W + max(1, len(self._axis_cols)) * self._col_w
        self._h = MONTH_H + HEADER_H + max(1, len(self._rows)) * ROW_H
        self.setMinimumSize(self._w, self._h)
        self.resize(self._w, self._h)
        self.update()

    def _build_cols(self, span_s, span_e):
        cols = []
        if self._zoom == "week":
            d = span_s
            while d <= span_e:
                end = d + timedelta(days=6)
                cols.append((d, end, f"{d.month}/{d.day}", False))
                d = end + timedelta(days=1)
        else:
            d = span_s
            while d <= span_e:
                cols.append((d, d, f"{d.month}/{d.day}", d.weekday() >= 5))
                d += timedelta(days=1)
        return cols

    def _col_of(self, day):
        if self._axis_start is None or day is None:
            return None
        delta = (day - self._axis_start).days
        if delta < 0:
            return None
        idx = delta // 7 if self._zoom == "week" else delta
        return idx if 0 <= idx < len(self._axis_cols) else None

    def _seg_rect(self, row_index, s, e):
        """三段中某一段的矩形；越界裁剪到轴范围，返回 None 表示不可见。"""
        c0, c1 = self._col_of(s), self._col_of(e)
        if c0 is None and c1 is None:
            return None
        c0 = 0 if c0 is None else c0
        c1 = len(self._axis_cols) - 1 if c1 is None else c1
        if c1 < c0:
            return None
        x = NAME_W + c0 * self._col_w
        w = (c1 - c0 + 1) * self._col_w
        y = MONTH_H + HEADER_H + row_index * ROW_H + (ROW_H - BAR_H) / 2
        return QRectF(x, y, w, BAR_H)

    def auto_height(self):
        return self._h

    def canvas_size(self):
        return QSize(self._w, self._h)

    def row_at(self, y):
        top = MONTH_H + HEADER_H
        if y < top:
            return None
        idx = int((y - top) // ROW_H)
        return idx if 0 <= idx < len(self._rows) else None

    def conflict_batch_ids(self):
        out = set()
        for c in self._conflicts:
            out.update(c.get("batches") or [])
        return out

    def row_payload(self, idx):
        if idx is None or not (0 <= idx < len(self._rows)):
            return None
        r = self._rows[idx]
        spans = {}
        for area, (s, e) in (r.get("spans") or {}).items():
            if s and e:
                spans[area] = f"{s.isoformat()}~{e.isoformat()}"
        return {
            "batch_id": r.get("batch_id"), "batch_no": r.get("batch_no"),
            "docs": r.get("docs") or {}, "proj": r.get("proj") or {},
            "overdue": r.get("overdue") or 0, "spans": spans,
        }

    # ── 交互 ──

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        if event.buttons() == Qt.NoButton:
            idx = self.row_at(event.position().y())
            if idx != self._hover_row:
                self._hover_row = idx
                self.update()
                if self._hover_cb:
                    self._hover_cb(self.row_payload(idx))

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if event.button() != Qt.LeftButton:
            return
        idx = self.row_at(event.position().y())
        if idx is not None and self._activate_cb:
            self._activate_cb(self.row_payload(idx))

    def leaveEvent(self, event):
        super().leaveEvent(event)
        if self._hover_row is not None:
            self._hover_row = None
            self.update()
        if self._hover_cb:
            self._hover_cb(None)

    # ── 绘制 ──

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor("#FFFFFF"))
        if not self._rows or not self._axis_cols:
            self._draw_placeholder(p)
            return
        self._draw_axis(p)
        self._draw_conflict_bands(p)
        for i in range(len(self._rows)):
            self._draw_row(p, i)
        self._draw_today(p)
        self._draw_hover(p)
        self._draw_divider(p)

    def _draw_placeholder(self, p):
        p.fillRect(QRectF(0, 0, NAME_W, MONTH_H + HEADER_H), QColor(GRAY_SOFT))
        f = QFont("Microsoft YaHei UI", 8)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(TEXT_SECONDARY))
        p.drawText(QRectF(0, 0, NAME_W, MONTH_H + HEADER_H), Qt.AlignCenter, "批次")
        f2 = QFont("Microsoft YaHei UI", 10)
        p.setFont(f2)
        p.setPen(QColor(TEXT_TERTIARY))
        p.drawText(QRectF(NAME_W, 0, max(200, self.width() - NAME_W), max(60, self.height())),
                   Qt.AlignCenter,
                   "当前项目没有启用中的批次" if not self._rows else "所选批次还没有计划日期")

    def _draw_axis(self, p):
        p.fillRect(QRectF(0, 0, self._w, MONTH_H), QColor(GRAY_SOFT))
        f = QFont("Microsoft YaHei UI", 8)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(TEXT_SECONDARY))
        p.drawText(QRectF(0, 0, NAME_W, MONTH_H), Qt.AlignCenter, "批次 / 提交状态")

        band_start, band_key, band_w = None, None, 0

        def flush():
            if band_key is None:
                return
            x = NAME_W + band_start * self._col_w
            w = band_w * self._col_w
            if w >= 46:
                p.setPen(QColor(TEXT_SECONDARY))
                p.drawText(QRectF(x, 0, w, MONTH_H), Qt.AlignCenter, band_key)

        for i, (s, _e, _lb, _wk) in enumerate(self._axis_cols):
            key = f"{s.year}年{s.month}月"
            if key != band_key:
                flush()
                band_start, band_key, band_w = i, key, 1
            else:
                band_w += 1
        flush()

        top = MONTH_H
        p.fillRect(QRectF(0, top, self._w, HEADER_H), QColor("#FFFFFF"))
        p.setFont(QFont("Microsoft YaHei UI", 8))
        for i, (s, e, label, is_weekend) in enumerate(self._axis_cols):
            x = NAME_W + i * self._col_w
            in_today = s <= self._today <= e
            if in_today:
                p.fillRect(QRectF(x, top, self._col_w, HEADER_H), QColor("#FFF4D6"))
            elif is_weekend:
                p.fillRect(QRectF(x, top, self._col_w, HEADER_H), QColor(WEEKEND_BG))
            p.setPen(QColor("#C07500") if in_today else TEXT_SECONDARY)
            p.drawText(QRectF(x, top, self._col_w, HEADER_H), Qt.AlignCenter, label)
        p.setPen(QColor(HAIRLINE))
        p.drawLine(0, top + HEADER_H - 1, self._w, top + HEADER_H - 1)

    def _draw_row(self, p, i):
        r = self._rows[i]
        y = MONTH_H + HEADER_H + i * ROW_H
        conflicted = r.get("batch_id") in self.conflict_batch_ids()

        if conflicted:
            p.fillRect(QRectF(NAME_W, y, self._w - NAME_W, ROW_H),
                       QColor(*CONFLICT_RGB, 10))
        # 周末底
        if self._zoom == "day":
            for j, (_s, _e, _lb, wk) in enumerate(self._axis_cols):
                if wk:
                    p.fillRect(QRectF(NAME_W + j * self._col_w, y, self._col_w, ROW_H),
                               QColor(WEEKEND_BG))
        p.setPen(QColor(HAIRLINE))
        p.drawLine(0, y + ROW_H - 1, self._w, y + ROW_H - 1)

        # 三段色块（境内 / 海运 / 境外）
        for area in SEGMENTS:
            s, e = (r.get("spans") or {}).get(area, (None, None))
            if not s or not e:
                continue
            rect = self._seg_rect(i, s, e)
            if rect is None:
                continue
            p.setPen(QPen(QColor(AREA_COLORS[area]), 1))
            p.setBrush(QBrush(QColor(AREA_COLORS[area])))
            p.drawRoundedRect(rect, 5, 5)

        # 条首状态帽（提交状态：绿 / 橙 / 灰）
        first = next(((r.get("spans") or {}).get(a) for a in SEGMENTS
                      if (r.get("spans") or {}).get(a, (None, None))[0]), None)
        docs = r.get("docs") or {}
        if first:
            rect = self._seg_rect(i, first[0], first[1])
            if rect is not None:
                cap = QColor(doc_state_color(docs.get("req_total", 0),
                                            docs.get("req_submitted", 0)))
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(cap))
                p.drawRoundedRect(QRectF(rect.left() - 5, rect.top() - 1,
                                         5, rect.height() + 2), 2, 2)

        self._draw_row_text(p, r, y, conflicted)

    def _draw_row_text(self, p, r, y, conflicted):
        # 批次号
        f = QFont("Microsoft YaHei UI", 11)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(TEXT_PRIMARY))
        fm = QFontMetrics(f)
        name = fm.elidedText(r.get("batch_no") or "", Qt.ElideRight, NAME_W - 120)
        p.drawText(QRectF(14, y + 6, NAME_W - 120, 20), Qt.AlignVCenter | Qt.AlignLeft, name)

        # 状态徽章
        st_color = {"running": ACCENT, "ready": GREEN}.get(r.get("status"), TEXT_SECONDARY)
        p.setFont(QFont("Microsoft YaHei UI", 9))
        p.setPen(QColor(st_color))
        p.drawText(QRectF(NAME_W - 104, y + 6, 92, 20), Qt.AlignVCenter | Qt.AlignRight,
                   STATUS_CN.get(r.get("status"), r.get("status") or ""))

        # 第二行：提交状态 + 逾期 + 资源冲突
        docs = r.get("docs") or {}
        proj = r.get("proj") or {}
        parts = [doc_state_text(docs.get("req_total", 0), docs.get("req_submitted", 0))]
        if proj.get("total"):
            parts.append(f"项目级 {proj.get('submitted', 0)}/{proj.get('total')}")
        if r.get("overdue"):
            parts.append(f"逾期 {r['overdue']}")
        if conflicted:
            parts.append("⚠ 资源冲突")
        text = " · ".join(parts)
        f2 = QFont("Microsoft YaHei UI", 8)
        p.setFont(f2)
        tone = doc_state_color(docs.get("req_total", 0), docs.get("req_submitted", 0))
        if conflicted or r.get("overdue"):
            tone = RED
        p.setPen(QColor(tone))
        p.drawText(QRectF(14, y + 28, NAME_W - 28, 18), Qt.AlignVCenter | Qt.AlignLeft,
                   QFontMetrics(f2).elidedText(text, Qt.ElideRight, NAME_W - 28))

    def _draw_conflict_bands(self, p):
        if not self._conflicts:
            return
        top = MONTH_H
        f = QFont("Microsoft YaHei UI", 7)
        f.setBold(True)
        for c in self._conflicts:
            w0, w1 = (c.get("window") or (None, None))
            s, e = _parse(w0), _parse(w1)
            if not s or not e:
                continue
            c0, c1 = self._col_of(s), self._col_of(e)
            if c0 is None and c1 is None:
                continue
            c0 = 0 if c0 is None else c0
            c1 = len(self._axis_cols) - 1 if c1 is None else c1
            if c1 < c0:
                continue
            x0 = NAME_W + c0 * self._col_w
            w = (c1 - c0 + 1) * self._col_w
            p.fillRect(QRectF(x0, top, w, self._h - top),
                       QColor(*CONFLICT_RGB, LEVEL_ALPHA.get(c.get("level"), 12)))
            p.setPen(QPen(QColor(*CONFLICT_RGB), 1, Qt.DashLine))
            p.drawLine(int(x0), top, int(x0), self._h)
            p.drawLine(int(x0 + w), top, int(x0 + w), self._h)
            p.setPen(Qt.NoPen)
            p.setFont(f)
            p.setPen(QColor(RED))
            tag = f"⚠{KIND_CN.get(c.get('kind'), '冲突')}"
            fm = QFontMetrics(f)
            if w >= fm.horizontalAdvance(tag) + 4:
                p.drawText(QRectF(x0 + 2, MONTH_H, w - 4, HEADER_H), Qt.AlignCenter, tag)

    def _draw_today(self, p):
        idx = self._today_col
        if idx is None:
            return
        x = NAME_W + idx * self._col_w + self._col_w // 2
        p.setPen(QPen(QColor(GOLD), 2))
        p.drawLine(int(x), MONTH_H, int(x), self._h)
        p.setPen(Qt.NoPen)

    def _draw_hover(self, p):
        if self._hover_row is None:
            return
        y = MONTH_H + HEADER_H + self._hover_row * ROW_H
        p.fillRect(QRectF(NAME_W, y, self._w - NAME_W, ROW_H), QColor(ACCENT).lighter(190))
        p.setPen(QPen(QColor(ACCENT), 2))
        p.drawLine(NAME_W, int(y), self._w, int(y))
        p.drawLine(NAME_W, int(y + ROW_H - 1), self._w, int(y + ROW_H - 1))
        p.setPen(Qt.NoPen)

    def _draw_divider(self, p):
        p.setPen(QPen(QColor("#AEB5C3"), 2))
        p.drawLine(NAME_W, 0, NAME_W, self._h)


class BatchOverviewGantt(QScrollArea):
    """全批次总览主控件：双向滚动；行悬停 / 行点击向外发信号。"""

    rowHovered = Signal(object)     # 行明细；移出为 None
    rowActivated = Signal(object)   # 行明细（点击 → 工作台切到该批次的单批次视图）

    def __init__(self, rows=None, today=None, zoom="week", conflicts=None, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._last_rows = rows or []
        self._last_conflicts = conflicts
        self._canvas = _OverviewCanvas(rows or [], today, zoom, conflicts)
        self._canvas._hover_cb = lambda payload: self.rowHovered.emit(payload)
        self._canvas._activate_cb = lambda payload: self.rowActivated.emit(payload)
        self.setWidget(self._canvas)

    def update_data(self, rows, today=None, conflicts=None):
        """就地刷新（不替换 canvas 控件，避免销毁信号发射者）。"""
        self._last_rows = rows or []
        if conflicts is not None:
            self._last_conflicts = conflicts
        self._canvas.update_data(self._last_rows, today, self._last_conflicts)

    def set_zoom(self, zoom):
        self._canvas._zoom = zoom if zoom in ("day", "week") else "week"
        self._canvas.update_data(self._last_rows, None, self._last_conflicts)

    @property
    def zoom(self):
        return self._canvas._zoom

    def auto_height(self):
        return self._canvas.auto_height()

    def canvas(self):
        return self._canvas
