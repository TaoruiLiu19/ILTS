"""
全批次总览画布（C+D 里程碑双轴）—— 只读展示，方便看排期。

设计（对照《后续改进方向》P0-全批次甘特：
  行 = 批次；每行 = 弱色三段带 + 4 个里程碑菱形 + 左列批注；跨批共柜用细曲线串联。
  双轴（单画布 + 顶部开关）：
    · 绝对日历：真实日期轴（日/周缩放）+ 今天线 + 周末底色 + 资源冲突红带；
    · 相对里程碑：所有批次按锚点（装船/到港/交付）对齐到第 0 天，横向比节奏。
  只读交互：悬停菱形→气泡；单击菱形→内联详情；双击行→跳该批次单批次甘特。

画布本身"笨"：行数据（含 spans / milestones / 提交状态 / 共柜 links）由工作台算好注入。
"""
from datetime import date, timedelta

from PySide6.QtWidgets import QWidget, QScrollArea
from PySide6.QtCore import Qt, Signal, QRectF, QSize
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont, QFontMetrics, QPainterPath

from services.clock import get_today
from ui.theme import (
    AREA_COLORS, HAIRLINE, TEXT_SECONDARY, TEXT_PRIMARY,
    TEXT_TERTIARY, ACCENT, RED, GREEN, ORANGE, GRAY_SOFT
)

NAME_W = 292              # 左侧批次列宽
COL_W_DAY = 45            # 日视图列宽
COL_W_WEEK = 62           # 周视图列宽
MONTH_H = 20              # 月份带高度
HEADER_H = 22             # 表头高度
ROW_H = 84                # 行高（左列信息 + 弱色带 + 里程碑，留足呼吸）
BAND_H = 22               # 三段弱色带高度
DIAMOND_BIG = 10          # 关键里程碑（装船/交付）菱形半径
DIAMOND_SMALL = 8         # 次级里程碑（提箱/到港）
GOLD = "#E8A50C"          # 今日线
CONFLICT_RGB = (255, 59, 48)
WEEKEND_BG = "#FAFAFC"
REL_ANCHOR_BG = "#EEF3FF"  # 相对模式锚点列底色

SEGMENTS = ("DOME", "SEA", "OVERSEA")
LEVEL_ALPHA = {"high": 26, "medium": 16, "low": 10}

KIND_CN = {
    "PORT_WINDOW": "港口窗口", "CUSTOMS_BROKER": "报关行", "VESSEL_VOYAGE": "船名航次",
    "FREE_TIME": "免堆/免箱", "DEST_STORAGE": "境外堆存",
}
STATUS_CN = {"draft": "草稿", "ready": "已就绪", "running": "进行中",
             "completed": "待确认完成", "closed": "已完成", "cancelled": "已取消"}

# 里程碑：key · 文案 · 是否关键（大菱形）
MILESTONES = (
    ("pickup", "提空箱", False),
    ("sail", "装船(ETD)", True),
    ("arrive", "到港(ETA)", False),
    ("deliver", "交付", True),
)
ANCHOR_LABELS = {"sail": "装船", "arrive": "到港", "deliver": "交付"}


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
    if not req_total:
        return TEXT_TERTIARY
    if req_submitted >= req_total:
        return GREEN
    return ORANGE if req_submitted > 0 else "#C7C7CC"


def doc_state_text(req_total, req_submitted):
    if not req_total:
        return "无必填单证"
    if req_submitted >= req_total:
        return f"✓ 全提交 {req_submitted}/{req_total}"
    if req_submitted == 0:
        return f"未提交 0/{req_total}"
    return f"部分提交 {req_submitted}/{req_total}"


def _diamond_color(key, big):
    if key == "sail":
        return ACCENT
    if key == "deliver":
        return GREEN
    return "#B9BFCB"  # 提箱/到港用中性灰，克制不抢眼


def _diamond_r(key):
    return DIAMOND_BIG if key in ("sail", "deliver") else DIAMOND_SMALL


