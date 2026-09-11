"""
页面二：主看板 —— 摘要卡片（批次条 + 三栏 + 三区进度）+ 「甘特工作台」入口

v6.9 起卡片**不再折叠**：
  · 甘特、批次列表、动态调整、单证清单全部迁到非模态「甘特工作台」窗口
    （ui/gantt_workbench.py），卡片只回答"这个项目现在什么状况"；
  · 好处：卡片高度恒定（不再随批次节点数从 180px 涨到 1100px），
    而甘特在独立窗口里可以最大化、可以多批次合并、可以导出图片。
"""

from datetime import date

from services.clock import get_today

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QScrollArea, QMessageBox, QSizePolicy, QComboBox
)
from PySide6.QtCore import Qt, Signal, QSize

import db
from config import get_country, get_port
from services import batches as batches_svc
from services.node_status import (
    compute_node_status, compute_all_status, get_current_node,
    sync_doc_completion, sync_active_projects
)
from services.file_checklist import count_files, pending_required
from services.cargo_check import summary
from ui.theme import (
    card_shadow, ACCENT, GREEN, RED, ORANGE, TEXT_PRIMARY,
    TEXT_SECONDARY, TEXT_TERTIARY, BORDER, ACCENT_SOFT
)
from ui.icons import icon, pixmap
from ui.widgets.mini_bar import MiniBar


def _parse(s):
    if isinstance(s, date):
        return s
    if not s:
        return None
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))


