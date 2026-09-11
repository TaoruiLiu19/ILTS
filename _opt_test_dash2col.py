"""
甘特工作台验收（原「两栏展开区」验收的迁移版）：
  1) 卡片去折叠：恒定高度摘要卡 + 工作台入口，卡片内不再有 expand_area
  2) 工作台单批次模式：三区相对轴甘特 / 批次列表 / 右栏两标签 / 收纳
  3) 甘特 ↔ 单证清单联动（悬停、点击钉住）
  4) 工作台合并模式：真实日历轴 + 日/周缩放 + 行=节点 + 每批次色带 + 图例 + 冲突带
  5) 操作型工作台：勾单证 / 推迟提前 / 撤销，且只重画必要部分
  6) ScopedScrollArea 滚轮边界吞掉（窗口内滚动不改值）
"""

import os, sys, tempfile, shutil
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import db
from services.clock import set_simulated_today
from datetime import date
set_simulated_today(date(2026, 9, 9))

_TMP = tempfile.mkdtemp(prefix="wb2col_")
db.DB_PATH = os.path.join(_TMP, "t.db")
db._conn = None
db.init_db()
FAILED = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")
    if not cond:
        FAILED.append(msg)


from mock_data import DEMO_PROJECT, build_project          # noqa: E402
from services import batches as bsvc                       # noqa: E402

r = build_project(DEMO_PROJECT)
PID = r["project_id"]
B01 = r["batch_id"]
# 第二个批次：与 B01 同港口/同船期区间，便于验证合并视图与冲突标注
B02 = db.create_batch(PID)["batch_id"]
db.upsert_route(B02, export_port="QD", customs_broker="中外运报关行",
                etd="2026-09-15", eta="2026-10-26",
                free_demurrage_until="2026-10-01")
bsvc.ensure_batch_nodes(PID, B02)
db.upsert_vessel(PID, vessel_name="COSCO INTEGRITY", voyage="V0123", batch_id=B02)
db.update_project(PID, current_batch_id=B01)

from PySide6.QtWidgets import QApplication                    # noqa: E402
from PySide6.QtCore import Qt, QPoint                         # noqa: E402
app = QApplication(sys.argv)
from ui.theme import GLOBAL_QSS, ensure_check_asset           # noqa: E402
ensure_check_asset()
app.setStyleSheet(GLOBAL_QSS)
from ui.pages.dashboard_page import DashboardPage             # noqa: E402
from ui.widgets.scoped_scroll import ScopedScrollArea         # noqa: E402

dp = DashboardPage(); dp.resize(1440, 900); dp.show(); dp.refresh(); app.processEvents()
card = [dp.list_layout.itemAt(i).widget() for i in range(dp.list_layout.count())
        if dp.list_layout.itemAt(i).widget()
        and dp.list_layout.itemAt(i).widget().__class__.__name__ == "ProjectCard"][0]

# ══════════ 1) 卡片去折叠 ══════════
print("== 1) 卡片去折叠：恒定高度摘要卡 ==")
check(not hasattr(card, "expand_area"), "卡片已无 expand_area（折叠机制移除）")
check(not hasattr(card, "_toggle_expand"), "卡片已无 _toggle_expand")
check(card.height() <= 220, f"卡片高度恒定（{card.height()}px，不随节点数变化）")
check(card.mini_bar is not None and card.mini_bar.height() == 34, "三区进度条保留在卡片上")
check(bool(card.etd_label.text()) and bool(card.progress_label.text()),
      f"摘要信息保留：{card.etd_label.text()} / {card.progress_label.text()}")
check(hasattr(card, "workbench_btn") and card.workbench_btn.isVisibleTo(card),
      "卡片提供「甘特工作台」入口")
check(hasattr(card, "open_btn"), "卡片右栏提供「打开工作台」入口")

# ══════════ 2) 工作台单批次模式 ══════════
print("== 2) 工作台单批次模式 ==")
wb = dp.open_workbench(PID, B01, "single"); app.processEvents()
check(wb.isVisible(), "非模态工作台已打开")
check(wb.mode == "single", "默认单批次模式")
check(not wb.isModal(), "非模态（不阻塞看板）")
check(not hasattr(wb, "_batch_panel"),
      "工作台已无左侧批次选择卡片（批次切换走顶部下拉）")
check(wb._batch_combo.count() == 2,
      f"顶部批次下拉列出全部批次（{wb._batch_combo.count()} 项）")
check(wb._gantt_grid is not None and wb._gantt_grid.auto_height() == 755,
      f"单批次甘特完整高度 {wb._gantt_grid.auto_height()}px（15 节点 × 45 + 表头）")
check(wb._gantt_grid.verticalScrollBarPolicy() == Qt.ScrollBarAsNeeded,
      "窗口内甘特纵向按需滚动（窗口不够高时不会整块消失）")