class _OverviewCanvas(QWidget):
    """全批次总览自绘画布：里程碑双轴（C+D），只读。数据由工作台注入。"""

    milestoneHovered = Signal(object)     # (row_idx, key, payload)
    milestoneActivated = Signal(object)
    rowActivated = Signal(object)

    def __init__(self, rows=None, today=None, zoom="week", conflicts=None,
                 links=None, axis_mode="abs", anchor="sail", interactive=True,
                 parent=None):
        super().__init__(parent)
        self._today = today or get_today()
        self._zoom = zoom if zoom in ("day", "week") else "week"
        self._rows = []
        self._conflicts = list(conflicts or [])
        self._links = list(links or [])
        self._axis_mode = axis_mode if axis_mode in ("abs", "rel") else "abs"
        self._anchor = anchor if anchor in ANCHOR_LABELS else "sail"
        self._interactive = bool(interactive)
        self._axis_cols = []          # abs: [(start,end,label,is_weekend)]; rel: 偏移周列
        self._axis_start = None
        self._off_start = 0           # 相对模式最左偏移（周对齐）
        self._today_col = None
        self._col_w = COL_W_WEEK
        self._hover_row = None
        self._hover_ms = None         # (row_key, milestone_key)
        self._selected = None         # dict(row, key, rect)
        self._hover_cb = None
        self._activate_cb = None
        self._w = NAME_W
        self._h = 0
        self.setMouseTracking(True)
        self.update_data(rows, today, conflicts, links)

    # ── 数据 ──

    def update_data(self, rows, today=None, conflicts=None, links=None):
        if today is not None:
            self._today = today
        if conflicts is not None:
            self._conflicts = list(conflicts)
        if links is not None:
            self._links = list(links)
        self._rows = list(rows or [])
        self._build_axis()
        self._w = NAME_W + max(1, len(self._axis_cols)) * self._col_w
        self._h = MONTH_H + HEADER_H + max(1, len(self._rows)) * ROW_H
        self.setMinimumSize(self._w, self._h)
        self.resize(self._w, self._h)
        self.update()

    def _build_axis(self):
        self._axis_cols = []
        self._off_start = 0
        self._today_col = None
        if not self._rows:
            return
        self._col_w = COL_W_WEEK if self._zoom == "week" else COL_W_DAY
        if self._axis_mode == "rel":
            offs = []
            for r in self._rows:
                anc = (r.get("milestones") or {}).get(self._anchor)
                if not anc:
                    continue
                for k in ("pickup", "sail", "arrive", "deliver"):
                    d = (r.get("milestones") or {}).get(k)
                    if d:
                        offs.append((d - anc).days)
            if not offs:
                return
            mn, mx = min(offs), max(offs)
            if self._zoom == "week":
                start = int(mn // 7) * 7
                self._off_start = start
                d0 = start
                while d0 <= mx:
                    self._axis_cols.append((d0, d0 + 6, f"{d0 // 7:+d}周", False))
                    d0 += 7
            else:
                self._off_start = mn
                for off in range(mn, mx + 1):
                    self._axis_cols.append((off, off, f"{off:+d}", False))
            return

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
            d = s
            while d <= e:
                if self._zoom == "week":
                    self._axis_cols.append((d, d + timedelta(days=6),
                                            f"{d.month}/{d.day}", False))
                    d += timedelta(days=7)
                else:
                    self._axis_cols.append((d, d, f"{d.month}/{d.day}", d.weekday() >= 5))
                    d += timedelta(days=1)
            self._today_col = self._col_of(self._today)

    def _col_of(self, day):
        """绝对模式：日期 → 列下标。"""
        if self._axis_start is None or day is None:
            return None
        delta = (day - self._axis_start).days
        if delta < 0:
            return None
        idx = delta // 7 if self._zoom == "week" else delta
        return idx if 0 <= idx < len(self._axis_cols) else None

    def _x_of(self, row_index, day):
        """给定行 + 日期 → 画布 x（绝对/相对双模式统一）。"""
        if day is None:
            return None
        if self._axis_mode == "rel":
            anchor = (self._rows[row_index].get("milestones") or {}).get(self._anchor)
            if not anchor:
                return None
            off = (day - anchor).days
            idx = (off - self._off_start) // 7 if self._zoom == "week" \
                else (off - self._off_start)
            if not (0 <= idx < len(self._axis_cols)):
                return None
            return NAME_W + idx * self._col_w + self._col_w / 2
        idx = self._col_of(day)
        if idx is None:
            return None
        return NAME_W + idx * self._col_w + self._col_w / 2

    def _row_top(self, row_index):
        return MONTH_H + HEADER_H + row_index * ROW_H

    def _band_rect(self, row_index, s, e):
        """弱色带矩形，越界返回 None。"""
        x0, x1 = self._x_of(row_index, s), self._x_of(row_index, e)
        if x0 is None or x1 is None:
            return None
        y = self._row_top(row_index) + (ROW_H - BAND_H) / 2
        return QRectF(min(x0, x1), y, abs(x1 - x0), BAND_H)

    def _milestone_rect(self, row_index, key):
        d = (self._rows[row_index].get("milestones") or {}).get(key)
        x = self._x_of(row_index, d)
        if x is None:
            return None
        r = _diamond_r(key)
        y = self._row_top(row_index) + ROW_H / 2
        return QRectF(x - r, y - r, 2 * r, 2 * r)

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

    def _hit_milestone(self, pos):
        for i in range(len(self._rows)):
            for k, _lab, _big in MILESTONES:
                r = self._milestone_rect(i, k)
                if r and r.adjusted(-3, -3, 3, 3).contains(pos):
                    return i, k
        return None, None

    def conflict_batch_ids(self):
        out = set()
        for c in self._conflicts:
            out.update(c.get("batches") or [])
        return out

    def row_payload(self, idx):
        if idx is None or not (0 <= idx < len(self._rows)):
            return None
        r = self._rows[idx]
        return {
            "batch_id": r.get("batch_id"), "batch_no": r.get("batch_no"),
            "spans": r.get("spans") or {},
            "docs": r.get("docs") or {}, "proj": r.get("proj") or {},
            "overdue": r.get("overdue") or 0, "milestones": r.get("milestones") or {},
        }

    def milestone_payload(self, row_index, key):
        r = self._rows[row_index]
        d = (r.get("milestones") or {}).get(key)
        return {"batch_id": r.get("batch_id"), "batch_no": r.get("batch_no"),
                "key": key, "label": dict(MILESTONES)[key][0] if key in dict(MILESTONES) else key,
                "date": d.isoformat() if d else None,
                "status": r.get("status")}

    # ── 交互 ──

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        if not self._interactive:
            return
        pos = event.position()
        i, k = self._hit_milestone(pos)
        if k is not None:
            if (i, k) != self._hover_ms:
                self._hover_ms = (i, k)
                self._hover_row = None
                self.update()
                if self._hover_cb:
                    self._hover_cb(self.milestone_payload(i, k))
            return
        self._hover_ms = None
        if event.buttons() == Qt.NoButton:
            idx = self.row_at(pos.y())
            if idx != self._hover_row:
                self._hover_row = idx
                self.update()

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if not self._interactive:
            return
        if event.button() != Qt.LeftButton:
            return
        pos = event.position()
        i, k = self._hit_milestone(pos)
        if k is not None:
            self._selected = {"row": i, "key": k}
            self.update()
            if self._activate_cb:
                self._activate_cb(self.milestone_payload(i, k))
            return
        # 点击非里程碑也可选中当前行（便于右列看批注），双击才跳转

    def mouseDoubleClickEvent(self, event):
        super().mouseDoubleClickEvent(event)
        if not self._interactive:
            return
        if event.button() != Qt.LeftButton:
            return
        idx = self.row_at(event.position().y())
        if idx is not None:
            self.rowActivated.emit(self.row_payload(idx))

    def leaveEvent(self, event):
        super().leaveEvent(event)
        if not self._interactive:
            return
        if self._hover_row is not None or self._hover_ms is not None:
            self._hover_row = None
            self._hover_ms = None
            self.update()

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
        self._draw_links(p)
        for i in range(len(self._rows)):
            self._draw_row(p, i)
        if self._axis_mode == "abs":
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
        p.drawText(QRectF(NAME_W, 0, max(200, self.width() - NAME_W),
                          max(60, self.height())), Qt.AlignCenter,
                   "当前项目没有启用中的批次" if not self._rows else "所选批次还没有计划日期")

    def _draw_axis(self, p):
        p.fillRect(QRectF(0, 0, self._w, MONTH_H), QColor(GRAY_SOFT))
        f = QFont("Microsoft YaHei UI", 8)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(TEXT_SECONDARY))
        title = "批次"
        if self._axis_mode == "rel":
            title = f"相对里程碑（{ANCHOR_LABELS[self._anchor]}日 = 第 0 天）"
        p.drawText(QRectF(0, 0, self._w, MONTH_H), Qt.AlignCenter, title)

        top = MONTH_H
        p.fillRect(QRectF(0, top, self._w, HEADER_H), QColor("#FFFFFF"))
        p.setFont(QFont("Microsoft YaHei UI", 8))
        for i, col in enumerate(self._axis_cols):
            x = NAME_W + i * self._col_w
            if self._axis_mode == "rel":
                is_anchor_col = col[0] <= 0 <= col[1] if self._zoom == "week" \
                    else col[0] == 0
                if is_anchor_col:
                    p.fillRect(QRectF(x, top, self._col_w, HEADER_H),
                               QColor(REL_ANCHOR_BG))
            else:
                s, e, _l, is_weekend = col
                if s <= self._today <= e:
                    p.fillRect(QRectF(x, top, self._col_w, HEADER_H), QColor("#FFF4D6"))
                elif is_weekend:
                    p.fillRect(QRectF(x, top, self._col_w, HEADER_H), QColor(WEEKEND_BG))
            p.setPen(QColor("#C07500") if self._axis_mode == "abs"
                     and col[0] <= self._today <= col[1] else TEXT_SECONDARY)
            p.drawText(QRectF(x, top, self._col_w, HEADER_H), Qt.AlignCenter, col[2])
        p.setPen(QColor(HAIRLINE))
        p.drawLine(0, top + HEADER_H - 1, self._w, top + HEADER_H - 1)

    def _draw_row(self, p, i):
        r = self._rows[i]
        y = self._row_top(i)
        conflicted = r.get("batch_id") in self.conflict_batch_ids()
        if conflicted:
            p.fillRect(QRectF(NAME_W, y, self._w - NAME_W, ROW_H),
                       QColor(*CONFLICT_RGB, 10))

        # 弱色三段带
        for area in SEGMENTS:
            s, e = (r.get("spans") or {}).get(area, (None, None))
            if not s or not e:
                continue
            rect = self._band_rect(i, s, e)
            if rect is None:
                continue
            col = QColor(AREA_COLORS[area])
            col.setAlpha(70)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(col))
            p.drawRoundedRect(rect, 4, 4)

        # 提交状态首帽
        first = next(((r.get("spans") or {}).get(a) for a in SEGMENTS
                      if (r.get("spans") or {}).get(a, (None, None))[0]), None)
        docs = r.get("docs") or {}
        if first:
            b0 = self._band_rect(i, first[0], first[1])
            if b0 is not None:
                cap = QColor(doc_state_color(docs.get("req_total", 0),
                                             docs.get("req_submitted", 0)))
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(cap))
                p.drawRoundedRect(QRectF(b0.left() - 5, b0.top() - 1, 5,
                                         b0.height() + 2), 2, 2)

        # 里程碑菱形
        ms = r.get("milestones") or {}
        for k, _lab, big in MILESTONES:
            if not ms.get(k):
                continue
            rectd = self._milestone_rect(i, k)
            if rectd is None:
                continue
            center = rectd.center()
            rad = rectd.width() / 2
            path = QPainterPath()
            path.moveTo(center.x(), center.y() - rad)
            path.lineTo(center.x() + rad, center.y())
            path.lineTo(center.x(), center.y() + rad)
            path.lineTo(center.x() - rad, center.y())
            path.closeSubpath()
            fill = QColor(_diamond_color(k, big))
            p.setBrush(QBrush(fill))
            p.setPen(QPen(QColor("#FFFFFF"), 1.5) if big
                     else QPen(QColor("#FFFFFF"), 1))
            p.drawPath(path)

        self._draw_row_text(p, r, y, conflicted)

    def _draw_row_text(self, p, r, y, conflicted):
        ms = r.get("milestones") or {}
        docs = r.get("docs") or {}
        proj = r.get("proj") or {}

        # 行1：批次号 + 状态徽章
        f = QFont("Microsoft YaHei UI", 11)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(TEXT_PRIMARY))
        fm = QFontMetrics(f)
        name = fm.elidedText(r.get("batch_no") or "", Qt.ElideRight, NAME_W - 112)
        p.drawText(QRectF(14, y + 6, NAME_W - 112, 18), Qt.AlignVCenter | Qt.AlignLeft, name)
        st_color = {"running": ACCENT, "ready": GREEN, "completed": ORANGE,
                    "closed": TEXT_SECONDARY}.get(r.get("status"), TEXT_SECONDARY)
        p.setFont(QFont("Microsoft YaHei UI", 9))
        p.setPen(QColor(st_color))
        p.drawText(QRectF(NAME_W - 100, y + 7, 88, 17), Qt.AlignVCenter | Qt.AlignRight,
                   STATUS_CN.get(r.get("status"), r.get("status") or ""))

        # 行2：装船（核心排期，高亮）
        p.setFont(QFont("Microsoft YaHei UI", 9))
        p.setPen(QColor(ACCENT))
        sail_txt = f"装船 {ms['sail'].strftime('%m-%d')}" if ms.get("sail") else "装船 —"
        p.drawText(QRectF(14, y + 29, NAME_W - 28, 18), Qt.AlignVCenter | Qt.AlignLeft, sail_txt)

        # 行3：到港 · 交付
        a = ms.get("arrive")
        dd = ms.get("deliver")
        seg3 = []
        if a:
            seg3.append(f"到港 {a.strftime('%m-%d')}")
        if dd:
            seg3.append(f"交付 {dd.strftime('%m-%d')}")
        if seg3:
            p.setFont(QFont("Microsoft YaHei UI", 8))
            p.setPen(QColor(TEXT_SECONDARY))
            p.drawText(QRectF(14, y + 51, NAME_W - 28, 16), Qt.AlignVCenter | Qt.AlignLeft,
                       "  ·  ".join(seg3))

        # 行4：提交状态 / 逾期 / 冲突（有异常才显示红线提示）
        parts = [doc_state_text(docs.get("req_total", 0), docs.get("req_submitted", 0))]
        if proj.get("total") and proj.get("submitted", 0) < proj.get("total") \
                and not parts[0].startswith("全提交"):
            parts.append(f"项目级 {proj.get('submitted', 0)}/{proj.get('total')}")
        if r.get("overdue"):
            parts.append(f"逾期 {r['overdue']}")
        if conflicted:
            parts.append("⚠ 资源冲突")
        p.setFont(QFont("Microsoft YaHei UI", 8))
        tone = doc_state_color(docs.get("req_total", 0), docs.get("req_submitted", 0))
        if conflicted or r.get("overdue"):
            tone = RED
        elif parts[0].startswith("全提交"):
            tone = GREEN
        p.setPen(QColor(tone))
        p.drawText(QRectF(14, y + 70, NAME_W - 28, 14), Qt.AlignVCenter | Qt.AlignLeft,
                   QFontMetrics(QFont("Microsoft YaHei UI", 8)).elidedText(
                       "  ·  ".join(parts), Qt.ElideRight, NAME_W - 28))
        p.setPen(QColor(HAIRLINE))
        p.drawLine(0, y + ROW_H - 1, self._w, y + ROW_H - 1)

    def _draw_links(self, p):
        if not self._links:
            return
        idx = {r.get("batch_id"): i for i, r in enumerate(self._rows)}
        for ln in self._links:
            a, b = ln.get("batch_a"), ln.get("batch_b")
            ia, ib = idx.get(a), idx.get(b)
            if ia is None or ib is None:
                continue
            ms_a = self._rows[ia].get("milestones") or {}
            ms_b = self._rows[ib].get("milestones") or {}
            xa = self._x_of(ia, ms_a.get("sail"))
            xb = self._x_of(ib, ms_b.get("sail"))
            if xa is None or xb is None:
                continue
            ya = self._row_top(ia) + ROW_H / 2
            yb = self._row_top(ib) + ROW_H / 2
            dx = abs(xb - xa)
            ctrlx = max(dx * 0.35, 46)
            path = QPainterPath()
            path.moveTo(xa, ya)
            path.cubicTo(xa + (ctrlx if xb >= xa else -ctrlx), ya,
                         xb - (ctrlx if xb >= xa else -ctrlx), yb, xb, yb)
            pen = QPen(QColor("#C4CBD6"), 1)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
            # 共柜标签（放大到一定宽度才显示）
            mx, my = (xa + xb) / 2, (ya + yb) / 2
            if dx > 90:
                p.setFont(QFont("Microsoft YaHei UI", 7))
                p.setPen(QColor(TEXT_TERTIARY))
                tag = f"共柜 {ln.get('container_no') or ''}"
                p.drawText(QRectF(mx - 60, my - 8, 120, 14), Qt.AlignCenter, tag)

    def _draw_conflict_bands(self, p):
        if not self._conflicts or self._axis_mode == "rel":
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
            p.setPen(Qt.NoPen)
            p.setPen(QColor(RED))
            p.setFont(f)
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
        if not self._interactive:
            return
        if self._hover_ms is not None:
            i, k = self._hover_ms
            r = self._milestone_rect(i, k)
            if r:
                p.setPen(QPen(QColor(ACCENT), 2))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(r.adjusted(-4, -4, 4, 4))
            return
        if self._hover_row is None:
            return
        y = self._row_top(self._hover_row)
        p.fillRect(QRectF(NAME_W, y, self._w - NAME_W, ROW_H),
                   QColor(ACCENT).lighter(192))
        p.setPen(QPen(QColor(ACCENT), 2))
        p.drawLine(NAME_W, int(y), self._w, int(y))

    def _draw_divider(self, p):
        p.setPen(QPen(QColor("#AEB5C3"), 2))
        p.drawLine(NAME_W, 0, NAME_W, self._h)


