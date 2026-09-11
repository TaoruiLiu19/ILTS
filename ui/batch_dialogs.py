"""
批次化 UI 辅助对话框（1A/1C，苹果极简风·无 emoji）
  · BatchDialog            —— 批次管理：批次信息 / 船期与线路 / 集装箱 / 客户货主（§12.2、§12.8）
  · ScheduleChangeDialog   —— 船期变更登记：A/B/C/D 影响预览 + 应用 + 历史（§12.9、D34）
  · PlanDateDialog         —— 计划日期只读视图（§12.10、schedule2.result_summary）
  · CustomerMasterDialog   —— 客户/货主主档 增改查（§12.8）
  · CancelledBatchAuditDialog —— §8/D14/T20「显示已取消」开关 + 已取消批次 op_log 只读审计
  · RouteImpactDialog      —— §8/D18/T21 换线影响评估清单（新增/删除/改名/顺序/日期）

所有「船期变更」一律走 services.schedule_change.apply（写 batch_schedule_changes + op_log，
不写 shift_history）；批次对话框直接改 ETD/ETA 时仅记录 schedule_recompute，不视为船期变更。
"""

from datetime import date

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QTableWidget, QTableWidgetItem, QHeaderView, QDateEdit, QComboBox,
    QGroupBox, QFormLayout, QMessageBox, QFrame, QWidget, QCheckBox,
    QListWidget, QListWidgetItem, QTextEdit
)
from PySide6.QtCore import Qt, QDate
from PySide6.QtGui import QColor

import db
from config import get_country
from services import ports
from services import node_template as nt
from services import schedule2
from services.schedule_change import preview as sc_preview, apply as sc_apply, classify
from services.schedule_change import ScheduleError
from services import batches as batches_svc
from ui.theme import (
    ACCENT, GRAY_SOFT, TEXT_PRIMARY, TEXT_SECONDARY, TEXT_TERTIARY,
    HAIRLINE, ACCENT_SOFT, RED, GREEN, ORANGE
)


def _oplog(*args, **kw):
    try:
        from services.oplog import record
        return record(*args, **kw)
    except Exception:
        return None


def _d2q(s):
    """'YYYY-MM-DD' → QDate；供 QDateEdit.setDate"""
    try:
        if isinstance(s, date):
            return QDate(s.year, s.month, s.day)
        y, m, d = str(s).split("-")
        return QDate(int(y), int(m), int(d))
    except Exception:
        return None


def _q2d(q):
    try:
        return q.toString("yyyy-MM-dd")
    except Exception:
        return None


# ════════════════════ 批次管理 ════════════════════