check(wb._tabbar.count() == 2, "右栏含 2 个标签")
check(wb._shift_page is not None and wb._files_page is not None, "两个面板已预构建")
check(wb._chip["missing"].text() == "42", f"统计字牌缺单证 {wb._chip['missing'].text()}")

print("== 2b) 标签切换 / 右栏收纳 ==")
wb._set_active_panel("shift", sync_tabbar=True); app.processEvents()
check(wb._tab == "shift" and wb._tabbar.currentIndex() == 0, "切到动态调整且标签同步")
wb._set_active_panel("files", sync_tabbar=True); app.processEvents()
check(wb._tab == "files" and wb._tabbar.currentIndex() == 1, "切到单证清单")
check(wb._file_panel is not None, "单证面板可访问")
check(wb._right_panel.width() > 400, f"右栏默认展开（{wb._right_panel.width()}px）")
wb._toggle_right(); app.processEvents()
check(wb._right_collapsed and wb._right_panel.width() < 60,
      f"收起后右栏缩为窄条（{wb._right_panel.width()}px）")
check(not wb._tabbar.isVisible() and not wb._panel_host.isVisible(), "收起时标签与面板隐藏")
wb._toggle_right(); app.processEvents()
check(not wb._right_collapsed and wb._tabbar.isVisible(), "再次展开恢复")

# ══════════ 3) 甘特 ↔ 单证清单联动 ══════════
print("== 3) 甘特 ↔ 单证清单联动 ==")
wb._set_active_panel("shift", sync_tabbar=True)
nodes = db.get_nodes_by_batch(B01)
wb._on_gantt_activate(nodes[5]); app.processEvents()
check(wb._tab == "files", "点甘特节点自动切到单证标签")
check(wb._focused_node == nodes[5]["node_id"], "节点已钉住")
wb._on_gantt_activate(nodes[5]); app.processEvents()
check(wb._focused_node is None, "再次点击同节点取消钉住")

# ══════════ 4) 全批次总览模式 ══════════
print("== 4) 全批次总览：行=批次 · 三段色块 · 提交状态 · 冲突 ==")
wb.set_mode("merged"); app.processEvents()
m = wb._overview
check(wb.mode == "merged" and m is not None, "切到全批次模式")
rows = wb._overview_data
check(len(rows) == 2, f"行=2 个批次（{len(rows)}）")
check(all(set(r["spans"]) == {"DOME", "SEA", "OVERSEA"} for r in rows),
      "每行含境内/海运/境外三段色块区间")
check(all(r["docs"]["req_total"] == 39 for r in rows),
      f"提交状态分母 = 批次级必填 39（{rows[0]['docs']}）")
check(all(r["proj"]["total"] == 3 for r in rows),
      f"项目级单证统计 = 3 份（不随批次翻倍）：{rows[0]['proj']}")
check(wb.zoom == "week", "默认周刻度")
week_cols = len(m.canvas()._axis_cols)
check(week_cols <= 20, f"周刻度列数 {week_cols}（一屏可见）")
wb.set_zoom("day"); app.processEvents()
day_cols = len(wb._overview.canvas()._axis_cols)
check(day_cols > week_cols, f"日刻度列数 {day_cols} > 周刻度 {week_cols}（真实日历逐日）")
check(wb._overview.canvas()._w > 1000,
      f"日刻度画布变宽 {wb._overview.canvas()._w}px（横向滚动）")
wb.set_zoom("week"); app.processEvents()

check(not wb._right_panel.isVisible(), "全批次模式隐藏右栏操作面板（操作只在单批次做）")
check(not hasattr(wb, "_batch_panel"), "工作台不再有左侧批次卡片（任何模式都不显示）")
check(not wb._right_panel.isVisible(), "全批次模式隐藏右栏操作面板（操作只在单批次做）")
check(wb._overview_summary.isVisible() and "批次" in wb._overview_summary.text(),
      f"顶栏批次级汇总：{wb._overview_summary.text()}")
check(len(wb._conflicts) > 0, f"冲突扫描命中 {len(wb._conflicts)} 条："
      f"{[c['kind'] for c in wb._conflicts]}")
check(wb._conflict_count.text() == f"{len(wb._conflicts)} 条", "冲突条计数一致")
check(bool(m.canvas().conflict_batch_ids()),
      f"冲突行可在图上定位（{len(m.canvas().conflict_batch_ids())} 个批次）")

print("== 4b) 总览交互：悬停明细 / 点击行跳单批次 ==")
wb._on_overview_hover(wb._overview.canvas().row_payload(0))
txt = wb._hover_info.text()
check(rows[0]["batch_no"] in txt and ("DOME" in txt or "SEA" in txt),
      f"悬停显示批次提交状态与三段区间：{txt[:60]}")
