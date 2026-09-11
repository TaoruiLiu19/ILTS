"""
页面一：启动页（v6.10 精简版）—— 一行统计 + 「项目 · 批次」列表 + 三个入口按钮。

设计取舍：
  · 旧版是 5 张入口卡 + 一条待办条 + 一份完整待办长列表，信息重复且看不到批次；
  · 现在只回答两件事：**现在有多少活要干**（一行统计）与**每个项目的每个批次什么状况**
    （项目下逐批次一行：船期 / 票货提交进度 / 项目级提交进度 / 逾期 / 资源冲突）；
  · 完整待办明细仍在顶栏「今日待办」弹窗与提醒中心，不在这里重复列。
"""

from services.clock import get_today

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QScrollArea
)
from PySide6.QtCore import Qt, Signal

import db
from services import batches as batches_svc
from services import reminder as rmd
from services.file_checklist import count_files
from ui.theme import (
    card_shadow, ACCENT, GREEN, RED, ORANGE, CANCELLED,
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_TERTIARY, BORDER, HAIRLINE, ACCENT_SOFT, GRAY_SOFT
)
from ui.icons import icon


def _batch_conflict_counts(project_id):
    """{batch_id: 冲突条数}——启动页每个批次行标 ⚠ 用；扫描失败不影响页面。"""
    try:
        from services.resource_conflict import scan_batch_conflicts
        out = {}
        for c in scan_batch_conflicts(project_id):
            for bid in c.get("batches") or []:
                out[bid] = out.get(bid, 0) + 1
        return out
    except Exception:
        return {}


