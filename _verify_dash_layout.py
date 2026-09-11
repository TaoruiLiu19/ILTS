"""
总看板 / 甘特工作台布局与数据链路校验（v6.9）：
  A 卡片：恒定高度摘要卡、批次号完整显示、信息全保留、多批次长名称不撑爆
  B 空批次：工作台占位卡 + 「补齐计划节点」一键入口（有/无船期两条路径）
  C services.batches.ensure_batch_nodes：按 15 节点模板补齐（幂等 + 边界 + 取消守卫）
  D 端到端：新增批次 → BatchDialog 保存 → 工作台甘特即可用
  E 工作台：合并视图冲突标注 + 导出 PNG + 窗口状态记忆
"""

import os, sys, tempfile, shutil
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:      # Windows 控制台默认 GBK，节点名/emoji 会炸 → 统一 UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import db
from services.clock import set_simulated_today
from datetime import date
set_simulated_today(date(2026, 9, 9))

_TMP = tempfile.mkdtemp(prefix="dashlayout_")
db.DB_PATH = os.path.join(_TMP, "t.db")
db._conn = None
db.init_db()
FAILED = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")
    if not cond:
        FAILED.append(msg)


from mock_data import DEMO_PROJECT, build_project            # noqa: E402
from services import batches as batches_svc                  # noqa: E402
from services import node_template as nt                     # noqa: E402

r = build_project(DEMO_PROJECT)
PID = r["project_id"]
B01 = r["batch_id"]

from PySide6.QtWidgets import QApplication, QMessageBox      # noqa: E402
from PySide6.QtCore import QDate                             # noqa: E402
from PySide6.QtGui import QFontMetrics                       # noqa: E402
app = QApplication(sys.argv)
from ui.theme import GLOBAL_QSS, ensure_check_asset          # noqa: E402
ensure_check_asset()
app.setStyleSheet(GLOBAL_QSS)
from ui.pages.dashboard_page import DashboardPage            # noqa: E402


def cards_of(dp):
    return [dp.list_layout.itemAt(i).widget() for i in range(dp.list_layout.count())
            if dp.list_layout.itemAt(i).widget()
            and dp.list_layout.itemAt(i).widget().__class__.__name__ == "ProjectCard"]


dp = DashboardPage(); dp.resize(1440, 900); dp.show(); dp.refresh(); app.processEvents()
card = cards_of(dp)[0]

# ══════════ A 卡片（恒定高度摘要卡）══════════
print("== A 卡片：恒定高度 + 批次号完整显示 ==")
hf = card.layout().itemAt(0).widget()
check(hf.height() >= 176, f"头部高度内容驱动（实测 {hf.height()}px ≥ 176）")
check(hf.maximumHeight() > 10000, "头部不再固定高度")
check(card.height() <= 220, f"卡片恒定高度（{card.height()}px，无折叠区域）")
check(not hasattr(card, "expand_area"), "卡片已无展开区（甘特/操作面板迁至工作台）")

cb = card.batch_combo
check(cb.height() >= 36, f"批次下拉框高 {cb.height()}px ≥ 36px（QSS padding 需求）")
check(cb.height() >= cb.minimumSizeHint().height(), "下拉框未被压缩")
fm = QFontMetrics(cb.font())
need = max(fm.horizontalAdvance(cb.itemText(i)) for i in range(cb.count()))
check(cb.width() - 40 >= need,
      f"批次号完整显示（文本 {need}px ≤ 可用 {cb.width() - 40}px）")
check(cb.itemText(cb.currentIndex()).startswith("● "),
      f"当前批次以 ● 标记：{cb.itemText(cb.currentIndex())!r}")

check(card.mini_bar.height() == 34, "三区进度条保留（34px）")
for attr, name in (("port_label", "出口港"), ("vessel_label", "班轮"),
                   ("cur_label", "当前节点"), ("etd_label", "ETD"),
                   ("progress_label", "进度"), ("batch_summary", "风险汇总"),
                   ("batch_status", "批次状态")):
    check(bool(getattr(card, attr).text()), f"{name}保留：{getattr(card, attr).text()!r}")

# ══════════ B 空批次：工作台占位 + 一键补齐 ══════════
print("== B 空批次：工作台占位卡与一键补齐 ==")
B02 = db.create_batch(PID)["batch_id"]
db.update_project(PID, current_batch_id=B02)
card._load(); card._after_change(); app.processEvents()
check(len(card._nodes) == 0, f"新批次当前 {len(card._nodes)} 节点（复现用户场景）")
check(card.height() <= 220, f"空批次下卡片高度不变（{card.height()}px）")

wb = dp.open_workbench(PID, B02, "single"); app.processEvents()
check(wb.isVisible(), "工作台打开")
g = wb._gantt_grid
check(g is not None, "空批次仍构建了甘特控件")
check(g.auto_height() >= 180, f"空批次甘特占位高度 {g.auto_height()}px（原为 0 → 整块消失）")
check(g._canvas._empty is True, "甘特进入空批次占位绘制模式")
check(wb._fill_btn.isVisibleTo(wb), "工作台提供「补齐计划节点」一键入口")

# 缺船期 → 不生成节点，走引导（屏蔽模态对话框）
_info, _open = QMessageBox.information, wb._open_batch
QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
wb._open_batch = lambda: None
wb._fill_batch_nodes()
QMessageBox.information = _info
wb._open_batch = _open
check(len(db.get_nodes_by_batch(B02)) == 0, "缺 ETD/ETA 时一键补齐不生成节点（引导去批次管理）")