target = rows[-1]["batch_id"]
wb._on_overview_row({"batch_id": target}); app.processEvents()
check(wb.mode == "single", "点击总览某行 → 切回单批次视图")
check(wb._batch_id == target, "当前批次已切到被点击的批次")
check(wb._right_panel.isVisible(), "回到单批次后右栏恢复（可继续操作）")

# ══════════ 5) 操作型工作台 ══════════
print("== 5) 操作：勾单证 / 推迟提前 / 撤销（不重建甘特与面板）==")
wb.set_mode("single"); app.processEvents()
wb._set_active_panel("files")
g0, fp0 = wb._gantt_grid, wb._file_panel
fid = db.get_files_by_batch(B01)[0]["file_id"]
wb._toggle_file(fid, True); app.processEvents()
check(wb._gantt_grid is g0, "勾单证后甘特控件未被重建（避免卡顿）")
check(wb._file_panel is fp0, "勾单证后单证面板未被重建（卡死根因回归）")
check(wb._tab == "files", "勾单证后仍停在单证标签")
wb._toggle_file(fid, False); app.processEvents()
check(wb._file_panel is fp0, "撤交同样不重建")

wb._step_spin.setValue(1)
_cur = wb._batch_id                       # 上一节点击色带可能已切批次 → 以工作台当前批次为准
before = {n["node_id"]: n["plan_start"] for n in db.get_nodes_by_batch(_cur)}
wb._shift_node(7, +1); app.processEvents()
after = {n["node_id"]: n["plan_start"] for n in db.get_nodes_by_batch(_cur)}
check(after[7] != before[7], f"推迟生效（节点7 {before[7]} → {after[7]}）")
check("推迟" in wb._flash_note, f"工作台提示：{wb._flash_note[:36]}")
check(card.progress_label.text() != "", "看板卡片摘要已同步刷新")
wb._undo_shift(); app.processEvents()
check({n["node_id"]: n["plan_start"] for n in db.get_nodes_by_batch(_cur)}[7] == before[7],
      "撤销位移回到原位")

# ══════════ 6) ScopedScrollArea 滚轮边界 ══════════
print("== 6) ScopedScrollArea 滚轮边界吞掉 ==")
from PySide6.QtWidgets import QWidget, QVBoxLayout, QCheckBox  # noqa: E402


class E:
    def __init__(s, dy):
        s.dy = dy
        s.accepted = False

    def angleDelta(s):
        return QPoint(0, s.dy)

    def accept(s):
        s.accepted = True

    def ignore(s):
        pass


sc = ScopedScrollArea(); sc.setWidgetResizable(True)
body = QWidget()
lay = QVBoxLayout(body)
for i in range(50):
    lay.addWidget(QCheckBox(f"r{i}"))
sc.setWidget(body); sc.resize(300, 120); sc.show(); app.processEvents()
vb = sc.verticalScrollBar()
vb.setValue(vb.maximum())
ev = E(-120)
sc.wheelEvent(ev)
check(vb.value() == vb.maximum() and ev.accepted, "底部继续滚轮被吞（值不变且已接受）")
vb.setValue(vb.minimum())
ev2 = E(120)
sc.wheelEvent(ev2)
check(vb.value() == vb.minimum() and ev2.accepted, "顶部滚轮被吞（值不变且已接受）")

# ══════════ 7) 卡片 ↔ 工作台同步 ══════════
print("== 7) 卡片 ↔ 工作台双向同步 ==")
wb.close(); app.processEvents()
check(not wb.isVisible(), "关闭即隐藏（实例保留、状态不丢）")
wb = dp.open_workbench(PID, B01, "single"); app.processEvents()
check(wb.isVisible() and wb._tab == "files", "重新打开保持上次标签状态")
check(wb._batch_id == B01, "工作台当前批次 = B01")
# 先把卡片确定性地置回 B01，再切到 B02，才能真正验证「卡片驱动工作台」
db.update_project(PID, current_batch_id=B01)
card._load(); card._update_header(); app.processEvents()
check(card._current_batch_id == B01, "卡片当前批次 = B01")
idx_b02 = next(i for i in range(card.batch_combo.count())
               if card.batch_combo.itemData(i) == B02)
card.batch_combo.setCurrentIndex(idx_b02)   # 卡片侧显式切到 B02
app.processEvents()
check(card._current_batch_id == B02, "卡片已切到 B02")
check(wb._batch_id == B02, "卡片切批次 → 工作台跟随到同一批次")
check(wb._project_id == PID, "工作台项目未变")

print("\n" + ("PASS ALL" if not FAILED else f"FAIL {len(FAILED)}"))
for f in FAILED:
    print(" -", f)
wb.close()
app.quit()
shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(0 if not FAILED else 1)
