"""§12.4 提醒中心：按**批次**分组，含免箱期倒计时 / 到货通知 / 申报截止 / 依赖等待。

定位（与「今日待办」弹窗的分工）：
  · `ui/dialogs.py:TodayTodoDialog` = 启动自动弹出的「今日待办」（按 P0/P1/P2 分级）。
  · `ReminderCenterDialog`（本文件）= 常驻入口的「提醒中心」：按**批次**分组，
    固定四节 + 其他，并可按「项目 → 批次 → 节点」下钻（§10.5 三级）。
两者数据同源（`services.reminder`），因此口径一致。
"""

from PySide6.QtCore import Qt, QSize
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton, QScrollArea,
    QWidget, QCheckBox, QTabWidget,
)

import db
from services import reminder as R
from ui.theme import (ACCENT, ACCENT_SOFT, BORDER, CARD, GRAY_SOFT, GREEN,
                      HAIRLINE, ORANGE, RED, TEXT_PRIMARY, TEXT_SECONDARY,
                      TEXT_TERTIARY)

# 提醒类型 → 中文（展示用；未知类型回退原 key）
TYPE_CN = {
    R.T_NODE_OVERDUE: "节点逾期",
    R.T_NODE_END_TODAY: "节点今日截止",
    R.T_NODE_START_TODAY: "节点今日启动",
    R.T_FILE_LATE: "单证超建议日",
    R.T_FILE_MISSING_ACTIVE: "单证缺失",
    R.T_FILE_NEAR: "单证临近",
    R.T_DETENTION: "免箱期倒计时",
    R.T_DEMURRAGE: "免堆期倒计时",
    R.T_CONTAINER_RETURN: "还箱截止",
    R.T_ARRIVAL_NOTICE: "到货通知",
    R.T_INSURANCE: "保险到期",
    R.T_DECLARATION: "申报截止",
    R.T_DEP_WAITING: "依赖等待",
}

# 分节 → 图标
SECTION_ICON = {
    "DETENTION": "clock_hist",
    "ARRIVAL": "anchor",
    "DECLARATION": "doc",
    "DEPENDENCY": "route",
    "OTHER": "bell",
}

LEVEL_COLOR = {"P0": RED, "P1": ORANGE, "P2": ACCENT}
LEVEL_LABEL = {"P0": "需处理", "P1": "进行中", "P2": "今日启动"}
# 固定四节的强调色
SECTION_ACCENT = {
    "DETENTION": RED,
    "ARRIVAL": ACCENT,
    "DECLARATION": ORANGE,
    "DEPENDENCY": "#8E8E93",
    "OTHER": TEXT_SECONDARY,
}


def _icon(name, color, size=15):
    try:
        from ui.icons import icon as _ic
        return _ic(name, color, size)
    except Exception:
        return None