class BatchDialog(QDialog):
    """批次管理：批次信息 / 船期与线路 / 集装箱 / 客户货主 一体编辑"""

    _ROLE_LABELS = [("CUSTOMER", "客户"), ("SHIPPER", "发货人"),
                    ("CONSIGNEE", "收货人"), ("IMPORTER", "进口商"),
                    ("NOTIFY", "通知方(可多)")]

    def __init__(self, project_id, batch_id=None, parent=None):
        super().__init__(parent)
        self._project_id = project_id
        proj = db.get_project(project_id)
        self._batch_id = batch_id or db.current_batch_id(project_id)
        self._project_no = (proj or {}).get("project_no") or "P"
        self.auto_note = ""      # 保存后回传给调用方的提示（如「已按模板生成节点」）
        self.setWindowTitle("批次管理")
        self.setMinimumSize(820, 640)
        self._build()
        self._load()

    # ── 构建 ──
    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(12)

        # 空批次提示：新增批次尚无节点 → 保存后按标准模板补齐（甘特图才有内容）
        self.empty_hint = QLabel(
            "本批次尚无计划节点：确认 ETD / ETA 后点「保存批次」，"
            "将按 15 节点标准模板自动生成计划节点与单证清单。")
        self.empty_hint.setWordWrap(True)
        self.empty_hint.setStyleSheet(
            f"font-size: 12px; color: {ACCENT}; background: {ACCENT_SOFT};"
            f" border-radius: 10px; padding: 10px 12px;")
        self.empty_hint.setVisible(False)
        lay.addWidget(self.empty_hint)

        scroll_host = QWidget()
        sv = QVBoxLayout(scroll_host)
        sv.setContentsMargins(0, 0, 6, 0)
        sv.setSpacing(12)

        # 批次信息
        g_info = QGroupBox("批次信息")
        f1 = QFormLayout(g_info)
        f1.setLabelAlignment(Qt.AlignRight)
        f1.setVerticalSpacing(8)
        self.batch_no = QLineEdit()
        self.batch_no.setPlaceholderText(f"默认 {self._project_no}-B01")
        f1.addRow("批次号", self.batch_no)
        self.batch_name = QLineEdit()
        self.batch_name.setPlaceholderText("默认 B01")
        f1.addRow("名称", self.batch_name)
        rbk = QHBoxLayout()
        self.booking_no = QLineEdit()
        self.booking_no.setPlaceholderText("订舱号")
        self.mbl_no = QLineEdit()
        self.mbl_no.setPlaceholderText("MBL 主提单号")
        rbk.addWidget(self.booking_no)
        rbk.addWidget(self.mbl_no)
        f1.addRow("订舱号 / MBL", rbk)
        self.hbl_no = QLineEdit()
        self.hbl_no.setPlaceholderText("HBL 分提单号（可多个，用 / 分隔）")
        f1.addRow("HBL", self.hbl_no)
        sv.addWidget(g_info)

        # 船期与线路
        g_route = QGroupBox("船期与线路（境内倒排 · 境外顺排 · 海运=ETA−ETD）")
        f2 = QFormLayout(g_route)
        f2.setLabelAlignment(Qt.AlignRight)
        f2.setVerticalSpacing(8)

        port_row = QHBoxLayout()
        self.port_combo = QComboBox()
        self.port_combo.addItem("请选择出发港", None)
        for p in ports.list_ports():
            self.port_combo.addItem(f"{p['name']} · {p['key']}", p["key"])
        port_row.addWidget(self.port_combo, 1)
        f2.addRow("出发国内港口", port_row)

        etd_row = QHBoxLayout()
        self.etd_edit = QDateEdit()
        self.etd_edit.setCalendarPopup(True)
        self.etd_edit.setDisplayFormat("yyyy-MM-dd")
        self.eta_edit = QDateEdit()
        self.eta_edit.setCalendarPopup(True)
        self.eta_edit.setDisplayFormat("yyyy-MM-dd")
        etd_row.addWidget(QLabel("ETD"))
        etd_row.addWidget(self.etd_edit)
        etd_row.addWidget(QLabel("ETA"))
        etd_row.addWidget(self.eta_edit)
        hint = QLabel("北京时间")
        hint.setStyleSheet(f"font-size: 11px; color: {TEXT_TERTIARY};")
        etd_row.addWidget(hint)
        f2.addRow("船期", etd_row)

        vs_row = QHBoxLayout()
        self.vessel_name = QLineEdit()
        self.vessel_name.setPlaceholderText("船名")
        self.voyage = QLineEdit()
        self.voyage.setPlaceholderText("航次")
        vs_row.addWidget(self.vessel_name)
        vs_row.addWidget(self.voyage)
        f2.addRow("船名 / 航次", vs_row)

        cus_row = QHBoxLayout()
        self.customs_broker = QLineEdit()
        self.customs_broker.setPlaceholderText("报关行")
        self.customs_mode = QComboBox()
        for m in ("一般贸易", "买单", "市场采购", "跨境电商"):
            self.customs_mode.addItem(m)
        cus_row.addWidget(self.customs_broker)
        cus_row.addWidget(self.customs_mode)
        f2.addRow("报关行 / 方式", cus_row)

        rel_row = QHBoxLayout()
        self.release_mode = QComboBox()
        for mode, lb in (("ORIGINAL", "正本"), ("TELEX", "电放"), ("SWB", "海运单")):
            self.release_mode.addItem(lb, mode)
        self.pickup_loc = QLineEdit()
        self.pickup_loc.setPlaceholderText("提空箱地点")
        rel_row.addWidget(self.release_mode)
        rel_row.addWidget(self.pickup_loc)
        f2.addRow("换单方式 / 提箱地", rel_row)

        free_row = QHBoxLayout()
        self.free_demurrage = QDateEdit()
        self.free_demurrage.setCalendarPopup(True)
        self.free_demurrage.setDisplayFormat("yyyy-MM-dd")
        self.free_detention = QDateEdit()
        self.free_detention.setCalendarPopup(True)
        self.free_detention.setDisplayFormat("yyyy-MM-dd")
        self._empty_free = QCheckBox("无")
        self._empty_free.toggled.connect(
            lambda on: (self.free_demurrage.setEnabled(not on),
                        self.free_detention.setEnabled(not on)))
        free_row.addWidget(self._empty_free)
        free_row.addWidget(QLabel("免堆期"))
        free_row.addWidget(self.free_demurrage)
        free_row.addWidget(QLabel("免箱期"))
        free_row.addWidget(self.free_detention)
        f2.addRow("免堆/免箱截止", free_row)
        sv.addWidget(g_route)

        # 集装箱
        g_box = QGroupBox("集装箱（FCL 独占柜）")
        gb = QVBoxLayout(g_box)
        gb.setContentsMargins(12, 12, 12, 12)
        gb.setSpacing(6)
        self.box_table = QTableWidget(0, 5)
        self.box_table.setHorizontalHeaderLabels(["柜号", "封号", "柜型", "提箱日", "还箱日"])
        self.box_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.box_table.verticalHeader().setVisible(False)
        self.box_table.verticalHeader().setDefaultSectionSize(30)
        self.box_table.setShowGrid(False)
        gb.addWidget(self.box_table)
        brow = QHBoxLayout()
        bbtn = QPushButton("＋ 添加柜")
        bbtn.setObjectName("secondary")
        bbtn.setCursor(Qt.PointingHandCursor)
        bbtn.clicked.connect(lambda: (self.box_table.insertRow(self.box_table.rowCount()),
                                      self.box_table.scrollToBottom()))
        brow.addWidget(bbtn)
        rbtn = QPushButton("移除选中柜")
        rbtn.setObjectName("ghost")
        rbtn.setCursor(Qt.PointingHandCursor)
        rbtn.clicked.connect(self._del_box)
        brow.addWidget(rbtn)
        brow.addStretch()
        gb.addLayout(brow)
        sv.addWidget(g_box)

        # 客户/货主
        g_party = QGroupBox("客户 / 货主（缺失角色将阻断对应单证提交）")
        gp = QVBoxLayout(g_party)
        gp.setContentsMargins(12, 12, 12, 12)
        gp.setSpacing(6)
        self._party_pickers = {}
        for role, label in self._ROLE_LABELS:
            row = QHBoxLayout()
            lbl = QLabel(label)
            lbl.setFixedWidth(90)
            lbl.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
            row.addWidget(lbl)
            combo = QComboBox()
            combo.addItem("（未选择）", None)
            row.addWidget(combo, 1)
            if role == "NOTIFY":
                multi_btn = QPushButton("多选")
                multi_btn.setObjectName("secondary")
                multi_btn.setCursor(Qt.PointingHandCursor)
                multi_btn.clicked.connect(lambda _=False, r=role: self._pick_multi(r))
                row.addWidget(multi_btn)
            self._party_pickers[role] = combo
            gp.addLayout(row)
        mgmt_btn = QPushButton("客户主档管理")
        mgmt_btn.setObjectName("secondary")
        mgmt_btn.setCursor(Qt.PointingHandCursor)
        mgmt_btn.setToolTip("增改查客户/货主；去重提示由主档查询提供")
        mgmt_btn.clicked.connect(self._open_master)
        gp.addWidget(mgmt_btn, 0, Qt.AlignLeft)
        sv.addWidget(g_party)

        sv.addStretch()

        from PySide6.QtWidgets import QScrollArea
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(scroll_host)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        lay.addWidget(scroll, 1)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton("取消")
        cancel.setObjectName("secondary")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        save = QPushButton("保存批次")
        save.setObjectName("primary")
        save.setCursor(Qt.PointingHandCursor)
        save.clicked.connect(self._save)
        btns.addWidget(save)
        lay.addLayout(btns)

    # ── 加载 ──
    def _load(self):
        b = db.get_batch(self._batch_id)
        route = db.get_route(self._batch_id)
        proj = db.get_project(self._project_id)

        if b:
            self.batch_no.setText(b.get("batch_no") or "")
            self.batch_name.setText(b.get("batch_name") or "")
            self.booking_no.setText(b.get("booking_no") or "")
            self.mbl_no.setText(b.get("mbl_no") or "")
            hbls = b.get("hbl_nos")
            import json
            try:
                arr = json.loads(hbls) if hbls else []
            except Exception:
                arr = []
            self.hbl_no.setText(" / ".join(arr))

        if route:
            self._select_port(route.get("export_port"))
            if route.get("etd"):
                d = _d2q(route["etd"])
                if d:
                    self.etd_edit.setDate(d)
            if route.get("eta"):
                d = _d2q(route["eta"])
                if d:
                    self.eta_edit.setDate(d)
            self.customs_broker.setText(route.get("customs_broker") or "")
            self._select_combo(self.customs_mode, route.get("customs_mode"))
            self._select_combo(self.release_mode, route.get("release_mode"), by="data")
            self.pickup_loc.setText(route.get("container_pickup_location") or "")
            self._set_free(route.get("free_demurrage_until"), route.get("free_detention_until"))
        elif proj:
            # 新批次可继承项目级 etd/eta 便于编辑
            if proj.get("etd"):
                d = _d2q(proj["etd"])
                if d:
                    self.etd_edit.setDate(d)
            if proj.get("eta"):
                d = _d2q(proj["eta"])
                if d:
                    self.eta_edit.setDate(d)
            self._select_port(proj.get("export_port"))

        vessel = db.get_vessel(self._project_id, self._batch_id)
        if vessel:
            self.vessel_name.setText(vessel.get("vessel_name") or "")
            self.voyage.setText(vessel.get("voyage") or "")

        # 集装箱
        self.box_table.setRowCount(0)
        for c in db.get_containers(self._batch_id):
            r = self.box_table.rowCount()
            self.box_table.insertRow(r)
            self._box_row(r, c)

        # 客户/货主
        self._load_party_options()
        bound = {}
        for p in db.get_batch_parties(self._batch_id):
            bound.setdefault(p["role"], []).append(p["party_id"])
        for role, _ in self._ROLE_LABELS:
            combo = self._party_pickers[role]
            ids = bound.get(role) or []
            if ids:
                combo.setCurrentIndex(max(0, 1 + self._party_index(role, ids[0])))

        # 空批次（无节点）→ 顶部提示「保存后按模板生成」
        if getattr(self, "empty_hint", None) is not None:
            self.empty_hint.setVisible(not db.get_nodes_by_batch(self._batch_id))

    def _box_row(self, r, c=None):
        c = c or {}
        vals = [c.get("container_no", ""), c.get("seal_no", ""),
                c.get("container_type", ""), c.get("pickup_at", ""), c.get("returned_at", "")]
        for col, v in enumerate(vals):
            cell = QTableWidgetItem(v or "")
            self.box_table.setItem(r, col, cell)

    def _del_box(self):
        rows = sorted({i.row() for i in self.box_table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.box_table.removeRow(r)

    def _select_port(self, key):
        if not key:
            return
        for i in range(self.port_combo.count()):
            if self.port_combo.itemData(i) == key:
                self.port_combo.setCurrentIndex(i)
                return

    def _select_combo(self, combo, val, by="text"):
        if not val:
            return
        for i in range(combo.count()):
            data = combo.itemData(i) if by == "data" else combo.itemText(i)
            if data == val:
                combo.setCurrentIndex(i)
                return

    def _set_free(self, dem, det):
        if not dem and not det:
            self._empty_free.setChecked(True)
        else:
            self._empty_free.setChecked(False)
            if dem:
                d = _d2q(dem)
                if d:
                    self.free_demurrage.setDate(d)
            if det:
                d = _d2q(det)
                if d:
                    self.free_detention.setDate(d)

    # ── 客户/货主 ──
    def _load_party_options(self):
        parties = db.list_parties()
        for role, _ in self._ROLE_LABELS:
            combo = self._party_pickers[role]
            self._fill_combo(combo, parties)

    def _fill_combo(self, combo, parties):
        cur = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("（未选择）", None)
        for p in parties:
            combo.addItem(f"{p['party_name']}" + (f" / {p['name_en']}" if p.get("name_en") else ""),
                          p["party_id"])
        # 恢复旧选中
        for i in range(combo.count()):
            if combo.itemData(i) == cur:
                combo.setCurrentIndex(i)
                break
        combo.blockSignals(False)

    def _party_index(self, role, party_id):
        combo = self._party_pickers[role]
        for i in range(combo.count()):
            if combo.itemData(i) == party_id:
                return i - 1
        return -1

    def _pick_multi(self, role):
        combo = self._party_pickers[role]
        # 收集当前批次已绑定的该角色 party 集合
        MultiPartyPicker(self._batch_id, role, combo, self).exec()
        self._load_party_options()

    # ── 保存 ──
    def _save(self):
        import json
        etd = _q2d(self.etd_edit.date()) if not self._empty_free or True else None
        eta = _q2d(self.eta_edit.date())

        # 批次信息
        db.update_batch(self._batch_id,
                        batch_no=self.batch_no.text().strip() or None,
                        batch_name=self.batch_name.text().strip() or None,
                        booking_no=self.booking_no.text().strip() or None,
                        mbl_no=self.mbl_no.text().strip() or None,
                        hbl_nos=json.dumps(
                            [s.strip() for s in self.hbl_no.text().split("/") if s.strip()],
                            ensure_ascii=False) or None)

        # 线路
        port_key = self.port_combo.currentData()
        free_dem = None if self._empty_free.isChecked() else _q2d(self.free_demurrage.date())
        free_det = None if self._empty_free.isChecked() else _q2d(self.free_detention.date())
        route = db.get_route(self._batch_id)
        old_etd, old_eta = (route["etd"], route["eta"]) if route else (None, None)
        db.upsert_route(self._batch_id,
                        export_port=port_key,
                        etd=etd, eta=eta,
                        customs_broker=self.customs_broker.text().strip() or None,
                        customs_mode=self.customs_mode.currentText(),
                        release_mode=self.release_mode.currentData(),
                        container_pickup_location=self.pickup_loc.text().strip() or None,
                        free_demurrage_until=free_dem, free_detention_until=free_det)

        # 船（仅当有船名/航次输入）
        vn = self.vessel_name.text().strip()
        if vn or self.voyage.text().strip():
            db.upsert_vessel(self._project_id, vessel_name=vn or None,
                             voyage=self.voyage.text().strip() or None,
                             batch_id=self._batch_id)

        # 集装箱全量替换
        boxtable = self.box_table
        old_boxes = {c["container_no"]: c for c in db.get_containers(self._batch_id)}
        seen = set()
        for r in range(boxtable.rowCount()):
            def txt(c):
                it = boxtable.item(r, c)
                return it.text().strip() if it else ""
            no = txt(0)
            if not no:
                continue
            if no in seen:
                continue
            seen.add(no)
            c = old_boxes.pop(no, None)
            if c:
                db.update_container(c["container_id"], seal_no=txt(1) or None,
                                    container_type=txt(2) or None,
                                    pickup_at=txt(3) or None, returned_at=txt(4) or None)
            else:
                cid = db.insert_container(self._batch_id, no, seal_no=txt(1) or None,
                                          container_type=txt(2) or None,
                                          pickup_at=txt(3) or None, returned_at=txt(4) or None)
                db.link_container(self._batch_id, cid)
        for no, c in old_boxes.items():
            db.get_conn().execute("DELETE FROM containers WHERE container_id=?", (c["container_id"],))
        db.get_conn().commit()

        # 客户/货主绑定
        for role, _ in self._ROLE_LABELS:
            combo = self._party_pickers[role]
            pid = combo.currentData()
            if role == "NOTIFY":
                continue  # 多选由 MultiPartyPicker 维护
            if pid:
                db.set_batch_parties(self._batch_id, role, [pid])
            else:
                db.set_batch_parties(self._batch_id, role, [])

        # 船期变化 → 重算计划日期（不视为船期变更，仅记 schedule_recompute）
        if etd and eta and (etd, eta) != (old_etd, old_eta):
            db.update_project(self._project_id, etd=etd, eta=eta)
            schedule2.recompute_batch_schedule(
                self._project_id, batch_id=self._batch_id,
                reason=f"批次编辑船期 {old_etd}~{old_eta}→{etd}~{eta}")

        # 空批次（新增批次尚未建节点）→ 按 15 节点标准模板补齐节点 + 单证清单
        # 否则该批次永远 0 节点，看板甘特图无内容可画。
        filled = batches_svc.ensure_batch_nodes(
            self._project_id, self._batch_id,
            reason=f"批次管理保存（船期 {etd} → {eta}）")
        self.auto_note = ""
        if filled.get("created"):
            self.auto_note = (f"已按 15 节点标准模板生成 {filled['nodes']} 个计划节点"
                              f"与 {filled['files']} 份单证清单。")
        elif filled.get("reason") and not db.get_nodes_by_batch(self._batch_id):
            self.auto_note = f"未生成计划节点：{filled['reason']}。"
        if getattr(self, "empty_hint", None) is not None:
            self.empty_hint.setVisible(not db.get_nodes_by_batch(self._batch_id))

        # 派生批次状态
        st = batches_svc.derive_batch_status(self._batch_id)
        if st:
            db.update_batch(self._batch_id, status=st)
        batches_svc.update_project_status(self._project_id)

        _oplog("batch_edit", self._project_id, batch_id=self._batch_id,
               subject=self.batch_name.text().strip() or self.batch_no.text().strip() or "批次",
               detail=f"批次信息更新（船期 {etd} → {eta}）" if etd else "批次信息更新")
        self.accept()

    def _open_master(self):
        CustomerMasterDialog(self._batch_id, self).exec()
        self._load_party_options()


class MultiPartyPicker(QDialog):
    """通知方等多选：勾选已绑定 + 从主档选择"""

    def __init__(self, batch_id, role, combo, parent=None):
        super().__init__(parent)
        self._batch_id, self._role = batch_id, role
        self.setWindowTitle("多选：通知方")
        self.setMinimumWidth(440)
        self.setMinimumHeight(380)
        self._build()

    def _build(self):
        from PySide6.QtWidgets import QScrollArea
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)
        tip = QLabel("勾选当前批次的通知方（可多个）。未建立主档的可在「客户主档」中新增。")
        tip.setWordWrap(True)
        tip.setStyleSheet(f"font-size: 12px; color: {TEXT_TERTIARY};")
        lay.addWidget(tip)

        self._checks = {}
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(2, 2, 2, 2)
        bl.setSpacing(4)
        cur = {p["party_id"] for p in db.get_batch_parties(self._batch_id, self._role)}
        for p in db.list_parties():
            cb = QCheckBox(p["party_name"] + (f" / {p['name_en']}" if p.get("name_en") else ""))
            cb.setChecked(p["party_id"] in cur)
            self._checks[p["party_id"]] = cb
            bl.addWidget(cb)
        bl.addStretch()
        scroll.setWidget(body)
        lay.addWidget(scroll, 1)

        mgmt = QPushButton("客户主档管理")
        mgmt.setObjectName("secondary")
        mgmt.clicked.connect(self._open_master)
        lay.addWidget(mgmt, 0, Qt.AlignLeft)

        btns = QHBoxLayout()
        btns.addStretch()
        save = QPushButton("保存")
        save.setObjectName("primary")
        save.clicked.connect(self._save)
        cancel = QPushButton("取消")
        cancel.setObjectName("secondary")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        btns.addWidget(save)
        lay.addLayout(btns)

    def _open_master(self):
        CustomerMasterDialog(self._batch_id, self).exec()
        # 刷新勾选列表
        for pid, cb in list(self._checks.items()):
            cb.setParent(None)
        self._checks = {}
        # 简易重建
        self.reject()
        MultiPartyPicker(self._batch_id, self._role).exec()

    def _save(self):
        ids = [pid for pid, cb in self._checks.items() if cb.isChecked()]
        db.set_batch_parties(self._batch_id, self._role, ids)
        self.accept()


# ════════════════════ 船期变更登记 ════════════════════

class ScheduleChangeDialog(QDialog):
    """船期变更：新 ETD/ETA + 原因/来源 → 影响预览(A/B/C/D) → 应用；含历史视图"""

    def __init__(self, project_id, batch_id=None, parent=None):
        super().__init__(parent)
        self._project_id = project_id
        self._batch_id = batch_id or db.current_batch_id(project_id)
        self.setWindowTitle("登记船期变更")
        self.setMinimumWidth(700)
        self.setMinimumHeight(600)
        self._build()
        self._load()

    def _build(self):
        from PySide6.QtWidgets import QScrollArea
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)

        route = db.get_route(self._batch_id)
        cur = f"当前船期 ETD {route['etd']} → ETA {route['eta']}" if route and route.get("etd") else "当前批次尚未设定船期"
        cur_lbl = QLabel(cur)
        cur_lbl.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {TEXT_PRIMARY};"
                              f" background: {GRAY_SOFT}; border-radius: 8px; padding: 8px 12px;")
        lay.addWidget(cur_lbl)

        f = QFormLayout()
        f.setLabelAlignment(Qt.AlignRight)
        f.setVerticalSpacing(8)
        dt_row = QHBoxLayout()
        self.new_etd = QDateEdit()
        self.new_etd.setCalendarPopup(True)
        self.new_etd.setDisplayFormat("yyyy-MM-dd")
        self.new_eta = QDateEdit()
        self.new_eta.setCalendarPopup(True)
        self.new_eta.setDisplayFormat("yyyy-MM-dd")
        dt_row.addWidget(QLabel("新 ETD"))
        dt_row.addWidget(self.new_etd)
        dt_row.addWidget(QLabel("新 ETA"))
        dt_row.addWidget(self.new_eta)
        f.addRow("新船期", dt_row)
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("原因（如：船公司改配 / 延期 / 换船）")
        f.addRow("原因", self.reason)
        self.source = QLineEdit()
        self.source.setPlaceholderText("来源（邮件 / 电话 / 船司通知）")
        f.addRow("来源", self.source)
        lay.addLayout(f)

        prev_btn = QPushButton("预览影响")
        prev_btn.setObjectName("secondary")
        prev_btn.setCursor(Qt.PointingHandCursor)
        prev_btn.clicked.connect(self._preview)
        lay.addWidget(prev_btn, 0, Qt.AlignLeft)

        self.preview_box = QFrame()
        self.preview_box.setStyleSheet(
            f"QFrame {{ background: #FAFAFC; border: 1px solid {HAIRLINE}; border-radius: 10px; }}")
        self._pv_lay = QVBoxLayout(self.preview_box)
        self._pv_lay.setContentsMargins(14, 12, 14, 12)
        self.preview_box.setVisible(False)
        lay.addWidget(self.preview_box)

        apply_btn = QPushButton("确认应用船期变更")
        apply_btn.setObjectName("primary")
        apply_btn.setCursor(Qt.PointingHandCursor)
        apply_btn.clicked.connect(self._apply)
        lay.addWidget(apply_btn, 0, Qt.AlignRight)

        hist_lbl = QLabel("变更历史（只读）")
        hist_lbl.setStyleSheet(f"font-size: 12px; color: {TEXT_TERTIARY};")
        lay.addWidget(hist_lbl)
        self.history_list = QListWidget()
        self.history_list.setMaximumHeight(180)
        lay.addWidget(self.history_list, 1)

    def _load(self):
        route = db.get_route(self._batch_id)
        d = _d2q((route or {}).get("etd")) or QDate.currentDate().addDays(7)
        self.new_etd.setDate(d)
        d = _d2q((route or {}).get("eta")) or QDate.currentDate().addDays(60)
        self.new_eta.setDate(d)
        for c in db.get_schedule_changes(self._batch_id):
            self.history_list.addItem(
                f"{c['created_at'][:16]}  {c['rule_class']}类 · ETD {c['old_etd']}→{c['new_etd']} · "
                f"ETA {c['old_eta']}→{c['new_eta']} · 影响 {c['affected_nodes']}节点  · {c['reason'] or ''}")

    def _preview(self):
        try:
            body = sc_preview(self._project_id, self._batch_id,
                              _q2d(self.new_etd.date()), _q2d(self.new_eta.date()))
        except ScheduleError as e:
            QMessageBox.warning(self, "无法预览", str(e))
            return
        lines = [
            ("判定结果", f"{body['class']} 类" + {
                "A": "（整船期同幅平移）", "B": "（仅 ETA 变化 → 境外顺移 + 海运时长重算）",
                "C": "（仅 ETD 变化 → 境内顺移 + 海运 start 变更）",
                "D": "（ETD/ETA 变化幅度不同 → 组合重排）"}[body["class"]]),
            ("ETD", f"{body['old_etd']} → {body['new_etd']}（{body['delta_etd']:+d} 天）"),
            ("ETA", f"{body['old_eta']} → {body['new_eta']}（{body['delta_eta']:+d} 天）"),
            ("海运时长", f"{body['sea_duration']} 天（新 ETA − 新 ETD）"),
            ("受影响节点", f"{body['affected_nodes']} 个（Done/已填实际完成日的冻结节点不动）"),
        ]
        self._render_preview(lines, body)
        self.preview_box.setVisible(True)

    def _render_preview(self, lines, body):
        from PySide6.QtWidgets import QScrollArea
        # 清空
        while self._pv_lay.count():
            it = self._pv_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        title = QLabel("影响预览")
        title.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {TEXT_PRIMARY};")
        self._pv_lay.addWidget(title)
        for k, v in lines:
            row = QHBoxLayout()
            kl = QLabel(k)
            kl.setFixedWidth(80)
            kl.setStyleSheet(f"font-size: 12px; color: {TEXT_TERTIARY};")
            row.addWidget(kl)
            vl = QLabel(v)
            vl.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
            row.addWidget(vl)
            row.addStretch()
            self._pv_lay.addLayout(row)
        warn = QLabel("免堆期/免箱期不自动顺延，请与船公司/堆场复核后人工调整。")
        warn.setWordWrap(True)
        warn.setStyleSheet(f"font-size: 11px; color: {RED};")
        self._pv_lay.addWidget(warn)

    def _apply(self):
        try:
            res = sc_apply(self._project_id, self._batch_id,
                           _q2d(self.new_etd.date()), _q2d(self.new_eta.date()),
                           reason=self.reason.text().strip(),
                           source=self.source.text().strip())
        except ScheduleError as e:
            QMessageBox.warning(self, "变更被拦截", str(e))
            return
        QMessageBox.information(self, "船期变更", res["alert"])
        self.accept()


