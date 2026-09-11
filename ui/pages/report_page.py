"""
页面：生成报告 — 日报/周报 预览 → 生成 Word（无 python-docx 降级 txt）
风格与全局一致（Apple 白色极简、圆角卡片、紧凑对齐、无 emoji）。
流程：先「预览报告」只渲染不产文件 → 满意后「生成」弹确认框直接导出；
两个按钮互不自动触发（职责分离）。
两级口径（§11）：筛选链 项目 → 批次 → 客户；单批次报告标题带【项目名 · B01】。
"""

from datetime import date, timedelta
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QRadioButton, QButtonGroup, QDateEdit, QComboBox, QCheckBox,
    QTextBrowser, QMessageBox
)

import db
from services.clock import get_today
from services import reporting
from services import report_exporter
from ui.theme import (
    card_shadow, ACCENT, ACCENT_SOFT, TEXT_PRIMARY, TEXT_SECONDARY,
    TEXT_TERTIARY, BORDER, HAIRLINE, GRAY_SOFT, GREEN, BG
)


class ReportPage(QWidget):
    navigate = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._recent = []          # (filename, path, kind)
        self._build()

    # ── UI ──

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 28, 32, 24)
        outer.setSpacing(16)

        title = QLabel("生成报告")
        title.setObjectName("title")
        outer.addWidget(title)

        # 控制条（卡片）
        controls = QFrame()
        controls.setObjectName("card")
        card_shadow(controls, blur=20, dy=5, alpha=18)
        c_layout = QVBoxLayout(controls)
        c_layout.setContentsMargins(20, 16, 20, 16)
        c_layout.setSpacing(14)

        row1 = QHBoxLayout()
        row1.setSpacing(20)
        self.type_group = QButtonGroup(self)
        self.radio_daily = QRadioButton("日报")
        self.radio_weekly = QRadioButton("周报")
        self.radio_daily.setChecked(True)
        self.type_group.addButton(self.radio_daily)
        self.type_group.addButton(self.radio_weekly)
        self.radio_daily.toggled.connect(self._on_type_changed)
        row1.addWidget(self.radio_daily)
        row1.addWidget(self.radio_weekly)

        row1.addSpacing(8)
        row1.addWidget(QLabel("日期"))
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setFixedWidth(360)
        self.date_edit.setFixedHeight(38)
        self.date_edit.setStyleSheet(
            f"QDateEdit {{ background: {BG}; border: 1px solid {BORDER};"
            f"border-radius: 10px; padding: 0 14px; font-size: 16px;"
            f"font-weight: 600; color: {TEXT_PRIMARY}; min-height: 24px; }}"
            f"QDateEdit::drop-down {{ border: none; width: 26px; }}")
        row1.addWidget(self.date_edit)

        row1.addSpacing(8)
        row1.addWidget(QLabel("项目"))
        self.project_combo = QComboBox()
        self.project_combo.setFixedWidth(200)
        self.project_combo.currentIndexChanged.connect(self._on_project_changed)
        row1.addWidget(self.project_combo)

        row1.addSpacing(8)
        row1.addWidget(QLabel("批次"))
        self.batch_combo = QComboBox()
        self.batch_combo.setFixedWidth(190)
        self.batch_combo.setToolTip("选「全部批次」= 全项目报告（标题保持原名）；"
                                    "选具体批次 = 单批次报告（标题带【项目名 · B01】）")
        row1.addWidget(self.batch_combo)

        row1.addSpacing(8)
        row1.addWidget(QLabel("客户"))
        self.customer_combo = QComboBox()
        self.customer_combo.setFixedWidth(170)
        row1.addWidget(self.customer_combo)

        # §T30 报关要素筛选（报关行 / 报关方式）
        row1.addSpacing(8)
        row1.addWidget(QLabel("报关行"))
        self.broker_combo = QComboBox()
        self.broker_combo.setFixedWidth(150)
        self.broker_combo.setToolTip("按报关行筛选（T30：报关要素可筛选）")
        row1.addWidget(self.broker_combo)

        row1.addSpacing(8)
        row1.addWidget(QLabel("报关方式"))
        self.mode_combo = QComboBox()
        self.mode_combo.setFixedWidth(130)
        self.mode_combo.setToolTip("按报关方式筛选（一般贸易/买单/市场采购/跨境电商）")
        row1.addWidget(self.mode_combo)
        row1.addStretch()

        self.brief_check = QCheckBox("简报模式（仅概览 + 未完成清单）")
        row1.addWidget(self.brief_check)
        c_layout.addLayout(row1)

        # 按钮行
        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)
        self.preview_btn = QPushButton("预览报告")
        self.preview_btn.setObjectName("primary")
        self.preview_btn.setCursor(Qt.PointingHandCursor)
        self.preview_btn.clicked.connect(self._preview)
        btn_row.addWidget(self.preview_btn)

        self.gen_btn = QPushButton("生成 Word 报告")
        self.gen_btn.setCursor(Qt.PointingHandCursor)
        self.gen_btn.clicked.connect(self._generate)
        btn_row.addWidget(self.gen_btn)
        btn_row.addStretch()

        self.recent_lbl = QLabel("最近生成：—")
        self.recent_lbl.setStyleSheet(f"font-size: 12px; color: {TEXT_SECONDARY};")
        btn_row.addWidget(self.recent_lbl)

        self.open_btn = QPushButton("打开所在目录")
        self.open_btn.setCursor(Qt.PointingHandCursor)
        self.open_btn.setStyleSheet(
            f"background: {ACCENT_SOFT}; color: {ACCENT}; border: none;"
            f"border-radius: 8px; padding: 6px 14px; font-weight: 600;")
        self.open_btn.hide()
        self.open_btn.clicked.connect(lambda: report_exporter.open_report_folder())
        btn_row.addWidget(self.open_btn)
        c_layout.addLayout(btn_row)
        outer.addWidget(controls)

        # 预览区
        self.preview = QTextBrowser()
        self.preview.setObjectName("reportPreview")
        self.preview.setStyleSheet(
            f"QTextBrowser {{ background: {BG}; border: 1px solid {BORDER};"
            f"border-radius: 12px; padding: 14px; font-family: 'Microsoft YaHei';"
            f"font-size: 13px; color: {TEXT_PRIMARY}; }}")
        self.preview.setOpenExternalLinks(False)
        self.preview.setPlaceholderText("点击「预览报告」查看内容，满意后再生成。")
        outer.addWidget(self.preview, stretch=1)

        self._load_projects()
        self._load_customers()
        self._sync_date_default()

    def _sync_date_default(self):
        from PySide6.QtCore import QDate
        t = get_today()
        if self.radio_weekly.isChecked():
            t = t - timedelta(days=t.weekday())
        self.date_edit.setDate(QDate(t.year, t.month, t.day))

    def _on_type_changed(self, *_):
        self._sync_date_default()

    def _load_projects(self):
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        self.project_combo.addItem("全部项目", None)
        for p in db.get_projects_by_status("Active") + db.get_projects_by_status("Cancelled"):
            self.project_combo.addItem(p["project_name"], p["project_id"])
        self.project_combo.blockSignals(False)
        # 保留此前选择
        last = self._selected_project()
        if last is None:
            self.project_combo.setCurrentIndex(0)
        self._load_batches()
        self._load_customers()
        self._load_customs()

    def _load_batches(self):
        """批次下拉（§11 二级口径）：全部批次 / 各启用批次。

        已取消批次按 §8/D14 全模块隐藏，需查审计请走批次列表的「显示已取消」开关。
        """
        self.batch_combo.blockSignals(True)
        self.batch_combo.clear()
        self.batch_combo.addItem("全部批次", None)
        for pid in self._project_ids():
            for b in db.get_batches(pid):
                self.batch_combo.addItem(reporting.batch_label(b), b["batch_id"])
        self.batch_combo.setCurrentIndex(0)
        self.batch_combo.blockSignals(False)

    def _load_customers(self):
        """客户下拉（§5.5 D32 / §11）：全部客户 / 未填写 / 单客户。"""
        cur = self.customer_combo.currentData() if self.customer_combo.count() else None
        self.customer_combo.blockSignals(True)
        self.customer_combo.clear()
        for opt in reporting.customer_options():
            self.customer_combo.addItem(opt["name"], opt["id"])
        for i in range(self.customer_combo.count()):
            if self.customer_combo.itemData(i) == cur:
                self.customer_combo.setCurrentIndex(i)
                break
        self.customer_combo.blockSignals(False)

    def _load_customs(self):
        """§T30 报关要素下拉：报关行 / 报关方式（含「全部」）。"""
        from services import customs_stats as cs
        for combo, opts in ((self.broker_combo, cs.customs_broker_options()),
                            (self.mode_combo, cs.customs_mode_options())):
            cur = combo.currentData() if combo.count() else None
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("全部", None)
            for o in opts or []:
                if o.get("value") is None:
                    continue
                combo.addItem(o["label"], o["value"])
            for i in range(combo.count()):
                if combo.itemData(i) == cur:
                    combo.setCurrentIndex(i)
                    break
            combo.blockSignals(False)

    def _selected_broker(self):
        return self.broker_combo.currentData() if self.broker_combo.count() else None

    def _selected_customs_mode(self):
        return self.mode_combo.currentData() if self.mode_combo.count() else None

    def _project_ids(self):
        pid = self._selected_project()
        if pid:
            return [pid]
        return [p["project_id"]
                for p in db.get_projects_by_status("Active")
                + db.get_projects_by_status("Cancelled")]

    def _on_project_changed(self, *_):
        self._load_batches()
        self._load_customers()
        self._load_customs()

    def _selected_project(self):
        return self.project_combo.currentData()

    def _selected_batch(self):
        return self.batch_combo.currentData() if self.batch_combo.count() else None

    def _selected_customer(self):
        return self.customer_combo.currentData() if self.customer_combo.count() else None

    # ── 报告模型 ──

    def _ref_date(self):
        py = self.date_edit.date().toPython()
        return date(py.year, py.month, py.day)

    def _kind(self):
        return "weekly" if self.radio_weekly.isChecked() else "daily"

    def _build_model(self, for_generate=False):
        kind = self._kind()
        ref = self._ref_date()
        project_filter = self._selected_project()
        report_no = (report_exporter.next_report_no(ref) if for_generate
                     else report_exporter.peek_report_no(ref))
        return reporting.build_report(kind, ref, project_filter=project_filter,
                                      brief=self.brief_check.isChecked(),
                                      report_no=report_no,
                                      batch_filter=self._selected_batch(),
                                      customer_filter=self._selected_customer(),
                                      customs_broker=self._selected_broker(),
                                      customs_mode=self._selected_customs_mode())

    # ── 预览（只渲染，不产文件、不占用编号） ──

    def _preview(self):
        model = self._build_model(for_generate=False)
        self._display(model)
        self.gen_btn.setText("生成 Word 报告" if report_exporter.docx_available()
                             else "生成纯文本 (.txt)")

    def _display(self, model):
        html = reporting.render_html(model)
        self.preview.setHtml(html)

    # ── 生成（确认后直接导出） ──

    def _generate(self):
        model = self._build_model(for_generate=True)
        blocks = self._blocks_for(model)
        wc_note = ""
        m_c = model["overview"]["project_count"]
        b_c = model["overview"].get("batch_count", 0)
        batch_txt = f" · {b_c} 个批次" if b_c else ""
        e_count = len(model["unfinished"])
        e_txt = f", {e_count} 项逾期" if e_count else "，无逾期"
        range_txt = model["range_text"]
        if model["kind"] == "weekly" and model["weekly_compare"] is None:
            wc_note = "，前一周无数据（首份周报）"

        confirm = QMessageBox(self)
        confirm.setWindowTitle("生成报告")
        confirm.setText(
            f"将生成 {model['kind']}报告：{model['title']}\n"
            f"范围 {range_txt} · 包含 {m_c} 个项目{batch_txt}{e_txt}{wc_note}。\n"
            f"筛选：{model['project_filter_label']}\n"
            f"编号：{model['report_no']}\n确认导出吗？")
        ok = confirm.addButton("确认生成", QMessageBox.AcceptRole)
        confirm.addButton("取消", QMessageBox.RejectRole)
        confirm.exec()
        if confirm.clickedButton() != ok:
            return

        path, kind = report_exporter.export(model, blocks)
        self._recent.insert(0, (kind, path))
        self._recent = self._recent[:5]
        self.recent_lbl.setText(f"最近生成：{kind.upper()} 已保存")
        self.open_btn.show()
        self._display(model)
        self.tray_note(kind, path)
        # 展示预览
        self.preview.setHtml(reporting.render_html(model))

    def tray_note(self, kind, path):
        # 触发主窗口托盘提示（若存在）
        win = self.window()
        if hasattr(win, "tray"):
            try:
                win.tray.showMessage("报告已生成",
                                     f"{kind.upper()} → {path}",
                                     win.tray.Information)
            except Exception:
                pass

    def _blocks_for(self, model):
        all_blks = reporting.blocks(model)
        if not model["brief"]:
            return all_blks
        # 简报：仅保留头部 + 「一、总体概览」与「二、未完成清单」两个板块
        want = {"一、总体概览", "二、未完成清单"}
        keep, collecting = [], False
        for b in all_blks:
            if b["t"] == "h":
                collecting = b["text"] in want
                if collecting:
                    keep.append(b)
                continue
            if collecting or b["t"] in ("title",):
                keep.append(b)
            elif not keep and b["t"] in ("para", "note"):
                keep.append(b)   # 头部信息（标题/编号/范围）
        return keep

    def refresh(self):
        self._load_projects()
        self._sync_date_default()