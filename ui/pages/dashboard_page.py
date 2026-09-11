"""
页面二：主看板 — 折叠卡片 + 动态调整(推迟/提前)面板 + 甘特 + 文件清单
"""

from datetime import date

from services.clock import get_today

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QScrollArea, QSpinBox, QMessageBox, QTabBar, QSizePolicy, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView
)
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QCursor, QColor

import db
from config import get_country, get_port
from services import batches as batches_svc
from services import modes as mode_names
from services.node_status import (
    compute_node_status, get_current_node, compute_all_status,
    sync_doc_completion, sync_active_projects
)
from services.file_checklist import count_files
from services.cargo_check import summary
from services.scheduler import apply_shift, undo_last_shift, ShiftError
from ui.theme import (
    card_shadow, ACCENT, GREEN, RED, ORANGE, TEXT_PRIMARY,
    TEXT_SECONDARY, TEXT_TERTIARY, BORDER, HAIRLINE, ACCENT_SOFT, GRAY_SOFT,
    NODE_STATUS_COLORS
)
from ui.icons import icon, pixmap
from ui.widgets.mini_bar import MiniBar
from ui.widgets.gantt_grid import GanttGrid
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
    if isinstance(s, date):
        return s
    if not s:
        return None
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))


class ShiftRow(QFrame):
    """动态调整面板中的单节点行：名称 + 净位移 + 提前/推迟按钮"""

    def __init__(self, node, today, on_shift, parent=None):
        super().__init__(parent)
        self._node = node
        self._today = today
        self._on_shift = on_shift
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