# ════════════════════ 计划日期只读视图 ════════════════════

class PlanDateDialog(QDialog):
    """计划日期只读视图：由 §5.3 规则唯一计算，标注冻结/日历模式与最后变化标记"""

    def __init__(self, project_id, batch_id=None, parent=None):
        super().__init__(parent)
        self._project_id = project_id
        self._batch_id = batch_id or db.current_batch_id(project_id)
        self.setWindowTitle("计划日期（§5.3 计算）")
        self.setMinimumWidth(720)
        self.setMinimumHeight(480)
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)
        s = schedule2.result_summary(self._project_id, self._batch_id)
        hint = QLabel(
            "境内=倒排 · 境外=顺排 · 海运=ETA−ETD；标记「冻结」的节点为已完成/已填实际完成日，"
            "不参与重算。手动位移已叠加在下方日期之上。")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"font-size: 12px; color: {TEXT_TERTIARY};")
        lay.addWidget(hint)

        table = QTableWidget(0, 6)
        table.setHorizontalHeaderLabels(["序", "节点", "区域", "计划开始", "计划结束", "标记"])
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(30)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setShowGrid(False)

        area_cn = {"DOME": "境内", "SEA": "海运", "OVERSEA": "境外"}
        for n in s["nodes"]:
            r = table.rowCount()
            table.insertRow(r)
            marks = []
            if n["frozen"]:
                marks.append("冻结")
            if n["mode"] == "WORKDAY":
                marks.append("工作日")
            st = str(n["status"] or "")
            vals = [str(n["seq"]), f"{n['node_id']}. {n['node_name']}",
                    area_cn.get(n["area"], n["area"]),
                    n["plan_start"] or "-", n["plan_end"] or "-",
                    " · ".join(marks) if marks else ("已完成" if st == "Done" else "未开始")]
            for c, v in enumerate(vals):
                cell = QTableWidgetItem(v)
                cell.setTextAlignment(Qt.AlignCenter if c == 0 else Qt.AlignLeft)
                if (c == 3 or c == 4) and not n["plan_start"]:
                    cell.setForeground(QColor("#B8B8BE"))
                if marks and "冻结" in marks and c == 5:
                    cell.setForeground(QColor("#B8860B"))
                table.setItem(r, c, cell)
        lay.addWidget(table, 1)

        foot = QHBoxLayout()
        etdeta = QLabel(f"锚点 ETD {s['etd'] or '-'} · ETA {s['eta'] or '-'}")
        etdeta.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
        foot.addWidget(etdeta)
        foot.addStretch()
        close = QPushButton("关闭")
        close.setObjectName("secondary")
        close.clicked.connect(self.accept)
        foot.addWidget(close)
        lay.addLayout(foot)