class ReminderItemRow(QFrame):
    """提醒中心一行：优先级点 + 消息 + 类型徽标 + 依据/截止。"""

    def __init__(self, item, parent=None):
        super().__init__(parent)
        self._item = item
        color = LEVEL_COLOR.get(item.get("level"), TEXT_TERTIARY)
        self.setObjectName("reminderRow")
        self.setStyleSheet(
            f"#reminderRow {{ background: {CARD}; border: 1px solid {HAIRLINE};"
            f" border-radius: 10px; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(9)

        dot = QLabel()
        dot.setFixedSize(9, 9)
        dot.setStyleSheet(f"background: {color}; border-radius: 4px;")
        lay.addWidget(dot, 0, Qt.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(2)
        msg = QLabel(item.get("msg") or "")
        msg.setWordWrap(True)
        msg.setStyleSheet(f"font-size: 13px; color: {TEXT_PRIMARY};")
        col.addWidget(msg)

        meta = []
        lvl = item.get("level")
        meta.append(f"{lvl} {LEVEL_LABEL.get(lvl, '')}".strip())
        tcn = TYPE_CN.get(item.get("type"), item.get("type") or "")
        if tcn:
            meta.append(tcn)
        if item.get("deadline"):
            meta.append(f"截止 {item['deadline']}")
        elif item.get("due_date"):
            meta.append(f"截至 {item['due_date']}")
        if item.get("baseline_source"):
            meta.append(f"依据：{item['baseline_source']}")
        if item.get("match_source"):
            meta.append(f"提前量来源：{item['match_source']}")
        if item.get("pinned"):
            meta.append("置顶")
        mlab = QLabel(" · ".join(meta))
        mlab.setWordWrap(True)
        mlab.setStyleSheet(f"font-size: 10px; color: {TEXT_TERTIARY};")
        col.addWidget(mlab)
        lay.addLayout(col, 1)

    def item(self):
        return self._item


class BatchGroup(QFrame):
    """单个批次分组卡（§12.4：按批次分组）。"""

    def __init__(self, group, parent=None):
        super().__init__(parent)
        self._group = group
        self.setObjectName("batchGroup")
        self.setStyleSheet(
            f"#batchGroup {{ background: {CARD}; border: 1px solid {BORDER};"
            f" border-radius: 12px; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel(f"{group.get('project_name') or ''} · {group.get('batch_no') or ''}")
        title.setStyleSheet(
            f"font-size: 14px; font-weight: 600; color: {TEXT_PRIMARY};")
        head.addWidget(title)
        st = QLabel(_status_cn(group.get("batch_status")))
        st.setStyleSheet(
            f"font-size: 10px; color: {TEXT_SECONDARY}; background: {GRAY_SOFT};"
            " border-radius: 6px; padding: 2px 7px;")
        head.addWidget(st)
        if group.get("planned_date"):
            pd = QLabel(f"发运 {group['planned_date']}")
            pd.setStyleSheet(f"font-size: 10px; color: {TEXT_TERTIARY};")
            head.addWidget(pd)
        head.addStretch()
        total = QLabel(f"共 {group.get('total', 0)} 项")
        total.setStyleSheet(
            f"font-size: 11px; font-weight: 600; color: {ACCENT};"
            f" background: {ACCENT_SOFT}; border-radius: 8px; padding: 2px 9px;")
        head.addWidget(total)
        lay.addLayout(head)

        for sec in group.get("sections") or []:
            if not sec.get("count"):
                continue
            lay.addWidget(self._section_block(sec))

    def _section_block(self, sec):
        box = QFrame()
        box.setStyleSheet("background: transparent;")
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(5)

        color = SECTION_ACCENT.get(sec["key"], TEXT_SECONDARY)
        head = QHBoxLayout()
        head.setSpacing(6)
        ico = QLabel()
        pix = _icon(SECTION_ICON.get(sec["key"], "bell"), color, 14)
        if pix is not None:
            ico.setPixmap(pix.pixmap(14, 14))
        head.addWidget(ico)
        lbl = QLabel(sec["label"])
        lbl.setStyleSheet(f"font-size: 12px; font-weight: 600; color: {color};")
        head.addWidget(lbl)
        cnt = QLabel(str(sec["count"]))
        cnt.setStyleSheet(
            f"font-size: 10px; font-weight: 600; color: #FFFFFF; background: {color};"
            " border-radius: 7px; padding: 1px 7px;")
        head.addWidget(cnt)
        head.addStretch()
        v.addLayout(head)
        for it in sec["items"]:
            v.addWidget(ReminderItemRow(it))
        return box

    def group(self):
        return self._group


def _status_cn(status):
    return {"draft": "草稿", "ready": "已就绪", "running": "进行中",
            "completed": "待确认完成", "closed": "已完成",
            "cancelled": "已取消"}.get(status, status or "")


class _ThreeLevelPage(QWidget):
    """三级汇总页：项目 → 批次 → 节点（§10.5）。"""

    def __init__(self, data, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        if not data["projects"]:
            empty = QLabel("启用中批次暂无待办")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet(f"font-size: 13px; color: {TEXT_TERTIARY}; padding: 30px;")
            lay.addWidget(empty)
            return

        for proj in data["projects"]:
            card = QFrame()
            card.setObjectName("threeCard")
            card.setStyleSheet(
                f"#threeCard {{ background: {CARD}; border: 1px solid {BORDER};"
                f" border-radius: 12px; }}")
            v = QVBoxLayout(card)
            v.setContentsMargins(14, 12, 14, 12)
            v.setSpacing(7)

            head = QHBoxLayout()
            ptitle = QLabel(f"{proj['project_name']}"
                            f"（{proj['enabled_batch_count']} 启用批次"
                            f" / 共 {proj['batch_count']} 批次）")
            ptitle.setStyleSheet(
                f"font-size: 14px; font-weight: 600; color: {TEXT_PRIMARY};")
            head.addWidget(ptitle)
            head.addStretch()
            head.addWidget(self._badge(proj["counts"], proj["total"]))
            v.addLayout(head)

            for b in proj["batches"]:
                bh = QHBoxLayout()
                bh.setSpacing(8)
                btitle = QLabel(f"▸ {b['batch_no']}  {b.get('batch_name') or ''}")
                btitle.setStyleSheet(
                    f"font-size: 12px; font-weight: 600; color: {TEXT_SECONDARY};")
                bh.addWidget(btitle)
                bh.addWidget(self._badge(b["counts"], b["total"], small=True))
                bh.addStretch()
                v.addLayout(bh)
                for nd in b["nodes"]:
                    if not nd["total"]:
                        continue
                    nh = QHBoxLayout()
                    nh.setSpacing(6)
                    nlab = QLabel(f"      {nd['node_name']}（{nd['total']}）")
                    nlab.setStyleSheet(f"font-size: 11px; color: {TEXT_TERTIARY};")
                    nh.addWidget(nlab)
                    nh.addWidget(self._badge(nd["counts"], nd["total"], small=True))
                    nh.addStretch()
                    v.addLayout(nh)
            lay.addWidget(card)

    def _badge(self, counts, total, small=False):
        color = RED if counts.get("P0") else (ORANGE if counts.get("P1") else GREEN)
        lbl = QLabel(f"{total}")
        size = "10px" if small else "11px"
        lbl.setStyleSheet(
            f"font-size: {size}; font-weight: 600; color: #FFFFFF;"
            f" background: {color}; border-radius: 8px; padding: 1px 8px;")
        lbl.setToolTip(f"需处理 {counts.get('P0', 0)} · 进行中 {counts.get('P1', 0)}"
                       f" · 今日启动 {counts.get('P2', 0)}")
        return lbl


class ReminderCenterDialog(QDialog):
    """提醒中心（§12.4）。

    ReminderCenterDialog(project_ids=None, today=None, parent=None, batch_ids=None)
      · project_ids=None → 全部 Active + Completed 项目
      · batch_ids 指定 → 只看这些批次
    离屏自测可直接构造（不 exec），或用 `data()` 取视图模型。
    """

    def __init__(self, project_ids=None, today=None, parent=None, batch_ids=None):
        super().__init__(parent)
        self.setWindowTitle("提醒中心")
        self.setMinimumSize(QSize(760, 560))
        self._project_ids = project_ids
        self._batch_ids = batch_ids
        self._today = today
        self._only_active = True
        self._data = None
        self._build()
        self.refresh()

    # ── 构建 ──

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(10)
        ico = QLabel()
        pix = _icon("bell", ACCENT, 24)
        if pix is not None:
            ico.setPixmap(pix.pixmap(24, 24))
        head.addWidget(ico)
        tcol = QVBoxLayout()
        tcol.setSpacing(2)
        title = QLabel("提醒中心")
        title.setStyleSheet(
            f"font-size: 18px; font-weight: 600; color: {TEXT_PRIMARY};")
        tcol.addWidget(title)
        self.sub_lbl = QLabel("按批次分组 · 免箱期倒计时 / 到货通知 / 申报截止 / 依赖等待")
        self.sub_lbl.setStyleSheet(f"font-size: 11px; color: {TEXT_TERTIARY};")
        tcol.addWidget(self.sub_lbl)
        head.addLayout(tcol)
        head.addStretch()
        self.badge_lbl = QLabel("0")
        self.badge_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 600; color: #FFFFFF; background: {RED};"
            " border-radius: 11px; padding: 3px 12px;")
        head.addWidget(self.badge_lbl)
        lay.addLayout(head)

        # 选项行
        opt = QHBoxLayout()
        opt.setSpacing(10)
        self.active_only = QCheckBox("仅启用中批次（徽标口径）")
        self.active_only.setChecked(True)
        self.active_only.stateChanged.connect(lambda _s: self.refresh())
        opt.addWidget(self.active_only)
        opt.addStretch()
        fresh = QPushButton("刷新")
        fresh.setObjectName("secondary")
        fresh.clicked.connect(self.refresh)
        opt.addWidget(fresh)
        lay.addLayout(opt)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._build_group_page(), "按批次")
        self.tabs.addTab(self._build_three_page(), "三级汇总")
        lay.addWidget(self.tabs, 1)

        foot = QHBoxLayout()
        self.foot_lbl = QLabel("")
        self.foot_lbl.setStyleSheet(f"font-size: 11px; color: {TEXT_TERTIARY};")
        foot.addWidget(self.foot_lbl)
        foot.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("primary")
        close_btn.clicked.connect(self.accept)
        foot.addWidget(close_btn)
        lay.addLayout(foot)

    def _build_group_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }")
        self._group_body = QWidget()
        self._group_lay = QVBoxLayout(self._group_body)
        self._group_lay.setContentsMargins(2, 6, 8, 6)
        self._group_lay.setSpacing(10)
        self._group_lay.addStretch()
        scroll.setWidget(self._group_body)
        v.addWidget(scroll, 1)
        return page

    def _build_three_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }")
        self._three_body = QWidget()
        self._three_lay = QVBoxLayout(self._three_body)
        self._three_lay.setContentsMargins(2, 6, 8, 6)
        self._three_lay.setSpacing(10)
        self._three_lay.addStretch()
        scroll.setWidget(self._three_body)
        v.addWidget(scroll, 1)
        return page

    # ── 数据 ──

    def refresh(self):
        only_active = self.active_only.isChecked()
        self._data = R.reminder_center(
            self._project_ids, today=self._today, batch_ids=self._batch_ids,
            enabled_only=only_active)
        three = R.compute_reminders_three_level(
            self._project_ids, today=self._today, enabled_only=only_active)

        t = self._data["totals"]
        self.badge_lbl.setText(f"待办合计 {t['total']}")
        self.badge_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 600; color: #FFFFFF;"
            f" background: {RED if t['total'] else GREEN};"
            " border-radius: 11px; padding: 3px 12px;")
        self.sub_lbl.setText(
            f"{self._data['today']} · 按批次分组 · 需处理 {t['P0']} / 进行中 {t['P1']}"
            f" / 今日启动 {t['P2']}")
        self.foot_lbl.setText(
            f"徽标口径 = 启用中批次待办合计 = {three['badge']['total']} 项"
            f"（覆盖 {three['badge']['batches']} 个批次）")

        self._fill_groups(self._data["groups"])
        self._fill_three(three)

    def _fill_groups(self, groups):
        while self._group_lay.count():
            it = self._group_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        if not groups:
            empty = QLabel("暂无待办，一切正常")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet(f"font-size: 13px; color: {TEXT_TERTIARY}; padding: 34px;")
            self._group_lay.addWidget(empty)
        else:
            for g in groups:
                self._group_lay.addWidget(BatchGroup(g))
        self._group_lay.addStretch()

    def _fill_three(self, three):
        while self._three_lay.count():
            it = self._three_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._three_lay.addWidget(_ThreeLevelPage(three))
        self._three_lay.addStretch()

    # ── 供自测 ──

    def data(self):
        return self._data

    def group_titles(self):
        """按批次分组标题（自测断言用）。"""
        titles = []
        for i in range(self._group_lay.count()):
            w = self._group_lay.itemAt(i).widget()
            if isinstance(w, BatchGroup):
                titles.append(w.group().get("batch_no"))
        return titles

    def section_labels(self):
        """所有出现过的分节名（自测断言用）。"""
        out = []
        for g in (self._data or {}).get("groups", []):
            for s in g["sections"]:
                if s["count"]:
                    out.append(s["label"])
        return out


class TodayTodoCenterDialog(ReminderCenterDialog):
    """兼容别名：提醒中心（历史调用名）。"""