class ProjectCard(QFrame):
    """折叠/展开的项目卡片（含 批次列表 + 当前批次甘特/单证）"""

    def __init__(self, project, parent=None, on_changed=None, init_state=None, on_state=None):
        super().__init__(parent)
        self._project = project
        self._today = get_today()
        self._on_changed = on_changed
        self._on_state = on_state           # 向上级上报折叠状态变化（供跨重建记忆）
        _st = init_state or {}
        self._expanded = bool(_st.get("expanded", False))
        self._step_spin = None
        self._op_note = None
        self._flash_note = ""       # 跨重建保留的操作提示
        # 右侧面板当前活动标签（跨重建记忆；甘特恒显不在此列）
        self._tab = "shift" if _st.get("active_tab") == "shift" else "files"
        self._right_collapsed = bool(_st.get("right", False))   # 右栏可收纳
        # 甘特 ↔ 单证清单 联动状态
        self._focused_node = None      # 点击钉住的节点（跨重建保留）
        self._pop = None               # 节点速览悬浮卡（懒创建）
        self._gantt_grid = None
        self._file_panel = None
        # 批次列表与当前批次
        self._batches = []
        self._current_batch_id = None
        self._batch_list_widget = None
        self._batch_buttons = []
        self._load()
        self.setObjectName("card")
        self._apply_card_shadow()
        self._build()

    # ── 数据 ──

    def _load(self):
        pid = self._project["project_id"]
        # 刷新项目行：current_batch_id / status 可能已被其它面板改写，
        # 用陈旧内存值会导致「切了批次又跳回旧批次」。
        fresh = db.get_project(pid)
        if fresh:
            self._project = fresh
        self._today = get_today()
        self._batches = db.get_batches(pid)
        # 取最近 non-cancelled 作为当前
        if self._batches:
            # current_batch_id from project if exists
            cur = self._project.get("current_batch_id")
            found = next((b for b in self._batches if b["batch_id"] == cur), None)
            if not found:
                found = self._batches[-1] if self._batches else None
            if found:
                self._current_batch_id = found["batch_id"]
        else:
            # 无批次 → 创建默认（兼容旧项目迁移）
            from db import create_default_batch
            b = create_default_batch(pid)
            self._batches = db.get_batches(pid)
            self._current_batch_id = b["batch_id"]
        # 按当前批次加载子数据
        self._nodes = db.get_nodes_by_batch(self._current_batch_id)
        self._files = db.get_files_by_batch(self._current_batch_id)
        self._vessel = db.get_vessel(pid, self._current_batch_id)
        cargo = db.get_cargo_items(pid, self._current_batch_id)
        self._cargo = cargo
        cs = summary(cargo) if cargo else {"count": 0, "over": 0}
        self._cargo_count = cs["count"]
        self._cargo_over = cs["over"]

    def _after_change(self):
        # 位移等变更后按「必填齐 + 已过结束日」规则同步节点完成状态
        sync_doc_completion(self._project["project_id"])
        self._load()
        self._update_header()
        if self._expanded:
            self._build_expanded()
        if self._on_changed:
            self._on_changed()

    # ── 头部 ──

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header_frame = QFrame()
        header_frame.setFixedHeight(150)
        header_layout = QHBoxLayout(header_frame)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(0)

        # 状态竖条
        self._bar = QFrame()
        self._bar.setFixedWidth(4)
        header_layout.addWidget(self._bar)

        # 左区
        left = QFrame()
        left.setFixedWidth(256)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(20, 14, 12, 14)
        left_layout.setSpacing(2)

        country_tmpl = get_country(self._project["country"])
        flag = country_tmpl.get("flag", "")
        # §D30/T32：全取消项目在看板上标注「已取消」
        status_tag = ""
        if self._project.get("status") == "Cancelled":
            status_tag = " · 已取消"
        self.name_label = QLabel(f"{flag}  {self._project['project_name']}{status_tag}")
        self.name_label.setStyleSheet(f"font-size: 15px; font-weight: 600; color: {TEXT_PRIMARY};")
        self.name_label.setWordWrap(True)
        left_layout.addWidget(self.name_label)

        id_label = QLabel(f"ID · {self._project['project_id'][:12]}")
        id_label.setStyleSheet(f"font-size: 11px; color: {TEXT_TERTIARY};")
        left_layout.addWidget(id_label)

        self.port_label = QLabel("")
        self.port_label.setStyleSheet(f"font-size: 11px; color: {TEXT_SECONDARY};")
        left_layout.addWidget(self.port_label)

        self.vessel_label = QLabel("")
        self.vessel_label.setStyleSheet(f"font-size: 11px; color: {TEXT_SECONDARY};")
        self.vessel_label.setWordWrap(True)
        left_layout.addWidget(self.vessel_label)
        left_layout.addStretch()
        header_layout.addWidget(left)

        # 中区
        mid = QFrame()
        mid_layout = QVBoxLayout(mid)
        mid_layout.setContentsMargins(12, 14, 12, 14)
        mid_layout.setSpacing(8)

        # 批次切换行
        batch_row = QHBoxLayout()
        batch_row.setSpacing(8)
        batch_row.addWidget(QLabel("批次"), 0)
        self.batch_combo = QComboBox()
        self.batch_combo.setObjectName("batchCombo")
        self.batch_combo.setCursor(Qt.PointingHandCursor)
        self.batch_combo.currentIndexChanged.connect(self._on_batch_changed)
        batch_row.addWidget(self.batch_combo, 1)
        self.batch_status = QLabel("")
        batch_row.addWidget(self.batch_status)
        mid_layout.addLayout(batch_row)

        # §12.1 「N 批次 · 风险汇总」+ 新增/复制批次入口
        summary_row = QHBoxLayout()
        summary_row.setSpacing(8)
        self.batch_summary = QLabel("")
        self.batch_summary.setObjectName("batchSummary")
        summary_row.addWidget(self.batch_summary, 1)
        self.new_batch_btn = QPushButton("新增批次")
        self.new_batch_btn.setObjectName("ghost")
        self.new_batch_btn.setCursor(Qt.PointingHandCursor)
        self.new_batch_btn.clicked.connect(self._open_new_batch)
        summary_row.addWidget(self.new_batch_btn)
        self.copy_batch_btn = QPushButton("复制批次")
        self.copy_batch_btn.setObjectName("ghost")
        self.copy_batch_btn.setCursor(Qt.PointingHandCursor)
        self.copy_batch_btn.clicked.connect(self._copy_current_batch)
        summary_row.addWidget(self.copy_batch_btn)
        mid_layout.addLayout(summary_row)

        self.cur_label = QLabel("")
        self.cur_label.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY}; font-weight: 500;")
        mid_layout.addWidget(self.cur_label)

        self.mini_bar = MiniBar(self._nodes, self._today, self)
        mid_layout.addWidget(self.mini_bar)
        mid_layout.addStretch()
        header_layout.addWidget(mid, stretch=2)

        # 右区
        right = QFrame()
        right.setFixedWidth(310)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(12, 14, 20, 12)
        right_layout.setSpacing(2)

        self.etd_label = QLabel("")
        self.etd_label.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
        right_layout.addWidget(self.etd_label)

        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
        right_layout.addWidget(self.progress_label)

        self.cargo_label = QLabel("")
        self.cargo_label.setStyleSheet(f"font-size: 12px;")
        right_layout.addWidget(self.cargo_label)

        self.history_label = QLabel("")
        self.history_label.setStyleSheet(f"font-size: 10px; color: {TEXT_TERTIARY};")
        right_layout.addWidget(self.history_label)

        self.expand_btn = QPushButton("展开")
        self.expand_btn.setObjectName("ghost")
        self.expand_btn.setCursor(Qt.PointingHandCursor)
        self.expand_btn.setIcon(icon("chevron_down", ACCENT, 14))
        self.expand_btn.setIconSize(QSize(14, 14))
        self.expand_btn.clicked.connect(self._toggle_expand)
        right_layout.addWidget(self.expand_btn, 0, Qt.AlignRight)
        header_layout.addWidget(right)

        layout.addWidget(header_frame)

        # 展开区
        self.expand_area = QFrame()
        self.expand_area.setVisible(False)
        self.expand_area.setStyleSheet(
            f"background: #FAFAFC; border-top: 1px solid {BORDER};"
            f" border-bottom-left-radius: 16px; border-bottom-right-radius: 16px;"
        )
        expand_layout = QVBoxLayout(self.expand_area)
        expand_layout.setContentsMargins(16, 16, 16, 16)
        expand_layout.setSpacing(12)
        layout.addWidget(self.expand_area)

        self._update_header()
        if self._expanded:
            self._apply_expanded()

    def _update_header(self):
        # 状态条
        statuses = compute_all_status(self._nodes, self._today)
        has_overdue = any(s == "Overdue" for s in statuses.values())
        has_active = any(s == "Active" for s in statuses.values())
        bar_color = RED if has_overdue else (ORANGE if has_active else GREEN)
        self._bar.setStyleSheet(f"background: {bar_color};")

        # 批次切换下拉
        cur = db.get_batch(self._current_batch_id)
        if getattr(self, "batch_combo", None) is not None:
            b = self.batch_combo.blockSignals(True)
            self.batch_combo.clear()
            for bt in self._batches:
                no = bt["batch_no"] or bt["batch_name"]
                if bt["batch_id"] == self._current_batch_id:
                    no += " (当前)"
                self.batch_combo.addItem(no or bt["batch_name"], bt["batch_id"])
            self.batch_combo.setCurrentIndex(max(0, self._current_batch_index()))
            self.batch_combo.blockSignals(b)
            st = batches_svc.state_label((cur or {}).get("status") or "draft")
            color = {"进行中": RED, "逾期": RED}.get(st, TEXT_SECONDARY)
            self.batch_status.setText(st)
            self.batch_status.setStyleSheet(
                f"font-size: 11px; font-weight: 600; color: {TEXT_SECONDARY};"
                f" background: {ACCENT_SOFT}; border-radius: 6px; padding: 2px 8px;"
            )
        self._refresh_batch_summary()

        # 目的港（来自线路）
        route = db.get_route(self._current_batch_id)
        port = get_port((route or {}).get("export_port") or self._project.get("export_port"))
        self.port_label.setText(f"出口港 · {port['name']}" if port else "通用方案")

        # 班轮（船名/航次）
        if self._vessel:
            vname = self._vessel.get("vessel_name") or ""
            vvoy = self._vessel.get("voyage") or ""
            parts = []
            if vname:
                parts.append(f"🚢 {vname}")
            if vvoy:
                parts.append(f"航次 {vvoy}")
            if not parts:
                parts.append("已登记班轮")
            self.vessel_label.setText(" · ".join(parts))
        else:
            self.vessel_label.setText("")

        # 当前节点
        current = get_current_node(self._nodes, self._today)
        self.cur_label.setText(
            f"当前 · 节点{current['node_id']} {current['node_name']}"
            if current else "全部完成")

        # ETD/进度/货物（用当前批次线路）
        etd = (route or {}).get("etd") if route else self._project.get("etd")
        days_to_etd = (_parse(etd) - self._today).days if etd else 0
        self.etd_label.setText(
            f"ETD {etd} · 距 {'离港' if days_to_etd >= 0 else '已离港'} {abs(days_to_etd)} 天"
            if etd else "未设船期")

        done_count = sum(1 for s in statuses.values() if s == "Done")
        fc = count_files(self._files)
        self.progress_label.setText(
            f"进度 {done_count}/{len(self._nodes)} 节点 · 缺单证 {fc['pending']}")

        over_txt = f" · 超限 {self._cargo_over}" if self._cargo_over else ""
        if self._cargo_count:
            color = RED if self._cargo_over else TEXT_SECONDARY
            self.cargo_label.setText(f"货物 {self._cargo_count} 项{over_txt}（台账）")
            self.cargo_label.setStyleSheet(f"font-size: 12px; color: {color};")
        else:
            self.cargo_label.setText("")
            self.cargo_label.setStyleSheet("")

        # 位移历史概要
        hist = db.get_shift_history(self._project["project_id"], limit=1, batch_id=self._current_batch_id)
        if hist:
            h = hist[0]
            self.history_label.setText(
                f"位移留痕：节点{h['node_id']} {'推迟' if h['delta'] > 0 else '提前'} {abs(h['delta'])} 天")
        else:
            self.history_label.setText("")

        # 迷你进度条跟随最新节点日期
        if getattr(self, "mini_bar", None):
            self.mini_bar.update_data(self._nodes, self._today)

    def _current_batch_index(self):
        for i, bt in enumerate(self._batches):
            if bt["batch_id"] == self._current_batch_id:
                return i
        return 0

    def _on_batch_changed(self, index):
        if index < 0:
            return
        bid = self.batch_combo.itemData(index)
        if bid == self._current_batch_id:
            return
        self._current_batch_id = bid
        db.update_project(self._project["project_id"], current_batch_id=bid)
        self._load()
        self._after_change()

    # ── §12.1 批次列表（N 批次 · 风险汇总）与新增批次 ──

    def _batch_metrics(self, batch_id):
        """单批次汇总：线路 / 状态 / 发运日 / 单证完成率 / 待办数。"""
        nodes = db.get_nodes_by_batch(batch_id)
        files = db.get_files_by_batch(batch_id)
        route = db.get_route(batch_id) or {}
        b = db.get_batch(batch_id) or {}
        n_done = sum(1 for n in nodes if n.get("status") == "Done")
        req = [f for f in files if f.get("doc_type") == "required"]
        submitted = sum(1 for f in req if f.get("status") == "submitted")
        overdue = sum(1 for n in nodes if compute_node_status(n, self._today) == "Overdue")
        pending = len(req) - submitted
        return {
            "batch_id": batch_id,
            "batch_no": b.get("batch_no") or "",
            "batch_name": b.get("batch_name") or "",
            "status": b.get("status") or "draft",
            "status_cn": batches_svc.state_label(b.get("status") or "draft"),
            "mode": route.get("mode_primary") or "SEA",
            "port": route.get("export_port") or "",
            "etd": route.get("etd") or "",
            "eta": route.get("eta") or "",
            "node_done": n_done, "node_total": len(nodes),
            "doc_rate": (submitted / len(req) * 100) if req else 0.0,
            "doc_total": len(req), "doc_submitted": submitted,
            "todo": pending, "overdue": overdue,
        }

    def _risk_summary(self):
        """风险汇总：N 批次 · 逾期节点 X · 待办单证 Y。"""
        all_b = list(self._batches)
        cur = db.get_batch(self._current_batch_id)
        if cur and cur.get("status") == "cancelled" and \
                not any(b["batch_id"] == cur["batch_id"] for b in all_b):
            all_b.append(cur)
        overdue = todo = 0
        for bt in all_b:
            m = self._batch_metrics(bt["batch_id"])
            overdue += m["overdue"]
            todo += m["todo"]
        return {"n_batch": len(all_b), "overdue": overdue, "todo": todo}

    def _refresh_batch_summary(self):
        if getattr(self, "batch_summary", None) is None:
            return
        s = self._risk_summary()
        bits = [f"{s['n_batch']} 批次"]
        if s["overdue"]:
            bits.append(f"逾期节点 {s['overdue']}")
        if s["todo"]:
            bits.append(f"待办单证 {s['todo']}")
        if not s["overdue"] and not s["todo"]:
            bits.append("无风险")
        self.batch_summary.setText(" · ".join(bits))
        tone = RED if (s["overdue"] or s["todo"]) else GREEN
        self.batch_summary.setStyleSheet(
            f"font-size: 11px; font-weight: 600; color: {tone};")

    def _node_waiting(self):
        """§10.5：{node_key: '《MBL 主提单》'} —— 该节点上存在「待上游」的必填单证。"""
        out = {}
        try:
            from services import doc_dependency as dep
            # apply_context → (by_file_id, by_doc_key, ctx)，与 FilePanel 同源
            by_file, by_key, _ctx = dep.apply_context(self._current_batch_id, self._files)
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
                out[nk] = ("、".join(f"《{n}》" for n in names)
                           if names else "上游单证")
        return out

    def _open_new_batch(self):
        """§12.1 新增批次：自动编号建批次后打开批次管理补齐线路/船期。"""
        from ui.batch_dialogs import BatchDialog
        try:
            nb = db.create_batch(self._project["project_id"])
        except Exception as e:
            QMessageBox.warning(self, "新增批次失败", str(e))
            return
        self._batches = db.get_batches(self._project["project_id"])
        self._current_batch_id = nb["batch_id"]
        db.update_project(self._project["project_id"], current_batch_id=nb["batch_id"])
        self._load()
        self._after_change()
        dlg = BatchDialog(self._project["project_id"], nb["batch_id"], self)
        if dlg.exec():
            self._batches = db.get_batches(self._project["project_id"])
            self._load()
            self._after_change()
        self._flash_note = f"✓ 已新增批次 {nb['batch_no']}"

    def _copy_current_batch(self):
        """§8 复制批次：复制批次信息 + 线路 + 冻结模板快照（不带状态/日志）。"""
        src = db.get_batch(self._current_batch_id)
        if not src:
            return
        try:
            nb = batches_svc.copy_batch(self._project["project_id"], self._current_batch_id)
        except Exception as e:
            QMessageBox.warning(self, "复制批次失败", str(e))
            return
        self._batches = db.get_batches(self._project["project_id"])
        self._current_batch_id = nb["batch_id"]
        db.update_project(self._project["project_id"], current_batch_id=nb["batch_id"])
        self._load()
        self._after_change()
        self._flash_note = f"✓ 已复制为 {nb['batch_no']}（冻结模板快照）"
    def _build_batch_table(self):
        """§12.1 批次列表：批次号/名称/线路/状态/发运日/单证完成率/待办数。"""
        rows = []
        all_b = list(self._batches)
        cur = db.get_batch(self._current_batch_id)
        if cur and cur.get("status") == "cancelled" and \
                not any(b["batch_id"] == cur["batch_id"] for b in all_b):
            all_b.append(cur)
        for bt in all_b:
            rows.append(self._batch_metrics(bt["batch_id"]))

        t = QTableWidget(len(rows), 7)
        t.setHorizontalHeaderLabels(
            ["批次号", "名称", "线路", "状态", "发运日", "单证完成率", "待办数"])
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QTableWidget.NoEditTriggers)
        t.setSelectionBehavior(QTableWidget.SelectRows)
        t.setShowGrid(False)
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        t.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for i, r in enumerate(rows):
            cells = [
                r["batch_no"], r["batch_name"],
                f'{mode_names.label(r["mode"])} · {r["port"] or "—"}',
                r["status_cn"], r["etd"] or "—",
                f'{r["doc_submitted"]}/{r["doc_total"]}（{r["doc_rate"]:.0f}%）',
                str(r["todo"]),
            ]
            for c, v in enumerate(cells):
                item = QTableWidgetItem(str(v))
                if r["batch_id"] == self._current_batch_id:
                    item.setBackground(QColor(ACCENT_SOFT))
                t.setItem(i, c, item)
        t.setFixedHeight(min(210, 34 + 30 * max(1, len(rows))))

        def _row_dbl(row, _col):
            if 0 <= row < len(rows):
                self._current_batch_id = rows[row]["batch_id"]
                db.update_project(self._project["project_id"],
                                  current_batch_id=self._current_batch_id)
                self._load()
                self._after_change()
        t.cellDoubleClicked.connect(_row_dbl)
        return t

    # ── 展开/收起 ──

    def _toggle_expand(self):
        self._expanded = not self._expanded
        self._apply_expanded()
        if self._on_state:
            self._on_state("expanded", self._expanded)

    def _apply_card_shadow(self):
        """收起态浮起阴影（开销低）；展开后卡片很高，阴影会拖慢整卡重绘"""
        self._card_shadow = card_shadow(self, blur=18, dy=4, alpha=18)

    def _clear_card_shadow(self):
        self._card_shadow = None
        self.setGraphicsEffect(None)

    def _apply_expanded(self):
        if self._expanded:
            self.expand_btn.setText("收起")
            self.expand_btn.setIcon(icon("chevron_up", ACCENT, 14))
            self._clear_card_shadow()      # 性能：展开后去掉高开销阴影
            self._build_expanded()
            self.expand_area.setVisible(True)
        else:
            self.expand_btn.setText("展开")
            self.expand_btn.setIcon(icon("chevron_down", ACCENT, 14))
            # 收起前先隐藏展开区 → 阴影作用于矮卡片，避免整幅展开高卡做 18px 模糊(性能)
            self.expand_area.setVisible(False)
            self._apply_card_shadow()

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _build_expanded(self):
        layout = self.expand_area.layout()
        self._clear_layout(layout)
        if self._pop:
            self._pop.hide_card()      # 重建时收起悬浮卡

        statuses = compute_all_status(self._nodes, self._today)
        overdue = sum(1 for s in statuses.values() if s == "Overdue")
        active = sum(1 for s in statuses.values() if s == "Active")
        fc = count_files(self._files)

        # §12.1 批次列表（批次号/名称/线路/状态/发运日/单证完成率/待办数）
        batch_head = QLabel("批次列表（双击切换当前批次）")
        batch_head.setObjectName("section")
        layout.addWidget(batch_head)
        try:
            layout.addWidget(self._build_batch_table())
        except Exception:
            pass

        # 统计字牌（整行置顶），记录数值标签供轻量刷新
        self._chip = {}
        cargo_cap = f"货物 {self._cargo_count} / 超限 {self._cargo_over}"
        cargo_val = "⚠" if self._cargo_over else "✓"
        cargo_col = RED if self._cargo_over else GREEN
        cargo_bg = "#FDEBEA" if self._cargo_over else "#E8F8EC"
        stat_row = QHBoxLayout()
        stat_row.setSpacing(10)
        stat_row.addWidget(self._stat_chip("active", "进行中", active, ACCENT, ACCENT_SOFT))
        stat_row.addWidget(self._stat_chip("overdue", "逾期", overdue, RED, "#FDEBEA"))
        stat_row.addWidget(self._stat_chip("missing", "缺单证", fc["pending"], ORANGE, "#FFF3E4"))
        stat_row.addWidget(self._stat_chip("cargo", cargo_cap, cargo_val, cargo_col, cargo_bg))
        stat_row.addStretch()
        layout.addLayout(stat_row)

        # 两栏：左 = 时间轴·甘特（完整显示，无内滚）｜右 = 可收纳切换面板
        cols = QHBoxLayout()
        cols.setSpacing(18)
        cols.addWidget(self._build_left_col(), stretch=1)
        cols.addWidget(self._build_right_col(), stretch=0)
        layout.addLayout(cols, stretch=1)

    def _build_left_col(self):
        """左栏：时间轴甘特，整幅显示、无上下滚动条"""
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        gantt_label = QLabel("时间轴")
        gantt_label.setObjectName("section")
        v.addWidget(gantt_label)

        gantt = GanttGrid(
            self._nodes, self._today,
            export_port=self._project.get("export_port"),
            over_count=self._cargo_over,
            buffer_days=self._project.get("buffer_days", 4))
        # §10.5 依赖提醒：上游单证未完成 → 对应节点标黄「待上游」
        try:
            from services import doc_dependency as _dep
            gantt.set_dependency_waiting(self._node_waiting())
        except Exception:
            pass
        gantt.setFixedHeight(gantt.auto_height())   # 完整高度，不出现上下滚动条
        gantt.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        gantt.setMinimumWidth(340)   # 避免宽画布把整页撑出横向滚动条（甘特内部横向滚动即可）
        gantt.nodeHovered.connect(self._on_gantt_hover)
        gantt.nodeActivated.connect(self._on_gantt_activate)
        self._gantt_grid = gantt
        v.addWidget(gantt)
        return w

    def _build_right_col(self):
        """右栏：可收纳面板（标签 + 面板 + 动作行）；收纳开关融入标签行右端"""
        panel = QWidget()
        self._right_panel = panel
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        # 头部：切换标签 + 收纳开关（融入标签栏背景，弱化不突兀）
        self._tabbar = QTabBar()
        self._tabbar.addTab("动态调整 · 推迟/提前")
        self._tabbar.addTab("单证清单")
        self._tabbar.setDocumentMode(True)
        self._tabbar.currentChanged.connect(self._on_tab_changed)
        self._head_bar = QWidget()
        self._head_bar.setStyleSheet(f"background: #FAFAFC; border-radius: 8px;")
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
            "QPushButton:hover { background: #E5E5EA; }"
        )
        self._collapse_btn.clicked.connect(self._toggle_right)
        hb.addWidget(self._collapse_btn, 0, Qt.AlignVCenter)
        v.addWidget(self._head_bar)

        # 当前面板宿主
        self._panel_host = QWidget()
        self._panel_host_layer = QVBoxLayout(self._panel_host)
        self._panel_host_layer.setContentsMargins(0, 0, 0, 0)
        self._panel_host_layer.setSpacing(0)
        v.addWidget(self._panel_host, stretch=1)

        # 动作行（随面板收纳）
        self._action_row_area = QWidget()
        ar = QVBoxLayout(self._action_row_area)
        ar.setContentsMargins(0, 0, 0, 0)
        ar.setSpacing(0)
        ar.addLayout(self._build_action_row())
        v.addWidget(self._action_row_area)

        # 收起态重开按钮条（仅收起时可见，垂直居中）
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
            "QPushButton { background: #FFFFFF; border: 1px solid #E5E5EA;"
            " border-radius: 16px; }"
            "QPushButton:hover { border-color: #D1D1D6; background: #F5F5F7; }")
        self._reopen_btn.clicked.connect(self._toggle_right)
        rb.addWidget(self._reopen_btn, 0, Qt.AlignHCenter)
        rb.addStretch()
        v.addWidget(self._reopen_bar)

        # 预构建两个页面（跨标签切换复用，不丢滚动/焦点）
        self._shift_page = self._build_shift_page()
        self._files_page = self._build_files_page()
        self._set_active_panel(self._tab)

        # 重建后恢复点击钉住的节点（仅在单证标签激活时定位，不反向切换）
        if self._focused_node is not None and self._tab == "files":
            self._file_panel.focus_node(self._focused_node)
            self._file_panel.scroll_to_node(self._focused_node)

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
            blay.insertWidget(blay.count() - 1, ShiftRow(
                n, self._today, self._shift_node))
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
        file_panel = FilePanel(self._files, self._nodes, self._today,
                               batch_id=self._current_batch_id)
        file_panel.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        file_panel.file_toggled.connect(lambda fid, checked: self._toggle_file(fid, checked))
        lay.addWidget(file_panel, 1)
        self._file_panel = file_panel
        return page

    def _set_active_panel(self, tab, sync_tabbar=True):
        """切换右侧活动面板，仅替换宿主内容，不重建整卡（保留甘特/滚动）"""
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
        self._panel_host_layer.addWidget(page)
        if self._on_state:
            self._on_state("active_tab", tab)

    def _on_tab_changed(self, index):
        tab = "shift" if index == 0 else "files"
        if tab == self._tab:
            return
        self._set_active_panel(tab, sync_tabbar=False)

    def _toggle_right(self):
        self._right_collapsed = not self._right_collapsed
        self._apply_right_collapsed()
        if self._on_state:
            self._on_state("right", self._right_collapsed)

    def _apply_right_collapsed(self):
        """收起/展开右栏：收起时仅留一条居中展开按钮，甘特随之占满全宽"""
        c = self._right_collapsed
        self._head_bar.setVisible(not c)
        self._panel_host.setVisible(not c)
        self._action_row_area.setVisible(not c)
        self._reopen_bar.setVisible(c)
        self._right_panel.setFixedWidth(48 if c else 540)

    def _build_action_row(self):
        act_row = QHBoxLayout()
        act_row.setSpacing(8)

        batch_btn = QPushButton("批次管理")
        batch_btn.setObjectName("secondary")
        batch_btn.setCursor(Qt.PointingHandCursor)
        batch_btn.setToolTip("编辑批次：订舱/提单、船期与线路、集装箱、客户货主")
        batch_btn.clicked.connect(self._open_batch)
        act_row.addWidget(batch_btn)

        sched_btn = QPushButton("船期变更")
        sched_btn.setObjectName("secondary")
        sched_btn.setCursor(Qt.PointingHandCursor)
        sched_btn.setToolTip("登记船期变更（A/B/C/D 自动重排 + 影响预览 + 历史）")
        sched_btn.clicked.connect(self._open_schedule_change)
        act_row.addWidget(sched_btn)

        plan_btn = QPushButton("计划日期")
        plan_btn.setObjectName("secondary")
        plan_btn.setCursor(Qt.PointingHandCursor)
        plan_btn.setToolTip("查看当前批次计划日期（§5.3 规则计算）")
        plan_btn.clicked.connect(self._open_plan_date)
        act_row.addWidget(plan_btn)

        cust_btn = QPushButton("客户货主")
        cust_btn.setObjectName("secondary")
        cust_btn.setCursor(Qt.PointingHandCursor)
        cust_btn.setToolTip("客户/货主主档管理与当前批次角色绑定")
        cust_btn.clicked.connect(self._open_customer_master)
        act_row.addWidget(cust_btn)

        cargo_btn = QPushButton("货物台账")
        cargo_btn.setObjectName("secondary")
        cargo_btn.setCursor(Qt.PointingHandCursor)
        cargo_btn.setToolTip("查看 / 编辑当前批次货物清单，登记装箱箱号与封号")
        cargo_btn.clicked.connect(self._open_cargo)
        act_row.addWidget(cargo_btn)

        vessel_btn = QPushButton("班轮 · 船位")
        vessel_btn.setObjectName("secondary")
        vessel_btn.setCursor(Qt.PointingHandCursor)
        vessel_btn.setToolTip("维护当前批次船名/航次，手工登记船位与实际 ETA")
        vessel_btn.clicked.connect(self._open_vessel)
        act_row.addWidget(vessel_btn)
        act_row.addStretch()

        undo_btn = QPushButton("撤销上一步位移")
        undo_btn.setObjectName("ghost")
        undo_btn.setCursor(Qt.PointingHandCursor)
        undo_btn.setIcon(icon("undo", ACCENT, 14))
        undo_btn.setIconSize(QSize(14, 14))
        has_hist = bool(db.get_shift_history(self._project["project_id"], limit=1,
                                             batch_id=self._current_batch_id))
        undo_btn.setEnabled(has_hist)
        undo_btn.clicked.connect(self._undo_shift)
        act_row.addWidget(undo_btn)
        return act_row

    def _stat_chip(self, key, caption, value, color, bg):
        box = QFrame()
        box.setFixedHeight(36)
        box.setStyleSheet(f"background: {bg}; border-radius: 10px;")
        h = QHBoxLayout(box)
        h.setContentsMargins(14, 0, 14, 0)
        h.setSpacing(8)
        cap = QLabel(caption)
        cap.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
        h.addWidget(cap)
        val = QLabel(str(value))
        val.setStyleSheet(f"font-size: 16px; font-weight: 600; color: {color};")
        h.addWidget(val)
        self._chip[key] = val
        return box

    def _update_chips(self):
        """单证勾选等轻量变更后仅刷新统计字牌数值，不重建甘特"""
        if not getattr(self, "_chip", None):
            return
        statuses = compute_all_status(self._nodes, self._today)
        self._chip["active"].setText(str(sum(1 for s in statuses.values() if s == "Active")))
        self._chip["overdue"].setText(str(sum(1 for s in statuses.values() if s == "Overdue")))
        fc = count_files(self._files)
        self._chip["missing"].setText(str(fc["pending"]))
        color = RED if self._cargo_over else GREEN
        self._chip["cargo"].setText("⚠" if self._cargo_over else "✓")
        self._chip["cargo"].setStyleSheet(f"font-size: 16px; font-weight: 600; color: {color};")

    # ── 甘特 ↔ 单证清单 联动 ──

    def _ensure_popover(self):
        if self._pop is None:
            self._pop = NodePopover(self.window())
        return self._pop

    def _node_files(self, nid):
        return [f for f in self._files if f.get("node_id") == nid]

    def _on_gantt_hover(self, node):
        """悬停：弹速览卡；单证标签激活时同步高亮对应分组（收起时不强行切换）"""
        pop = self._ensure_popover()
        if node is None:
            pop.hide_card()
            if self._focused_node is None and self._file_panel is not None:
                self._file_panel.clear_focus()
            return
        pop.show_node(node, self._node_files(node["node_id"]), QCursor.pos())
        if self._file_panel is not None and self._tab == "files":
            self._file_panel.focus_node(node["node_id"])

    def _on_gantt_activate(self, node):
        """单击：切到单证标签并钉住该节点分组；再次点击同一节点取消钉住"""
        nid = node["node_id"]
        pop = self._ensure_popover()
        if self._focused_node == nid:
            # 再次点击同节点 → 取消钉住
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

    # ── 动作 ──

    def _shift_node(self, node_id, sign):
        step = self._step_spin.value() if self._step_spin else 1
        days = sign * step
        try:
            res = apply_shift(self._project["project_id"], node_id, days,
                              batch_id=self._current_batch_id)
        except ShiftError as e:
            QMessageBox.warning(self, "位移被拦截", str(e))
            return
        self._flash_note = f"✓ {res['note']}"
        self._after_change()

    def _undo_shift(self):
        try:
            res = undo_last_shift(self._project["project_id"],
                                  batch_id=self._current_batch_id)
        except ShiftError as e:
            QMessageBox.warning(self, "撤销被拦截", str(e))
            return
        self._flash_note = f"✓ {res['note']}"
        self._after_change()

    def _open_batch(self):
        from ui.batch_dialogs import BatchDialog
        dlg = BatchDialog(self._project["project_id"], self._current_batch_id, self)
        if dlg.exec():
            self._batches = db.get_batches(self._project["project_id"])
            self._load()
            self._after_change()

    def _open_schedule_change(self):
        from ui.batch_dialogs import ScheduleChangeDialog
        dlg = ScheduleChangeDialog(self._project["project_id"], self._current_batch_id, self)
        if dlg.exec():
            self._load()
            self._after_change()

    def _open_plan_date(self):
        from ui.batch_dialogs import PlanDateDialog
        dlg = PlanDateDialog(self._project["project_id"], self._current_batch_id, self)
        dlg.exec()

    def _open_customer_master(self):
        from ui.batch_dialogs import CustomerMasterDialog
        dlg = CustomerMasterDialog(self._current_batch_id, self)
        dlg.exec()

    def _open_cargo(self):
        dlg = CargoDialog(self._project["project_id"], self)
        if dlg.exec():
            self._after_change()

    def _open_vessel(self):
        dlg = VesselDialog(self._project["project_id"], self)
        if dlg.exec():
            note = dlg.result_note()
            if note:
                QMessageBox.information(self, "班轮 · 船位", note)
            self._after_change()

    def _toggle_file(self, file_id, checked):
        """勾选/取消单证 → 落库 + 原地刷新行（绝不重建面板，避免销毁信号发射者）"""
        finfo = db.get_files(self._project["project_id"], batch_id=self._current_batch_id) or []
        doc = next((f for f in finfo if f["file_id"] == file_id), {})
        doc_name = doc.get("doc_name", "")
        node_id = doc.get("node_id")
        if checked:
            # ★ D32 硬阻断：必填客户角色/税号缺失时不得提交
            from services import batches as bsv
            missing = bsv.missing_roles(self._current_batch_id, doc_name)
            proj = self._project
            tax_missing = bsv.importer_tax_missing(self._current_batch_id, proj.get("country"))
            if missing:
                roles_cn = "、".join({"SHIPPER": "发货人", "CONSIGNEE": "收货人",
                                      "IMPORTER": "进口商", "CUSTOMER": "客户"}.get(r, r)
                                     for r in missing)
                if self._file_panel is not None:
                    self._file_panel.update_files(self._files, self._nodes)
                ret = QMessageBox.warning(
                    self, "客户资料缺失",
                    f"「{doc_name}」缺少必填角色：{roles_cn}。\n"
                    f"请先到「客户货主」补录资料并绑定到当前批次后再提交。",
                    QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Ok)
                if ret == QMessageBox.Ok:
                    self._open_customer_master()
                self._flash_note = f"未提交：{doc_name} 缺客户资料"
                return
            if tax_missing and any(k in doc_name for k in
                                   ("出口报关单", "进口证", "进口税费", "非自动进口许可证", "许可证")):
                if self._file_panel is not None:
                    self._file_panel.update_files(self._files, self._nodes)
                QMessageBox.warning(
                    self, "税号缺失",
                    f"目的国要求进口商税号（CNPJ/CPF），请先在「客户货主」为进口商补录税号。")
                self._flash_note = f"未提交：{doc_name} 缺进口商税号"
                return
            db.update_file(file_id, status="submitted", submitted_date=db.today_str())
            _oplog("file_submit", self._project["project_id"], node_id=node_id,
                   subject=doc_name, detail="提交", batch_id=self._current_batch_id,
                   node_key=doc.get("node_key"))
        else:
            db.update_file(file_id, status="pending", submitted_date=None)
            _oplog("file_withdraw", self._project["project_id"], node_id=node_id,
                   subject=doc_name, detail="撤交", batch_id=self._current_batch_id,
                   node_key=doc.get("node_key"))
        # 单证全交清 + 已过节点结束日 → 自动完成（反之回退）
        sync_doc_completion(self._project["project_id"])
        self._load()
        self._update_header()
        self._update_chips()          # 仅刷新统计字牌
        if self._file_panel is not None:
            # ★ 原地增量刷新：只改这一行（及其节点状态），不销毁任何控件
            self._file_panel.update_files(self._files, self._nodes,
                                          batch_id=self._current_batch_id)
            if self._focused_node is not None:
                self._file_panel.focus_node(self._focused_node)
        self._flash_note = f"✓ {'提交' if checked else '撤交'} {doc_name}"