# ════════════════════ 客户主档管理 ════════════════════

class PartyEditDialog(QDialog):
    """单个客户/货主 增/改 表单"""

    def __init__(self, group_box, party=None, parent=None):
        super().__init__(parent)
        self._gb = group_box
        self._party = party
        self.setWindowTitle("编辑客户/货主" if party else "新增客户/货主")
        self.setMinimumWidth(440)
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(8)
        f = QFormLayout()
        f.setLabelAlignment(Qt.AlignRight)
        f.setVerticalSpacing(8)

        self.name = QLineEdit()
        self.name.setPlaceholderText("中文/当地名称（必填）")
        f.addRow("名称 *", self.name)
        self.name_en = QLineEdit()
        self.name_en.setPlaceholderText("英文名称（提单/单证抬头）")
        f.addRow("英文名", self.name_en)
        r = QHBoxLayout()
        self.country = QLineEdit()
        self.country.setPlaceholderText("国家/地区")
        self.tax_id_type = QComboBox()
        for t in ("CNPJ", "CPF", "VAT", "EIN", "OTHER"):
            self.tax_id_type.addItem(t)
        r.addWidget(self.country)
        r.addWidget(self.tax_id_type)
        f.addRow("国家 / 税号类型", r)
        self.tax_id = QLineEdit()
        self.tax_id.setPlaceholderText("税号（目的国要求时必填，如巴西 CNPJ/CPF）")
        f.addRow("税号", self.tax_id)
        self.address = QLineEdit()
        f.addRow("地址", self.address)
        r2 = QHBoxLayout()
        self.contact = QLineEdit()
        self.contact.setPlaceholderText("联系人")
        self.phone = QLineEdit()
        self.phone.setPlaceholderText("电话")
        r2.addWidget(self.contact)
        r2.addWidget(self.phone)
        f.addRow("联系人 / 电话", r2)
        self.email = QLineEdit()
        self.email.setPlaceholderText("邮箱")
        f.addRow("邮箱", self.email)

        role_lbl = QLabel("角色（同一公司可多角色）")
        role_lbl.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
        f.addRow("角色", role_lbl)
        self._role_checks = {}
        role_row = QHBoxLayout()
        for role, label in (("CUSTOMER", "客户"), ("SHIPPER", "发货人"),
                            ("CONSIGNEE", "收货人"), ("NOTIFY", "通知方"),
                            ("IMPORTER", "进口商")):
            cb = QCheckBox(label)
            self._role_checks[role] = cb
            role_row.addWidget(cb)
        f.addRow("", role_row)
        lay.addLayout(f)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton("取消")
        cancel.setObjectName("secondary")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        save = QPushButton("保存")
        save.setObjectName("primary")
        save.clicked.connect(self._save)
        btns.addWidget(save)
        lay.addLayout(btns)

        if self._party:
            p = self._party
            self.name.setText(p.get("party_name") or "")
            self.name_en.setText(p.get("name_en") or "")
            self.country.setText(p.get("country") or "")
            for i in range(self.tax_id_type.count()):
                if self.tax_id_type.itemText(i) == (p.get("tax_id_type") or ""):
                    self.tax_id_type.setCurrentIndex(i)
            self.tax_id.setText(p.get("tax_id") or "")
            self.address.setText(p.get("address") or "")
            self.contact.setText(p.get("contact_name") or "")
            self.phone.setText(p.get("phone") or "")
            self.email.setText(p.get("email") or "")
            for role, cb in self._role_checks.items():
                cb.setChecked(role in (p.get("roles") or []))

    def _save(self):
        name = self.name.text().strip()
        if not name:
            QMessageBox.warning(self, "无法保存", "请填写客户/货主名称")
            return
        roles = [r for r, cb in self._role_checks.items() if cb.isChecked()]
        kw = dict(party_name=name, name_en=self.name_en.text().strip() or None,
                  country=self.country.text().strip() or None,
                  tax_id=self.tax_id.text().strip() or None,
                  tax_id_type=self.tax_id_type.currentText(),
                  address=self.address.text().strip() or None,
                  contact_name=self.contact.text().strip() or None,
                  phone=self.phone.text().strip() or None,
                  email=self.email.text().strip() or None,
                  roles=roles)
        if self._party:
            db.update_party(self._party["party_id"], **kw)
        else:
            db.insert_party(**kw)
        self.accept()


