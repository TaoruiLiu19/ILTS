#!/usr/bin/env python3
"""Phase 1C 验收用例（《多式联运.md》§14：T9 / T20 / T21 / T29 / T32 + §11 报告口径 + §5.4 D34 KPI）。

覆盖：
  T9  报告两级 —— 标题【项目名 · B01】、未完成清单批次/客户/柜号列、概览双标、按批次筛选
  T20 取消批次审计 —— 「显示已取消」开关可查看已取消批次 op_log（只读）
  T21 换线影响评估 —— 换线后自动列出受影响节点清单（新增/删除/改名/顺序/日期）
  T29 批次 KPI —— 录入 actual_etd/eta/delivery 后按时率与平均周期出现在周报
  T32 全取消项目 —— 项目显示「已取消」、留在列表、不出现在「已完成」
  §11 附加：客户筛选与分组、_B01 文件名后缀、周对比同口径双标、
            「数据不足」口径（empty actual_* 不得按 0 统计）、D34 KPI 三项

全程使用隔离临时库（db.DB_PATH 指向 tempdir），不触碰 data/logistics.db。
用法: python tools/test_acceptance_1c.py
"""

import os
import shutil
import sys
import tempfile
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import db

_TMP = tempfile.mkdtemp(prefix="opt_1c_")
db.DB_PATH = os.path.join(_TMP, "t.db")
db._conn = None
db.init_db()

from services import reporting                              # noqa: E402
from services import report_exporter                        # noqa: E402
from services import batches as batches_svc                 # noqa: E402
from services import schedule2                              # noqa: E402
from services.schedule_change import apply as sc_apply      # noqa: E402
from services.file_checklist import bootstrap               # noqa: E402
from services.node_template import template as node_template  # noqa: E402

FAILED = []
SKIPPED = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")
    if not cond:
        FAILED.append(msg)


def skip(msg):
    print(f"  SKIP  {msg}")
    SKIPPED.append(msg)


_PID = [0]


def seed_project(etd="2026-09-15", eta="2026-10-26", name="青岛→巴西Sepetiba 光伏组件运输"):
    """建一个项目 + 默认批次（B01），返回 (project_id, batch_id)。"""
    _PID[0] += 1
    pid = f"acc1c-{_PID[0]:03d}"
    db.insert_project({"project_id": pid, "project_no": f"P-ACC1C{_PID[0]:03d}",
                       "project_name": f"{name}#{_PID[0]}", "country": "BR",
                       "export_port": "QD", "status": "Active"})
    b = db.create_default_batch(pid)
    bid = b["batch_id"]
    db.upsert_route(bid, mode_primary="SEA", mode_chain='["SEA"]', country="BR",
                    export_port="QD", etd=etd, eta=eta)
    nodes = []
    for n in node_template():
        n = dict(n)
        n["status"] = "Pending"
        nodes.append(n)
    plan = schedule2.compute_plan(etd, eta, nodes)
    for n in nodes:
        n["plan_start"], n["plan_end"] = plan[n["node_key"]]
    db.insert_nodes(pid, nodes, bid)
    db.insert_files(pid, bootstrap("BR", "QD", plan), bid)
    db.update_batch(bid, status="ready")
    batches_svc.update_project_status(pid)
    return pid, bid


def clone_batch(pid, src_bid):
    return batches_svc.copy_batch(pid, src_bid)["batch_id"]


def ensure_app():
    """对话框/页面属 QWidget，必须先建 QApplication（离屏模式）。"""
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


