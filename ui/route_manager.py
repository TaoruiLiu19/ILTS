"""
线路管理 UI（P0-1 / P0-2，苹果极简风 · 无 emoji）
  · RouteManagerDialog       —— 「线路管理」双标签：线路模板 + 批次线路
  · RouteTemplatePanel       —— (P0-1) 线路模板：列出 / 新建 / 查看各段
  · BatchRoutePanel          —— (P0-2) 批次线路：按模板新建候选 / 设为执行 / 查看各段

后端支撑：db.list_projects / create_route_template / get_route_template /
        create_route_from_template / set_active_route / get_routes / get_legs
"""
from PySide6.QtWidgets import (
    QDialog, QTabWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView, QComboBox,
    QGroupBox, QFormLayout, QMessageBox, QListWidget, QListWidgetItem,
    QWidget, QStackedLayout
)
from PySide6.QtCore import Qt

import db
from ui.theme import (
    ACCENT, TEXT_PRIMARY, TEXT_SECONDARY, TEXT_TERTIARY,
    HAIRLINE, ACCENT_SOFT, GREEN, ORANGE
)

MODE_LABELS = {"sea": "海运", "road": "公路", "rail": "铁路", "air": "空运"}


def _mode_label(m):
    return MODE_LABELS.get(m, str(m or "—"))


def _legs_summary(legs):
    if not legs:
        return "（无段）"
    return " → ".join(_mode_label(l["mode"]) for l in sorted(legs, key=lambda x: x["seq"]))


# ══════════════════════ 新建线路模板 ══════════════════════