class ProjectCard(QFrame):
    """项目摘要卡（恒定高度）：批次条 + 三栏信息；甘特与操作在「甘特工作台」。"""

    def __init__(self, project, parent=None, on_changed=None, on_open_workbench=None,
                 on_workbench_sync=None):
        super().__init__(parent)
        self._project = project
        self._today = get_today()
        self._on_changed = on_changed
        self._on_open_workbench = on_open_workbench
        self._on_workbench_sync = on_workbench_sync
        # 批次与当前上下文
        self._batches = []
        self._current_batch_id = None
        self._nodes = []
        self._files = []
        self._vessel = None
        self._cargo = []
        self._cargo_count = 0
        self._cargo_over = 0
        self._load()
        self.setObjectName("card")
        self._card_shadow = card_shadow(self, blur=18, dy=4, alpha=18)
        self._build()

    # ── 数据 ──

    def _load(self):
        pid = self._project["project_id"]
        # 刷新项目行：current_batch_id / status 可能已被工作台等改写，
        # 用陈旧内存值会导致「切了批次又跳回旧批次」。
        fresh = db.get_project(pid)
        if fresh:
            self._project = fresh
        self._today = get_today()
        self._batches = db.get_batches(pid)
        if not self._batches:
            from db import create_default_batch
            create_default_batch(pid)
            self._batches = db.get_batches(pid)
        cur = self._project.get("current_batch_id")
        found = next((b for b in self._batches if b["batch_id"] == cur), None)
        self._current_batch_id = (found or self._batches[-1])["batch_id"]

        self._nodes = db.get_nodes_by_batch(self._current_batch_id)
        self._files = db.get_files_by_batch(self._current_batch_id)
        # 项目级单证（项目日报等）：同项目一份，缺证只算一次（不随批次翻倍）
        self._project_files = db.get_project_files(pid) or []
        self._vessel = db.get_vessel(pid, self._current_batch_id)
        cargo = db.get_cargo_items(pid, self._current_batch_id)
        self._cargo = cargo
        cs = summary(cargo) if cargo else {"count": 0, "over": 0}
        self._cargo_count = cs["count"]
        self._cargo_over = cs["over"]

    def _after_change(self):
        # 「必填齐 + 已过结束日」同步节点完成状态
        sync_doc_completion(self._project["project_id"])
        self._load()
        self._update_header()
        if self._on_changed:
            self._on_changed()

    # ── 构建 ──

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header_frame = QFrame()
        # 头部高度交给内容决定（批次条 48 + 三栏行）；垂直策略取 Maximum：
        # 只吃自己需要的高度，绝不吸收外层多余空间（否则卡片会被列表布局撑高）。
        header_frame.setMinimumHeight(178)
        header_frame.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        header_layout = QHBoxLayout(header_frame)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(0)

        # 状态竖条
        self._bar = QFrame()
        self._bar.setFixedWidth(4)
        header_layout.addWidget(self._bar)

        head_body = QFrame()
        hb = QVBoxLayout(head_body)
        hb.setContentsMargins(0, 0, 0, 0)
        hb.setSpacing(0)
        hb.addWidget(self._build_batch_bar())

        cols = QHBoxLayout()
        cols.setContentsMargins(0, 0, 0, 0)
        cols.setSpacing(0)
        cols.addWidget(self._build_header_left())
        cols.addWidget(self._build_header_mid(), stretch=2)
        cols.addWidget(self._build_header_right())
        hb.addLayout(cols, stretch=1)

        header_layout.addWidget(head_body, stretch=1)
        layout.addWidget(header_frame)

        self._update_header()

    def _build_batch_bar(self):
        """批次条：整行贯通。批次下拉给足 36px 高 / 300px 宽（不再被压缩）。"""
        bar = QFrame()
        bar.setObjectName("batchBar")
        bar.setFixedHeight(48)
        # 贴卡片顶部：右上角跟随卡片 16px 圆角（Qt 不裁剪子控件，方角会溢出圆角）
        bar.setStyleSheet(
            f"QFrame#batchBar {{ background: #FAFAFC; border-bottom: 1px solid {BORDER};"
            f" border-top-right-radius: 15px; }}")
        h = QHBoxLayout(bar)
        h.setContentsMargins(20, 6, 20, 6)
        h.setSpacing(10)

        cap = QLabel("批次")
        cap.setStyleSheet(f"font-size: 12px; color: {TEXT_TERTIARY}; font-weight: 600;")
        h.addWidget(cap)

        self.batch_combo = QComboBox()
        self.batch_combo.setObjectName("batchCombo")
        self.batch_combo.setCursor(Qt.PointingHandCursor)
        # QSS 给 QComboBox 的 padding 是 9px/上下 + 14px 字号 → 需要 36px；
        # 设置最小高度后布局无法再把它压扁，批次号才能完整显示。
        self.batch_combo.setMinimumHeight(36)
        self.batch_combo.setMinimumWidth(300)
        self.batch_combo.setMaximumWidth(520)
        self.batch_combo.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.batch_combo.setToolTip("切换当前批次（批次号 · 名称 · 状态）")
        self.batch_combo.currentIndexChanged.connect(self._on_batch_changed)
        h.addWidget(self.batch_combo)

        self.batch_status = QLabel("")
        h.addWidget(self.batch_status)
        h.addStretch()

        self.batch_summary = QLabel("")
        self.batch_summary.setObjectName("batchSummary")
        h.addWidget(self.batch_summary)

        self.workbench_btn = QPushButton("甘特工作台")
        self.workbench_btn.setObjectName("ghost")
        self.workbench_btn.setCursor(Qt.PointingHandCursor)
        self.workbench_btn.setToolTip("打开独立窗口：单批次甘特 / 多批次合并甘特 + 单证清单 + 推迟提前")
        self.workbench_btn.clicked.connect(
            lambda: self._open_workbench("single"))
        h.addWidget(self.workbench_btn)

        self.new_batch_btn = QPushButton("新增批次")
        self.new_batch_btn.setObjectName("ghost")
        self.new_batch_btn.setCursor(Qt.PointingHandCursor)
        self.new_batch_btn.setToolTip("新建空白批次；在「批次管理」确认 ETD/ETA 保存后"
                                      "自动按模板生成计划节点与单证清单")
        self.new_batch_btn.clicked.connect(self._open_new_batch)
        h.addWidget(self.new_batch_btn)

        self.copy_batch_btn = QPushButton("复制批次")
        self.copy_batch_btn.setObjectName("ghost")
        self.copy_batch_btn.setCursor(Qt.PointingHandCursor)
        self.copy_batch_btn.setToolTip("复制当前批次的线路/节点/单证（状态清零）")
        self.copy_batch_btn.clicked.connect(self._copy_current_batch)
        h.addWidget(self.copy_batch_btn)
        return bar

    def _build_header_left(self):
        """左栏：项目名 / ID / 出口港 / 班轮"""
        left = QFrame()
        left.setFixedWidth(256)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(20, 12, 12, 12)
        left_layout.setSpacing(4)

        country_tmpl = get_country(self._project["country"])
        flag = country_tmpl.get("flag", "")
        status_tag = " · 已取消" if self._project.get("status") == "Cancelled" else ""
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
        return left

    def _build_header_mid(self):
        """中栏：当前节点 + 三区进度条"""
        mid = QFrame()
        mid_layout = QVBoxLayout(mid)
        mid_layout.setContentsMargins(12, 12, 12, 12)
        mid_layout.setSpacing(6)

        self.cur_label = QLabel("")
        self.cur_label.setStyleSheet(
            f"font-size: 13px; color: {TEXT_PRIMARY}; font-weight: 600;")
        mid_layout.addWidget(self.cur_label)

        self.mini_bar = MiniBar(self._nodes, self._today, self)
        mid_layout.addWidget(self.mini_bar)
        mid_layout.addStretch()
        return mid

    def _build_header_right(self):
        """右栏：ETD / 进度 / 货物 / 位移留痕 + 工作台入口"""
        right = QFrame()
        right.setFixedWidth(310)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(12, 12, 20, 12)
        right_layout.setSpacing(4)

        self.etd_label = QLabel("")
        self.etd_label.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
        right_layout.addWidget(self.etd_label)

        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
        right_layout.addWidget(self.progress_label)

        self.cargo_label = QLabel("")
        self.cargo_label.setStyleSheet("font-size: 12px;")
        right_layout.addWidget(self.cargo_label)

        self.history_label = QLabel("")
        self.history_label.setStyleSheet(f"font-size: 10px; color: {TEXT_TERTIARY};")
        right_layout.addWidget(self.history_label)
        right_layout.addStretch()

        self.open_btn = QPushButton("打开工作台")
        self.open_btn.setObjectName("ghost")
        self.open_btn.setCursor(Qt.PointingHandCursor)
        self.open_btn.setIcon(icon("chevron_right", ACCENT, 14))
        self.open_btn.setIconSize(QSize(14, 14))
        self.open_btn.setToolTip("在独立窗口中查看甘特、勾单证、推迟/提前（可多批次合并）")
        self.open_btn.clicked.connect(lambda: self._open_workbench("single"))
        right_layout.addWidget(self.open_btn, 0, Qt.AlignRight)
        return right

    # ── 头部刷新 ──

    def _update_header(self):
        statuses = compute_all_status(self._nodes, self._today)
        has_overdue = any(s == "Overdue" for s in statuses.values())
        has_active = any(s == "Active" for s in statuses.values())
        bar_color = RED if has_overdue else (ORANGE if has_active else GREEN)
        self._bar.setStyleSheet(f"background: {bar_color}; border-top-left-radius: 15px;")

        cur = db.get_batch(self._current_batch_id)
        if getattr(self, "batch_combo", None) is not None:
            b = self.batch_combo.blockSignals(True)
            self.batch_combo.clear()
            longest = ""
            for bt in self._batches:
                no = bt["batch_no"] or bt["batch_name"]
                nm = bt.get("batch_name") or ""
                label = no if (not nm or nm in no) else f"{no} · {nm}"
                if bt["batch_id"] == self._current_batch_id:
                    label = f"● {label}"
                self.batch_combo.addItem(label, bt["batch_id"])
                if len(label) > len(longest):
                    longest = label
            self.batch_combo.setCurrentIndex(max(0, self._current_batch_index()))
            self.batch_combo.blockSignals(b)
            self.batch_combo.setToolTip(longest)
            st = batches_svc.state_label((cur or {}).get("status") or "draft")
            tone = {"进行中": ACCENT, "逾期": RED}.get(st, TEXT_SECONDARY)
            self.batch_status.setText(st)
            self.batch_status.setStyleSheet(
                f"font-size: 11px; font-weight: 600; color: {tone};"
                f" background: {ACCENT_SOFT}; border-radius: 8px; padding: 5px 10px;")
        self._refresh_batch_summary()

        route = db.get_route(self._current_batch_id)
        port = get_port((route or {}).get("export_port") or self._project.get("export_port"))
        self.port_label.setText(f"出口港 · {port['name']}" if port else "通用方案")

        if self._vessel:
            vname = self._vessel.get("vessel_name") or ""
            vvoy = self._vessel.get("voyage") or ""
            parts = []
            if vname:
                parts.append(f"🚢 {vname}")
            if vvoy:
                parts.append(f"航次 {vvoy}")
            self.vessel_label.setText(" · ".join(parts) if parts else "已登记班轮")
        else:
            self.vessel_label.setText("")

        current = get_current_node(self._nodes, self._today)
        self.cur_label.setText(
            f"当前 · 节点{current['node_id']} {current['node_name']}" if current else "全部完成")

        etd = (route or {}).get("etd") if route else self._project.get("etd")
        days_to_etd = (_parse(etd) - self._today).days if etd else 0
        self.etd_label.setText(
            f"ETD {etd} · 距 {'离港' if days_to_etd >= 0 else '已离港'} {abs(days_to_etd)} 天"
            if etd else "未设船期")

        done_count = sum(1 for s in statuses.values() if s == "Done")
        # 缺单证口径 = 批次级未交必填 + 项目级未交必填（项目级同项目只算一次）
        pending = pending_required(self._files, self._project_files)
        self.progress_label.setText(
            f"进度 {done_count}/{len(self._nodes)} 节点 · 缺单证 {pending}")

        if self._cargo_count:
            color = RED if self._cargo_over else TEXT_SECONDARY
            over_txt = f" · 超限 {self._cargo_over}" if self._cargo_over else ""
            self.cargo_label.setText(f"货物 {self._cargo_count} 项{over_txt}（台账）")
            self.cargo_label.setStyleSheet(f"font-size: 12px; color: {color};")
        else:
            self.cargo_label.setText("")
            self.cargo_label.setStyleSheet("")

        hist = db.get_shift_history(self._project["project_id"], limit=1,
                                    batch_id=self._current_batch_id)
        if hist:
            h = hist[0]
            self.history_label.setText(
                f"位移留痕：节点{h['node_id']} {'推迟' if h['delta'] > 0 else '提前'} "
                f"{abs(h['delta'])} 天")
        else:
            self.history_label.setText("")

        if getattr(self, "mini_bar", None):
            self.mini_bar.update_data(self._nodes, self._today)

        if getattr(self, "workbench_btn", None) is not None:
            self.workbench_btn.setToolTip(
                f"打开甘特工作台（当前批次 {cur.get('batch_no') if cur else '-'}）："
                f"单批次甘特 / 多批次合并甘特 + 单证清单 + 推迟提前")

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
        self._notify_workbench()

    def _refresh_batch_summary(self):
        if getattr(self, "batch_summary", None) is None:
            return
        s = batches_svc.project_risk_summary(self._project["project_id"], self._today)
        bits = [f"{s['n_batch']} 批次"]
        if s["overdue"]:
            bits.append(f"逾期节点 {s['overdue']}")
        if s["todo"]:
            bits.append(f"待办单证 {s['todo']}")
        if not s["overdue"] and not s["todo"]:
            bits.append("无风险")
        self.batch_summary.setText(" · ".join(bits))
        tone = RED if (s["overdue"] or s["todo"]) else GREEN
        self.batch_summary.setStyleSheet(f"font-size: 11px; font-weight: 600; color: {tone};")

    # ── 批次动作 ──

    def _open_workbench(self, mode="single"):
        if self._on_open_workbench:
            self._on_open_workbench(self._project["project_id"], self._current_batch_id, mode)

    def _notify_workbench(self):
        """卡片侧改了当前批次/新增批次 → 工作台若开着同一项目则跟随刷新。"""
        if self._on_workbench_sync:
            self._on_workbench_sync(self._project["project_id"], self._current_batch_id)

    def _open_new_batch(self):
        """§12.1 新增批次：自动编号建批次后打开批次管理补齐线路/船期。

        新增批次本身是「空批次」（无线路/节点/单证）；在批次管理中确认 ETD/ETA 保存后，
        `services.batches.ensure_batch_nodes` 会按 15 节点模板补齐节点与单证，甘特才有内容。
        """
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
        note = ""
        if dlg.exec():
            note = getattr(dlg, "auto_note", "") or ""
        self._batches = db.get_batches(self._project["project_id"])
        self._load()
        self._update_header()
        self._after_change()
        self._notify_workbench()
        if note:
            QMessageBox.information(
                self, "新增批次完成", f"批次 {nb['batch_no']} 已建立。\n{note}")
        elif not self._nodes:
            QMessageBox.information(
                self, "新增批次完成",
                f"批次 {nb['batch_no']} 已建立，但尚无计划节点。\n"
                f"请在「批次管理」确认 ETD / ETA 并保存，系统将按 15 节点标准模板"
                f"自动生成计划节点与单证清单；也可在工作台点「补齐计划节点」。")

    def _copy_current_batch(self):
        """§8 复制批次：复制批次信息 + 线路 + 冻结模板快照（不带状态/日志）。"""
        if not db.get_batch(self._current_batch_id):
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
        self._notify_workbench()
        QMessageBox.information(
            self, "复制批次完成",
            f"已复制为 {nb['batch_no']}（含线路、15 节点与单证清单，状态清零）。")