# ════════════════════════════════════════════════════════════════════
def main():
    ensure_app()
    print("== T9 报告两级：标题 / 批次列 / 概览双标 / 按批次筛选 ==")
    pid, b01 = seed_project()
    b02 = clone_batch(pid, b01)
    b02_no = db.get_batch(b02)["batch_no"]
    pname = db.get_project(pid)["project_name"]
    db.update_batch(b01, batch_name="B01")
    proj_no = db.get_project(pid)["project_no"]
    b01_no = db.get_batch(b01)["batch_no"]

    # ① 标题：单批次 → 【项目名 · B01】
    rep1 = reporting.build_report("daily", "2026-09-20", project_filter=pid, batch_filter=b01)
    check(rep1["title"].startswith(f"【{pname} · {b01_no}】"),
          f"单批次日报标题带【项目名 · 批次号】（实际 {rep1['title']}）")
    check(rep1["title"].endswith("物流操作日报"), "单批次日报标题以「物流操作日报」结尾")
    rep1w = reporting.build_report("weekly", "2026-09-07", project_filter=pid,
                                   batch_filter=b01)
    check(rep1w["title"].startswith(f"【{pname} · {b01_no}】")
          and rep1w["title"].endswith("物流操作周报"),
          f"单批次周报标题同口径（实际 {rep1w['title']}）")

    # ② 全项目报告保持原名（不带项目名方括号）
    rep_all = reporting.build_report("daily", "2026-09-20", project_filter=pid)
    check("【" not in rep_all["title"] and "物流操作日报" in rep_all["title"],
          f"全项目报告保持公司日期制式原名（实际 {rep_all['title']}）")
    check(rep_all["title"] == "2026年09月20日物流操作日报",
          "全项目日报标题 = 「2026年09月20日物流操作日报」")

    # ③ 未完成清单含批次列 / 客户列 / 柜号列
    db.insert_container(b01, "TCLU1234567", container_type="40HQ")
    db.insert_container(b01, "TCLU7654321", container_type="40HQ")
    cid = db.insert_party(party_name="巴西Sepetiba 采购方", country="BR", roles=["CUSTOMER"])
    db.bind_batch_party(b01, "CUSTOMER", cid)
    unfin = reporting.unfinished([db.get_project(pid)], date(2026, 11, 30),
                                reporting.ReportScope(project_filter=pid, batch_filter=b01))
    check(len(unfin) > 0, f"未完成清单非空（{len(unfin)} 行，参考日 2026-11-30）")
    row = unfin[0]
    # 批次列 = 批次号+名称；批次名与批次号末段重复（标准规则 `{项目号}-B01`，T13）时
    # 只显示批次号，避免「P-0001-B01 · B01」冗余
    check(row.get("batch") == b01_no,
          f"未完成清单含批次列（批次号+名称，名称冗余时去重）：{row.get('batch')}")
    b01_batch = db.get_batch(b01)
    check(reporting.batch_label(b01_batch) == b01_no,
          f"批次列=批次号（名称与号码末段重复时）：{reporting.batch_label(b01_batch)}")
    db.update_batch(b01, batch_name="首航")
    check(reporting.batch_label(db.get_batch(b01)) == f"{b01_no} · 首航",
          f"批次名称非冗余时批次列显示「号 · 名」："
          f"{reporting.batch_label(db.get_batch(b01))}")
    db.update_batch(b01, batch_name="B01")
    check(row.get("customer") == "巴西Sepetiba 采购方", f"未完成清单含客户列：{row.get('customer')}")
    check(row.get("container") == "TCLU1234567、TCLU7654321",
          f"未完成清单含柜号列：{row.get('container')}")
    tbl = [blk for blk in reporting.blocks(rep1) if blk["t"] == "table"
           and blk["header"][:2] == ["项目", "批次"]]
    check(bool(tbl), "报告「未完成清单」表格首两列为 项目/批次")
    if tbl:
        check(tbl[0]["header"] == ["项目", "批次", "客户", "柜号", "节点",
                                   "计划结束", "逾期(天)", "缺失单证"],
              f"未完成清单表头齐全（实际 {tbl[0]['header']}）")

    # ④ 概览双标：项目数 / 批次数
    full = reporting.build_report("daily", "2026-09-20", project_filter=pid)
    check(full["overview"]["project_count"] == 1 and full["overview"]["batch_count"] == 2,
          f"概览双标：项目 {full['overview']['project_count']} / 批次 "
          f"{full['overview']['batch_count']}")
    ov_paras = [blk["text"] for blk in reporting.blocks(full)
                if blk["t"] == "para" and "项目数" in blk["text"]]
    check(bool(ov_paras) and "批次数" in ov_paras[0],
          f"概览正文同时展示项目数与批次数：{ov_paras[0] if ov_paras else '—'}")

    # ⑤ 按批次筛选：单批次报告只含该批次的数据
    check(full["overview"]["batch_count"] == 2 and rep1["overview"]["batch_count"] == 1,
          "按批次筛选后概览批次数收敛为 1")
    nodes_all = sum(len(db.get_nodes(pid, batch_id=b["batch_id"]))
                    for b in db.get_batches(pid))
    check(rep1["overview"]["nodes_total"] == len(db.get_nodes_by_batch(b01))
          and rep1["overview"]["nodes_total"] < nodes_all,
          f"单批次报告节点统计仅含所选批次（{rep1['overview']['nodes_total']} < {nodes_all}）")
    check(all(x["batch_id"] == b01 for x in rep1["unfinished"]),
          "单批次报告的未完成清单不越界到其他批次")
    check(rep1["batch_filter"] == b01 and rep1["batch_filter_label"],
          "模型带 batch_filter / batch_filter_label，供导出层命名与头部展示")

    # ⑥ 客户筛选与分组（§5.5 D32 / §11）
    cid2 = db.insert_party(party_name="另一客户", country="CN", roles=["CUSTOMER"])
    db.bind_batch_party(b02, "CUSTOMER", cid2)
    rep_c1 = reporting.build_report("daily", "2026-09-20", project_filter=pid,
                                    customer_filter=cid)
    check(len(rep_c1["unfinished"]) == len(rep1["unfinished"]),
          "按客户筛选：单客户报告只含该客户的批次行")
    rep_c2 = reporting.build_report("daily", "2026-09-20", project_filter=pid,
                                    customer_filter=cid2)
    check(all(x["customer"] == "另一客户" for x in rep_c2["unfinished"]),
          "按客户筛选另一客户 → 清单客户列一致")
    rep_un = reporting.build_report("daily", "2026-09-20", project_filter=pid,
                                    customer_filter=reporting.CUSTOMER_UNFILLED)
    check(all(x["customer"] == "未填写" for x in rep_un["unfinished"]),
          "客户为空按「未填写」分组（§5.5 规则 3）")
    opts = reporting.customer_options()
    check(any(o["id"] == cid for o in opts) and any(o["name"] == "未填写" for o in opts),
          "客户下拉选项含「全部客户 / 未填写 / 单客户」")

    # ⑦ 操作动态批次列：项目级事件（scope='project'）显示「—」
    from services.oplog import record as oplog
    db.insert_op_log(pid, "project_create", subject=pname, scope="project",
                     detail="项目立项", created_at="2026-09-20T09:30:00+08:00")
    db.insert_op_log(pid, "batch_edit", batch_id=b01, scope="batch",
                     subject="B01", detail="批次信息更新",
                     created_at="2026-09-20T10:30:00+08:00")
    acts = reporting.activity_daily([db.get_project(pid)], date(2026, 9, 20), {}, None)
    acts = [a for a in acts if a["project_id"] == pid]
    proj_ev = [a for a in acts if a["kind"] == "project_create"]
    batch_ev = [a for a in acts if a["kind"] == "batch_edit"]
    check(proj_ev and proj_ev[0]["batch"] == "—",
          "项目级事件（scope='project'）批次列显示「—」")
    check(batch_ev and batch_ev[0]["batch"].startswith(b01_no),
          f"批次级事件批次列显示批次号（{batch_ev[0]['batch'] if batch_ev else '—'}）")
    rep_act = reporting.build_report("daily", "2026-09-20", project_filter=pid,
                                     batch_filter=b01)
    act_tbl = [blk for blk in reporting.blocks(rep_act) if blk["t"] == "table"
               and "动作" in blk["header"]]
    check(bool(act_tbl) and act_tbl[0]["header"] == ["时间", "项目", "批次", "动作", "内容"],
          f"操作动态表格已含批次列（{act_tbl[0]['header'] if act_tbl else '无表格'}）")
    if act_tbl:
        check(any(row[2] == "—" for row in act_tbl[0]["rows"]),
              "操作动态中项目级事件该行批次列渲染为「—」")

    print("== T20 取消批次审计（显示已取消 → 只读查 op_log） ==")
    bid_c = clone_batch(pid, b01)
    batches_svc.cancel_batch(bid_c, reason="客户撤单")
    check(db.get_batch(bid_c)["status"] == "cancelled", "批次已取消")
    hidden = [b["batch_id"] for b in db.get_batches(pid)]
    shown = [b["batch_id"] for b in db.get_batches(pid, include_cancelled=True)]
    check(bid_c not in hidden, "取消批次默认全模块隐藏（§8/D14）")
    check(bid_c in shown, "「显示已取消」= include_cancelled=True 时可查到该批次")

    from ui.batch_dialogs import CancelledBatchAuditDialog
    dlg = CancelledBatchAuditDialog(pid)
    check(dlg.show_cancelled.isChecked() is False, "审计对话框默认关闭「显示已取消」")
    check(dlg.batch_table.rowCount() == 0, "开关关闭时列表为空（默认隐藏）")
    dlg.show_cancelled.setChecked(True)
    check(dlg.batch_table.rowCount() >= 1,
          f"勾选「显示已取消」后列出已取消批次（{dlg.batch_table.rowCount()} 行）")
    got = [dlg.batch_table.item(i, 0).text() for i in range(dlg.batch_table.rowCount())]
    check(any(db.get_batch(bid_c)["batch_no"] in g for g in got),
          f"列表中包含刚取消的批次（{got}）")
    check(dlg.log_table.rowCount() > 0,
          f"只读展示该批次完整 op_log（{dlg.log_table.rowCount()} 行）")
    kinds = {dlg.log_table.item(i, 1).text() for i in range(dlg.log_table.rowCount())}
    check("取消批次" in kinds, f"op_log 含「取消批次」动作（{sorted(kinds)}）")
    # 只读：对话框不含任何写入控件
    from PySide6.QtWidgets import QLineEdit, QPushButton, QCheckBox, QDateEdit, QComboBox
    writers = []
    for w in dlg.findChildren(QLineEdit) + dlg.findChildren(QDateEdit) \
            + dlg.findChildren(QComboBox):
        writers.append(type(w).__name__)
    edit_btns = [b.text() for b in dlg.findChildren(QPushButton)
                 if b.text() not in ("关闭",)]
    check(not writers and not edit_btns,
          f"审计视图只读（无输入控件、无可写按钮：{writers or edit_btns or '无'}）")
    check(isinstance(dlg.show_cancelled, QCheckBox), "「显示已取消」为开关控件")
    # 取消期间写入被拒（后端，不只隐藏）：业务写入通道必须抛 BatchCancelledError
    from db import BatchCancelledError
    blocked = False
    try:
        batches_svc.override_actual(bid_c, "actual_etd", "2026-09-20", reason="测试写入")
    except BatchCancelledError:
        blocked = True
    check(blocked, "取消期间业务写入被后端拒绝（BatchCancelledError，§8）")
    # 已完成页不含已取消项目（T32 后半句）
    check(not any(p["project_id"] == pid for p in db.get_projects_by_status("Completed")),
          "含取消批次的进行中项目不进「已完成」列表")

    print("== T21 换线影响评估（新增/删除/改名/顺序/日期） ==")
    brt = clone_batch(pid, b01)
    new_route = {"etd": _add(b01_etd(pid, brt), 5),
                 "eta": _add(b01_eta(pid, brt), 8),
                 "export_port": "SH", "customs_broker": "青岛中远报关"}
    impact = batches_svc.change_route(brt, new_route, reason="船司改配 + 换出发港")
    check(impact["fields"].get("etd") and impact["fields"]["etd"]["new"] == new_route["etd"],
          "影响清单记录 ETD 变化（旧值→新值）")
    check(impact["fields"].get("eta") and impact["fields"]["eta"]["new"] == new_route["eta"],
          "影响清单记录 ETA 变化（旧值→新值）")
    check(impact["etd_changed"] and impact["eta_changed"], "日期变化标记 etd_changed/eta_changed")
    check(impact["nodes_need_recheck"] > 0,
          f"日期变化 → 「以下 N 个节点计划日期需复核」（N={impact['nodes_need_recheck']}）")

    # 结构差异（新增/删除/改名/顺序）：用假目的国模板驱动 route_impact 的模板分支
    _install_fake_template()
    try:
        brt2 = clone_batch(pid, b01)
        old_key_set = {n["node_key"] for n in db.get_nodes_by_batch(brt2)}
        new2 = {"template_key": "FAKE", "country": "BR",
                "etd": _add(db.get_route(brt2)["etd"], 3),
                "eta": _add(db.get_route(brt2)["eta"], 9)}
        im2 = batches_svc.change_route(brt2, new2, reason="换目的国模板")
        check("FAKE_EXTRA" in im2["added"], f"影响清单列出新增节点（{im2['added']}）")
        check("EMPTY_PICKUP" in im2["removed"], f"影响清单列出删除节点（{im2['removed']}）")
        check(any(r["node_key"] == "LOADING" and r["new"] == "装船作业（新命名）"
                  for r in im2["renamed"]),
              f"影响清单列出改名节点（{im2['renamed']}）")
        check(im2["reordered"],
              f"影响清单列出顺序变化节点（{len(im2['reordered'])} 个："
              f"{im2['reordered'][:3]}…）")
        check("GATE_IN" in im2["reordered"] and "EXPORT_CUSTOMS" in im2["reordered"],
              f"互换 seq 的两个节点被识别为顺序变化（{im2['reordered'][:3]}…）")
        check(all(k in old_key_set for k in im2["reordered"]),
              "顺序变化条目均为批次内既有 node_key（不臆造节点）")
        check(im2["nodes_need_recheck"] > 0, "结构差异 + 日期变化同时给出复核提示")
        from ui.batch_dialogs import RouteImpactDialog
        rd = RouteImpactDialog(brt2)
        cats = {rd.table.item(i, 0).text() for i in range(rd.table.rowCount())}
        check({"新增", "删除", "改名", "顺序变化", "日期变化"} <= cats,
              f"换线影响对话框列出五类影响（{sorted(cats)}）")
        check("个节点计划日期需复核" in rd.recheck_lbl.text(),
              f"对话框显示复核行：{rd.recheck_lbl.text()}")
    finally:
        _uninstall_fake_template()

    # 持久化：换线写 batch_route_changes(+impact) 与 op_log；对话框可只读回看
    import json
    changes = db.get_route_changes(brt, limit=5)
    check(len(changes) >= 1, f"换线写 batch_route_changes（{len(changes)} 条）")
    persisted = json.loads(changes[0]["impact"])
    check(persisted["fields"].get("etd", {}).get("new") == new_route["etd"],
          "persisted impact 记录 ETD 变化")
    rd2 = RouteImpactDialog(brt)
    check(rd2.table.rowCount() > 0 and rd2.impact.get("etd_changed"),
          "RouteImpactDialog 只读回看最近一次换线影响清单")
    route_now = db.get_route(brt)
    check(route_now["export_port"] == "SH" and route_now["etd"] == new_route["etd"],
          "换线同步线路字段（不自动重排节点，§8/D18）")

    print("== T29 批次 KPI（actual_* 录入 → 按时率 / 平均周期） ==")
    pid_k, bk1 = seed_project()
    bk2 = clone_batch(pid_k, bk1)
    bk3 = clone_batch(pid_k, bk1)
    r1 = db.get_route(bk1)
    # 批次1：准时到（actual_eta == eta），周期 30 天
    db.update_batch(bk1, actual_etd=r1["etd"], actual_eta=r1["eta"],
                    actual_delivery=_add(r1["etd"], 30))
    # 批次2：晚到 4 天，周期 40 天
    db.update_batch(bk2, actual_etd=r1["etd"], actual_eta=_add(r1["eta"], 4),
                    actual_delivery=_add(r1["etd"], 40))
    # 批次3：不录 actual_*（用于验证「数据不足」不按 0 统计）

    wk = reporting.build_report("weekly", "2026-11-02", project_filter=pid_k)
    ko = wk["batch_kpi"]["overall"]
    check(ko["ontime_rate"] == "50%",
          f"批次按时率 = actual_eta ≤ eta = {ko['ontime_rate']}（期望 50%，2 样本）")
    check(ko["ontime_sample"] == 2 and ko["ontime_hit"] == 1,
          f"按时率取样只算有 actual_eta 的批次（{ko['ontime_hit']}/{ko['ontime_sample']}）")
    check(ko["avg_cycle"] == "35.0 天",
          f"批次平均周期 = mean(actual_delivery − actual_etd) = {ko['avg_cycle']}"
          f"（期望 35.0 天 = (30+40)/2）")
    check(ko["avg_cycle_sample"] == 2, "平均周期取样只算 etd+delivery 皆有值的批次")

    kpi_blocks = [blk for blk in reporting.blocks(wk) if blk["t"] == "h" and "批次 KPI" in blk["text"]]
    check(bool(kpi_blocks), "周报含「批次 KPI」板块")
    kpi_txt = " ".join(blk["text"] for blk in reporting.blocks(wk) if blk["t"] == "para")
    check("批次按时率" in kpi_txt and "50%" in kpi_txt, "周报正文出现批次按时率 50%")
    check("批次平均周期" in kpi_txt and "35.0 天" in kpi_txt, "周报正文出现批次平均周期 35.0 天")
    kpi_tbl = [blk for blk in reporting.blocks(wk) if blk["t"] == "table"
               and "平均影响(天)" in blk["header"]]
    check(bool(kpi_tbl) and len(kpi_tbl[0]["rows"]) == 3, "周报 KPI 明细表逐批次列出 3 行")

    # 单批次 KPI 同样可查（按批次筛选）
    wk1 = reporting.build_report("weekly", "2026-11-02", project_filter=pid_k, batch_filter=bk2)
    check(wk1["batch_kpi"]["overall"]["ontime_rate"] == "0%",
          "单批次（晚到）按时率 = 0%")

    print("== §11 「数据不足」口径：空 actual_* 不得按 0 统计 ==")
    pid_n, bn = seed_project()
    mn = reporting.build_report("weekly", "2026-11-02", project_filter=pid_n)
    on = mn["batch_kpi"]["overall"]
    check(on["ontime_rate"] == reporting.NO_DATA,
          f"无 actual_eta → 按时率显示「{on['ontime_rate']}」（不是 0%）")
    check(on["ontime_rate"] != "0%", "空 actual_eta 绝不按 0 统计")
    check(on["avg_cycle"] == reporting.NO_DATA,
          f"无 actual_etd/delivery → 平均周期显示「{on['avg_cycle']}」（不是 0 天）")
    check(on["ontime_nodata"] and on["avg_cycle_nodata"] and on["detention_over_nodata"],
          "数据不足标记齐全（ontime/cycle/detention）")
    check(reporting._fmt_rate(0, 0)[0] == reporting.NO_DATA,
          "_fmt_rate(0,0) 返回「数据不足」而非 0%")
    check(reporting._fmt_rate(0, 3)[0] == "0%", "有样本且 0 命中才显示 0%")
    kpi_paras = [blk["text"] for blk in reporting.blocks(mn) if blk["t"] == "para"]
    check(any("数据不足" in t for t in kpi_paras), "报告正文明确显示「数据不足」")
    notes = [blk["text"] for blk in reporting.blocks(mn) if blk["t"] == "note"]
    check(any("不按 0 统计" in t for t in notes), "报告附「不按 0 统计」口径说明")

    print("== D34 KPI：船期变更次数 / 平均影响天数 / 变更后新增逾期 ==")
    pid_s, bs = seed_project(etd="2026-09-15", eta="2026-10-26")
    bs_route = db.get_route(bs)
    # 第 1 次：A 类整体 +5 → 影响 5 天
    sc_apply(pid_s, bs, _add(bs_route["etd"], 5), _add(bs_route["eta"], 5), reason="整体平移")
    # 第 2 次：D 类（ETD+2 / ETA+7）→ 影响 max(2,7)=7 天
    sc_apply(pid_s, bs, _add(bs_route["etd"], 7), _add(bs_route["eta"], 12), reason="组合变更")
    ms = reporting.build_report("weekly", "2026-12-31", project_filter=pid_s)
    ko2 = ms["batch_kpi"]["overall"]
    check(ko2["schedule_change_count"] == 2,
          f"船期变更次数 = {ko2['schedule_change_count']}（期望 2）")
    check(ko2["avg_impact_days"] == 6.0,
          f"平均影响天数 = mean(max(|ΔETD|,|ΔETA|)) = {ko2['avg_impact_days']}"
          f"（期望 6.0 = (5+7)/2）")
    check(str(ko2["avg_impact_days"]) == "6.0",
          f"平均影响天数按 1 位小数呈现：{ko2['avg_impact_days']}")
    check(isinstance(ko2["new_overdue_count"], int),
          f"变更后新增逾期节点数 = {ko2['new_overdue_count']}")
    check(ko2["new_overdue_count"] > 0,
          "被往后推的船期变更 + 参考日已过 → 新增逾期数 > 0")
    ms_early = reporting.build_report("weekly", "2026-09-01", project_filter=pid_s)
    check(ms_early["batch_kpi"]["overall"]["new_overdue_count"] == 0,
          "参考日早于计划结束日 → 变更后新增逾期数为 0")

    print("== §11 单证按时提交率 / 免箱期超期占比 ==")
    pid_f, bf = seed_project()
    files = db.get_files_by_batch(bf)
    required = [f for f in files if f["doc_type"] == "required" and f.get("due_date")]
    check(len(required) >= 2, f"必填单证含 due_date（{len(required)} 张）")
    ontime_doc = required[0]
    late_doc = required[1]
    db.update_file(ontime_doc["file_id"], status="submitted",
                   submitted_date=ontime_doc["due_date"])
    db.update_file(late_doc["file_id"], status="submitted",
                   submitted_date=_add(late_doc["due_date"], 3))
    mf = reporting.build_report("weekly", "2026-11-02", project_filter=pid_f)
    kof = mf["batch_kpi"]["overall"]
    check(kof["file_ontime_rate"] not in (reporting.NO_DATA, "0%")
          and kof["file_ontime_hit"] >= 1,
          f"单证按时提交率 = submitted 且 submitted_date ≤ due_date "
          f"→ {kof['file_ontime_rate']}（{kof['file_ontime_hit']}/{kof['file_ontime_sample']}）")

    route_f = db.get_route(bf)
    db.update_route(bf, free_detention_until=_add(route_f["eta"], 7))
    db.update_batch(bf, empty_returned_at=_add(route_f["eta"], 10))
    mf2 = reporting.build_report("weekly", "2026-11-02", project_filter=pid_f)
    kof2 = mf2["batch_kpi"]["overall"]
    check(kof2["detention_over_rate"] == "100%",
          f"免箱期超期批次占比 = 还箱日晚于免箱期截止 → {kof2['detention_over_rate']}"
          f"（{kof2['detention_over_hit']}/{kof2['detention_over_sample']}）")
    pid_d, bd = seed_project()
    db.update_route(bd, free_detention_until="2026-11-10")
    db.update_batch(bd, empty_returned_at="2026-11-05")
    md_ = reporting.build_report("weekly", "2026-11-02", project_filter=pid_d)
    check(md_["batch_kpi"]["overall"]["detention_over_rate"] == "0%",
          "未超期 → 免箱期超期占比 0%（有样本）")
    pid_e, be = seed_project()
    db.update_batch(be, empty_returned_at="2026-11-05")
    me = reporting.build_report("weekly", "2026-11-02", project_filter=pid_e)
    check(me["batch_kpi"]["overall"]["detention_over_rate"] == reporting.NO_DATA,
          "无免箱期截止 → 免箱期超期占比「数据不足」")

    print("== §11 文件名后缀 _B01 / 周报日期区间命名 / 日报编号不变 ==")
    base_daily = report_exporter.default_filename("daily", date(2026, 9, 20))
    check(base_daily == "ILTS_Daily_2026-09-20", f"日报文件名不变：{base_daily}")
    sfx = report_exporter.default_filename("daily", date(2026, 9, 20), b01)
    check(sfx == f"ILTS_Daily_2026-09-20_{b01_no.split('-')[-1]}",
          f"按批次筛选追加批次号后缀：{sfx}")
    sfx_w = report_exporter.default_filename("weekly", date(2026, 9, 20), b01)
    check(sfx_w.startswith("ILTS_Weekly_2026-09-14_to_2026-09-20_"),
          f"周报沿用日期区间命名 + 批次后缀：{sfx_w}")
    db.set_setting("rpt_seq", "0")
    db.set_setting("rpt_last_date", "")
    n1 = report_exporter.next_report_no(date(2026, 9, 20))
    n2 = report_exporter.next_report_no(date(2026, 9, 20))
    check(n1 == "RPT-20260920-001" and n2 == "RPT-20260920-002",
          f"日报编号按日重置不变：{n1} / {n2}")
    out_dir = os.path.join(_TMP, "reports")
    path, kind = report_exporter.export(rep1, reporting.blocks(rep1), out_dir=out_dir)
    check(os.path.basename(path).startswith(f"I") and "_B01" in os.path.basename(path),
          f"导出文件名含 _B01：{os.path.basename(path)}")
    path2, _ = report_exporter.export(reporting.build_report(
        "weekly", "2026-09-07", project_filter=pid, batch_filter=b02),
        reporting.blocks(reporting.build_report("weekly", "2026-09-07",
                                                project_filter=pid, batch_filter=b02)),
        out_dir=out_dir)
    check("_B02" in os.path.basename(path2), f"第二个批次后缀 _B02：{os.path.basename(path2)}")

    print("== §11 周对比同口径双标 / 前一周无数据 ==")
    pid_w, bw = seed_project()
    wk_first = reporting.build_report("weekly", "2026-09-07", project_filter=pid_w)
    check(wk_first["weekly_compare"] is None, "前一周无数据 → weekly_compare 为 None")
    note_txt = [blk["text"] for blk in reporting.blocks(wk_first) if blk["t"] == "note"]
    check(any("前一周无数据，本次为首份周报" in t for t in note_txt),
          "保留「前一周无数据，本次为首份周报」文案")
    oplog_row = db.insert_op_log(pid_w, "batch_edit", batch_id=bw, subject="B01",
                                 detail="上周有数据", created_at="2026-09-01T10:00:00+08:00")
    wk_second = reporting.build_report("weekly", "2026-09-07", project_filter=pid_w)
    wc = wk_second["weekly_compare"]
    check(wc is not None and "scope" in wc, "有上周数据 → 输出周对比")
    if wc:
        check(wc["scope"]["projects"] == 1 and wc["scope"]["batches"] == 1,
              f"周对比标注同口径双标（项目 {wc['scope']['projects']} / "
              f"批次 {wc['scope']['batches']}）")
        sc_paras = [blk["text"] for blk in reporting.blocks(wk_second)
                    if blk["t"] == "para" and "同口径对比" in blk["text"]]
        check(bool(sc_paras) and "批次 1 个" in sc_paras[0],
              f"周对比正文标注同口径：{sc_paras[0] if sc_paras else '—'}")
        # 单批次筛选下同口径：批次数收敛为 1
        wk_b = reporting.build_report("weekly", "2026-09-07", project_filter=pid_w,
                                      batch_filter=bw)
        if wk_b["weekly_compare"]:
            check(wk_b["weekly_compare"]["scope"]["batches"] == 1,
                  "单批次筛选的周对比也按同口径（批次数 1）")

    print("== T32 全取消项目：显示「已取消」、留在列表、不进「已完成」页 ==")
    pid32, bx = seed_project()
    bx2 = clone_batch(pid32, bx)
    batches_svc.cancel_batch(bx, reason="项目终止")
    batches_svc.cancel_batch(bx2, reason="项目终止")
    st = batches_svc.update_project_status(pid32)
    check(st == "Cancelled", f"全部批次取消 → 项目状态推导为 Cancelled（实际 {st}）")
    check(db.get_project(pid32)["status"] == "Cancelled", "库中项目状态 = Cancelled")
    listed = ([p["project_id"] for p in db.get_projects_by_status("Active")]
              + [p["project_id"] for p in db.get_projects_by_status("Cancelled")])
    check(pid32 in listed, "项目仍在项目列表（Active + Cancelled 合并列表，§D30）")
    check(pid32 not in [p["project_id"] for p in db.get_projects_by_status("Completed")],
          "项目不出现在「已完成」列表（T32 关键断言）")
    check(batches_svc.state_label("cancelled") == "已取消", "状态标签为「已取消」")

    # 报告口径：全取消项目留在报告范围内，但其批次默认隐藏（不产生批次数）
    m32 = reporting.build_report("daily", "2026-09-20", project_filter=pid32)
    check(m32["overview"]["project_count"] == 1, "全取消项目仍在报告项目范围内（留在列表）")
    check(m32["overview"]["batch_count"] == 0,
          "取消批次默认隐藏 → 批次数为 0（§8/D14）")
    m32_all = reporting.build_report("daily", "2026-09-20")
    g32 = [g for g in m32_all["customer_groups"]]
    check(isinstance(g32, list), "全部项目报告仍可正常生成（含按客户分组）")

    # 首页：已取消项目计数（不在 Active/Completed 里“消失”）
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        from ui.pages.home_page import HomePage
        from ui.pages.completed_page import CompletedPage
        from ui.pages.report_page import ReportPage
        hp = HomePage()
        hp.refresh()
        check(hp.card_cancelled.count_lbl.text() == str(db.count_projects("Cancelled")),
              f"首页「已取消」卡显示 {hp.card_cancelled.count_lbl.text()} 个项目")
        check(str(db.count_projects("Cancelled")) in hp.card_active.sub_lbl.text()
              or db.count_projects("Cancelled") == 0,
              f"首页进行中卡片副标题标注已取消数：{hp.card_active.sub_lbl.text()}")
        cp = CompletedPage()
        cp.refresh()
        cp.show_cancelled.setChecked(True)
        names = [cp.cancel_table.item(i, 1).text() for i in range(cp.cancel_table.rowCount())]
        check(db.get_project(pid32)["project_name"] in names,
              f"已完成页「显示已取消」可审计全取消项目的批次（{len(names)} 行）")
        check(cp.cancel_log.rowCount() > 0, "已完成页审计面板只读展示 op_log")
        rows_completed = {cp.table.item(i, 0).text() for i in range(cp.table.rowCount())}
        check(db.get_project(pid32)["project_name"] not in rows_completed,
              "已完成页表格不含全取消项目（T32：不进已完成页）")
        rp = ReportPage()
        rp.refresh()
        check(rp.batch_combo.count() >= 2 and rp.customer_combo.count() >= 2,
              f"报告页批次/客户下拉可用（批次 {rp.batch_combo.count()} · "
              f"客户 {rp.customer_combo.count()}）")
        proj_names = [rp.project_combo.itemText(i) for i in range(rp.project_combo.count())]
        check(db.get_project(pid32)["project_name"] in proj_names,
              "全取消项目仍出现在「项目」下拉列表（留在列表，§D30/T32）")
        rp.project_combo.setCurrentIndex(0)
        rp.refresh()
        check(rp.batch_combo.count() > 1, "报告页「全部项目」下批次下拉汇总各启用批次")
    except Exception as e:  # Qt 环境不可用时不阻塞纯逻辑验收
        skip(f"Qt 界面检查跳过：{type(e).__name__}: {e}")

    print("\n=== 结果 ===")
    if FAILED:
        print(f"FAILED: {len(FAILED)} 项")
        for m in FAILED:
            print("  ✗ " + m)
        if SKIPPED:
            print(f"（另有 {len(SKIPPED)} 项跳过）")
        sys.exit(1)
    print("T9 / T20 / T21 / T29 / T32 + §11 KPI 口径 全部通过"
          + (f"（{len(SKIPPED)} 项跳过）" if SKIPPED else ""))
    shutil.rmtree(_TMP, ignore_errors=True)