class RouteTemplateEditDialog(QDialog):
    """新建线路模板：模板名 + 描述 + 若干段（运输方式/起讫）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新建线路模板")
        self.setMinimumSize(520, 420)
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(12)

        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("如：公路+海运+公路")
        form.addRow("模板名称", self.name)
        self.desc = QLineEdit()
        self.desc.setPlaceholderText("可选说明")
        form.addRow("描述", self.desc)
        lay.addLayout(form)

        g = QGroupBox("运输段（按顺序，一段=一种运输方式）")
        gl = QVBoxLayout(g)
        self.legs = QTableWidget(0, 4)
        self.legs.setHorizontalHeaderLabels(["段序", "运输方式", "起点", "终点"])
        self.legs.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.legs.verticalHeader().setVisible(False)
        self.legs.setEditTriggers(QTableWidget.NoEditTriggers)
        self.legs.setSelectionBehavior(QTableWidget.SelectRows)
        gl.addWidget(self.legs)
        rrow = QHBoxLayout()
        self.btn_add = QPushButton("＋添加段")
        self.btn_sel = QPushButton("− 删除选中段")
        self.btn_add.setCursor(Qt.PointingHandCursor)
        self.btn_sel.setCursor(Qt.PointingHandCursor)
        rrow.addWidget(self.btn_add)
        rrow.addWidget(self.btn_sel)
        rrow.addStretch()
        gl.addLayout(rrow)
        lay.addWidget(g, 1)

        btm = QHBoxLayout()
        btm.addStretch()
        self.btn_save = QPushButton("保存模板")
        self.btn_cancel = QPushButton("取消")
        self.btn_save.setCursor(Qt.PointingHandCursor)
        self.btn_cancel.setCursor(Qt.PointingHandCursor)
        btm.addWidget(self.btn_cancel)
        btm.addWidget(self.btn_save)
        lay.addLayout(btm)

        self.btn_add.clicked.connect(self._add_leg)
        self.btn_sel.clicked.connect(self._del_leg)
        self.btn_save.clicked.connect(self._save)
        self.btn_cancel.clicked.connect(self.reject)

    def _add_leg(self, _=None, seq=None):
        n = self.legs.rowCount()
        r = seq if seq is not None else n + 1
        self.legs.insertRow(n)
        combo = QComboBox()
        for key, lab in MODE_LABELS.items():
            combo.addItem(f"{lab}", key)
        self.legs.setItem(n, 0, QTableWidgetItem(str(r)))
        self.legs.setCellWidget(n, 1, combo)
        self.legs.setItem(n, 2, QTableWidgetItem(""))
        self.legs.setItem(n, 3, QTableWidgetItem(""))

    def _del_leg(self):
        rows = {i.row() for i in self.legs.selectedIndexes()}
        for r in sorted(rows, reverse=True):
            self.legs.removeRow(r)
        self._renumber()

    def _renumber(self):
        for i in range(self.legs.rowCount()):
            self.legs.item(i, 0).setText(str(i + 1))

    def _save(self):
        name = self.name.text().strip()
        if not name:
            QMessageBox.warning(self, "提示", "请填写模板名称")
            return
        if self.legs.rowCount() == 0:
            QMessageBox.warning(self, "提示", "请至少添加一个运输段")
            return
        legs = []
        for i in range(self.legs.rowCount()):
            combo = self.legs.cellWidget(i, 1)
            mode = combo.currentData()
            legs.append({
                "seq": i + 1,
                "mode": mode,
                "origin_name": (self.legs.item(i, 2).text() or "").strip() or None,
                "dest_name": (self.legs.item(i, 3).text() or "").strip() or None,
            })
        self.template_id = db.create_route_template(name, self.desc.text().strip() or None,
                                                    legs=legs)
        self.accept()


# ══════════════════════ 线路模板标签页 (P0-1) ══════════════════════

class RouteTemplatePanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()
        self.reload()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(10)

        top = QHBoxLayout()
        self.lbl_count = QLabel("")
        self.lbl_count.setStyleSheet(f"color: {TEXT_SECONDARY};")
        self.btn_new = QPushButton("新建模板")
        self.btn_new.setCursor(Qt.PointingHandCursor)
        top.addWidget(self.lbl_count)
        top.addStretch()
        top.addWidget(self.btn_new)
        lay.addLayout(top)

        body = QHBoxLayout()
        body.setSpacing(12)
        self.tpl_list = QListWidget()
        self.tpl_list.setMinimumWidth(220)
        body.addWidget(self.tpl_list)
        self.detail = QGroupBox("模板详情")
        dlay = QVBoxLayout(self.detail)
        self.d_name = QLabel("选择左侧模板查看详情")
        self.d_name.setWordWrap(True)
        dlay.addWidget(self.d_name)
        self.d_legs = QLabel("")
        self.d_legs.setWordWrap(True)
        self.d_legs.setStyleSheet(f"color: {TEXT_SECONDARY};")
        dlay.addWidget(self.d_legs)
        dlay.addStretch()
        body.addWidget(self.detail, 1)
        lay.addLayout(body, 1)

        self.btn_new.clicked.connect(self._new)
        self.tpl_list.currentRowChanged.connect(self._show_tpl)

    def reload(self):
        self.tpls = db.list_route_templates()
        self.tpl_list.clear()
        for t in self.tpls:
            legs = _legs_summary(t.get("legs", []))
            it = QListWidgetItem(f"{t['template_name']}  · {legs}")
            it.setData(Qt.UserRole, t["template_id"])
            self.tpl_list.addItem(it)
        if self.tpl_list.count():
            self.tpl_list.setCurrentRow(0)
        self.lbl_count.setText(f"共 {len(self.tpls)} 个模板")

    def _show_tpl(self, row):
        if row < 0 or row >= len(self.tpls):
            self.d_name.setText("选择左侧模板查看详情")
            self.d_legs.setText("")
            return
        t = self.tpls[row]
        legs = t.get("legs", [])
        lines = [f"段序 {l['seq']} · {_mode_label(l['mode'])} · "
                 f"{l.get('origin_name') or '—'} → {l.get('dest_name') or '—'}"
                 for l in sorted(legs, key=lambda x: x["seq"])] or ["（无段）"]
        self.d_name.setText(f"{t['template_name']}"
                            f"{('  ·  ' + t['description']) if t.get('description') else ''}")
        self.d_legs.setText("\n".join(lines))

    def _new(self):
        dlg = RouteTemplateEditDialog(self)
        if dlg.exec():
            self.reload()


# ══════════════════════ 批次线路标签页 (P0-2) ══════════════════════

class BatchRoutePanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._routes = []
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(10)

        sel = QHBoxLayout()
        sel.setSpacing(8)
        sel.addWidget(QLabel("项目"))
        self.combo_proj = QComboBox()
        self.combo_proj.setMinimumWidth(220)
        sel.addWidget(self.combo_proj)
        sel.addWidget(QLabel("批次"))
        self.combo_batch = QComboBox()
        self.combo_batch.setMinimumWidth(220)
        sel.addWidget(self.combo_batch)
        sel.addStretch()
        sel.addWidget(QLabel("当前执行："))
        self.lbl_active = QLabel("—")
        self.lbl_active.setStyleSheet(f"color: {GREEN}; font-weight: 600;")
        sel.addWidget(self.lbl_active)
        lay.addLayout(sel)

        self.routes = QTableWidget(0, 5)
        self.routes.setHorizontalHeaderLabels(["线路", "状态", "执行", "段", "说明"])
        self.routes.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.routes.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.routes.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.routes.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.routes.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.routes.verticalHeader().setVisible(False)
        self.routes.setEditTriggers(QTableWidget.NoEditTriggers)
        self.routes.setSelectionBehavior(QTableWidget.SelectRows)
        lay.addWidget(self.routes, 1)

        self.d_legs = QLabel("选中线路后查看各段")
        self.d_legs.setWordWrap(True)
        self.d_legs.setStyleSheet(f"color: {TEXT_SECONDARY}; background: {ACCENT_SOFT};"
                                  f" border-radius: 8px; padding: 8px 10px;")
        lay.addWidget(self.d_legs)

        btm = QHBoxLayout()
        btm.addStretch()
        self.btn_newroute = QPushButton("从模板新建线路")
        self.btn_active = QPushButton("设为执行线路")
        self.btn_refresh = QPushButton("刷新")
        for b in (self.btn_newroute, self.btn_active, self.btn_refresh):
            b.setCursor(Qt.PointingHandCursor)
        btm.addWidget(self.btn_refresh)
        btm.addWidget(self.btn_newroute)
        btm.addWidget(self.btn_active)
        lay.addLayout(btm)

        self.combo_proj.currentIndexChanged.connect(self._reload_batches)
        self.combo_batch.currentIndexChanged.connect(self._reload_routes)
        self.routes.itemSelectionChanged.connect(self._show_route_legs)
        self.btn_newroute.clicked.connect(self._new_route)
        self.btn_active.clicked.connect(self._set_active)
        self.btn_refresh.clicked.connect(self._seed_and_reload)
        self._load_projects()

    # ── 数据 ──
    def _load_projects(self):
        self.combo_proj.clear()
        self.projs = db.list_projects()
        for p in self.projs:
            name = p.get("project_no") or p.get("project_id") or p.get("project_name") or ""
            self.combo_proj.addItem(f"{name}", p["project_id"])

    def _reload_batches(self):
        self.combo_batch.clear()
        pid = self.combo_proj.currentData()
        if not pid:
            return
        self.batches = db.get_batches(pid)
        for b in self.batches:
            bn = b.get("batch_no") or b.get("batch_name") or b["batch_id"]
            self.combo_batch.addItem(f"{bn}", b["batch_id"])

    def _reload_routes(self):
        bid = self.combo_batch.currentData()
        if not bid:
            self.routes.setRowCount(0)
            self.lbl_active.setText("—")
            self.d_legs.setText("选中线路后查看各段")
            return
        self._routes = db.get_routes(bid)
        self.routes.setRowCount(0)
        active = None
        for r in self._routes:
            row = self.routes.rowCount()
            self.routes.insertRow(row)
            self.routes.setItem(row, 0, QTableWidgetItem(r["route_name"]))
            self.routes.setItem(row, 1, QTableWidgetItem(r.get("status") or "—"))
            self.routes.setItem(row, 2, QTableWidgetItem("● 执行中" if r["is_active"] else ""))
            legs = db.get_legs(r["route_id"])
            self.routes.setItem(row, 3, QTableWidgetItem(str(len(legs))))
            self.routes.setItem(row, 4, QTableWidgetItem(_legs_summary(legs)))
            if r["is_active"]:
                active = r
        self.lbl_active.setText(active["route_name"] if active else "无执行线路")
        self.d_legs.setText("")

    def _show_route_legs(self):
        sel = self.routes.selectedItems()
        if not sel:
            return
        row = sel[0].row()
        r = self._routes[row]
        legs = db.get_legs(r["route_id"])
        lines = [f"段{l['seq']} · {_mode_label(l['mode'])} · "
                 f"{l.get('origin_name') or '—'} → {l.get('dest_name') or '—'}"
                 for l in sorted(legs, key=lambda x: x["seq"])] or ["（无段）"]
        self.d_legs.setText(f"[{r['route_name']}]\n" + " | ".join(lines))

    # ── 动作 ──
    def _new_route(self):
        bid = self.combo_batch.currentData()
        if not bid:
            QMessageBox.information(self, "提示", "请先选择批次")
            return
        tpls = db.list_route_templates()
        if not tpls:
            if QMessageBox.question(self, "提示", "还没有线路模板，是否现在新建？",
                                    QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
                dlg = RouteTemplateEditDialog(self)
                if dlg.exec():
                    if callable(self.on_template_changed):
                        self.on_template_changed()
            return
        menu_items = [f"{t['template_name']}  · {_legs_summary(t.get('legs', []))}"
                      for t in tpls]
        from PySide6.QtWidgets import QInputDialog
        choice, ok = QInputDialog.getItem(self, "从模板新建线路", "选择线路模板：",
                                          menu_items, 0, False)
        if not ok:
            return
        idx = menu_items.index(choice)
        tpl_id = tpls[idx]["template_id"]
        db.create_route_from_template(bid, tpl_id)
        self._reload_routes()

    def _set_active(self):
        sel = self.routes.selectedItems()
        if not sel:
            QMessageBox.information(self, "提示", "请先选择一条候选线路")
            return
        row = sel[0].row()
        r = self._routes[row]
        if r["is_active"]:
            QMessageBox.information(self, "提示", "该线路已是执行线路")
            return
        if QMessageBox.question(self, "确认",
                                f"将「{r['route_name']}」设为执行线路？\n"
                                "（当前执行线路将被替换；执行后线路只读）",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        db.set_active_route(self.combo_batch.currentData(), r["route_id"])
        self._reload_routes()

    def _seed_and_reload(self):
        self._reload_batches()
        self._reload_routes()


# ══════════════════════ 容器：双标签对话框 ══════════════════════

class RouteManagerDialog(QDialog):
    """「线路管理」：线路模板 (P0-1) + 批次线路 (P0-2)。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("线路管理")
        self.setMinimumSize(760, 560)
        self.resize(820, 600)
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.tabs = QTabWidget()
        self.panel_batch = BatchRoutePanel(self)
        self.panel_tpl = RouteTemplatePanel()
        self.panel_batch.on_template_changed = self.panel_tpl.reload
        self.tabs.addTab(self.panel_tpl, "线路模板")
        self.tabs.addTab(self.panel_batch, "批次线路")
        lay.addWidget(self.tabs)
        foot = QHBoxLayout()
        foot.setContentsMargins(20, 10, 20, 14)
        foot.addStretch()
        btn_close = QPushButton("关闭")
        btn_close.setCursor(Qt.PointingHandCursor)
        foot.addWidget(btn_close)
        lay.addLayout(foot)
        btn_close.clicked.connect(self.accept)
        self.tabs.currentChanged.connect(self._on_tab)

    def _on_tab(self, idx):
        if idx == 1:
            self.panel_batch._load_projects()