class DashboardPage(QWidget):
    navigate = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._projects = []
        self._workbench = None
        self._build()

    # ── 甘特工作台 ──

    def open_workbench(self, project_id=None, batch_id=None, mode="single"):
        """打开（或切换）非模态甘特工作台；无 project_id 时取第一个进行中项目。"""
        from ui.gantt_workbench import GanttWorkbenchWindow
        if project_id is None:
            projects = (db.get_projects_by_status("Active")
                        or db.get_projects_by_status("Completed"))
            if not projects:
                QMessageBox.information(self, "甘特工作台", "还没有项目，请先「新建项目」。")
                return None
            project_id = projects[0]["project_id"]
        if self._workbench is None:
            self._workbench = GanttWorkbenchWindow(
                self, on_changed=self._on_workbench_changed)
        self._workbench.open_for(project_id, batch_id, mode)
        return self._workbench

    def _on_workbench_changed(self, project_id):
        """工作台改了数据（勾单证/位移/切批次）→ 刷新对应卡片摘要与顶部统计。"""
        for i in range(self.list_layout.count()):
            w = self.list_layout.itemAt(i).widget()
            if w is None or w.__class__.__name__ != "ProjectCard":
                continue
            if (getattr(w, "_project", {}) or {}).get("project_id") == project_id:
                w._load()
                w._update_header()
        self._refresh_metrics()

    def _sync_workbench(self, project_id, batch_id=None):
        """卡片侧切了批次/新增批次 → 同项目的工作台跟着切（不同项目则不动）。"""
        wb = self._workbench
        if wb is None or not wb.isVisible() or wb._project_id != project_id:
            return
        if batch_id:
            wb.sync_batch(batch_id)
        wb.refresh()

    # ── 构建 ──

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 24)
        layout.setSpacing(16)

        title_row = QHBoxLayout()
        title = QLabel("主看板")
        title.setObjectName("title")
        title_row.addWidget(title)
        title_row.addStretch()

        merge_btn = QPushButton("多批次甘特")
        merge_btn.setObjectName("secondary")
        merge_btn.setCursor(Qt.PointingHandCursor)
        merge_btn.setToolTip("打开甘特工作台的合并视图：真实日历轴，行=节点、每批次一条色带，"
                             "并标注资源冲突")
        merge_btn.clicked.connect(lambda: self.open_workbench(None, None, "merged"))
        title_row.addWidget(merge_btn)

        new_btn = QPushButton("新建项目")
        new_btn.setObjectName("primary")
        new_btn.setCursor(Qt.PointingHandCursor)
        new_btn.setIcon(icon("new", "#FFFFFF", 16))
        new_btn.setIconSize(QSize(16, 16))
        new_btn.clicked.connect(lambda: self.navigate.emit("new_project"))
        title_row.addWidget(new_btn)
        layout.addLayout(title_row)

        self.stat_row = QHBoxLayout()
        self.stat_row.setSpacing(12)
        layout.addLayout(self.stat_row)

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
        """顶部统计：按「项目内**全部启用中批次**」聚合（与 §10.5 徽标、启动页同口径）。

        旧实现只取每个项目的「当前批次」（`db.get_files(proj_id)` 无 batch_id），
        多批次下会漏算其它批次；缺单证一并计入**项目级单证**（同项目一份，只算一次）。
        """
        today = get_today()
        total_nodes = 0
        total_overdue = 0
        total_missing = 0
        total_today_end = 0
        for proj in self._projects:
            pid = proj["project_id"]
            for b in db.get_batches(pid):
                nodes = db.get_nodes_by_batch(b["batch_id"])
                files = db.get_files_by_batch(b["batch_id"])
                statuses = compute_all_status(nodes, today)
                total_nodes += len(nodes)
                total_overdue += sum(1 for s in statuses.values() if s == "Overdue")
                total_today_end += sum(1 for n in nodes
                                       if compute_node_status(n, today) == "Active"
                                       and _parse(n["plan_end"]) == today)
                total_missing += count_files(files)["pending"]
            total_missing += count_files(db.get_project_files(pid))["pending"]
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
        """卡片内变更后刷新顶部统计（不重建卡片）"""
        self._render_stats(self._collect_stats())

    def refresh(self):
        sync_active_projects()
        # §D30/T32：全部批次被取消的项目状态为 Cancelled，必须留在列表并标注「已取消」。
        self._projects = (db.get_projects_by_status("Active")
                          + db.get_projects_by_status("Cancelled"))
        self._render_stats(self._collect_stats())

        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()

        if not self._projects:
            self._render_empty()
        else:
            for proj in self._projects:
                card = ProjectCard(
                    proj, self,
                    on_changed=self._refresh_metrics,
                    on_open_workbench=self.open_workbench,
                    on_workbench_sync=self._sync_workbench)
                self.list_layout.insertWidget(self.list_layout.count() - 1, card)

        # 工作台开着 → 跟随刷新（项目/批次集合可能已变）
        if self._workbench is not None and self._workbench.isVisible():
            self._workbench.refresh()

    def _render_empty(self):
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