class CustomerMasterDialog(QDialog):
    """客户/货主主档管理：列表 + 增改删；可批量绑定到当前批次角色"""

    def __init__(self, batch_id=None, parent=None):
        super().__init__(parent)
        self._batch_id = batch_id
        self.setWindowTitle("客户 / 货主主档管理")
        self.setMinimumSize(720, 480)
        self._build()
        self._load()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)
        head = QHBoxLayout()
        t = QLabel("客户 / 货主主档")
        t.setStyleSheet(f"font-size: 16px; font-weight: 600; color: {TEXT_PRIMARY};")
        head.addWidget(t)
        head.addStretch()
        new_btn = QPushButton("＋ 新增")
        new_btn.setObjectName("primary")
        new_btn.setCursor(Qt.PointingHandCursor)
        new_btn.clicked.connect(self._add)
        head.addWidget(new_btn)
        lay.addLayout(head)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["名称", "英文名", "国家", "税号", "角色"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setColumnWidth(1, 140)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setShowGrid(False)
        self.table.itemSelectionChanged.connect(self._on_select)
        lay.addWidget(self.table, 1)

        cur_lbl = QLabel("当前批次角色绑定：")
        cur_lbl.setStyleSheet(f"font-size: 12px; color: {TEXT_TERTIARY};")
        lay.addWidget(cur_lbl)
        self._role_combo = QComboBox()
        self._role_combo.addItem("（选择要绑定到批次的角色）", None)
        for role, label in (("CUSTOMER", "客户"), ("SHIPPER", "发货人"),
                            ("CONSIGNEE", "收货人"), ("IMPORTER", "进口商"),
                            ("NOTIFY", "通知方")):
            self._role_combo.addItem(label, role)
        bind_row = QHBoxLayout()
        bind_row.addWidget(self._role_combo, 1)
        bind_btn = QPushButton("绑定选中到批次")
        bind_btn.setObjectName("secondary")
        bind_btn.setCursor(Qt.PointingHandCursor)
        bind_btn.clicked.connect(self._bind)
        bind_row.addWidget(bind_btn)
        lay.addLayout(bind_row)

        btns = QHBoxLayout()
        edit_btn = QPushButton("编辑")
        edit_btn.setObjectName("secondary")
        edit_btn.clicked.connect(self._edit)
        btns.addWidget(edit_btn)
        del_btn = QPushButton("删除")
        del_btn.setObjectName("ghost")
        del_btn.clicked.connect(self._delete)
        btns.addWidget(del_btn)
        btns.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("primary")
        close_btn.clicked.connect(self.accept)
        btns.addWidget(close_btn)
        lay.addLayout(btns)

        self._selected = None

    def _load(self):
        self.table.setRowCount(0)
        self._rows = []
        for p in db.list_parties():
            r = self.table.rowCount()
            self.table.insertRow(r)
            roles = p.get("roles") or []
            role_txt = "、".join({"CUSTOMER": "客户", "SHIPPER": "发货人",
                                  "CONSIGNEE": "收货人", "NOTIFY": "通知方",
                                  "IMPORTER": "进口商"}.get(x, x) for x in roles)
            vals = [p["party_name"], p.get("name_en") or "", p.get("country") or "",
                    p.get("tax_id") or "", role_txt]
            for c, v in enumerate(vals):
                cell = QTableWidgetItem(v)
                if c != 0:
                    cell.setForeground(QColor("#4A4A4E"))
                self.table.setItem(r, c, cell)
            self._rows.append(p["party_id"])

    def _on_select(self):
        rows = {i.row() for i in self.table.selectedIndexes()}
        self._selected = (next(iter(rows)) if rows else None)

    def _add(self):
        d = PartyEditDialog(group_box=None, party=None, parent=self)
        if d.exec():
            self._load()

    def _edit(self):
        if self._selected is None:
            return
        pid = self._rows[self._selected]
        p = db.get_party(pid)
        d = PartyEditDialog(group_box=None, party=p, parent=self)
        if d.exec():
            self._load()

    def _delete(self):
        if self._selected is None:
            return
        pid = self._rows[self._selected]
        p = db.get_party(pid)
        ret = QMessageBox.question(self, "删除确认",
                                   f"确定删除「{p['party_name']}」？将同时解除其批量绑定。",
                                   QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        conn = db.get_conn()
        conn.execute("DELETE FROM parties WHERE party_id=?", (pid,))
        conn.execute("DELETE FROM party_roles WHERE party_id=?", (pid,))
        conn.execute("DELETE FROM batch_parties WHERE party_id=?", (pid,))
        conn.commit()
        self._load()

    def _bind(self):
        role = self._role_combo.currentData()
        if not role:
            QMessageBox.information(self, "绑定", "请先选择要绑定的角色")
            return
        if self._selected is None:
            QMessageBox.information(self, "绑定", "请先选择要绑定的客户")
            return
        pid = self._rows[self._selected]
        if self._batch_id:
            db.bind_batch_party(self._batch_id, role, pid)
            QMessageBox.information(self, "绑定", "已绑定到当前批次")


# ════════════════════ §8/D14/T20 已取消批次审计 ════════════════════

class CancelledBatchAuditDialog(QDialog):
    """「显示已取消」开关 + 已取消批次完整 `op_log` 只读审计（§8/D14，T20）。

    行为（与 §8「取消批次口径」一致）：
      · 默认隐藏：开关未勾选时列表为空并给出说明；
      · 勾选「显示已取消」→ 列出该（项目/全部）下所有 `status='cancelled'` 的批次；
      · 选中批次 → 只读展示其完整 `op_log`（时间 / 动作 / 对象 / 内容）与取消原因；
      · 只读：本对话框不提供任何写入入口（恢复批次请走批次管理/仪表盘，§8 另定）。
    """

    KIND_LABEL = {
        "batch_cancel": "取消批次", "batch_restore": "恢复批次",
        "batch_edit": "批次变更", "batch_create": "新建批次",
        "batch_status": "批次状态", "batch_complete": "确认完成",
        "batch_close": "关闭批次", "batch_copy": "复制批次",
        "batch_actual_override": "实际值覆盖", "route_change": "换线",
        "batch_schedule_change": "船期变更", "schedule_recompute": "计划重算",
        "node_shift": "推迟/提前", "node_unshift": "撤销位移", "node_done": "自动完成",
        "file_submit": "提交单证", "file_withdraw": "撤交单证",
        "project_create": "新建项目", "project_close": "项目完结",
        "vessel_position": "船位登记", "cargo_edit": "货物变更",
    }

    def __init__(self, project_id=None, parent=None):
        """project_id=None → 全部项目的已取消批次。"""
        super().__init__(parent)
        self._project_id = project_id
        self.setWindowTitle("已取消批次审计（只读）")
        self.setMinimumSize(900, 560)
        self._rows = []
        self._build()
        self._reload()

    # ── 构建 ──
    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("已取消批次审计")
        title.setStyleSheet(f"font-size: 16px; font-weight: 600; color: {TEXT_PRIMARY};")
        head.addWidget(title)
        head.addSpacing(12)
        self.show_cancelled = QCheckBox("显示已取消")
        self.show_cancelled.setToolTip("§8/D14：取消批次默认全模块隐藏，勾选后可只读查看其完整 op_log")
        self.show_cancelled.toggled.connect(self._reload)
        head.addWidget(self.show_cancelled)
        head.addStretch()
        self.count_lbl = QLabel("")
        self.count_lbl.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
        head.addWidget(self.count_lbl)
        lay.addLayout(head)

        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"font-size: 12px; color: {TEXT_TERTIARY};")
        lay.addWidget(self.hint)

        split = QHBoxLayout()
        split.setSpacing(12)

        left = QVBoxLayout()
        left_lbl = QLabel("已取消批次")
        left_lbl.setStyleSheet(f"font-size: 12px; color: {TEXT_TERTIARY};")
        left.addWidget(left_lbl)
        self.batch_table = QTableWidget(0, 4)
        self.batch_table.setHorizontalHeaderLabels(["批次", "状态", "取消原因", "取消时间"])
        self.batch_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.batch_table.verticalHeader().setVisible(False)
        self.batch_table.verticalHeader().setDefaultSectionSize(28)
        self.batch_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.batch_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.batch_table.setShowGrid(False)
        self.batch_table.itemSelectionChanged.connect(self._load_log)
        left.addWidget(self.batch_table, 1)
        split.addLayout(left, 4)

        right = QVBoxLayout()
        right_lbl = QLabel("操作日志（op_log · 只读）")
        right_lbl.setStyleSheet(f"font-size: 12px; color: {TEXT_TERTIARY};")
        right.addWidget(right_lbl)
        self.log_table = QTableWidget(0, 4)
        self.log_table.setHorizontalHeaderLabels(["时间", "动作", "对象", "内容"])
        self.log_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.log_table.setColumnWidth(0, 130)
        self.log_table.setColumnWidth(1, 90)
        self.log_table.setColumnWidth(2, 110)
        self.log_table.verticalHeader().setVisible(False)
        self.log_table.verticalHeader().setDefaultSectionSize(28)
        self.log_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.log_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.log_table.setShowGrid(False)
        right.addWidget(self.log_table, 1)
        split.addLayout(right, 6)
        lay.addLayout(split, 1)

        foot = QHBoxLayout()
        ro = QLabel("本视图为只读审计：如需恢复批次，请使用批次列表的「恢复」入口（§8）。")
        ro.setStyleSheet(f"font-size: 11px; color: {TEXT_TERTIARY};")
        foot.addWidget(ro)
        foot.addStretch()
        close = QPushButton("关闭")
        close.setObjectName("secondary")
        close.clicked.connect(self.accept)
        foot.addWidget(close)
        lay.addLayout(foot)

    # ── 数据 ──
    def _cancelled_batches(self):
        out = []
        if self._project_id:
            pids = [self._project_id]
        else:
            pids = [p["project_id"] for p in db.get_projects_by_status("Active")
                    + db.get_projects_by_status("Cancelled")
                    + db.get_projects_by_status("Completed")]
        for pid in pids:
            for b in db.get_batches(pid, include_cancelled=True):
                if b.get("status") == "cancelled":
                    out.append(b)
        return out

    def _reload(self):
        show = self.show_cancelled.isChecked()
        self.batch_table.setRowCount(0)
        self.log_table.setRowCount(0)
        self._rows = []
        if not show:
            self.hint.setText("已取消批次默认隐藏（§8/D14）。勾选「显示已取消」可只读查看其完整 op_log。")
            self.count_lbl.setText("")
            return
        rows = self._cancelled_batches()
        self._rows = rows
        self.hint.setText(
            f"共 {len(rows)} 个已取消批次。取消期间禁止任何写入（后端拒绝）；"
            f"已生成的历史报告文件不追溯修改（§11）。")
        self.count_lbl.setText(f"已取消 {len(rows)} 个批次")
        for b in rows:
            r = self.batch_table.rowCount()
            self.batch_table.insertRow(r)
            cancel_at = ""
            logs = db.get_op_log_range(b["project_id"], batch_id=b["batch_id"], limit=1000)
            for lg in logs:
                if lg["kind"] in ("batch_cancel", "batch_restore"):
                    cancel_at = lg["created_at"][:16]
            vals = [_batch_label_simple(b), "已取消", b.get("cancel_reason") or "—", cancel_at or "—"]
            for c, v in enumerate(vals):
                cell = QTableWidgetItem(str(v))
                if c == 1:
                    cell.setForeground(QColor(CANCELLED_COLOR))
                self.batch_table.setItem(r, c, cell)
        if rows:
            self.batch_table.selectRow(0)

    def _load_log(self):
        self.log_table.setRowCount(0)
        rows = {i.row() for i in self.batch_table.selectedIndexes()}
        if not rows:
            return
        b = self._rows[sorted(rows)[0]]
        logs = db.get_op_log_range(b["project_id"], batch_id=b["batch_id"], limit=1000)
        if not logs:
            # 无批次维度日志时退回项目级（迁移前老数据），仍只读展示
            logs = [lg for lg in db.get_op_log_range(b["project_id"], limit=1000)
                    if not lg.get("batch_id")]
        for lg in logs:
            r = self.log_table.rowCount()
            self.log_table.insertRow(r)
            vals = [lg["created_at"][:19].replace("T", " "),
                    self.KIND_LABEL.get(lg["kind"], lg["kind"]),
                    lg.get("subject") or "—",
                    _detail_of(lg.get("detail"))]
            for c, v in enumerate(vals):
                self.log_table.setItem(r, c, QTableWidgetItem(str(v)))
        self.log_table.resizeRowsToContents()


# ════════════════════ §8/D18/T21 换线影响评估 ════════════════════

class RouteImpactDialog(QDialog):
    """换线影响评估清单（只读）：新增 / 删除 / 改名 / 顺序变化 / 日期变化 + 复核提示（§8，T21）。

    数据来源：`services.batches.change_route` 已持久化的 `batch_route_changes.impact`
    （换线时自动生成），或对给定 new_route 现算不落库（`route_impact`）。
    """

    def __init__(self, batch_id, new_route=None, parent=None):
        super().__init__(parent)
        self._batch_id = batch_id
        self._new_route = new_route
        self.setWindowTitle("换线影响评估")
        self.setMinimumSize(720, 540)
        self.impact = None
        self._build()
        self._load()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)

        head = QHBoxLayout()
        t = QLabel("换线影响评估")
        t.setStyleSheet(f"font-size: 16px; font-weight: 600; color: {TEXT_PRIMARY};")
        head.addWidget(t)
        head.addStretch()
        self.batch_lbl = QLabel("")
        self.batch_lbl.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
        head.addWidget(self.batch_lbl)
        lay.addLayout(head)

        self.hint = QLabel(
            "§8/D18：换线**不自动重排节点**，仅记录变更并给出影响清单；"
            "涉及 ETD/ETA 变化时，计划日期需按 §5.3 复核（本页只提示，不改日期）。")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"font-size: 12px; color: {TEXT_TERTIARY};")
        lay.addWidget(self.hint)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["影响类别", "节点/字段", "说明"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 120)
        self.table.setColumnWidth(1, 200)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setShowGrid(False)
        lay.addWidget(self.table, 1)

        self.recheck_lbl = QLabel("")
        self.recheck_lbl.setWordWrap(True)
        self.recheck_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 600; color: {ORANGE};")
        lay.addWidget(self.recheck_lbl)

        self.history_list = QListWidget()
        self.history_list.setMaximumHeight(120)
        self.history_list.setToolTip("换线变更历史（batch_route_changes，只读）")
        lay.addWidget(self.history_list)

        foot = QHBoxLayout()
        foot.addStretch()
        close = QPushButton("关闭")
        close.setObjectName("secondary")
        close.clicked.connect(self.accept)
        foot.addWidget(close)
        lay.addLayout(foot)

    # ── 数据 ──
    def _load(self):
        b = db.get_batch(self._batch_id)
        self.batch_lbl.setText(_batch_label_simple(b))
        if self._new_route is not None:
            self.impact = batches_svc.route_impact(self._batch_id, self._new_route)
        else:
            self.impact = self._latest_persisted()
        self._render(self.impact)
        for ch in db.get_route_changes(self._batch_id, limit=20):
            self.history_list.addItem(
                f"{ch['created_at'][:16]} · {ch.get('reason') or '换线'}"
                f" · 影响：{_impact_brief(ch.get('impact'))}")

    def _key_name(self, k):
        """node_key → `key（节点名）`；查不到中文名时只显示 key。"""
        n = db.node_by_key(self._batch_id, k)
        return f"{k}（{n['node_name']}）" if n else str(k)

    def _latest_persisted(self):
        """取最近一次换线记录的 impact（JSON）；没有记录时返回空清单。"""
        import json
        rows = db.get_route_changes(self._batch_id, limit=1)
        if not rows:
            return {"added": [], "removed": [], "renamed": [], "reordered": [],
                    "etd_changed": False, "eta_changed": False, "fields": {},
                    "nodes_need_recheck": 0}
        raw = rows[0].get("impact")
        try:
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def _render(self, im):
        im = im or {}
        added = im.get("added") or []
        removed = im.get("removed") or []
        renamed = im.get("renamed") or []
        reordered = im.get("reordered") or []
        fields = im.get("fields") or {}
        rows = []
        for k in added:
            rows.append(("新增", self._key_name(k), "该节点为换线后新增，需确认是否已补建/补录"))
        for k in removed:
            rows.append(("删除", self._key_name(k), "该节点为换线后删除，其单证与计划日期需人工确认"))
        for r in renamed:
            rows.append(("改名", self._key_name(r.get("node_key")),
                         f"{r.get('old')} → {r.get('new')}"))
        for k in reordered:
            rows.append(("顺序变化", self._key_name(k), "模板顺序调整，计划日期未改动，请复核"))
        for f, v in fields.items():
            label = ROUTE_FIELD_LABEL.get(f, f)
            cls = "日期变化" if f in ("etd", "eta") else "线路字段"
            rows.append((cls, label, f"{v.get('old') or '（空）'} → {v.get('new') or '（空）'}"))
        self.table.setRowCount(0)
        for cls, name, desc in rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            for c, v in enumerate((cls, name, desc)):
                cell = QTableWidgetItem(str(v))
                if cls in ("新增", "删除", "日期变化"):
                    cell.setForeground(QColor(ORANGE))
                self.table.setItem(r, c, cell)
        if not rows:
            self.table.insertRow(0)
            self.table.setItem(0, 0, QTableWidgetItem("无"))
            self.table.setItem(0, 1, QTableWidgetItem("—"))
            self.table.setItem(0, 2, QTableWidgetItem("无换线记录或本次换线未产生节点/日期差异"))

        n = im.get("nodes_need_recheck") or 0
        if n:
            self.recheck_lbl.setText(f"以下 {n} 个节点计划日期需复核（换线不自动重排，§8/D18）")
        elif im.get("etd_changed") or im.get("eta_changed"):
            self.recheck_lbl.setText("ETD/ETA 已变化，但当前批次无未冻结节点需复核。")
        else:
            self.recheck_lbl.setText("未涉及 ETD/ETA 变化，无需复核计划日期。")