class BatchRow(QFrame):
    """批次行：批次号 · 状态 · 船期 · 票货/项目级提交进度 · 逾期/冲突 · 进工作台。"""

    def __init__(self, m, proj_required, proj_pending, conflicts, on_open, parent=None):
        super().__init__(parent)
        self._pid = m["project_id"]
        self._bid = m["batch_id"]
        self._on_open = on_open
        self.setFixedHeight(40)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(
            f"BatchRow {{ background: #FFFFFF; border: 1px solid {HAIRLINE};"
            f" border-radius: 10px; }}"
            f"BatchRow:hover {{ border-color: {ACCENT}; background: {ACCENT_SOFT}; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 12, 0)
        lay.setSpacing(10)

        tone = ACCENT if m["status"] == "running" else (
            RED if (m["overdue"] or conflicts) else TEXT_SECONDARY)
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {tone}; font-size: 12px;")
        lay.addWidget(dot)

        no = QLabel(m["batch_no"] or m["batch_name"])
        no.setStyleSheet(f"font-size: 12px; font-weight: 600; color: {TEXT_PRIMARY};")
        lay.addWidget(no)

        st = QLabel(m["status_cn"])
        st.setStyleSheet(
            f"font-size: 10px; font-weight: 600; color: {tone};"
            f" background: {GRAY_SOFT}; border-radius: 6px; padding: 2px 8px;")
        lay.addWidget(st)

        span = QLabel(f'{m["etd"] or "未设船期"} → {m["eta"] or "—"}')
        span.setStyleSheet(f"font-size: 11px; color: {TEXT_SECONDARY};")
        lay.addWidget(span)
        lay.addStretch()

        done = m["doc_submitted"]
        total = m["doc_total"]
        docs = QLabel(f"票货 {done}/{total}")
        docs.setStyleSheet(
            f"font-size: 11px; color: {GREEN if total and done >= total else TEXT_SECONDARY};")
        lay.addWidget(docs)

        if proj_required:
            p_sub = proj_required - proj_pending
            pj = QLabel(f"项目级 {p_sub}/{proj_required}")
            pj.setStyleSheet(
                f"font-size: 11px; color:"
                f" {GREEN if proj_pending == 0 else TEXT_SECONDARY};")
            lay.addWidget(pj)

        if m["overdue"]:
            od = QLabel(f"逾期 {m['overdue']}")
            od.setStyleSheet(f"font-size: 11px; color: {RED};")
            lay.addWidget(od)
        if conflicts:
            cf = QLabel(f"⚠ 冲突 {conflicts}")
            cf.setStyleSheet(f"font-size: 11px; color: {RED};")
            lay.addWidget(cf)

        go = QLabel("甘特 ›")
        go.setStyleSheet(f"font-size: 11px; color: {ACCENT}; font-weight: 600;")
        lay.addWidget(go)

        self.setToolTip(f'{m["batch_no"]} · 节点 {m["node_done"]}/{m["node_total"]}'
                        f' · 待办 {m["todo"]} · 点击打开该批次甘特工作台')

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._on_open(self._pid, self._bid, "single")
        super().mousePressEvent(event)


class ProjectBlock(QFrame):
    """项目卡：项目名/ID + 逐批次行 + [主看板] [全批次总览]。"""

    def __init__(self, proj, batches, project_files, conflict_map, on_open, on_goto, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        card_shadow(self, blur=18, dy=4, alpha=16)
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 14)
        v.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        name = QLabel(proj.get("project_name") or proj["project_id"])
        name.setStyleSheet(f"font-size: 15px; font-weight: 600; color: {TEXT_PRIMARY};")
        head.addWidget(name)
        sub = QLabel(f'ID · {proj["project_id"][:12]} · {len(batches)} 批次')
        sub.setStyleSheet(f"font-size: 11px; color: {TEXT_TERTIARY};")
        head.addWidget(sub)
        head.addStretch()

        board = QPushButton("主看板")
        board.setObjectName("ghost")
        board.setCursor(Qt.PointingHandCursor)
        board.clicked.connect(lambda: on_goto("dashboard"))
        head.addWidget(board)

        allb = QPushButton("全批次总览")
        allb.setObjectName("ghost")
        allb.setCursor(Qt.PointingHandCursor)
        allb.setToolTip("打开甘特工作台的全批次总览（行=批次 · 时间轴 + 提交状态 + 冲突标注）")
        allb.clicked.connect(lambda: on_open(proj["project_id"], None, "merged"))
        head.addWidget(allb)
        v.addLayout(head)

        proj_req = count_files(project_files)["required"]
        proj_pending = count_files(project_files)["pending"]
        today = get_today()
        for b in batches:
            m = batches_svc.batch_metrics(b["batch_id"], today)
            m["project_id"] = proj["project_id"]
            v.addWidget(BatchRow(m, proj_req, proj_pending,
                                 conflict_map.get(b["batch_id"], 0), on_open))


class HomePage(QWidget):
    navigate = Signal(str)                     # 切页：dashboard / completed / new_project / report
    open_workbench = Signal(str, str, str)     # 打开甘特工作台：(项目, 批次或空, 模式)
    open_todo = Signal()                       # 打开「今日待办」弹窗

    def __init__(self, parent=None):
        super().__init__(parent)
        self._projects = []
        self._stats = {"projects": 0, "batches": 0, "todo": 0,
                       "overdue": 0, "missing": 0}
        self._build()

    # ── 构建 ──

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(32, 32, 32, 28)
        layout.setSpacing(16)

        # 标题 + 入口按钮
        head = QHBoxLayout()
        head.setSpacing(10)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel("国际物流全流程跟踪系统")
        title.setObjectName("title")
        title_box.addWidget(title)
        sub = QLabel("项目 → 批次 → 15 节点全流程跟踪 · 单证齐套 · 资源冲突提示")
        sub.setObjectName("subtitle")
        title_box.addWidget(sub)
        head.addLayout(title_box)
        head.addStretch()

        self.card_new = QPushButton("新建项目")
        self.card_new.setObjectName("primary")
        self.card_new.setCursor(Qt.PointingHandCursor)
        self.card_new.setIcon(icon("new", "#FFFFFF", 16))
        self.card_new.clicked.connect(lambda: self.navigate.emit("new_project"))
        head.addWidget(self.card_new)

        self.card_report = QPushButton("生成报告")
        self.card_report.setObjectName("secondary")
        self.card_report.setCursor(Qt.PointingHandCursor)
        self.card_report.clicked.connect(lambda: self.navigate.emit("report"))
        head.addWidget(self.card_report)

        self.card_completed = QPushButton("已完成")
        self.card_completed.setObjectName("secondary")
        self.card_completed.setCursor(Qt.PointingHandCursor)
        self.card_completed.clicked.connect(lambda: self.navigate.emit("completed"))
        head.addWidget(self.card_completed)
        layout.addLayout(head)

        # 一行统计
        self.stat_row = QHBoxLayout()
        self.stat_row.setSpacing(12)
        layout.addLayout(self.stat_row)

        # 项目 · 批次
        sec = QHBoxLayout()
        sec_title = QLabel("项目 · 批次")
        sec_title.setObjectName("section")
        sec.addWidget(sec_title)
        sec.addStretch()
        self.cancelled_label = QLabel("")
        self.cancelled_label.setStyleSheet(f"font-size: 11px; color: {TEXT_TERTIARY};")
        sec.addWidget(self.cancelled_label)
        layout.addLayout(sec)

        self.projects_box = QVBoxLayout()
        self.projects_box.setSpacing(12)
        layout.addLayout(self.projects_box)

        layout.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll)

    def _metric(self, caption, value, color, tip="", on_click=None):
        box = QFrame()
        box.setObjectName("card")
        box.setFixedSize(148, 70)
        if on_click:
            box.setCursor(Qt.PointingHandCursor)
            box.mousePressEvent = lambda e: on_click()
        if tip:
            box.setToolTip(tip)
        v = QVBoxLayout(box)
        v.setContentsMargins(16, 12, 16, 12)
        v.setSpacing(0)
        num = QLabel(str(value))
        num.setStyleSheet(f"font-size: 24px; font-weight: 600; color: {color};")
        v.addWidget(num)
        cap = QLabel(caption)
        cap.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
        v.addWidget(cap)
        return box

    # ── 刷新 ──

    def refresh(self):
        from services.node_status import sync_active_projects
        sync_active_projects()          # 与看板同一口径：先落定自动完成
        today = get_today()
        self._projects = (db.get_projects_by_status("Active")
                          + db.get_projects_by_status("Cancelled"))

        # 统计
        n_batch = 0
        n_overdue = 0
        n_missing = 0
        for p in self._projects:
            pid = p["project_id"]
            n_missing += count_files(db.get_project_files(pid))["pending"]
            for b in db.get_batches(pid):
                n_batch += 1
                nodes = db.get_nodes_by_batch(b["batch_id"])
                files = db.get_files_by_batch(b["batch_id"])
                n_overdue += sum(1 for n in nodes
                                 if rmd and _status_overdue(n, today))
                n_missing += count_files(files)["pending"]
        n_todo = rmd.badge_count(today=today)
        self._stats = {"projects": len(self._projects), "batches": n_batch,
                       "todo": n_todo, "overdue": n_overdue, "missing": n_missing}
        self._render_stats()

        # 项目 · 批次
        while self.projects_box.count():
            item = self.projects_box.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()

        if not self._projects:
            empty = QLabel("暂无项目，点右上角「新建项目」开始跟踪")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet(
                f"font-size: 14px; color: {TEXT_TERTIARY}; padding: 48px 0;")
            self.projects_box.addWidget(empty)
        else:
            for p in self._projects:
                pid = p["project_id"]
                batches = db.get_batches(pid)
                self.projects_box.addWidget(ProjectBlock(
                    p, batches, db.get_project_files(pid),
                    _batch_conflict_counts(pid),
                    on_open=lambda _pid, _bid, _mode: self.open_workbench.emit(_pid, _bid, _mode),
                    on_goto=self.navigate.emit))

        n_cancelled = sum(1 for p in self._projects if p.get("status") == "Cancelled")
        n_done = db.count_projects("Completed")
        bits = [f"已完成 {n_done} 个项目（进「已完成」页）"]
        if n_cancelled:
            bits.append(f"已取消 {n_cancelled} 个（全部批次取消，留在上方列表并标注）")
        self.cancelled_label.setText(" · ".join(bits))

    def _render_stats(self):
        while self.stat_row.count():
            item = self.stat_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        s = self._stats
        self.stat_row.addWidget(self._metric(
            "进行中项目", s["projects"], ACCENT,
            "点击进入主看板", lambda: self.navigate.emit("dashboard")))
        self.stat_row.addWidget(self._metric(
            "启用中批次", s["batches"], ACCENT,
            "draft / ready / running 状态的批次（不含已完成、已取消）"))
        self.stat_row.addWidget(self._metric(
            "今日待办", s["todo"], RED if s["todo"] else GREEN,
            "点击查看今日待办明细（口径 = 各启用中批次待办合计）",
            self.open_todo.emit))
        self.stat_row.addWidget(self._metric(
            "逾期节点", s["overdue"], RED if s["overdue"] else TEXT_TERTIARY,
            "所有启用中批次里已过计划结束日、仍未完成的节点数"))
        self.stat_row.addWidget(self._metric(
            "缺单证", s["missing"], RED if s["missing"] else TEXT_TERTIARY,
            "必填单证未提交数：批次级按批次累加 + 项目级只算一次"))
        self.stat_row.addStretch()


def _status_overdue(node, today):
    from services.node_status import compute_node_status
    return compute_node_status(node, today) == "Overdue"
