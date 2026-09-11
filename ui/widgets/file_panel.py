"""
单证清单面板 — 分组展示项目级 + 各节点单证，支持提交/撤销。

★ 关键约束（历史崩溃根因）：勾选框发出的 toggled 信号回调栈里**绝不能销毁该勾选框**。
   旧实现每次勾选都 `setWidget(新容器)` 整块重建，QScrollArea.setWidget 会同步析构旧内容，
   于是「信号发射者」被 deleteLater/析构 → shiboken 报 `C++ object already deleted`，
   表现为主看板勾选/取消时卡死或崩溃。
   现在：① 容器只创建一次，刷新走**原地增量更新**（只改受影响行的文案/颜色/勾选态）；
         ② 万一行数变化需要重建，也先清空布局再填充，绝不调用 setWidget；
         ③ 程序化改勾选态一律 blockSignals，杜绝信号回环。

★ §10.3 依赖链：未满足上游的单证标灰并显示「待上游：《MBL》」。
   依赖态由 `services.doc_dependency.apply_context(batch_id)` 一次算好后
   通过 `FilePanel(..., dep_map=...)` / `update_files(..., dep_map=...)` 注入；
   面板也可自行按 batch_id 取（`FilePanel(..., batch_id=...)`）。
"""

from datetime import date

from services.clock import get_today
from services.node_status import compute_node_status, get_current_node
from services import doc_dependency

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QCheckBox
)
from PySide6.QtCore import Qt, Signal

from ui.widgets.scoped_scroll import ScopedScrollArea
from ui.theme import (
    GREEN, RED, ORANGE, GRAY, TEXT_PRIMARY, TEXT_SECONDARY, TEXT_TERTIARY, HAIRLINE
)


def _parse(s):
    if isinstance(s, date):
        return s
    if not s:
        return None
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))


def _status_text(node_status):
    return node_status or ""


# §10.3 依赖等待态配色（灰）
WAITING_GRAY = "#8E8E93"


class FileRow(QFrame):
    """单证行：勾选框 + 类型 + 名称 + 部门份数 + 建议日 + 状态。

    apply(file_data, node_status, dep=None) 原地刷新，不重建控件。
    dep（可选）= {"blocked": bool, "text": "待上游：《MBL》", ...} → 整行标灰。
    """

    toggled = Signal(object, bool)      # file_id 可能是整数（批次单证）或 'pf-<n>'（项目级单证）

    def __init__(self, file_data, node_status=None, today=None, readonly=False,
                 dep=None, parent=None):
        super().__init__(parent)
        self._data = file_data
        self._readonly = readonly
        self._node_status = node_status
        self._today = today or get_today()
        self._dep = dep or {}
        self._focus = False
        self._compose()
        self.apply(file_data, node_status, dep)

    # ── 构建（只做一次） ──

    def _compose(self):
        self.setFixedHeight(44)
        self.setStyleSheet(self._style(False))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(12)

        self.checkbox = QCheckBox()
        self.checkbox.setEnabled(not self._readonly)
        self.checkbox.toggled.connect(self._on_toggle)
        layout.addWidget(self.checkbox)

        self.type_label = QLabel()
        self.type_label.setFixedSize(37, 20)
        self.type_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.type_label)

        self.name_label = QLabel()
        self.name_label.setStyleSheet(f"font-size: 13px; color: {TEXT_PRIMARY};")
        layout.addWidget(self.name_label)

        self.dept_label = QLabel()
        self.dept_label.setStyleSheet(f"font-size: 11px; color: {TEXT_SECONDARY};")
        layout.addWidget(self.dept_label)

        # §10.3 「待上游：《MBL》」提示标签（默认隐藏）
        self.dep_label = QLabel()
        self.dep_label.setStyleSheet(
            f"font-size: 11px; color: {WAITING_GRAY};"
            " background: #F2F2F7; border-radius: 5px; padding: 1px 6px;")
        self.dep_label.setVisible(False)
        layout.addWidget(self.dep_label)

        layout.addStretch()

        self.due_label = QLabel()
        self.due_label.setStyleSheet(f"font-size: 11px; color: {TEXT_TERTIARY};")
        layout.addWidget(self.due_label)

        self.status_label = QLabel()
        self.status_label.setFixedHeight(22)
        layout.addWidget(self.status_label)

    def _style(self, focus, blocked=False):
        bg = "#EAF3FF" if focus else ("#F7F7F9" if blocked else "transparent")
        return (f"FileRow {{ border-bottom: 1px solid {HAIRLINE};"
                f" background: {bg}; }}")

    # ── 刷新 ──

    def apply(self, file_data, node_status=None, dep=None):
        self._data = file_data
        if node_status is not None:
            self._node_status = node_status
        if dep is not None:
            self._dep = dep or {}
        f = file_data
        is_submitted = f.get("status") == "submitted"
        blocked = bool(self._dep.get("blocked")) and not is_submitted

        # 勾选态：程序化设置时阻断信号，避免回环
        blocked_sig = self.checkbox.blockSignals(True)
        self.checkbox.setChecked(is_submitted)
        self.checkbox.blockSignals(blocked_sig)

        required = f.get("doc_type") == "required"
        self.type_label.setText("必填" if required else "可选")
        self.type_label.setStyleSheet(
            f"color: {WAITING_GRAY if blocked else (RED if required else TEXT_TERTIARY)};"
            f" font-size: 10px;"
            f" font-weight: 600; background: {'#EDEDF0' if blocked else ('#FDEBE8' if required else '#F2F2F7')};"
            " border-radius: 5px;")

        self.name_label.setText(f.get("doc_name") or "")
        self.name_label.setStyleSheet(
            f"font-size: 13px; color: {WAITING_GRAY if blocked else TEXT_PRIMARY};")

        dept = f.get("owner_dept") or ""
        copies = f.get("copies")
        self.dept_label.setText(f"{dept} ×{copies}" if copies else dept)
        self.dept_label.setStyleSheet(
            f"font-size: 11px; color: {WAITING_GRAY if blocked else TEXT_SECONDARY};")

        # §10.3 依赖提示
        self.dep_label.setText(self._dep.get("text") or "")
        self.dep_label.setToolTip(self._dep.get("tooltip") or "")
        self.dep_label.setVisible(bool(blocked and self._dep.get("text")))

        due = f.get("due_date")
        self.due_label.setText(f"建议 {due[5:]}" if due else "全程")
        self.due_label.setStyleSheet(
            f"font-size: 11px; color: {WAITING_GRAY if blocked else TEXT_TERTIARY};")

        text, color = self._status(is_submitted, due, required, blocked)
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            f"color: {color}; font-size: 11px; font-weight: 600;")

        # 标灰：整行背景 + 勾选框置灰（仅视觉禁用，仍可勾选，便于「忽略依赖」直提）
        self.setStyleSheet(self._style(self._focus, blocked))

    def _status(self, is_submitted, due, required, blocked=False):
        f = self._data
        if is_submitted:
            txt = "已提交"
            if f.get("submitted_date"):
                txt += f" · {f['submitted_date'][5:]}"
            return txt, GREEN

        if blocked:
            return "待上游", WAITING_GRAY

        due_date = _parse(due) if due else None
        if self._node_status in ("Active", "Overdue") and required:
            return "缺失", RED
        if due_date and self._today > due_date:
            return "超期", RED
        if due_date and 0 <= (due_date - self._today).days <= 3:
            return "临近", ORANGE
        return "待准备", GRAY

    def dependency_state(self):
        """当前依赖态（供自测断言）。"""
        return dict(self._dep)

    def set_focus(self, focus):
        focus = bool(focus)
        if focus == self._focus:
            return
        self._focus = focus
        self.setStyleSheet(self._style(focus, bool(self._dep.get("blocked"))
                                       and self._data.get("status") != "submitted"))

    def _on_toggle(self, checked):
        self.toggled.emit(self._data.get("file_id", -1), checked)