# ══════════ C ensure_batch_nodes ══════════
print("== C services.batches.ensure_batch_nodes ==")
res = batches_svc.ensure_batch_nodes(PID, B02)
check(res["created"] is False and "ETD" in (res["reason"] or ""),
      f"缺 ETD/ETA 时不生成：{res['reason']}")
db.upsert_route(B02, export_port="QD", etd="2026-11-01", eta="2026-12-12")
res = batches_svc.ensure_batch_nodes(PID, B02)
check(res["created"] is True and res["nodes"] == 15,
      f"按模板生成 15 个节点（files={res['files']}）")
nodes = db.get_nodes_by_batch(B02)
check(all(n["plan_start"] and n["plan_end"] for n in nodes), "全部节点有计划日期")
check([n["node_key"] for n in nodes] == [t["node_key"] for t in nt.template()],
      "node_key 顺序与 15 节点模板一致")
check(db.get_batch(B02)["status"] == "ready", f"批次状态推进为 {db.get_batch(B02)['status']}")
check(batches_svc.ensure_batch_nodes(PID, B02)["created"] is False
      and len(db.get_nodes_by_batch(B02)) == 15, "幂等：重复调用不重复插入")

B04 = db.create_batch(PID)["batch_id"]
db.upsert_route(B04, export_port="QD", etd="2026-11-01", eta="2026-12-12")
db.update_batch(B04, status="cancelled")
res3 = batches_svc.ensure_batch_nodes(PID, B04)
check(res3["created"] is False and "取消" in (res3["reason"] or ""),
      f"已取消批次拒绝写入（§8）：{res3['reason']}")

wb.refresh(); app.processEvents()
check(wb._gantt_grid is not None and wb._gantt_grid.auto_height() > 600,
      f"有节点后工作台甘特完整高度 {wb._gantt_grid.auto_height()}px")
check(not wb._fill_btn.isVisibleTo(wb), "有节点后「补齐计划节点」入口自动隐藏")

# ══════════ D 端到端：新增批次 → 保存 → 甘特可用 ══════════
print("== D 端到端：新增批次 → BatchDialog 保存 → 甘特可用 ==")
from ui.batch_dialogs import BatchDialog                     # noqa: E402
B03 = db.create_batch(PID)
db.update_project(PID, current_batch_id=B03["batch_id"])
card._load(); card._after_change()
wb.sync_batch(B03["batch_id"]); app.processEvents()
check(wb._gantt_grid.auto_height() >= 180, "刚新增的空批次甘特仍可见（占位）")

dlg = BatchDialog(PID, B03["batch_id"])
check(dlg.empty_hint.isVisibleTo(dlg), "空批次对话框显示「保存后生成节点」提示")
dlg.port_combo.setCurrentIndex(1)
dlg.etd_edit.setDate(QDate(2026, 11, 1))
dlg.eta_edit.setDate(QDate(2026, 12, 12))
dlg._save()
check(bool(dlg.auto_note), f"保存后回传提示：{dlg.auto_note}")
check(len(db.get_nodes_by_batch(B03["batch_id"])) == 15, "保存批次后自动生成 15 个节点")
wb.refresh(); app.processEvents()
check(len(wb._nodes) == 15 and wb._gantt_grid.auto_height() > 600,
      f"新增批次甘特正常打开（{wb._gantt_grid.auto_height()}px）")

# ══════════ E 工作台：全批次总览 / 导出 / 状态记忆 ══════════
print("== E 工作台：全批次总览（行=批次 + 提交状态 + 冲突）+ 导出 + 状态记忆 ==")
db.upsert_route(B03["batch_id"], export_port="QD", customs_broker="中外运报关行",
                etd="2026-11-01", eta="2026-12-12")
db.upsert_route(B02, export_port="QD", customs_broker="中外运报关行")
wb.set_mode("merged"); app.processEvents()
m = wb._overview
rows = wb._overview_data
check(len(rows) == 3, f"全批次总览行数 = 启用中批次数（{len(rows)}）")
check(all(len(r["spans"]) == 3 for r in rows), "每行三段区间（境内/海运/境外）齐全")
check(all(r["proj"]["total"] == 3 for r in rows), "项目级单证只算一份（不随批次翻倍）")
check(wb._conflict_count.text() != "", f"冲突条计数：{wb._conflict_count.text()}")
check(wb._overview_summary.text().startswith("3 批次"),
      f"顶栏汇总：{wb._overview_summary.text()}")
check(not wb._right_panel.isVisible() and not hasattr(wb, "_batch_panel"),
      "全批次模式隐藏右栏操作面板，且工作台已无左侧批次卡片")

# 导出 PNG（绕过文件对话框直接 grab）
png = os.path.join(_TMP, "gantt_overview.png")
ok = m.canvas().grab().save(png)
check(ok and os.path.getsize(png) > 2000,
      f"全批次甘特可导出 PNG（{os.path.getsize(png) if ok else 0} 字节）")

check(wb.zoom == "week", "默认周刻度")
wb.set_zoom("day")
check(db.get_setting(wb.K_ZOOM) == "day", "刻度选择写入 settings（跨启动记忆）")
wb.set_mode("single")
wb._toggle_right()
check(db.get_setting(wb.K_RIGHT) == "1", "右栏收纳状态写入 settings")
wb._toggle_right()
wb.set_mode("merged")
check(db.get_setting(wb.K_MODE) == "merged", "模式写入 settings")
wb.close()
check(not wb.isVisible() and len(db.get_setting(wb.K_GEOM) or "") > 0,
      "关闭即隐藏并记忆窗口几何")

print("\n" + ("PASS ALL" if not FAILED else f"FAIL {len(FAILED)}"))
for f in FAILED:
    print(" -", f)
app.quit()
shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(0 if not FAILED else 1)