class BatchOverviewGantt(QScrollArea):
    """全批次总览主控件：里程碑双轴（只读）。悬停/单击里程碑、双击行发信号。"""

    rowHovered = Signal(object)
    rowActivated = Signal(object)
    milestoneHovered = Signal(object)
    milestoneActivated = Signal(object)

    def __init__(self, rows=None, today=None, zoom="week", conflicts=None,
                 links=None, axis_mode="abs", anchor="sail", interactive=True,
                 parent=None):
        super().__init__(parent)
        self.setWidgetResizable(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._last_rows = rows or []
        self._last_conflicts = conflicts
        self._last_links = list(links or [])
        self._axis_mode = axis_mode
        self._anchor = anchor
        self._canvas = _OverviewCanvas(rows or [], today, zoom, conflicts, links,
                                       axis_mode, anchor, interactive)
        self.setWidget(self._canvas)
        self._canvas._hover_cb = lambda payload: self.milestoneHovered.emit(payload)
        self._canvas._activate_cb = lambda payload: self.milestoneActivated.emit(payload)
        self._canvas.rowActivated.connect(self.rowActivated)

    def update_data(self, rows, today=None, conflicts=None, links=None):
        self._last_rows = rows or []
        if conflicts is not None:
            self._last_conflicts = conflicts
        if links is not None:
            self._last_links = list(links)
        self._canvas.update_data(self._last_rows, today, self._last_conflicts,
                                 self._last_links)

    def set_zoom(self, zoom):
        self._canvas._zoom = zoom if zoom in ("day", "week") else "week"
        self._canvas.update_data(self._last_rows, None, self._last_conflicts,
                                 self._last_links)

    def set_axis_mode(self, mode, anchor=None):
        self._axis_mode = mode if mode in ("abs", "rel") else "abs"
        if anchor and anchor in ANCHOR_LABELS:
            self._canvas._anchor = anchor
        self._canvas._axis_mode = self._axis_mode
        self._canvas.update_data(self._last_rows, None, self._last_conflicts,
                                 self._last_links)

    @property
    def zoom(self):
        return self._canvas._zoom

    @property
    def axis_mode(self):
        return self._axis_mode

    def auto_height(self):
        return self._canvas.auto_height()

    def canvas(self):
        return self._canvas