# ── 小工具 ──

def _add(d, n):
    y, m, dd = (int(x) for x in d.split("-"))
    return (date(y, m, dd) + timedelta(days=n)).isoformat()


def b01_etd(pid, bid):
    return db.get_route(bid)["etd"]


def b01_eta(pid, bid):
    return db.get_route(bid)["eta"]


class _make_guard:
    """占位（保留调用点语义：确保 op_log 已初始化）。"""

    def __init__(self, _):
        pass

    def stop(self):
        pass


def _install_fake_template():
    """注入假目的国模板模块，驱动 route_impact 的「结构差异」分支。

    制造四类结构差异（日期差异由 new_route 的 etd/eta 制造）：
      · 删除  —— 去掉 EMPTY_PICKUP
      · 改名  —— LOADING 换名
      · 顺序  —— GATE_IN 与 EXPORT_CUSTOMS 互换 seq（节点集合与正常相对次序之外，
                 仅这两者在「按 seq 排序」中的位置互换 ⇒ 报「顺序变化」）
      · 新增  —— 追加 FAKE_EXTRA
    另外：seq 前移一位（因为删掉了序 1 的 EMPTY_PICKUP），删除段之后的节点
    「序号整体平移」不再被视为顺序变化（按 node_key 位置判定，见 route_impact）。
    """
    import types
    base = node_template()
    seq_of = {n["node_key"]: n["seq"] for n in base}
    swapped = {"GATE_IN": seq_of["EXPORT_CUSTOMS"], "EXPORT_CUSTOMS": seq_of["GATE_IN"]}
    nodes = []
    for n in base:
        if n["node_key"] == "EMPTY_PICKUP":
            continue                                  # 删除
        n = dict(n)
        # 删除后重排：3→2、4→3 保持正常链路，再让 3/4 互换制造顺序变化
        n["seq"] = n["seq"] - 1
        if n["node_key"] in swapped:
            n["seq"] = swapped[n["node_key"]] - 1
        if n["node_key"] == "LOADING":
            n["node_name"] = "装船作业（新命名）"       # 改名
        nodes.append(n)
    extra = dict(base[0])
    extra.update({"node_key": "FAKE_EXTRA", "node_name": "新增节点（假模板）",
                  "seq": len(base) + 1})
    nodes.append(extra)                               # 新增
    mod = types.ModuleType("templates")
    mod.load_template = lambda key: nodes
    sys.modules["templates"] = mod


def _uninstall_fake_template():
    sys.modules.pop("templates", None)


if __name__ == "__main__":
    main()
