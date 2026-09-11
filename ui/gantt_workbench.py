"""
甘特工作台 —— 非模态独立窗口（**单批次**可操作 / **全批次**只读总览）。

设计要点（与看板卡片的分工）：
  · 看板卡片 = 摘要（批次条 + 三栏 + 三区进度），恒定高度，不再承载甘特与操作面板；
  · 本窗口 = 分析与操作：
      单批次模式：三区相对轴（境内逐日 · 海运压缩 · 境外逐日）+ 批次列表 + 右栏
                  （动态调整 · 推迟/提前 / 单证清单）——**只有这里能做操作**；
      全批次模式：**只读总览**，行 = 批次（全部启用中批次），横轴 = 真实日历（日/周缩放），
                  表达"时间轴 + 提交状态（全提交 / 部分 / 未提交）"与资源冲突标注；
                  隐藏批次列表与右栏操作面板，点击某行 = 跳到该批次的单批次视图。
  · 窗口非模态：可以一边看卡片一边看图；卡片切批次/新增批次会同步刷新本窗口。
  · 数据变更（勾单证/位移/补齐节点）统一走 `_notify_changed()` 回看板刷新摘要。
"""

from datetime import date

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QSpinBox, QMessageBox, QTabBar, QSizePolicy, QComboBox,
    QStackedWidget, QButtonGroup, QCheckBox, QFileDialog
)
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QCursor

import db
from config import get_port
from services import batches as batches_svc
from services.node_status import (
    compute_node_status, compute_all_status, sync_doc_completion
)
from services.file_checklist import merge_scope, pending_required
from services.cargo_check import summary
from services.scheduler import apply_shift, undo_last_shift, ShiftError
from services.clock import get_today
from ui.theme import (
    ACCENT, GREEN, RED, ORANGE, TEXT_PRIMARY,
    TEXT_SECONDARY, TEXT_TERTIARY, BORDER, HAIRLINE, ACCENT_SOFT, GRAY_SOFT,
    NODE_STATUS_COLORS, BG
)
from ui.icons import icon
from ui.widgets.gantt_grid import GanttGrid
from ui.widgets.gantt_overview import BatchOverviewGantt, doc_state_text
from ui.widgets.file_panel import FilePanel
from ui.widgets.scoped_scroll import ScopedScrollArea
from ui.widgets.node_popover import NodePopover
from ui.dialogs import CargoDialog, VesselDialog


def _oplog(*args, **kw):
    """操作日志埋点薄封装：失败不影响主流程。"""
    try:
        from services.oplog import record
        return record(*args, **kw)
    except Exception:
        return None


def _parse(s):
    """'YYYY-MM-DD' / date → date；坏值返回 None（总览轴计算容错）。"""
    if isinstance(s, date):
        return s
    if not s:
        return None
    try:
        y, m, d = str(s).split("-")
        return date(int(y), int(m), int(d))
    except Exception:
        return None


def _clear_layout(layout):
    """清空布局：取出全部条目（含末尾 stretch），只销毁控件。

    注意必须先 `takeAt` 再判断类型：布局里常带 QSpacerItem，
    直接 `item.widget().deleteLater()` 会在 stretch 上抛 AttributeError。
    调用方清空后自行补 stretch。
    """
    items = []
    while layout.count():
        items.append(layout.takeAt(0))
    for item in items:
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()