class DashboardPage(QWidget):
    navigate = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._projects = []
        # 折叠状态缓存：本次运行内跨重建/跨页面保持（project_id → 状态字典）
        self._card_state = {}
        self._build()

    def _get_card_state(self, pid):
        return self._card_state.setdefault(
            pid, {"expanded": False, "active_tab": "files", "right": False})

    def _on_card_state(self, pid, key, value):
        """卡片主动上报折叠状态变化 → 存入本页面缓存，供重建时恢复"""
        self._get_card_state(pid)[key] = value

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 24)
        layout.setSpacing(16)

        # 标题行
        title_row = QHBoxLayout()
        title = QLabel("主看板")
        title.setObjectName("title")
        title_row.addWidget(title)
        title_row.addStretch()

        new_btn = QPushButton("新建项目")
        new_btn.setObjectName("primary")
        new_btn.setCursor(Qt.PointingHandCursor)
        new_btn.setIcon(icon("new", "#FFFFFF", 16))
        new_btn.setIconSize(QSize(16, 16))
        new_btn.clicked.connect(lambda: self.navigate.emit("new_project"))
        title_row.addWidget(new_btn)
        layout.addLayout(title_row)

        # 统计条
        self.stat_row = QHBoxLayout()
        self.stat_row.setSpacing(12)
        layout.addLayout(self.stat_row)

        # 项目列表
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)

        self.list_widget = QWidget()
        self.list_layout = QVBoxLayout(self.list_widget)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(12)
        self.list_layout.addStretch()

        scroll.setWidget(self.list_widget)
        layout.addWidget(scroll, stretch=1)

    def _clear_stat(self):
        while self.stat_row.count():
            item = self.stat_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _collect_stats(self):
        today = get_today()
        total_nodes = 0
        total_overdue = 0
        total_missing = 0
        total_today_end = 0
        for proj in self._projects:
            nodes = db.get_nodes(proj["project_id"])
            files = db.get_files(proj["project_id"])
            statuses = compute_all_status(nodes, today)
            total_nodes += len(nodes)
            total_overdue += sum(1 for s in statuses.values() if s == "Overdue")
            total_today_end += sum(1 for n in nodes if compute_node_status(n, today) == "Active"
                                   and _parse(n["plan_end"]) == today)
            fc = count_files(files)
            total_missing += fc["pending"]
        return {"projects": len(self._projects), "nodes": total_nodes,
                "today_end": total_today_end, "overdue": total_overdue,
                "missing": total_missing}

    def _render_stats(self, stats):
        self._clear_stat()
        self.stat_row.addWidget(self._metric("进行中", stats["projects"], ACCENT))
        self.stat_row.addWidget(self._metric("今日截止", stats["today_end"], ORANGE))
        self.stat_row.addWidget(self._metric(
            "逾期", stats["overdue"], RED if stats["overdue"] else TEXT_TERTIARY))
        self.stat_row.addWidget(self._metric(
            "缺单证", stats["missing"], RED if stats["missing"] else TEXT_TERTIARY))
        self.stat_row.addStretch()

    def _refresh_metrics(self):
        """卡片内位移/台账/船位变更后刷新顶部统计（不重建卡片）"""
        stats = self._collect_stats()
        self._render_stats(stats)

    def refresh(self):
        sync_active_projects()               # 进入看板时先同步「必填齐+已过期末」自动完成
        # §D30/T32：全部批次被取消的项目状态为 Cancelled，必须**留在列表并标注「已取消」**，
        # 不能因为只取 Active 而从看板消失。
        self._projects = (db.get_projects_by_status("Active")
                          + db.get_projects_by_status("Cancelled"))
        self._render_stats(self._collect_stats())

        # 清空列表
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self._projects:
            empty_box = QVBoxLayout()
            empty_box.addStretch()
            empty_icon = QLabel()
            empty_icon.setFixedSize(56, 56)
            empty_icon.setPixmap(pixmap("box", "#D7D7DC", 56))
            empty_icon.setAlignment(Qt.AlignCenter)
            empty_box.addWidget(empty_icon, 0, Qt.AlignCenter)
            empty = QLabel("暂无进行中项目")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet(f"font-size: 15px; color: {TEXT_TERTIARY}; margin-top: 8px;")
            empty_box.addWidget(empty)
            hint = QLabel("点击右上角「新建项目」开始跟踪")
            hint.setAlignment(Qt.AlignCenter)
            hint.setStyleSheet(f"font-size: 13px; color: {TEXT_TERTIARY};")
            empty_box.addWidget(hint)
            empty_box.addStretch()
            self.list_layout.insertLayout(0, empty_box)
        else:
            for proj in self._projects:
                pid = proj["project_id"]
                card = ProjectCard(
                    proj, self,
                    on_changed=self._refresh_metrics,
                    init_state=self._get_card_state(pid),
                    on_state=lambda k, v, p=pid: self._on_card_state(p, k, v))
                self.list_layout.insertWidget(self.list_layout.count() - 1, card)

    def _metric(self, caption, value, color):
        box = QFrame()
        box.setObjectName("card")
        box.setFixedSize(150, 74)
        v = QVBoxLayout(box)
        v.setContentsMargins(18, 14, 18, 14)
        v.setSpacing(0)
        num = QLabel(str(value))
        num.setStyleSheet(f"font-size: 26px; font-weight: 600; color: {color};")
        v.addWidget(num)
        cap = QLabel(caption)
        cap.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
        v.addWidget(cap)
        return box