class FilePanel(ScopedScrollArea):
    """单证清单面板 + 甘特联动支持（focus_node / scroll_to_node）+ §10.3 依赖标灰"""

    file_toggled = Signal(object, bool)  # 同上：兼容项目级单证的 'pf-<n>' 字符串 id
    FOCUS_BG = "#EAF3FF"     # 悬停/选中节点时对应单证行的浅蓝底

    def __init__(self, files, nodes=None, today=None, readonly=False, parent=None,
                 batch_id=None, dep_map=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self._files = files or []
        self._nodes = nodes or []
        self._today = today or get_today()
        self._readonly = readonly
        self._rows = {}              # file_id → FileRow
        self._row_order = []         # [(row, node_id), ...] 保持插入顺序
        self._group_heads = {}       # node_id → 分组标题右侧状态标签
        self._focused_node = None
        self._batch_id = batch_id
        self._dep_by_file = {}
        self._dep_by_key = {}
        self._dep_ctx = {}
        self.set_dependencies(batch_id, dep_map)

        self._container = QWidget()
        self._container.setStyleSheet("background: transparent;")
        self._body = QVBoxLayout(self._container)
        self._body.setContentsMargins(0, 0, 0, 0)
        self._body.setSpacing(8)
        self.setWidget(self._container)     # ★ 只设置一次，之后永不再调
        self._build_groups()

    # ── §10.3 依赖态注入 ──

    def set_dependencies(self, batch_id=None, dep_map=None):
        """设置依赖态。dep_map 省略时按 batch_id 现场计算（一次算全批）。"""
        if batch_id is not None:
            self._batch_id = batch_id
        if dep_map is not None:
            self._dep_by_file = dict(dep_map)
            self._dep_by_key = {}
            self._dep_ctx = {}
            return
        if not self._batch_id:
            self._dep_by_file, self._dep_by_key, self._dep_ctx = {}, {}, {}
            return
        try:
            by_file, by_key, ctx = doc_dependency.apply_context(self._batch_id, self._files)
        except Exception:
            by_file, by_key, ctx = {}, {}, {}
        self._dep_by_file, self._dep_by_key, self._dep_ctx = by_file, by_key, ctx

    def dep_for(self, file_data):
        """取某行的依赖态（file_id 优先，回退按单证类型）。"""
        if not file_data:
            return {}
        fid = file_data.get("file_id")
        if fid is not None and fid in self._dep_by_file:
            return self._dep_by_file[fid]
        key = doc_dependency.key_of_doc_name(file_data.get("doc_name"))
        return self._dep_by_key.get(key) or {}

    def dependency_tree(self):
        """§10.3 依赖视图模型（供 DependencyView 使用）。"""
        if not self._batch_id:
            return {"batch_id": None, "nodes": [], "edges": [], "roots": [],
                    "waiting": [], "tree": []}
        return doc_dependency.dependency_view(self._batch_id)

    # ── 构建分组（容器复用，绝不 setWidget） ──

    def _build_groups(self):
        self._clear_body()
        self._rows = {}
        self._row_order = []
        self._group_heads = {}

        node_map = {n["node_id"]: n for n in self._nodes}

        project_files = [f for f in self._files if f.get("node_id") is None]
        if project_files:
            self._body.addWidget(self._make_group("项目级 · 全程常备", project_files, None, None))

        node_ids = sorted({f["node_id"] for f in self._files
                           if f.get("node_id") is not None})
        for nid in node_ids:
            node_files = [f for f in self._files if f.get("node_id") == nid]
            node_obj = node_map.get(nid)
            node_st = compute_node_status(node_obj, self._today) if node_obj else None
            node_name = node_obj["node_name"] if node_obj else f"节点{nid}"
            self._body.addWidget(
                self._make_group(f"节点{nid} · {node_name}", node_files, node_st, nid))

        # 分组不足时补一个弹性空隙，避免内容贴顶拉伸
        self._body.addStretch()

    def _clear_body(self):
        while self._body.count():
            item = self._body.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)      # 立即脱离父级，避免与新容器并存
                w.deleteLater()

    def _make_group(self, title, file_list, node_status, node_id):
        group = QFrame()
        group.setObjectName("fileGroup")
        group.setStyleSheet("#fileGroup { background: #FAFAFC; border-radius: 8px; }")
        glayout = QVBoxLayout(group)
        glayout.setContentsMargins(8, 8, 8, 8)
        glayout.setSpacing(2)

        head = QFrame()
        head.setStyleSheet("background: transparent;")
        hh = QHBoxLayout(head)
        hh.setContentsMargins(0, 0, 0, 0)
        hh.setSpacing(0)
        t = QLabel(title)
        t.setStyleSheet(f"font-size: 12px; font-weight: 600; color: {TEXT_SECONDARY};")
        hh.addWidget(t)
        st_lbl = QLabel("")
        st_lbl.setStyleSheet(f"font-size: 10px; color: {TEXT_TERTIARY};")
        hh.addWidget(st_lbl, 0, Qt.AlignRight)
        self._group_heads[node_id] = st_lbl
        glayout.addWidget(head)

        for f in file_list:
            row = FileRow(f, node_status, self._today, self._readonly,
                          dep=self.dep_for(f))
            row.toggled.connect(self.file_toggled.emit)
            glayout.addWidget(row)
            fid = f.get("file_id")
            if fid is not None:
                self._rows[fid] = row
            self._row_order.append((row, node_id))
        return group

    # ── 增量刷新（勾选/取消后调用；不销毁任何控件） ──

    def update_files(self, files, nodes=None, dep_map=None, batch_id=None):
        """用最新单证数据原地刷新。行集合变化时才重建分组（仍不调 setWidget）。"""
        self._files = files or []
        if nodes:
            self._nodes = nodes
        self.set_dependencies(batch_id, dep_map)

        new_ids = {f.get("file_id") for f in self._files}
        if new_ids != set(self._rows.keys()):
            self._build_groups()
            return

        node_map = {n["node_id"]: n for n in self._nodes}
        # 每个节点最新状态（用于行内「缺失」判定与分组标题）
        node_status = {}
        for nid, lbl in self._group_heads.items():
            node_obj = node_map.get(nid)
            st = compute_node_status(node_obj, self._today) if node_obj else None
            node_status[nid] = st
            lbl.setText(_status_text(st))
        for f in self._files:
            nid = f.get("node_id")
            row = self._rows.get(f.get("file_id"))
            if row is None:
                continue
            row.apply(f, node_status.get(nid) if nid is not None else None,
                      self.dep_for(f))

    # ── 甘特联动：高亮某节点对应行 / 滚动到该分组 ──

    def focus_node(self, node_id):
        """高亮 node_id 对应全部单证行；其他行还原。"""
        self._focused_node = node_id
        for row, nid in self._row_order:
            row.set_focus(nid is not None and nid == node_id)

    def clear_focus(self):
        self._focused_node = None
        for row, _ in self._row_order:
            row.set_focus(False)

    def scroll_to_node(self, node_id):
        """滚动让该节点分组首行可见（面板可见时有效）"""
        for row, nid in self._row_order:
            if nid == node_id:
                self.ensureWidgetVisible(row, 0, 60)
                return

    # 兼容旧调用名
    def refresh(self, files, nodes=None, dep_map=None, batch_id=None):
        self.update_files(files, nodes, dep_map=dep_map, batch_id=batch_id)