class ShiftRow(QFrame):
    """动态调整面板中的单节点行：名称 + 净位移 + 提前/推迟按钮"""

    def __init__(self, node, today, on_shift, parent=None):
        super().__init__(parent)
        self.setFixedHeight(34)
        self.setStyleSheet(f"ShiftRow {{ border-bottom: 1px solid {HAIRLINE}; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 0, 6, 0)
        lay.setSpacing(8)

        nid = node["node_id"]
        st = compute_node_status(node, today)
        done = node.get("status") == "Done"

        dot = QLabel()
        dot.setFixedSize(9, 9)
        color = NODE_STATUS_COLORS.get(st, GRAY_SOFT)
        dot.setStyleSheet(f"background: {color}; border-radius: 4px;")
        lay.addWidget(dot, 0, Qt.AlignVCenter)

        name = QLabel(f"{nid}.{node['node_name']}")
        name.setStyleSheet(f"font-size: 12px; color: {TEXT_PRIMARY};")
        name.setMaximumWidth(300)
        lay.addWidget(name)

        role = QLabel(node.get("role_label") or "")
        role.setStyleSheet(f"font-size: 10px; color: {TEXT_TERTIARY};")
        lay.addWidget(role, 0)

        lay.addStretch()

        span = QLabel(f"{node['plan_start'][5:]} ~ {node['plan_end'][5:]}")
        span.setStyleSheet(f"font-size: 11px; color: {TEXT_SECONDARY};")
        lay.addWidget(span)

        dd = node.get("delay_days") or 0
        if dd:
            dtext = QLabel(f"净位移 {dd:+d}天")
            dtext.setStyleSheet(
                f"font-size: 10px; font-weight: 600; color: {RED if dd > 0 else GREEN};")
            lay.addWidget(dtext)

        for label, sign, tip in (("提前 −", -1, "提前（可负向位移，联动下游）"),
                                 ("推迟 +", 1, "推迟（正向位移，联动下游）")):
            btn = QPushButton(label)
            btn.setFixedWidth(64)
            btn.setFixedHeight(24)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(
                f"QPushButton {{ background: {GRAY_SOFT}; color: {TEXT_SECONDARY};"
                f" border: none; border-radius: 6px; font-size: 11px; }}"
                f" QPushButton:hover {{ background: {ACCENT_SOFT}; color: {ACCENT}; }}")
            btn.setToolTip(tip)
            btn.setEnabled(not done and st != "Done")
            btn.clicked.connect(lambda _=False, s=sign: on_shift(nid, s))
            lay.addWidget(btn)

        if done:
            done_lbl = QLabel("已完成")
            done_lbl.setStyleSheet(f"font-size: 10px; color: {GREEN}; font-weight: 600;")
            lay.addWidget(done_lbl)


class GanttWorkbenchWindow(QMainWindow):
    """非模态甘特工作台。单批次（三区相对轴，可操作）/ 合并（真实日历轴 + 冲突）双模式。"""

    changed = Signal(str)      # project_id：窗口内数据变更，看板据此刷新摘要卡

    # 设置键（跨启动记忆）
    K_MODE = "wb_mode"
    K_ZOOM = "wb_zoom"
    K_RIGHT = "wb_right"
    K_TAB = "wb_tab"
    K_GEOM = "wb_geometry"

    def __init__(self, parent=None, on_changed=None, on_status=None):
        super().__init__(parent)
        self._on_changed = on_changed            # func(project_id)
        self._on_status = on_status              # func(text)：回看板显示一行提示
        self._project_id = None
        self._project = {}
        self._batch_id = None
        self._batches = []
        self._nodes = []
        self._files = []
        self._vessel = None
        self._cargo = []
        self._cargo_count = 0
        self._cargo_over = 0
        self._today = get_today()
        self.mode = "single"
        self.zoom = "week"
        self._conflicts = []                     # 当前模式下的资源冲突
        self._overview_data = []                 # 全批次总览的行数据（由 _overview_rows() 生成）
        self._chip = {}
        self._tab = "files"
        self._right_collapsed = False
        self._focused_node = None
        self._flash_note = ""
        self._pop = None
        self._file_panel = None
        self._gantt_grid = None
        self._merged = None       # 兼容字段：现在指向全批次总览画布（BatchOverviewGantt）
        self._overview = None
        self._fill_btn = None
        self._muted = False                      # 构建期间抑制信号副作用

        self.setWindowTitle("甘特工作台")
        self.setMinimumSize(1120, 700)
        self.resize(1400, 920)          # 首次打开就够大（15 节点甘特 755px）
        self._restore_state()
        self._build()

    # ══════════ 对外 ══════════

    def open_for(self, project_id=None, batch_id=None, mode=None):
        """打开（或切换）某项目的甘特工作台；batch_id=None → 用项目当前批次。"""
        project_id = project_id or self._project_id
        if project_id is None:
            return False
        self._project_id = project_id
        if batch_id:
            self._batch_id = batch_id
        if self._batch_id is None or db.get_batch(self._batch_id) is None:
            self._batch_id = db.current_batch_id(project_id)
        if mode in ("single", "merged"):
            self.mode = mode
        self._load()
        if mode in ("single", "merged"):
            self.mode = mode
        self._refresh_all()
        self.show()
        self.raise_()
        self.activateWindow()
        return True

    def sync_batch(self, batch_id):
        """外部（看板卡片）切了当前批次 → 跟随切换，但不抢焦点/不重新 raise。"""
        if not batch_id or batch_id == self._batch_id:
            return
        if db.get_batch(batch_id) is None:
            return
        self._batch_id = batch_id
        self._focused_node = None
        self._load()
        self._refresh_all()

    def refresh(self):
        """外部数据变化（卡片切批次 / 新增批次 / 位移）后重新读库刷新。"""
        if not self._project_id:
            return
        self._load()
        self._refresh_all()

    # ══════════ 数据 ══════════

    def _load(self):
        pid = self._project_id
        proj = db.get_project(pid) or {}
        self._project = proj
        self._today = get_today()
        self._batches = db.get_batches(pid)
        cur = db.get_batch(self._batch_id) if self._batch_id else None
        if not cur or (cur and cur.get("status") == "cancelled"):
            self._batch_id = db.current_batch_id(pid)
            cur = db.get_batch(self._batch_id)
        self._nodes = db.get_nodes_by_batch(self._batch_id)
        self._files = db.get_files_by_batch(self._batch_id)
        # 项目级单证（项目日报/物流动态跟踪表/项目进度报告）：同项目一份，
        # 与本批次票货单证合并成一个列表渲染（FilePanel 会把 node_id 为空的归到「项目级」分组）
        self._project_files = db.get_project_files(pid) or []
        self._all_files = merge_scope(self._files, self._project_files)
        self._vessel = db.get_vessel(pid, self._batch_id)
        cargo = db.get_cargo_items(pid, self._batch_id)
        self._cargo = cargo
        cs = summary(cargo) if cargo else {"count": 0, "over": 0}
        self._cargo_count = cs["count"]
        self._cargo_over = cs["over"]

    def _batch_nodes(self):
        return {b["batch_id"]: db.get_nodes_by_batch(b["batch_id"]) for b in self._batches}

    def _scan_conflicts(self):
        """冲突扫描：全批次看「本项目全部启用中批次之间」，单批次看「与本批次相关的风险」。

        服务侧跨批次规则需要 ≥2 个批次，故统一传全项目启用批次再按需过滤；
        单批次模式额外 include_intra=True，以便拿到「免堆/免箱 vs 计划节点」这一批次内自检项。
        """
        try:
            from services.resource_conflict import scan_batch_conflicts
        except Exception:
            self._conflicts = []
            return
        all_ids = [b["batch_id"] for b in self._batches] or \
            ([self._batch_id] if self._batch_id else [])
        if not all_ids:
            self._conflicts = []
            return
        try:
            if self.mode == "merged":
                # 全批次总览恒为全部启用中批次，无需再按选中集过滤
                self._conflicts = scan_batch_conflicts(self._project_id, all_ids, self._today)
            else:
                found = scan_batch_conflicts(self._project_id, all_ids, self._today,
                                             include_intra=True)
                self._conflicts = [c for c in found
                                   if self._batch_id in (c.get("batches") or [])]
        except Exception:
            self._conflicts = []

    # ── 全批次总览的数据与汇总 ──

    def _overview_rows(self):
        """全批次总览行数据：三段真实日历跨度 + 提交状态 + 项目级进度 + 逾期/冲突标记。

        画布只负责画；统计口径集中在这里，便于单测与复用。
        """
        conflict_ids = set()
        for c in self._conflicts:
            conflict_ids.update(c.get("batches") or [])
        proj_files = db.get_project_files(self._project_id) or []
        proj_req = [f for f in proj_files if f.get("doc_type") == "required"]
        proj_sub = sum(1 for f in proj_req if f.get("status") == "submitted")

        def _etd(bid):
            return (db.get_route(bid) or {}).get("etd") or "9999-99-99"

        rows = []
        for b in sorted(self._batches, key=lambda x: (_etd(x["batch_id"]), x["batch_no"])):
            bid = b["batch_id"]
            nodes = db.get_nodes_by_batch(bid)
            files = db.get_files_by_batch(bid)
            req = [f for f in files if f.get("doc_type") == "required"]
            spans = {}
            for area in ("DOME", "SEA", "OVERSEA"):
                seg = [n for n in nodes if n["area"] == area
                       and n.get("plan_start") and n.get("plan_end")]
                if seg:
                    spans[area] = (min(_parse(n["plan_start"]) for n in seg),
                                   max(_parse(n["plan_end"]) for n in seg))
            rows.append({
                "batch_id": bid, "batch_no": b.get("batch_no") or "",
                "status": b.get("status"), "etd": (db.get_route(bid) or {}).get("etd"),
                "spans": spans,
                "docs": {"req_total": len(req),
                         "req_submitted": sum(1 for f in req
                                              if f.get("status") == "submitted"),
                         "all_total": len(files)},
                "proj": {"total": len(proj_req), "submitted": proj_sub},
                "overdue": sum(1 for n in nodes
                               if compute_node_status(n, self._today) == "Overdue"),
                "conflict": bid in conflict_ids,
            })
        return rows

    def _overview_summary_text(self):
        """全批次顶栏汇总：N 批次 · 全提交 x · 未提交 y · 逾期节点 z。"""
        rows = self._overview_data
        full = sum(1 for r in rows if r["docs"]["req_total"]
                   and r["docs"]["req_submitted"] >= r["docs"]["req_total"])
        none = sum(1 for r in rows if r["docs"]["req_submitted"] == 0)
        partial = len(rows) - full - none
        overdue = sum(r["overdue"] for r in rows)
        bits = [f"{len(rows)} 批次", f"全提交 {full}"]
        if partial:
            bits.append(f"部分提交 {partial}")
        bits.append(f"未提交 {none}")
        if overdue:
            bits.append(f"逾期节点 {overdue}")
        return " · ".join(bits)

    def _notify_changed(self):
        if self._on_changed:
            self._on_changed(self._project_id)
        if self.changed:
            self.changed.emit(self._project_id)

    # ══════════ 构建 ══════════

    def _build(self):
        central = QWidget()
        central.setStyleSheet(f"background: {BG};")
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 16, 20, 14)
        root.setSpacing(10)

        root.addWidget(self._build_toolbar())

        body = QHBoxLayout()
        body.setSpacing(14)
        body.addWidget(self._build_gantt_area(), stretch=1)
        body.addWidget(self._build_right_col(), stretch=0)
        root.addLayout(body, stretch=1)

        root.addWidget(self._build_conflict_bar())
        self.setCentralWidget(central)
        self.statusBar().showMessage("就绪")

    def _build_toolbar(self):
        bar = QFrame()
        bar.setObjectName("wbBar")
        bar.setStyleSheet(f"QFrame#wbBar {{ background: #FFFFFF; border: 1px solid {BORDER};"
                          f" border-radius: 14px; }}")
        v = QVBoxLayout(bar)
        v.setContentsMargins(14, 10, 14, 10)
        v.setSpacing(8)

        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self._title_label = QLabel("甘特工作台")
        self._title_label.setStyleSheet(f"font-size: 16px; font-weight: 600; color: {TEXT_PRIMARY};")
        row1.addWidget(self._title_label)

        self._proj_combo = QComboBox()
        self._proj_combo.setMinimumWidth(260)
        self._proj_combo.setCursor(Qt.PointingHandCursor)
        self._proj_combo.setToolTip("切换项目（冲突扫描按项目内批次进行）")
        self._proj_combo.currentIndexChanged.connect(self._on_project_changed)
        row1.addWidget(self._proj_combo)

        self._sub_label = QLabel("")
        self._sub_label.setStyleSheet(f"font-size: 12px; color: {TEXT_TERTIARY};")
        row1.addWidget(self._sub_label)
        row1.addStretch()

        # 模式切换（分段控件）：只有两个模式
        self._mode_group = QButtonGroup(self)
        self._mode_btns = {}
        for key, text, tip in (
                ("single", "单批次", "三区相对轴：境内逐日 · 海运压缩 · 境外逐日；"
                                     "可勾单证、推迟/提前、撤销位移"),
                ("merged", "全批次", "只读总览：真实日历轴，行=批次，三段色块 + 提交状态 + 冲突标注；"
                                     "点击某行跳到该批次的单批次视图")):
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(tip)
            btn.setMinimumHeight(30)
            btn.setStyleSheet(
                f"QPushButton {{ background: {GRAY_SOFT}; color: {TEXT_SECONDARY}; border: none;"
                f" padding: 6px 16px; font-size: 13px; }}"
                f"QPushButton:checked {{ background: {ACCENT}; color: #FFFFFF; font-weight: 600; }}"
                f"QPushButton:hover:!checked {{ background: #E5E5EA; }}")
            self._mode_group.addButton(btn)
            self._mode_btns[key] = btn
            row1.addWidget(btn)
            btn.clicked.connect(lambda _=False, k=key: self.set_mode(k))

        export_btn = QPushButton("导出图片")
        export_btn.setObjectName("secondary")
        export_btn.setCursor(Qt.PointingHandCursor)
        export_btn.setToolTip("把当前甘特图存为 PNG（默认 data/reports/）")
        export_btn.clicked.connect(self._export_png)
        row1.addWidget(export_btn)
        v.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(8)

        self._batch_label = QLabel("批次")
        self._batch_label.setStyleSheet(f"font-size: 12px; color: {TEXT_TERTIARY}; font-weight: 600;")
        row2.addWidget(self._batch_label)

        self._batch_combo = QComboBox()
        self._batch_combo.setMinimumHeight(34)
        self._batch_combo.setMinimumWidth(260)
        self._batch_combo.setMaximumWidth(420)
        self._batch_combo.setCursor(Qt.PointingHandCursor)
        self._batch_combo.currentIndexChanged.connect(self._on_batch_combo)
        row2.addWidget(self._batch_combo)

        self._status_pill = QLabel("")
        row2.addWidget(self._status_pill)

        # 合并模式专用：缩放 + 批次全选/清空
        self._zoom_label = QLabel("刻度")
        self._zoom_label.setStyleSheet(f"font-size: 12px; color: {TEXT_TERTIARY}; font-weight: 600;")
        row2.addWidget(self._zoom_label)
        self._zoom_group = QButtonGroup(self)
        self._zoom_btns = {}
        for key, text in (("day", "日"), ("week", "周")):
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setMinimumHeight(30)
            btn.setStyleSheet(
                f"QPushButton {{ background: {GRAY_SOFT}; color: {TEXT_SECONDARY}; border: none;"
                f" padding: 5px 12px; font-size: 12px; }}"
                f"QPushButton:checked {{ background: {ACCENT_SOFT}; color: {ACCENT};"
                f" font-weight: 600; }}")
            self._zoom_group.addButton(btn)
            self._zoom_btns[key] = btn
            row2.addWidget(btn)
            btn.clicked.connect(lambda _=False, k=key: self.set_zoom(k))

        row2.addStretch()

        # 统计字牌（单批次模式）
        self._chips_row = QWidget()
        chips_lay = QHBoxLayout(self._chips_row)
        chips_lay.setContentsMargins(0, 0, 0, 0)
        chips_lay.setSpacing(8)
        self._chip = {}
        chips_lay.addWidget(self._stat_chip("active", "进行中", 0, ACCENT, ACCENT_SOFT))
        chips_lay.addWidget(self._stat_chip("overdue", "逾期", 0, RED, "#FDEBEA"))
        chips_lay.addWidget(self._stat_chip("missing", "缺单证", 0, ORANGE, "#FFF3E4"))
        chips_lay.addWidget(self._stat_chip("cargo", "货物", 0, GREEN, "#E8F8EC"))
        row2.addWidget(self._chips_row)

        # 全批次总览汇总（全批次模式）：N 批次 · 全提交 x · 未提交 y · 逾期节点 z
        self._overview_summary = QLabel("")
        self._overview_summary.setStyleSheet(
            f"font-size: 13px; font-weight: 600; color: {TEXT_PRIMARY};"
            f" background: {ACCENT_SOFT}; border-radius: 10px; padding: 6px 12px;")
        row2.addWidget(self._overview_summary)
        v.addLayout(row2)

        self._hint = QLabel("")
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet(f"font-size: 11px; color: {TEXT_TERTIARY};")
        v.addWidget(self._hint)
        return bar

    def _stat_chip(self, key, caption, value, color, bg):
        box = QFrame()
        box.setFixedHeight(34)
        box.setStyleSheet(f"background: {bg}; border-radius: 10px;")
        h = QHBoxLayout(box)
        h.setContentsMargins(12, 0, 12, 0)
        h.setSpacing(6)
        cap = QLabel(caption)
        cap.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
        h.addWidget(cap)
        val = QLabel(str(value))
        val.setStyleSheet(f"font-size: 15px; font-weight: 600; color: {color};")
        h.addWidget(val)
        self._chip[key] = val
        return box

    def _build_gantt_area(self):
        """中栏：标题行（补齐入口 / 悬停明细）+ 画布堆栈（单批次 | 全批次总览）。"""
        area = QFrame()
        area.setObjectName("wbCard2")
        area.setStyleSheet(f"QFrame#wbCard2 {{ background: #FFFFFF; border: 1px solid {BORDER};"
                           f" border-radius: 14px; }}")
        v = QVBoxLayout(area)
        v.setContentsMargins(12, 10, 12, 12)
        v.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(10)
        self._gantt_title = QLabel("时间轴")
        self._gantt_title.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {TEXT_PRIMARY};")
        head.addWidget(self._gantt_title)
        head.addStretch()

        self._fill_btn = QPushButton("补齐计划节点")
        self._fill_btn.setObjectName("secondary")
        self._fill_btn.setCursor(Qt.PointingHandCursor)
        self._fill_btn.setToolTip("按 15 节点标准模板生成计划节点与单证清单（需该批次已填 ETD/ETA）")
        self._fill_btn.clicked.connect(self._fill_batch_nodes)
        head.addWidget(self._fill_btn)
        v.addLayout(head)

        self._hover_info = QLabel("")
        self._hover_info.setWordWrap(True)
        self._hover_info.setStyleSheet(f"font-size: 11px; color: {TEXT_SECONDARY};")
        v.addWidget(self._hover_info)

        self._gantt_stack = QStackedWidget()
        self._single_host = QWidget()
        sl = QVBoxLayout(self._single_host)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        self._gantt_stack.addWidget(self._single_host)     # index 0
        self._overview_host = QWidget()
        ml = QVBoxLayout(self._overview_host)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(0)
        self._gantt_stack.addWidget(self._overview_host)   # index 1
        v.addWidget(self._gantt_stack, stretch=1)
        return area

    def _build_right_col(self):
        """右栏：可收纳面板（动态调整 / 单证清单）+ 动作行。"""
        panel = QWidget()
        self._right_panel = panel
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        self._tabbar = QTabBar()
        self._tabbar.addTab("动态调整 · 推迟/提前")
        self._tabbar.addTab("单证清单")
        self._tabbar.setDocumentMode(True)
        self._tabbar.currentChanged.connect(self._on_tab_changed)
        self._head_bar = QWidget()
        self._head_bar.setStyleSheet("background: #FAFAFC; border-radius: 8px;")
        hb = QHBoxLayout(self._head_bar)
        hb.setContentsMargins(4, 4, 4, 4)
        hb.setSpacing(4)
        hb.addWidget(self._tabbar, 1)
        self._collapse_btn = QPushButton()
        self._collapse_btn.setCursor(Qt.PointingHandCursor)
        self._collapse_btn.setFixedSize(28, 28)
        self._collapse_btn.setIcon(icon("chevron_right", TEXT_TERTIARY, 14))
        self._collapse_btn.setIconSize(QSize(14, 14))
        self._collapse_btn.setToolTip("收起面板")
        self._collapse_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; border-radius: 6px; }"
            "QPushButton:hover { background: #E5E5EA; }")
        self._collapse_btn.clicked.connect(self._toggle_right)
        hb.addWidget(self._collapse_btn, 0, Qt.AlignVCenter)
        v.addWidget(self._head_bar)

        self._panel_host = QWidget()
        self._panel_host_layer = QVBoxLayout(self._panel_host)
        self._panel_host_layer.setContentsMargins(0, 0, 0, 0)
        self._panel_host_layer.setSpacing(0)
        v.addWidget(self._panel_host, stretch=1)

        self._action_row_area = QWidget()
        ar = QVBoxLayout(self._action_row_area)
        ar.setContentsMargins(0, 0, 0, 0)
        ar.setSpacing(0)
        ar.addLayout(self._build_action_row())
        v.addWidget(self._action_row_area)

        self._reopen_bar = QWidget()
        rb = QVBoxLayout(self._reopen_bar)
        rb.setContentsMargins(0, 0, 0, 0)
        rb.addStretch()
        self._reopen_btn = QPushButton()
        self._reopen_btn.setCursor(Qt.PointingHandCursor)
        self._reopen_btn.setFixedSize(32, 32)
        self._reopen_btn.setIcon(icon("chevron_right", ACCENT, 15))
        self._reopen_btn.setIconSize(QSize(15, 15))
        self._reopen_btn.setToolTip("展开面板")
        self._reopen_btn.setStyleSheet(
            "QPushButton { background: #FFFFFF; border: 1px solid #E5E5EA; border-radius: 16px; }"
            "QPushButton:hover { border-color: #D1D1D6; background: #F5F5F7; }")
        self._reopen_btn.clicked.connect(self._toggle_right)
        rb.addWidget(self._reopen_btn, 0, Qt.AlignHCenter)
        rb.addStretch()
        v.addWidget(self._reopen_bar)

        self._shift_page = None
        self._files_page = None
        self._apply_right_collapsed()
        return panel

    def _build_shift_page(self):
        page = QWidget()
        page.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        tip = QLabel(
            "选中节点 → 点击「提前 − / 推迟 +」按步长整体位移：境内 1–4 联动 ETD 与海运；"
            "海运 5 联动 ETA 与境外全段；境外 6–12 口岸整段平移。已完成节点不可位移。")
        tip.setWordWrap(True)
        tip.setStyleSheet(f"font-size: 11px; color: {TEXT_TERTIARY};")
        lay.addWidget(tip)

        ctl_row = QHBoxLayout()
        ctl_row.addWidget(QLabel("步长"))
        step = QSpinBox()
        step.setRange(1, 90)
        step.setValue(1)
        step.setSuffix(" 天")
        step.setFixedWidth(86)
        ctl_row.addWidget(step)
        self._step_spin = step
        ctl_row.addStretch()
        note_hint = QLabel("位移后单证建议提交日自动重算")
        note_hint.setStyleSheet(f"font-size: 11px; color: {TEXT_TERTIARY};")
        ctl_row.addWidget(note_hint)
        lay.addLayout(ctl_row)

        shift_scroll = ScopedScrollArea()
        shift_scroll.setWidgetResizable(True)
        shift_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        shift_scroll.setStyleSheet(
            "ScopedScrollArea, QScrollArea { border: none; background: transparent; }")
        body = QWidget()
        blay = QVBoxLayout(body)
        blay.setContentsMargins(0, 0, 0, 0)
        blay.setSpacing(0)
        blay.addStretch()
        for n in self._nodes:
            blay.insertWidget(blay.count() - 1, ShiftRow(n, self._today, self._shift_node))
        shift_scroll.setWidget(body)
        lay.addWidget(shift_scroll, stretch=1)

        self._op_note = QLabel("")
        self._op_note.setWordWrap(True)
        self._op_note.setStyleSheet(f"font-size: 11px; color: {ACCENT};")
        if self._flash_note:
            self._op_note.setText(self._flash_note)
        lay.addWidget(self._op_note)
        return page

    def _build_files_page(self):
        page = QWidget()
        page.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        file_panel = FilePanel(self._all_files, self._nodes, self._today,
                               batch_id=self._batch_id)
        file_panel.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        file_panel.file_toggled.connect(lambda fid, checked: self._toggle_file(fid, checked))
        lay.addWidget(file_panel, 1)
        self._file_panel = file_panel
        return page

    def _build_action_row(self):
        act_row = QHBoxLayout()
        act_row.setSpacing(6)
        for text, tip, slot in (
            ("批次管理", "编辑批次：订舱/提单、船期与线路、集装箱、客户货主", self._open_batch),
            ("船期变更", "登记船期变更（A/B/C/D 自动重排 + 影响预览 + 历史）", self._open_schedule_change),
            ("计划日期", "查看当前批次计划日期（§5.3 规则计算）", self._open_plan_date),
            ("客户货主", "客户/货主主档管理与当前批次角色绑定", self._open_customer_master),
            ("货物台账", "查看 / 编辑当前批次货物清单，登记装箱箱号与封号", self._open_cargo),
            ("班轮·船位", "维护当前批次船名/航次，手工登记船位与实际 ETA", self._open_vessel),
        ):
            btn = QPushButton(text)
            btn.setObjectName("secondary")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            act_row.addWidget(btn)
        act_row.addStretch()

        self._undo_btn = QPushButton("撤销上一步位移")
        self._undo_btn.setObjectName("ghost")
        self._undo_btn.setCursor(Qt.PointingHandCursor)
        self._undo_btn.setIcon(icon("undo", ACCENT, 14))
        self._undo_btn.setIconSize(QSize(14, 14))
        self._undo_btn.clicked.connect(self._undo_shift)
        act_row.addWidget(self._undo_btn)
        return act_row

    def _build_conflict_bar(self):
        bar = QFrame()
        bar.setObjectName("wbConflict")
        bar.setStyleSheet(f"QFrame#wbConflict {{ background: #FFFFFF; border: 1px solid {BORDER};"
                          f" border-radius: 14px; }}")
        v = QVBoxLayout(bar)
        v.setContentsMargins(14, 10, 14, 10)
        v.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(8)
        self._conflict_title = QLabel("资源冲突")
        self._conflict_title.setStyleSheet(
            f"font-size: 13px; font-weight: 600; color: {TEXT_PRIMARY};")
        head.addWidget(self._conflict_title)
        self._conflict_count = QLabel("")
        self._conflict_count.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
        head.addWidget(self._conflict_count)
        head.addStretch()
        v.addLayout(head)

        self._conflict_scroll = ScopedScrollArea()
        self._conflict_scroll.setWidgetResizable(True)
        self._conflict_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._conflict_scroll.setStyleSheet(
            "ScopedScrollArea, QScrollArea { border: none; background: transparent; }")
        self._conflict_scroll.setFixedHeight(74)
        self._conflict_body = QWidget()
        self._conflict_vbox = QVBoxLayout(self._conflict_body)
        self._conflict_vbox.setContentsMargins(0, 0, 0, 0)
        self._conflict_vbox.setSpacing(4)
        self._conflict_vbox.addStretch()
        self._conflict_scroll.setWidget(self._conflict_body)
        v.addWidget(self._conflict_scroll)
        return bar

    # ══════════ 刷新 ══════════

    def _refresh_all(self):
        self._muted = True
        try:
            self._scan_conflicts()
            self._refresh_chips()
            self._refresh_gantt()
            self._refresh_conflict_bar()
            self._refresh_panels()
            self._refresh_toolbar()
            self._apply_mode_layout()
        finally:
            self._muted = False

    def _apply_mode_layout(self):
        """按模式显示/隐藏：全批次总览是**只读**视图 → 隐藏右栏操作面板。

        （右栏是勾单证/推迟提前用的，属于单批次操作流；
          批次切换走顶部「批次」下拉，工作台内不再有左侧批次卡片。）
        """
        single = self.mode == "single"
        self._right_panel.setVisible(single)

    def _refresh_toolbar(self):
        proj = getattr(self, "_project", None) or {}
        self._title_label.setText(proj.get("project_name") or "甘特工作台")
        self.setWindowTitle(f"甘特工作台 · {proj.get('project_name') or ''}")

        # 项目下拉：进行中 + 已完成（只读自查）
        self._proj_combo.blockSignals(True)
        self._proj_combo.clear()
        projects = db.get_projects_by_status("Active") + db.get_projects_by_status("Cancelled") \
            + db.get_projects_by_status("Completed")
        for p in projects:
            self._proj_combo.addItem(p["project_name"], p["project_id"])
        for i in range(self._proj_combo.count()):
            if self._proj_combo.itemData(i) == self._project_id:
                self._proj_combo.setCurrentIndex(i)
                break
        self._proj_combo.blockSignals(False)

        self._sub_label.setText(f"ID · {(self._project_id or '')[:12]}")

        cur = db.get_batch(self._batch_id) or {}
        st = batches_svc.state_label(cur.get("status") or "draft")
        tone = {"进行中": ACCENT, "逾期": RED}.get(st, TEXT_SECONDARY)
        self._status_pill.setText(st)
        self._status_pill.setStyleSheet(
            f"font-size: 11px; font-weight: 600; color: {tone}; background: {ACCENT_SOFT};"
            f" border-radius: 8px; padding: 5px 10px;")

        self._mode_btns["single"].setChecked(self.mode == "single")
        self._mode_btns["merged"].setChecked(self.mode == "merged")
        for k, b in self._zoom_btns.items():
            b.setChecked(self.zoom == k)

        single = self.mode == "single"
        self._batch_label.setVisible(single)
        self._batch_combo.setVisible(single)
        self._status_pill.setVisible(single)
        self._chips_row.setVisible(single)
        self._zoom_label.setVisible(not single)
        for b in self._zoom_btns.values():
            b.setVisible(not single)
        self._overview_summary.setVisible(not single)

        self._batch_combo.blockSignals(True)
        self._batch_combo.clear()
        longest = ""
        for bt in self._batches:
            no = bt["batch_no"] or bt["batch_name"]
            nm = bt.get("batch_name") or ""
            label = no if (not nm or nm in no) else f"{no} · {nm}"
            if bt["batch_id"] == self._batch_id:
                label = f"● {label}"
            self._batch_combo.addItem(label, bt["batch_id"])
            longest = label if len(label) > len(longest) else longest
        for i in range(self._batch_combo.count()):
            if self._batch_combo.itemData(i) == self._batch_id:
                self._batch_combo.setCurrentIndex(i)
                break
        self._batch_combo.blockSignals(False)
        self._batch_combo.setToolTip(longest)
        self._batch_combo.setMaximumWidth(940 if not single else 420)

        if single:
            route = db.get_route(self._batch_id) or {}
            port = get_port(route.get("export_port"))
            self._batch_label.setText("批次")
            self._hint.setText(
                f"单批次视图（可操作）：三区相对轴（境内逐日 · 海运压缩 · 境外逐日）；"
                f"出口港 {port['name'] if port else '未选'}；"
                f"勾单证 / 推迟提前 / 撤销都在这里做。")
            self._overview_summary.setVisible(False)
        else:
            self._overview_summary.setText(self._overview_summary_text())
            self._hint.setText(
                "全批次总览（只读）：行 = 批次，三段色块 = 境内/海运/境外的真实日历跨度，"
                "左列显示单证提交状态，红色竖带 = 资源冲突窗口；"
                "点某一行跳到该批次的单批次视图做操作。")

        has_nodes = bool(self._nodes)
        self._fill_btn.setVisible(not has_nodes and single)
        self._gantt_title.setText("时间轴" if single else "全批次总览（只读）")

    def _refresh_chips(self):
        statuses = compute_all_status(self._nodes, self._today)
        self._chip["active"].setText(str(sum(1 for s in statuses.values() if s == "Active")))
        self._chip["overdue"].setText(str(sum(1 for s in statuses.values() if s == "Overdue")))
        # 缺单证口径 = 批次级未交必填 + 项目级未交必填（项目级同项目只算一次，不随批次翻倍）
        pending = pending_required(self._files, self._project_files)
        self._chip["missing"].setText(str(pending))
        self._chip["cargo"].setText(f"{self._cargo_count}" + ("⚠" if self._cargo_over else ""))
        self._chip["cargo"].setStyleSheet(
            f"font-size: 15px; font-weight: 600; color: {RED if self._cargo_over else GREEN};")
        # 位移撤销可用性
        has_hist = bool(db.get_shift_history(self._project_id, limit=1, batch_id=self._batch_id))
        if getattr(self, "_undo_btn", None) is not None:
            self._undo_btn.setEnabled(has_hist)

    def _refresh_gantt(self):
        self._gantt_stack.setCurrentIndex(0 if self.mode == "single" else 1)
        if self.mode == "single":
            self._build_single_gantt()
        else:
            self._build_overview_gantt()

    def _build_single_gantt(self):
        _clear_layout(self._single_host.layout())

        route = db.get_route(self._batch_id) or {}
        has_ship = bool(route.get("etd") and route.get("eta"))
        empty_title = None
        empty_hints = None
        if not self._nodes:
            empty_title = "该批次还没有计划节点"
            empty_hints = (("点上方「补齐计划节点」按标准模板一键生成。",) if has_ship
                           else ("先在「批次管理」填写 ETD / ETA 并保存，",
                                 "系统会自动生成计划节点与单证清单。"))
        gantt = GanttGrid(
            self._nodes, self._today,
            export_port=(self._project or {}).get("export_port"),
            over_count=self._cargo_over,
            buffer_days=(self._project or {}).get("buffer_days", 4),
            empty_title=empty_title, empty_hints=empty_hints)
        try:
            gantt.set_dependency_waiting(self._node_waiting())
        except Exception:
            pass
        # 窗口内可滚动：不锁死高度，画布按自身完整高度呈现（窗口小则出现纵向滚动条）
        gantt.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        gantt.setMinimumWidth(340)
        gantt.nodeHovered.connect(self._on_gantt_hover)
        gantt.nodeActivated.connect(self._on_gantt_activate)
        self._single_host.layout().addWidget(gantt)
        self._gantt_grid = gantt

    def _build_overview_gantt(self):
        _clear_layout(self._overview_host.layout())

        self._overview_data = self._overview_rows()
        overview = BatchOverviewGantt(self._overview_data, self._today, self.zoom,
                                      self._conflicts)
        overview.rowHovered.connect(self._on_overview_hover)
        overview.rowActivated.connect(self._on_overview_row)
        self._overview_host.layout().addWidget(overview)
        self._overview = overview
        self._gantt_grid = None

    def _refresh_conflict_bar(self):
        _clear_layout(self._conflict_vbox)

        confs = self._conflicts
        scope = "全批次之间" if self.mode == "merged" else "当前批次"
        if not confs:
            lbl = QLabel(f"✓ {scope}未发现资源冲突"
                         + ("（港口窗口 / 报关行 / 船名航次 / 免堆免箱 / 境外堆存）"
                            if self.mode == "merged" else "（本批次免堆免箱 vs 计划节点）"))
            lbl.setStyleSheet(f"font-size: 12px; color: {GREEN};")
            self._conflict_vbox.addWidget(lbl)
            self._conflict_count.setText("0 条")
        else:
            tone = {"high": RED, "medium": ORANGE, "low": TEXT_SECONDARY}
            for c in confs:
                line = QLabel(
                    f"⚠ [{ {'high': '高', 'medium': '中', 'low': '低'}.get(c.get('level'), '低') }] "
                    f"{c.get('resource') or ''} · {c.get('message') or ''}")
                line.setWordWrap(True)
                line.setStyleSheet(
                    f"font-size: 12px; color: {tone.get(c.get('level'), TEXT_SECONDARY)};")
                line.setToolTip(c.get("message") or "")
                self._conflict_vbox.addWidget(line)
            self._conflict_count.setText(f"{len(confs)} 条")
        self._conflict_vbox.addStretch()
        if self.mode == "merged":
            self._conflict_title.setText("跨批次资源冲突（全批次总览）")
        else:
            self._conflict_title.setText("本批次资源风险")

    def _refresh_panels(self):
        """重建右栏两个页面（批次切换后节点/单证集合会变）。"""
        self._shift_page = self._build_shift_page()
        self._files_page = self._build_files_page()
        self._set_active_panel(self._tab, sync_tabbar=True)

    # ══════════ 模式 / 批次选择 ══════════

    def set_mode(self, mode):
        if mode not in ("single", "merged"):
            return
        if mode == self.mode:
            self._refresh_toolbar()
            return
        self.mode = mode
        self._refresh_all()
        self._save_state()

    def set_zoom(self, zoom):
        if zoom not in ("day", "week"):
            return
        self.zoom = zoom
        if self._overview is not None:
            self._overview.set_zoom(zoom)
        self._refresh_toolbar()
        self._save_state()

    def _on_project_changed(self, index):
        if self._muted or index < 0:
            return
        pid = self._proj_combo.itemData(index)
        if not pid or pid == self._project_id:
            return
        self.open_for(pid, None, self.mode)

    def _on_batch_combo(self, index):
        if self._muted or index < 0:
            return
        bid = self._batch_combo.itemData(index)
        if not bid or bid == self._batch_id:
            return
        self._switch_batch(bid)

    def _switch_batch(self, bid):
        self._batch_id = bid
        db.update_project(self._project_id, current_batch_id=bid)
        self._focused_node = None
        self._load()
        self._refresh_all()
        self._notify_changed()

    # ══════════ 右栏面板 ══════════

    def _set_active_panel(self, tab, sync_tabbar=True):
        self._tab = tab
        if sync_tabbar and getattr(self, "_tabbar", None) is not None:
            b = self._tabbar.blockSignals(True)
            self._tabbar.setCurrentIndex(0 if tab == "shift" else 1)
            self._tabbar.blockSignals(b)
        while self._panel_host_layer.count():
            item = self._panel_host_layer.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
        page = self._shift_page if tab == "shift" else self._files_page
        if page is not None:
            self._panel_host_layer.addWidget(page)
        if self._focused_node is not None and tab == "files" and self._file_panel is not None:
            self._file_panel.focus_node(self._focused_node)
            self._file_panel.scroll_to_node(self._focused_node)

    def _on_tab_changed(self, index):
        tab = "shift" if index == 0 else "files"
        if tab == self._tab:
            return
        self._set_active_panel(tab, sync_tabbar=False)

    def _toggle_right(self):
        self._right_collapsed = not self._right_collapsed
        self._apply_right_collapsed()
        self._save_state()

    def _apply_right_collapsed(self):
        c = self._right_collapsed
        self._head_bar.setVisible(not c)
        self._panel_host.setVisible(not c)
        self._action_row_area.setVisible(not c)
        self._reopen_bar.setVisible(c)
        self._right_panel.setFixedWidth(44 if c else 560)

    # ══════════ 甘特联动 ══════════

    def _ensure_popover(self):
        if self._pop is None:
            self._pop = NodePopover(self)
        return self._pop

    def _node_files(self, nid):
        return [f for f in self._files if f.get("node_id") == nid]

    def _on_gantt_hover(self, node):
        pop = self._ensure_popover()
        if node is None:
            pop.hide_card()
            if self._focused_node is None and self._file_panel is not None:
                self._file_panel.clear_focus()
            self._hover_info.setText("")
            return
        pop.show_node(node, self._node_files(node["node_id"]), QCursor.pos())
        self._hover_info.setText(
            f"节点{node['node_id']} {node['node_name']} · "
            f"{node['plan_start']} ~ {node['plan_end']} · "
            f"{compute_node_status(node, self._today)}")
        if self._file_panel is not None and self._tab == "files":
            self._file_panel.focus_node(node["node_id"])

    def _on_gantt_activate(self, node):
        nid = node["node_id"]
        pop = self._ensure_popover()
        if self._focused_node == nid:
            self._focused_node = None
            if self._file_panel is not None:
                self._file_panel.clear_focus()
            pop.hide_card()
            return
        self._focused_node = nid
        pop.show_node(node, self._node_files(nid), QCursor.pos())
        if self._tab != "files":
            self._set_active_panel("files")
        if self._file_panel is not None:
            self._file_panel.focus_node(nid)
            self._file_panel.scroll_to_node(nid)

    def _on_overview_hover(self, payload):
        """全批次总览行悬停：显示该批次三段区间 + 提交状态明细。"""
        if not payload:
            self._hover_info.setText("")
            return
        spans = payload.get("spans") or {}
        seg = " ｜ ".join(f"{k} {v}" for k, v in spans.items())
        docs = payload.get("docs") or {}
        proj = payload.get("proj") or {}
        state = doc_state_text(docs.get("req_total", 0), docs.get("req_submitted", 0))
        extra = f" · 项目级 {proj.get('submitted', 0)}/{proj.get('total')}" if proj.get("total") else ""
        od = f" · 逾期 {payload['overdue']}" if payload.get("overdue") else ""
        self._hover_info.setText(
            f"{payload.get('batch_no')}：{state}{extra}{od} · {seg}")

    def _on_overview_row(self, payload):
        """点击总览某一行 → 切到该批次的单批次视图（那里才能操作）。"""
        if not payload:
            return
        bid = payload.get("batch_id")
        if not bid or bid == self._batch_id and self.mode == "single":
            return
        self._batch_id = bid
        db.update_project(self._project_id, current_batch_id=bid)
        self.mode = "single"
        self._focused_node = None
        self._load()
        self._refresh_all()
        self._notify_changed()

    def _node_waiting(self):
        out = {}
        try:
            from services import doc_dependency as dep
            by_file, by_key, _ctx = dep.apply_context(self._batch_id, self._files)
        except Exception:
            return out
        by_key = by_key or {}
        for f in self._files or []:
            if f.get("status") == "submitted":
                continue
            st = by_file.get(f.get("file_id")) if by_file else None
            if not st or not st.get("blocked"):
                continue
            nk = f.get("node_key") or f.get("due_node_key")
            if nk and nk not in out:
                names = st.get("waiting_names") or []
                out[nk] = ("、".join(f"《{n}》" for n in names) if names else "上游单证")
        return out

    # ══════════ 操作 ══════════

    def _note(self, text):
        self._flash_note = text or ""
        if getattr(self, "_op_note", None) is not None:
            self._op_note.setText(self._flash_note)
        self.statusBar().showMessage(self._flash_note or "就绪")
        if self._on_status:
            self._on_status(self._flash_note)

    def _mark_changed(self):
        """位移等变更后：同步节点完成状态 + 重载 + 刷新界面 + 通知看板。"""
        sync_doc_completion(self._project_id)
        self._load()
        self._refresh_all()
        self._notify_changed()

    def _shift_node(self, node_id, sign):
        step = self._step_spin.value() if getattr(self, "_step_spin", None) else 1
        try:
            res = apply_shift(self._project_id, node_id, sign * step, batch_id=self._batch_id)
        except ShiftError as e:
            QMessageBox.warning(self, "位移被拦截", str(e))
            return
        self._note(f"✓ {res['note']}")
        self._mark_changed()

    def _undo_shift(self):
        try:
            res = undo_last_shift(self._project_id, batch_id=self._batch_id)
        except ShiftError as e:
            QMessageBox.warning(self, "撤销被拦截", str(e))
            return
        self._note(f"✓ {res['note']}")
        self._mark_changed()

    def _fill_batch_nodes(self):
        res = batches_svc.ensure_batch_nodes(self._project_id, self._batch_id,
                                            reason="甘特工作台补齐")
        if res.get("created"):
            self._note(f"✓ 已按模板生成 {res['nodes']} 个计划节点与 {res['files']} 份单证清单")
            self._mark_changed()
            return
        if res.get("reason") == "缺 ETD/ETA":
            QMessageBox.information(
                self, "需要先填写船期",
                "该批次还没有 ETD / ETA，无法排定计划日期。\n"
                "请在「批次管理」填写船期后保存，系统会自动生成计划节点与单证清单。")
            self._open_batch()
            return
        QMessageBox.warning(self, "无法生成计划节点", res.get("reason") or "未知原因")

    def _toggle_file(self, file_id, checked):
        """勾选/取消单证 → 落库 + 原地刷新行（不重建面板，避免销毁信号发射者）。

        两类单证分流：
          · 项目级（file_id 形如 `pf-12`）→ 写 `project_files`，**同项目一份、全批次共享**；
            日志 batch_id 为空、scope='project'；
          · 批次级（整数 file_id）→ 写 `files`，日志挂当前批次。
        """
        is_project = str(file_id).startswith("pf-")
        if is_project:
            doc = db.get_project_file(file_id) or {}
        else:
            finfo = db.get_files_by_batch(self._batch_id) or []
            doc = next((f for f in finfo if f["file_id"] == file_id), {})
        doc_name = doc.get("doc_name", "")
        node_id = doc.get("node_id")
        if checked:
            from services import batches as bsv
            # 项目级单证不校验批次客户角色（项目日报等本就没有角色要求；
            # 且它属于整个项目，不该被某个批次的客户资料缺失挡住）
            missing = set() if is_project else bsv.missing_roles(self._batch_id, doc_name)
            proj = self._project or {}
            tax_missing = False if is_project else \
                bsv.importer_tax_missing(self._batch_id, proj.get("country"))
            if missing:
                roles_cn = "、".join({"SHIPPER": "发货人", "CONSIGNEE": "收货人",
                                      "IMPORTER": "进口商", "CUSTOMER": "客户"}.get(r, r)
                                     for r in missing)
                if self._file_panel is not None:
                    self._file_panel.update_files(self._all_files, self._nodes)
                ret = QMessageBox.warning(
                    self, "客户资料缺失",
                    f"「{doc_name}」缺少必填角色：{roles_cn}。\n"
                    f"请先到「客户货主」补录资料并绑定到当前批次后再提交。",
                    QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Ok)
                if ret == QMessageBox.Ok:
                    self._open_customer_master()
                self._note(f"未提交：{doc_name} 缺客户资料")
                return
            if tax_missing and any(k in doc_name for k in
                                   ("出口报关单", "进口证", "进口税费", "非自动进口许可证", "许可证")):
                if self._file_panel is not None:
                    self._file_panel.update_files(self._all_files, self._nodes)
                QMessageBox.warning(
                    self, "税号缺失",
                    "目的国要求进口商税号（CNPJ/CPF），请先在「客户货主」为进口商补录税号。")
                self._note(f"未提交：{doc_name} 缺进口商税号")
                return
            if is_project:
                db.update_project_file(file_id, status="submitted",
                                       submitted_date=db.today_str())
                _oplog("file_submit", self._project_id, subject=doc_name, detail="提交",
                       batch_id=None, scope="project")
            else:
                db.update_file(file_id, status="submitted", submitted_date=db.today_str())
                _oplog("file_submit", self._project_id, node_id=node_id, subject=doc_name,
                       detail="提交", batch_id=self._batch_id, node_key=doc.get("node_key"))
        else:
            if is_project:
                db.update_project_file(file_id, status="pending", submitted_date=None)
                _oplog("file_withdraw", self._project_id, subject=doc_name, detail="撤交",
                       batch_id=None, scope="project")
            else:
                db.update_file(file_id, status="pending", submitted_date=None)
                _oplog("file_withdraw", self._project_id, node_id=node_id, subject=doc_name,
                       detail="撤交", batch_id=self._batch_id, node_key=doc.get("node_key"))
        sync_doc_completion(self._project_id)
        self._load()
        self._refresh_toolbar()
        self._refresh_chips()
        self._refresh_conflict_bar()
        if self._file_panel is not None:
            # ★ 原地增量刷新：只改这一行（及其节点状态），不销毁任何控件
            self._file_panel.update_files(self._all_files, self._nodes,
                                          batch_id=self._batch_id)
            if self._focused_node is not None:
                self._file_panel.focus_node(self._focused_node)
        self._note(f"✓ {'提交' if checked else '撤交'} {doc_name}"
                   + ("（项目级，全批次共享）" if is_project else ""))
        self._notify_changed()

    # ── 对话框 ──

    def _open_batch(self):
        from ui.batch_dialogs import BatchDialog
        dlg = BatchDialog(self._project_id, self._batch_id, self)
        note = ""
        if dlg.exec():
            note = getattr(dlg, "auto_note", "") or ""
            self._load()
            self._refresh_all()
            if note:
                self._note(f"✓ {note}")
                QMessageBox.information(self, "批次管理", note)
            self._notify_changed()

    def _open_schedule_change(self):
        from ui.batch_dialogs import ScheduleChangeDialog
        dlg = ScheduleChangeDialog(self._project_id, self._batch_id, self)
        if dlg.exec():
            self._load()
            self._refresh_all()
            self._notify_changed()

    def _open_plan_date(self):
        from ui.batch_dialogs import PlanDateDialog
        PlanDateDialog(self._project_id, self._batch_id, self).exec()

    def _open_customer_master(self):
        from ui.batch_dialogs import CustomerMasterDialog
        CustomerMasterDialog(self._batch_id, self).exec()
        self._load()
        self._refresh_all()
        self._notify_changed()

    def _open_cargo(self):
        dlg = CargoDialog(self._project_id, self)
        if dlg.exec():
            self._load()
            self._refresh_all()
            self._notify_changed()

    def _open_vessel(self):
        dlg = VesselDialog(self._project_id, self)
        if dlg.exec():
            note = dlg.result_note()
            if note:
                QMessageBox.information(self, "班轮 · 船位", note)
            self._load()
            self._refresh_all()
            self._notify_changed()

    def _export_png(self):
        """导出当前甘特为 PNG（默认 data/reports/），供周会/汇报使用。"""
        import os
        from services.clock import get_now_str
        widget = self._overview if self.mode == "merged" else self._gantt_grid
        if widget is None:
            QMessageBox.information(self, "导出图片", "当前没有可导出的甘特图。")
            return
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        dflt = os.path.join(root, "data", "reports",
                            f"甘特图_{(db.get_batch(self._batch_id) or {}).get('batch_no', 'batch')}"
                            f"_{self.mode}_{get_now_str('%Y%m%d-%H%M%S')}.png")
        os.makedirs(os.path.dirname(dflt), exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(self, "导出甘特图为 PNG", dflt, "PNG 图片 (*.png)")
        if not path:
            return
        target = widget.canvas() if hasattr(widget, "canvas") else getattr(widget, "_canvas", widget)
        ok = target.grab().save(path)
        if ok:
            self._note(f"✓ 已导出 {path}")
        else:
            QMessageBox.warning(self, "导出失败", f"无法写入 {path}")

    # ══════════ 状态记忆 ══════════

    def _restore_state(self):
        try:
            self.mode = db.get_setting(self.K_MODE) or "single"
            self.zoom = db.get_setting(self.K_ZOOM) or "week"
            self._right_collapsed = (db.get_setting(self.K_RIGHT) or "0") == "1"
            self._tab = db.get_setting(self.K_TAB) or "files"
            geom = db.get_setting(self.K_GEOM)
            if geom:
                self.restoreGeometry(bytes.fromhex(geom))
        except Exception:
            pass

    def _save_state(self):
        try:
            db.set_setting(self.K_MODE, self.mode)
            db.set_setting(self.K_ZOOM, self.zoom)
            db.set_setting(self.K_RIGHT, "1" if self._right_collapsed else "0")
            db.set_setting(self.K_TAB, self._tab)
            db.set_setting(self.K_GEOM, bytes(self.saveGeometry()).hex())
        except Exception:
            pass

    def closeEvent(self, event):
        """关闭 = 记住窗口状态并隐藏（实例保留，重新打开即时且不丢状态）。"""
        self._save_state()
        event.ignore()
        self.hide()