ROUTE_FIELD_LABEL = {
    "mode_primary": "运输方式", "mode_chain": "运输方式链", "country": "目的国",
    "template_key": "节点模板", "export_port": "出发港", "etd": "ETD", "eta": "ETA",
    "customs_broker": "报关行", "customs_mode": "报关方式", "release_mode": "换单方式",
    "container_pickup_location": "提箱地", "empty_return_location": "还箱地",
    "free_demurrage_until": "免堆期截止", "free_detention_until": "免箱期截止",
}

CANCELLED_COLOR = "#8E8E93"


def _batch_label_simple(b):
    if not b:
        return "—"
    no = b.get("batch_no") or b.get("batch_id") or "—"
    nm = (b.get("batch_name") or "").strip()
    return no if (not nm or nm == no or no.endswith("-" + nm)) else f"{no} · {nm}"


def _detail_of(raw):
    try:
        from services.oplog import display_detail
        return display_detail(raw or "")
    except Exception:
        return str(raw or "")


def _impact_brief(raw):
    import json
    try:
        im = json.loads(raw) if raw else {}
    except Exception:
        return "—"
    bits = []
    for k, lb in (("added", "新增"), ("removed", "删除"),
                  ("renamed", "改名"), ("reordered", "顺序")):
        if im.get(k):
            bits.append(f"{lb} {len(im[k])}")
    if im.get("nodes_need_recheck"):
        bits.append(f"{im['nodes_need_recheck']} 节点复核")
    return "；".join(bits) if bits else "无差异"
