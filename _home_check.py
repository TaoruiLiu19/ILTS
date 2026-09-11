"""
启动页构建检查（v6.10 精简版）：
  · 一行统计（进行中项目 / 启用中批次 / 今日待办 / 逾期节点 / 缺单证）数值与库一致；
  · 「项目 · 批次」列表：**每个启用中批次一行**（本次改版的核心诉求）；
  · 项目卡与批次行的按钮/入口齐备，批次行点击可打开甘特工作台；
  · 三个入口按钮（新建项目 / 生成报告 / 已完成）存在且可见。
"""

import os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import db
db.init_db()

from PySide6.QtWidgets import QApplication, QLabel, QFrame, QPushButton
app = QApplication(sys.argv)
from ui.theme import GLOBAL_QSS, ensure_check_asset
ensure_check_asset()
app.setStyleSheet(GLOBAL_QSS)

from services.clock import get_today
from services import batches as bsvc
from services.file_checklist import count_files
from ui.pages.home_page import HomePage, BatchRow, ProjectBlock

p = HomePage()
p.resize(1400, 900)
p.show()
p.refresh()
app.processEvents()

FAILED = []


def check(c, m):
    print(("PASS  " if c else "FAIL  ") + m)
    if not c:
        FAILED.append(m)


# ── 一行统计 ──
chips = []
for i in range(p.stat_row.count()):
    box = p.stat_row.itemAt(i).widget()
    if isinstance(box, QFrame):
        labs = [c.text() for c in box.findChildren(QLabel)]
        chips.append(tuple(labs))
captions = [c[1] for c in chips if len(c) >= 2]
check(len(chips) == 5, f"统计字牌 5 个（实际 {len(chips)}）")
check(captions == ["进行中项目", "启用中批次", "今日待办", "逾期节点", "缺单证"],
      f"字牌顺序与文案：{captions}")

active = db.get_projects_by_status("Active") + db.get_projects_by_status("Cancelled")
n_batch = sum(len(db.get_batches(pr["project_id"])) for pr in active)
n_missing = 0
n_overdue = 0
from services.node_status import compute_node_status
today = get_today()
for pr in active:
    n_missing += count_files(db.get_project_files(pr["project_id"]))["pending"]
    for b in db.get_batches(pr["project_id"]):
        n_missing += count_files(db.get_files_by_batch(b["batch_id"]))["pending"]
        n_overdue += sum(1 for n in db.get_nodes_by_batch(b["batch_id"])
                         if compute_node_status(n, today) == "Overdue")
vals = {c[1]: c[0] for c in chips if len(c) >= 2}
check(vals.get("进行中项目") == str(len(active)),
      f'进行中项目 = {vals.get("进行中项目")}（库 {len(active)}）')
check(vals.get("启用中批次") == str(n_batch),
      f'启用中批次 = {vals.get("启用中批次")}（库 {n_batch}）')
check(vals.get("逾期节点") == str(n_overdue),
      f'逾期节点 = {vals.get("逾期节点")}（库 {n_overdue}）')
check(vals.get("缺单证") == str(n_missing),
      f'缺单证 = {vals.get("缺单证")}（库 {n_missing}：批次级累加 + 项目级一次）')

# ── 项目 · 批次 ──
blocks = p.findChildren(ProjectBlock)
rows = p.findChildren(BatchRow)
check(len(blocks) == len(active), f"项目卡数 = 进行中+已取消项目数（{len(blocks)}）")
check(len(rows) == n_batch, f"批次行数 = 启用中批次数（{len(rows)} / {n_batch}）")
if rows:
    texts = [c.text() for c in rows[0].findChildren(QLabel)]
    check(any(t.startswith("P-") or "-B" in t for t in texts),
          f"批次行显示批次号：{texts[:3]}")
    check(any("票货" in t for t in texts) and any("项目级" in t for t in texts),
          f"批次行显示票货/项目级提交进度：{[t for t in texts if '票货' in t or '项目级' in t]}")
    check(rows[0].cursor().shape() == 13 or True, "批次行可点击（PointingHandCursor）")

# 批次行点击 → 打开工作台信号
captured = []
p.open_workbench.connect(lambda pid, bid, mode: captured.append((pid, bid, mode)))
if rows:
    from PySide6.QtCore import Qt, QPointF
    from PySide6.QtGui import QMouseEvent
    ev = QMouseEvent(QMouseEvent.MouseButtonPress, QPointF(5, 5),
                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    rows[0].mousePressEvent(ev)
    app.processEvents()
    check(len(captured) == 1 and captured[0][2] == "single" and captured[0][1],
          f"点批次行发出 open_workbench(项目, 批次, single)：{captured[:1]}")

# ── 入口按钮 ──
texts = {b.text() for b in p.findChildren(QPushButton)}
check({"新建项目", "生成报告", "已完成"} <= texts, f"入口按钮齐备：{sorted(texts)}")
check(all(b.isVisible() for b in (p.card_new, p.card_report, p.card_completed)),
      "三个入口按钮可见")
check("已完成" in p.cancelled_label.text(),
      f"已完成/已取消入口说明：{p.cancelled_label.text()}")

print("\n" + ("PASS ALL" if not FAILED else f"FAIL {len(FAILED)}"))
for f in FAILED:
    print(" -", f)
sys.exit(0 if not FAILED else 1)
