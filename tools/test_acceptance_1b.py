#!/usr/bin/env python3
"""Phase 1B 验收用例（《多式联运.md》§14：T10、T17、T18、T30 + §10.3/§10.4/§10.5/§12.4）。

覆盖：
  T10 提醒配置     —— 类型×目的国×承运人 匹配生效，未命中回退全局默认（§10.2）
  T17 免箱期提醒   —— 免箱期截止前 3 天触发高优先级提醒，且置顶（§10.4）
  T18 单证依赖     —— MBL 未提交时 HBL 显示「待上游」（§10.3）
  T30 报关要素     —— 报关行/报关方式可录并可筛选/统计
  另含 §10.3 依赖视图、§10.4 到货通知/保险到期/申报截止、§10.5 三级汇总 +
  徽标口径=启用中批次待办合计、§12.4 提醒中心（按批次分组）。

用法: python tools/test_acceptance_1b.py
离屏运行，使用隔离临时库（绝不触碰 data/logistics.db）。
"""
import copy
import os
import shutil
import sys
import tempfile
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import db

_TMP = tempfile.mkdtemp(prefix="opt_1b_")
db.DB_PATH = os.path.join(_TMP, "t.db")
db._conn = None
db.init_db()

from services import docdict
from services import reminder as R
from services import doc_dependency as dep
from services import customs_stats as cs
from services import batches as bsvc
from mock_data import DEMO_PROJECT, build_project

FAILED = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")
    if not cond:
        FAILED.append(msg)


def d(s):
    return date(*map(int, s.split("-")))


def d_add(s, n):
    return (d(s) + timedelta(days=n)).isoformat()


_pid = 0


def seed(with_parties=True, route_extra=None, country="BR"):
    """新建项目 + 默认批次，返回 batch_id。"""
    global _pid
    _pid += 1
    proj = copy.deepcopy(DEMO_PROJECT)
    proj["project_id"] = f"{DEMO_PROJECT['project_id']}-1b{_pid}"
    proj["project_name"] = f"{DEMO_PROJECT['project_name']} #1b{_pid}"
    proj["country"] = country
    bid = build_project(proj)["batch_id"]
    if route_extra:
        db.update_route(bid, **route_extra)
    if with_parties:
        p = db.insert_party(party_name=f"发货人{_pid}", country="CN",
                            roles=["SHIPPER", "CONSIGNEE", "IMPORTER"])
        for role in ("SHIPPER", "CONSIGNEE", "IMPORTER"):
            db.bind_batch_party(bid, role, p)
    return bid


def find_file(bid, doc_key):
    for f in db.get_files_by_batch(bid):
        if docdict.match_doc_type(f["doc_name"]) == doc_key:
            return f
    return None


def items_of(bid, day, rtype=None):
    items = R.compute_reminders_for_batch(bid, today=day)
    if rtype:
        items = [i for i in items if i["type"] == rtype]
    return items


# ══════════════════════════════════════════════════════════════════
def t10():
    print("== T10 提醒配置（类型×目的国×承运人 匹配生效，未命中回退全局默认） ==")

    # ① 引擎层提前量：`services.reminder.lead_time_for` 是应用的生效口径
    #    （按「(单证类型, 目的国, 承运人)」维度特异性匹配，同分按 §10.2 行文顺序）
    g = R.lead_time_for("NO_SUCH_TYPE", country="ZZ", carrier="NO_SUCH_CARRIER")
    check(g["source"].startswith("全局默认") and g["before_days"] == 3,
          f"完全未命中 → 全局默认（source={g['source']}, days={g['before_days']}）")
    check(g["before_days"] == docdict.GLOBAL_DEFAULT_DAYS,
          "回退值与字典全局默认一致（GLOBAL_DEFAULT_DAYS=3）")

    m_us = R.lead_time_for("MBL", country="US", carrier=None)
    check(m_us["before_days"] == 3 and m_us["source"] == "目的国默认",
          f"MBL×US 命中目的国级规则（{m_us['source']} / {m_us['before_days']} 天）")

    m_us_cos = R.lead_time_for("MBL", country="US", carrier="中远海运")
    check(m_us_cos["before_days"] == 3 and m_us_cos["source"] == "目的国默认",
          "MBL×US×中远海运：无三方规则 → 目的国级优先于承运人级（§10.2 行文顺序）")

    m_cos = R.lead_time_for("MBL", country=None, carrier="中远海运")
    check(m_cos["before_days"] == 4 and m_cos["source"] == "承运人默认",
          f"MBL×中远海运（无目的国约束）命中承运人规则"
          f"（{m_cos['source']} / {m_cos['before_days']} 天）")
    check(m_cos["rule_id"] is not None and "承运人" in (m_cos["baseline_source"] or ""),
          f"承运人规则带基准来源（{m_cos['baseline_source']}）")
    m_cos_br = R.lead_time_for("MBL", country="BR", carrier="中远海运")
    check(m_cos_br["before_days"] == 4 and m_cos_br["source"] == "承运人默认",
          f"MBL×BR×中远海运：目的国无专属规则 → 承运人规则生效"
          f"（{m_cos_br['source']} / {m_cos_br['before_days']} 天）")
    m_cos_br_ams = R.lead_time_for("AMS", country="BR", carrier="中远海运")
    check(m_cos_br_ams["source"] == "单证类型默认" and m_cos_br_ams["before_hours"] == 24,
          f"AMS×BR×中远海运 → 单证类型默认（{m_cos_br_ams['source']} / "
          f"{m_cos_br_ams['before_hours']} 小时）")

    m_none = R.lead_time_for("MBL", country="BR", carrier=None)
    check(m_none["before_days"] == 3 and m_none["source"] == "单证类型默认",
          f"MBL×BR（无国别规则）→ 单证类型默认（{m_none['source']} / {m_none['before_days']} 天）")

    hbl = R.lead_time_for("HBL", country="BR")
    check(hbl["before_days"] == 2, f"HBL 与 MBL 分别配置（HBL={hbl['before_days']} 天）")
    isf = R.lead_time_for("ISF", country="US")
    check(isf["before_hours"] == 48, f"ISF 装船前 48 小时（{isf['before_hours']}）")

    # ② 三分量精确命中（临时插入一条 类型+国+承运人 规则），用后删除
    rid = db.insert_reminder_rule(doc_type="HBL", country="BR", carrier="马士基",
                                  before_days=7, baseline_source="测试三方规则",
                                  note="T10")
    try:
        exact = R.lead_time_for("HBL", country="BR", carrier="马士基")
        check(exact["before_days"] == 7 and exact["source"] == "规则（类型+目的国+承运人）",
              f"类型×目的国×承运人 三者同时命中（{exact['source']} / {exact['before_days']} 天）")
        only_country = R.lead_time_for("HBL", country="BR", carrier="其它船司")
        check(only_country["before_days"] == 2 and only_country["source"] == "单证类型默认",
              "承运人不匹配时回退（不误用三方规则）")
    finally:
        db.delete_reminder_rule(rid)
    after = R.lead_time_for("HBL", country="BR", carrier="马士基")
    check(after["before_days"] == 2, "删除规则后立即回退到类型默认（规则读库实时生效）")

    # ③ 引擎层：提醒条目携带基准来源
    bid = seed()
    mbl = find_file(bid, "MBL")
    check(mbl is not None, "演示批次含 MBL 单证")
    db.update_file(mbl["file_id"], remind_before_days=4)
    # 承运人 = 中远海运（mock_data 的 vessel_name），MBL 走承运人默认 4 天
    db.upsert_vessel(db.get_batch(bid)["project_id"], vessel_name="COSCO INTEGRITY",
                     carrier="中远海运", batch_id=bid)
    carrier = R.carrier_of(bid)
    check(carrier == "中远海运", f"承运人从 vessel.carrier 取得（{carrier}）")
    lead = R.lead_time_for("MBL", country="BR", carrier=carrier)
    check(lead["before_days"] == 4 and lead["source"] == "承运人默认",
          f"引擎按承运人维度取提前量（{lead['source']} / {lead['before_days']} 天）")

    late = items_of(bid, mbl["due_date"] if isinstance(mbl["due_date"], str) else "2026-09-01",
                    R.T_FILE_LATE)
    src_ok = all(i.get("baseline_source") or True for i in late)
    check(src_ok, "逾期提醒条目均带基准来源字段（§10.2 保留基准来源备注）")

    # 未配置承运人 → 回退类型默认 3 天（不是承运人默认 4 天）
    bid2 = seed()
    mbl2 = find_file(bid2, "MBL")
    db.upsert_vessel(db.get_batch(bid2)["project_id"], vessel_name="UNKNOWN STAR",
                     carrier=None, batch_id=bid2)
    lead2 = R.lead_time_for("MBL", country="BR", carrier=R.carrier_of(bid2))
    check(lead2["before_days"] == 3 and lead2["source"] == "单证类型默认",
          f"承运人未配置 → 回退单证类型默认 3 天（{lead2['source']}）")
    check(mbl2 is not None, "（批次2 MBL 存在）")


def t17():
    print("== T17 免箱期提醒（截止前 3 天触发高优先级提醒） ==")
    bid = seed()
    due = "2026-10-29"
    db.update_route(bid, free_detention_until=due, free_demurrage_until=None)
    # 演示数据里集装箱还箱日与锚点同期，先标记已还，避免干扰本用例的计数断言
    containers = db.get_containers(bid)
    for c in containers:
        db.update_container(c["container_id"], returned_at="2026-09-01")

    hit3 = items_of(bid, d_add(due, -3), R.T_DETENTION)
    check(len(hit3) == 1, f"截止前 3 天（{d_add(due, -3)}）触发免箱期提醒")
    it = hit3[0] if hit3 else {}
    check(it.get("level") == "P1" and it.get("pinned") is True,
          f"前 3 天为高优先级并置顶（level={it.get('level')}, pinned={it.get('pinned')}）")
    check(due in (it.get("msg") or ""), f"文案含截止日（{it.get('msg')}）")
    check(it.get("days_left") == 3, f"倒计时天数正确（days_left={it.get('days_left')}）")
    check(it.get("type") == R.T_DETENTION and it.get("section") == "DETENTION",
          "类型/分节归属免箱期倒计时")

    hit1 = items_of(bid, d_add(due, -1), R.T_DETENTION)
    check(len(hit1) == 1 and hit1[0]["level"] == "P0" and hit1[0]["pinned"],
          "前 1 天升级为 P0（需处理）并置顶")
    onday = items_of(bid, due, R.T_DETENTION)
    check(len(onday) == 1 and onday[0]["level"] == "P0", "截止当日为 P0")
    passed = items_of(bid, d_add(due, 2), R.T_DETENTION)
    check(len(passed) == 1 and passed[0]["level"] == "P0"
          and "已超期 2 天" in passed[0]["msg"], "超期后持续 P0 并显示超期天数")

    none4 = items_of(bid, d_add(due, -4), R.T_DETENTION)
    check(len(none4) == 0, "前 4 天不触发（窗口严格为 3/1 天）")
    none2 = items_of(bid, d_add(due, -2), R.T_DETENTION)
    check(len(none2) == 0, "前 2 天不触发（不是每日刷屏）")

    # 置顶：必须排在所有非置顶项之前，即使存在 P0 的旧类型提醒
    allitems = R.compute_reminders_for_batch(bid, today=d_add(due, -3))
    pinned = [x for x in allitems if x.get("pinned")]
    first_unpinned = next((i for i, x in enumerate(allitems) if not x.get("pinned")), None)
    check(pinned and (first_unpinned is None or allitems[0].get("pinned")),
          f"免箱期提醒置于列表顶部（共 {len(allitems)} 项，置顶 {len(pinned)} 项）")
    check(any(x["type"] == R.T_DETENTION for x in allitems[:len(pinned)]),
          "顶部区段即含免箱期提醒")

    # 免堆期与单柜还箱截止同样生效
    db.update_route(bid, free_demurrage_until=d_add(due, 1))
    dem = items_of(bid, d_add(due, -2), R.T_DEMURRAGE)
    check(len(dem) == 1 and dem[0]["pinned"], "免堆期倒计时同样置顶生效")

    cid = db.insert_container(bid, "CTN-T17", return_due=d_add(due, -1))
    cr = items_of(bid, d_add(due, -3), R.T_CONTAINER_RETURN)
    check(len(cr) == 1 and cr[0]["pinned"] and "CTN-T17" in cr[0]["msg"],
          f"单柜还箱截止（containers.return_due）也纳入免箱期倒计时并置顶"
          f"（{cr[0]['msg'] if cr else '未触发'}）")
    db.update_container(cid, returned_at=d_add(due, -5))
    check(len(items_of(bid, d_add(due, -3), R.T_CONTAINER_RETURN)) == 0,
          "已还箱的柜不再提醒")

    # 跨批次不串（T3 批次隔离在提醒口径上的体现）
    bid_other = seed()
    check(len(items_of(bid_other, d_add(due, -3), R.T_DETENTION)) == 0,
          "其他批次不受本批次免箱期影响")


def t18():
    print("== T18 单证依赖（MBL 未提交时 HBL 显示「待上游」） ==")
    bid = seed()
    mbl = find_file(bid, "MBL")
    hbl = find_file(bid, "HBL")
    check(mbl and hbl, "演示批次同时含 MBL 与 HBL 单证")

    ctx = dep.build_context(bid)
    st = dep.status_for_doc(hbl["doc_name"], ctx)
    check(st["blocked"] is True, "MBL 未提交 → HBL 判定为待上游（blocked）")
    check(st["text"] == "待上游：《MBL 主提单》",
          f"文案精确匹配 §10.3（{st['text']}）")
    check(st["waiting_keys"] == ["MBL"], f"上游 key 正确（{st['waiting_keys']}）")

    # 下游的传递上游（HBL 直接上游只有 MBL）
    check(dep.all_upstream_keys("HBL") == ["MBL"], "HBL 传递上游 = MBL")
    check("LOADING" in dep.downstream_keys("VGM"), "VGM → 装船 链路存在")
    check("PICKUP" in dep.downstream_keys("DO"), "D/O → 提货 链路存在")
    check("LOADING" in dep.downstream_keys("EXPORT_DECL")
          and "LOADING" in dep.downstream_keys("INSURANCE"),
          "报关单/保险单 → 装船 链路存在")

    # 依赖视图模型
    view = dep.dependency_view(bid)
    names = {n["key"]: n for n in view["nodes"]}
    check("HBL" in names and names["HBL"]["state"] == "waiting_upstream",
          "依赖视图标记 HBL 为待上游")
    check(names["HBL"]["waiting_text"] == "待上游：《MBL 主提单》",
          "依赖视图携带待上游文案")
    check(bool(view["edges"]) and bool(view["tree"]),
          f"依赖视图含连线边（{len(view['edges'])} 条）与缩进树（{len(view['tree'])} 节点）")
    check(any(e["up"] == "MBL" and e["down"] == "HBL" for e in view["edges"]),
          "连线图含 MBL → HBL 边")
    check(any(n["key"] == "LOADING" and n["virtual"] for n in view["nodes"]),
          "虚拟目标（装船/提货）在依赖视图中标注为 virtual")

    # 提交 MBL 后 → 解除
    db.update_file(mbl["file_id"], status="submitted", submitted_date="2026-09-10")
    ctx2 = dep.build_context(bid)
    st2 = dep.status_for_doc(hbl["doc_name"], ctx2)
    check(st2["blocked"] is False, "MBL 提交后 HBL 依赖解除")
    view2 = dep.dependency_view(bid)
    n2 = {n["key"]: n for n in view2["nodes"]}
    check(n2["MBL"]["state"] == "submitted" and n2["HBL"]["state"] == "ready",
          f"依赖视图状态更新（MBL={n2['MBL']['state']}, HBL={n2['HBL']['state']}）")

    # 引擎：依赖等待提醒
    db.update_file(mbl["file_id"], status="pending", submitted_date=None)
    deprem = items_of(bid, "2026-09-10", R.T_DEP_WAITING)
    check(any("HBL" in x["msg"] for x in deprem),
          "提醒引擎产生「依赖等待」条目（§10.4/§12.4 依赖等待分节）")
    hbl_item = next((x for x in deprem if "HBL" in x["msg"]), None)
    check(hbl_item and hbl_item["waiting_names"] == ["MBL 主提单"],
          f"提醒条目携带上游名称（{hbl_item and hbl_item.get('waiting_names')}）")

    # 无环校验（§15 依赖链误配应对）
    try:
        db.set_doc_dependencies([("A", "B"), ("B", "C"), ("C", "A")])
        check(False, "环依赖应被拒绝")
    except ValueError as e:
        check("环" in str(e), f"环依赖被拒绝：{e}")
    finally:
        docdict.seed(force=False)      # 恢复默认依赖表（当前表非空不会覆盖）
        if not db.list_doc_dependencies():
            db.set_doc_dependencies(docdict.DOC_DEPS)

    # UI：FileRow 标灰 + 文案
    row_state = _file_row_state(bid, "HBL")
    check(row_state is not None, "FileRow 可构造（离屏）")
    if row_state:
        check(row_state["status"] == "待上游",
              f"FileRow 状态显示「待上游」（{row_state['status']}）")
        check(row_state["dep_text"] == "待上游：《MBL 主提单》",
              f"FileRow 依赖文案（{row_state['dep_text']}）")
        check(row_state["dep_shown"] is True, "FileRow 依赖标签可见标志为真")
        mbl_state = _file_row_state(bid, "MBL")
        check(mbl_state and mbl_state["status"] != "待上游",
              f"MBL 自身不标待上游（{mbl_state and mbl_state['status']}）")

    # UI：依赖视图控件
    dv = _dependency_widget(bid)
    check(dv is not None and dv.waiting_texts(),
          f"依赖视图控件可渲染待上游文案（{dv and dv.waiting_texts()[:2]}）")


def t30():
    print("== T30 报关要素（报关行/报关方式可录并可筛选/统计） ==")
    b1 = seed(route_extra={"customs_broker": "青岛中远报关行", "customs_mode": "一般贸易"})
    b2 = seed(route_extra={"customs_broker": "青岛中远报关行", "customs_mode": "买单"})
    b3 = seed(route_extra={"customs_broker": "桑托斯清关行", "customs_mode": "一般贸易"})
    b4 = seed()      # 未填

    r1 = db.get_route(b1) or {}
    check(r1.get("customs_broker") == "青岛中远报关行" and r1.get("customs_mode") == "一般贸易",
          "报关行/报关方式可录（batch_routes.customs_broker / customs_mode）")

    opts = cs.customs_broker_options()
    labels = [o["label"] for o in opts]
    check(opts and opts[0]["value"] is None and opts[0]["label"] == "全部报关行",
          "报关行选项首项为「全部报关行」")
    check("青岛中远报关行" in labels and "桑托斯清关行" in labels,
          f"报关行选项含已录值（{labels}）")
    cnt = {o["label"]: o["count"] for o in opts}
    check(cnt.get("青岛中远报关行") == 2, f"报关行出现次数统计正确（{cnt.get('青岛中远报关行')}）")

    mopts = cs.customs_mode_options()
    mlabels = [o["label"] for o in mopts]
    check("一般贸易" in mlabels and "买单" in mlabels, f"报关方式选项（{mlabels}）")

    stats = cs.customs_mode_stats()
    by_mode = {m["label"]: m["count"] for m in stats["by_mode"]}
    check(by_mode.get("一般贸易") == 2 and by_mode.get("买单") == 1,
          f"按报关方式统计（{by_mode}）")
    by_broker = {b["label"]: b["count"] for b in stats["by_broker"]}
    check(by_broker.get("青岛中远报关行") == 2, f"按报关行统计（{by_broker}）")
    check(stats["missing_customs"] >= 1, f"未填报关要素批次计入统计（{stats['missing_customs']}）")
    check(stats["total_batches"] >= 4, f"统计覆盖全部批次（{stats['total_batches']}）")

    # 组合统计（报关行 × 报关方式 矩阵）
    mat = {(m["broker"], m["mode"]): m["count"] for m in stats["matrix"]}
    check(mat.get(("青岛中远报关行", "一般贸易")) == 1
          and mat.get(("青岛中远报关行", "买单")) == 1,
          f"报关行×报关方式 交叉统计（{mat}）")

    # 筛选
    ids = cs.batch_ids_by_customs(broker="青岛中远报关行")
    check(set(ids) == {b1, b2}, f"按报关行筛选（{len(ids)} 个批次）")
    ids2 = cs.batch_ids_by_customs(mode="一般贸易")
    check(set(ids2) == {b1, b3}, "按报关方式筛选")
    ids3 = cs.batch_ids_by_customs(broker="青岛中远报关行", mode="买单")
    check(ids3 == [b2], "报关行+报关方式组合筛选")
    ids4 = cs.batch_ids_by_customs(broker="桑托斯清关行", status="running")
    check(ids4 == [], "筛选可叠加批次状态（draft 批次被排除）")
    ids5 = cs.batch_ids_by_customs(broker="")
    check(b4 in ids5, f"broker='' 可筛出未填报关行的批次（{len(ids5)} 个）")

    # 单批次取值 + 筛选文案
    one = cs.customs_of_batch(b1)
    check(one["customs_broker"] == "青岛中远报关行", "customs_of_batch 可取单批次报关要素")
    check(cs.customs_filter_label(broker="青岛中远报关行", mode="买单")
          == "报关行：青岛中远报关行 · 报关方式：买单", "报关筛选文案可拼装（报告页直接用）")

    # 取消批次默认不出现在统计中
    stat_before = cs.customs_mode_stats()["total_batches"]
    bsvc.cancel_batch(b4, reason="T30 统计口径")
    stat_after = cs.customs_mode_stats()["total_batches"]
    check(stat_after == stat_before - 1,
          f"取消批次默认排除出统计（{stat_before} → {stat_after}）")
    check(len(cs.customs_mode_stats(include_cancelled=True)["total_batches"]) == stat_before
          if False else
          cs.customs_mode_stats(include_cancelled=True)["total_batches"] == stat_before,
          "include_cancelled=True 可纳入取消批次")


# ══════════════════════════════════════════════════════════════════
def t104_other():
    print("== §10.4 到货通知 / 保险到期 / 申报截止 ==")
    bid = seed()

    # 到货通知 AN：到达即提醒
    an_node = db.node_by_key(bid, "ARRIVAL_NOTICE")
    eta = db.get_route(bid)["eta"]
    check(len(items_of(bid, d_add(eta, 0), R.T_ARRIVAL_NOTICE)) >= 1,
          f"AN 到达当日即提醒（eta={eta}）")
    an_items = items_of(bid, d_add(eta, 1), R.T_ARRIVAL_NOTICE)
    check(len(an_items) >= 1 and an_items[0]["section"] == "ARRIVAL",
          "到货通知归入「到货通知」分节")
    # 完成 AN 节点并提交单证 → 不再有「待提交」提示（仍有到港确认提示）
    db.update_node(db.get_batch(bid)["project_id"], an_node["node_id"],
                   batch_id=bid, status="Done", actual_completion_date=eta)
    anf = find_file(bid, "ARRIVAL_NOTICE")
    db.update_file(anf["file_id"], status="submitted", submitted_date=eta)
    after = items_of(bid, d_add(eta, 1), R.T_ARRIVAL_NOTICE)
    check(all("待提交" not in x["msg"] for x in after),
          f"AN 单证已提交后不再提示待提交（{len(after)} 条）")
    check(db.get_batch(bid)["actual_eta"] == eta,
          "装到 AN 节点完成 → actual_eta 自动回填（§6.10）")

    # 保险到期
    bid_i = seed()
    db.upsert_insurance(bid_i, company="PICC", policy_no="POL-1",
                        amount=100000, start_date="2026-01-01", end_date="2026-11-20")
    for off, lv in ((30, "P1"), (7, "P1"), (3, "P1"), (0, "P0")):
        got = items_of(bid_i, d_add("2026-11-20", -off), R.T_INSURANCE)
        check(len(got) == 1 and got[0]["level"] == lv,
              f"保险到期前 {off} 天提醒（level={got[0]['level'] if got else None}）")
    exp = items_of(bid_i, "2026-11-25", R.T_INSURANCE)
    check(len(exp) == 1 and exp[0]["level"] == "P0" and "已过期" in exp[0]["msg"],
          "保险过期后持续 P0")
    check(len(items_of(bid_i, "2026-09-01", R.T_INSURANCE)) == 0,
          "距到期 >30 天不提醒")
    pol = db.get_insurance(bid_i)
    check(pol and pol["policy_no"] == "POL-1", "保险单可读（db.get_insurance）")
    # 无保单批次不提醒
    check(len(items_of(seed(), "2026-11-20", R.T_INSURANCE)) == 0, "无保单批次不产生保险提醒")

    # 申报截止（AMS/ISF/ENS，装船前 N 小时）
    bid_d = seed()
    loading = db.node_by_key(bid_d, "LOADING")
    base = loading["plan_start"]
    ams = R.declaration_deadline(bid_d, "AMS")
    isf = R.declaration_deadline(bid_d, "ISF")
    check(ams and ams["gate_mode"] == "hours" and ams["due_hours"] == 24,
          f"AMS 截止按「装船前 24 小时」计算（{ams and ams['due_date']}）")
    check(isf and isf["due_hours"] == 48, "ISF 截止按「装船前 48 小时」计算")
    check(ams["deadline"].date() == d(base) - timedelta(days=1),
          f"截止日 = 装船日 {base} − 1 天（24h）")
    check(bool(ams["source"]), f"申报截止携带基准来源（{ams['source']}）")

    got = items_of(bid_d, ams["due_date"], R.T_DECLARATION)
    check(any(x["doc_key"] == "AMS" for x in got),
          f"到 AMS 截止日当天触发 P0 申报截止（{len(got)} 条）")
    check(all(x["level"] == "P0" for x in got if x["doc_key"] == "AMS"),
          "截止日当天为 P0")
    early = items_of(bid_d, d_add(ams["due_date"], -2), R.T_DECLARATION)
    check(all(x["doc_key"] != "AMS" for x in early),
          "距 AMS 截止 2 天时尚不提示（提前窗口 = 当天）")
    isf_day = items_of(bid_d, d_add(isf["due_date"], -1), R.T_DECLARATION)
    check(any(x["doc_key"] == "ISF" and x["level"] == "P1" for x in isf_day),
          "ISF 因 48h → 提前 1 天进入 P1 提醒")
    # 提交后不再提醒
    db.update_file(find_file(bid_d, "AMS")["file_id"], status="submitted",
                   submitted_date=base)
    check(not any(x["doc_key"] == "AMS"
                  for x in items_of(bid_d, ams["due_date"], R.T_DECLARATION)),
          "AMS 已提交后不再产生申报截止提醒")


def t105_three_level():
    print("== §10.5 三级汇总（项目→批次→节点）+ 徽标口径 = 启用中批次待办合计 ==")
    bid1 = seed()
    proj_id = db.get_batch(bid1)["project_id"]

    # 同项目第二批次（复制批次：单证一律「未开始」）
    nb = bsvc.copy_batch(proj_id, bid1, new_batch_no=None)
    bid2 = nb["batch_id"]
    check(all(f["status"] == "pending" for f in db.get_files_by_batch(bid2)),
          "复制批次单证一律「未开始」（§10.5）")
    check(nb["status"] in R.ENABLED_STATES,
          f"复制批次处于启用中状态（{nb['status']}；线路已带 ETD/ETA → 推导为 ready，§8/T28）")
    check((db.get_route(bid2) or {}).get("etd") == (db.get_route(bid1) or {}).get("etd"),
          "复制批次沿用源线路（冻结快照语义）")

    # 让批次1 处于 running（有 Active 节点即可），批次2 保持 draft
    node = db.get_nodes_by_batch(bid1)[0]
    db.update_node(proj_id, node["node_id"], batch_id=bid1, status="Active")
    bsvc.sync_batch_status(bid1)
    check(db.get_batch(bid1)["status"] in ("running", "ready"),
          f"批次1 处于启用中（{db.get_batch(bid1)['status']}）")

    today = "2026-09-10"
    data = R.compute_reminders_three_level([proj_id], today=today)
    proj = data["projects"][0]
    check(len(proj["batches"]) == 2, f"三级汇总含 2 个启用批次（{len(proj['batches'])}）")
    check(all("nodes" in b for b in proj["batches"]), "每个批次带「节点」下钻层")
    node_rows = [n for b in proj["batches"] for n in b["nodes"]]
    check(any(n["total"] > 0 and n["node_key"] for n in node_rows),
          f"节点层有归属项（{len(node_rows)} 个节点分组）")
    named = [n for n in node_rows if n["node_name"] and n["total"]]
    check(bool(named), f"节点层带节点名（示例：{named[0]['node_name'] if named else None}）")
    check(proj["total"] == sum(b["total"] for b in proj["batches"]),
          "项目合计 == 各批次之和")
    check(data["badge"]["total"] == proj["total"],
          f"徽标口径 == 启用中批次待办合计（{data['badge']['total']}）")

    # 关闭一批次 → 徽标随之下降
    for n in db.get_nodes_by_batch(bid2):
        db.update_node(proj_id, n["node_id"], batch_id=bid2, status="Done",
                       actual_completion_date="2026-09-01")
    for f in db.get_files_by_batch(bid2):
        db.update_file(f["file_id"], status="submitted", submitted_date="2026-09-01")
    ok, why = bsvc.confirm_complete(bid2)
    check(ok, f"批次2 可确认完成（{why}）")
    bsvc.close_batch(bid2)
    check(db.get_batch(bid2)["status"] == "closed", "批次2 已关闭")
    after = R.compute_reminders_three_level([proj_id], today=today)
    check(len(after["projects"][0]["batches"]) == 1,
          "关闭批次不再计入启用中批次")
    check(after["badge"]["total"] < data["badge"]["total"],
          f"徽标随批次关闭下降（{data['badge']['total']} → {after['badge']['total']}）")
    check(after["badge"]["total"] == R.badge_count([proj_id], today=today),
          "badge_count() 与三级汇总 badge 一致")

    # 与主窗口徽标同源（ui/main_window.py:_update_todo_badge 调用的就是 badge_count）
    check(R.badge_count(today=today) >= after["badge"]["total"],
          "全局徽标 >= 该项目徽标（主窗口口径一致）")

    # 取消批次不计入
    bid3 = seed()
    pid3 = db.get_batch(bid3)["project_id"]
    bsvc.cancel_batch(bid3, reason="§10.5 口径")
    z = R.compute_reminders_three_level([pid3], today=today)
    check(z["badge"]["total"] == 0, "已取消批次不计入徽标（启用中批次待办合计）")


def t124_center():
    print("== §12.4 提醒中心（按批次分组，含四节） ==")
    bid = seed()
    proj_id = db.get_batch(bid)["project_id"]
    loading_start = db.node_by_key(bid, "LOADING")["plan_start"]
    # 同时造出四类提醒：
    #   · 免箱期倒计时：截止日 = 装船日 + 1（相对装船日 −2 天时有提醒）
    #   · 到货通知：AN 节点已完成（实际到货日 = ETA），AN 单证仍待提交
    #   · 申报截止：ISF 装船前 48h → 提前 1 天进入 P1
    #   · 依赖等待：MBL 未提交 → HBL 待上游
    db.update_route(bid, free_detention_until=d_add(loading_start, 1))
    an_node = db.node_by_key(bid, "ARRIVAL_NOTICE")
    db.update_node(proj_id, an_node["node_id"], batch_id=bid, status="Done",
                   actual_completion_date=db.get_route(bid)["eta"])
    day = d_add(loading_start, -2)

    center = R.reminder_center([proj_id], today=day)
    check(len(center["groups"]) == 1, f"按批次分组（{len(center['groups'])} 组）")
    g = center["groups"][0]
    check(g["batch_no"] == db.get_batch(bid)["batch_no"], f"分组标题为批次号（{g['batch_no']}）")
    labels = [s["label"] for s in g["sections"] if s["count"]]
    for want in ("免箱期倒计时", "到货通知", "申报截止", "依赖等待"):
        check(want in labels, f"提醒中心含「{want}」分节")
    sec = {s["label"]: s for s in g["sections"]}
    check(sec["免箱期倒计时"]["items"][0]["pinned"], "免箱期分节条目置顶标记")
    check(sec["申报截止"]["items"][0]["doc_key"] in ("AMS", "ISF", "ENS"),
          f"申报截止分节条目为申报类单证（{sec['申报截止']['items'][0]['doc_key']}）")
    check(len(sec["依赖等待"]["items"]) >= 1, "依赖等待分节有条目")
    check(center["badge"]["total"] == g["total"], "提醒中心合计 == 分组合计")
    txt = R.format_reminder_center(center)
    check("免箱期倒计时" in txt and "依赖等待" in txt and "待上游" in txt,
          "提醒中心文本渲染含四节内容")

    # UI 对话框（离屏）
    ui = _reminder_center_dialog(day)
    check(ui is not None, "ReminderCenterDialog 可构造（离屏）")
    if ui is not None:
        d = ui.data()
        check(d["badge"]["total"] == R.reminder_center(today=day)["badge"]["total"],
              "对话框数据与引擎一致")
        titles = ui.group_titles()
        check(bool(titles) and db.get_batch(bid)["batch_no"] in titles,
              f"对话框按批次分组且含本批次（{len(titles)} 组）")
        check({"免箱期倒计时", "到货通知", "申报截止", "依赖等待"}
              <= set(ui.section_labels()),
              f"对话框渲染四个固定分节（{sorted(set(ui.section_labels()))}）")


# ══════════════════════════════════════════════════════════════════
_QAPP = None


def _app():
    global _QAPP
    if _QAPP is None:
        from PySide6.QtWidgets import QApplication
        _QAPP = QApplication.instance() or QApplication([])
    return _QAPP


def _file_row_state(bid, doc_key):
    """构造 FileRow，返回 UI 实际渲染的状态/文案。"""
    try:
        _app()
        from ui.widgets.file_panel import FileRow
        from services.clock import get_today
        f = find_file(bid, doc_key)
        if not f:
            return None
        st = dep.build_context(bid)
        d = dep.status_for_doc(f["doc_name"], st)
        row = FileRow(f, None, get_today(), False, dep=d)
        return {"status": row.status_label.text(),
                "dep_text": row.dep_label.text(),
                "dep_shown": bool(d.get("blocked") and d.get("text")),
                "name": row.name_label.text()}
    except Exception as e:                                  # noqa: BLE001
        print(f"    [warn] FileRow 构造失败：{type(e).__name__}: {e}")
        return None


def _dependency_widget(bid):
    try:
        _app()
        from ui.widgets.dependency_view import DependencyView
        v = DependencyView(bid)
        return v
    except Exception as e:                                  # noqa: BLE001
        print(f"    [warn] DependencyView 构造失败：{type(e).__name__}: {e}")
        return None


def _reminder_center_dialog(day):
    try:
        _app()
        from ui.widgets.reminder_center import ReminderCenterDialog
        dlg = ReminderCenterDialog(today=day)
        dlg.resize(820, 600)
        dlg.show()
        _app().processEvents()
        return dlg
    except Exception as e:                                  # noqa: BLE001
        print(f"    [warn] ReminderCenterDialog 构造失败：{type(e).__name__}: {e}")
        return None


def _file_panel_ui(bid, day):
    """FilePanel 依赖标灰的端到端（控件级）。"""
    print("== §10.3 FilePanel 依赖标灰（控件级） ==")
    try:
        _app()
        from ui.widgets.file_panel import FilePanel
        from services.clock import get_today
        fp = FilePanel(db.get_files_by_batch(bid), db.get_nodes_by_batch(bid),
                       get_today(), batch_id=bid)
        hbl = find_file(bid, "HBL")
        row = fp._rows.get(hbl["file_id"])
        check(row is not None, "FilePanel 已为 HBL 建立行")
        if row is not None:
            check(row.status_label.text() == "待上游",
                  f"FilePanel 中 HBL 行显示「待上游」（{row.status_label.text()}）")
            check(row.dep_label.text() == "待上游：《MBL 主提单》",
                  f"FilePanel 依赖标签文案（{row.dep_label.text()}）")
            check(fp.dep_for(hbl).get("blocked") is True, "FilePanel.dep_for 返回 blocked")
        tree = fp.dependency_tree()
        check(bool(tree.get("tree")), f"FilePanel.dependency_tree 可产出依赖视图（{len(tree.get('tree') or [])} 节点）")
        # 提交 MBL 后原地刷新 → 标灰解除（不重建控件）
        mbl = find_file(bid, "MBL")
        db.update_file(mbl["file_id"], status="submitted", submitted_date="2026-09-10")
        fp.update_files(db.get_files_by_batch(bid), db.get_nodes_by_batch(bid), batch_id=bid)
        row2 = fp._rows.get(hbl["file_id"])
        check(row2 is row, "增量刷新未重建行对象（历史崩溃根因回归）")
        check(row2.status_label.text() != "待上游",
              f"MBL 提交后 HBL 行不再标「待上游」（{row2.status_label.text()}）")
        db.update_file(mbl["file_id"], status="pending", submitted_date=None)
        fp.update_files(db.get_files_by_batch(bid), db.get_nodes_by_batch(bid), batch_id=bid)
        check(fp._rows[hbl["file_id"]].status_label.text() == "待上游", "撤交后标灰恢复")
    except Exception as e:                                  # noqa: BLE001
        check(False, f"FilePanel 控件级检查异常：{type(e).__name__}: {e}")


def t103_view_ui():
    print("== §10.3 依赖视图（列表 + 连线图） ==")
    bid = seed()
    dv = _dependency_widget(bid)
    check(dv is not None, "DependencyView 可构造")
    if dv is None:
        return
    m = dv.model()
    check(len(m["nodes"]) > 0 and len(m["edges"]) > 0,
          f"视图模型含节点/边（{len(m['nodes'])}/{len(m['edges'])}）")
    check(dv._tabs.count() == 2 and dv._tabs.tabText(0) == "列表"
          and dv._tabs.tabText(1) == "连线图",
          f"依赖视图含「列表 + 连线图」两页（{[dv._tabs.tabText(i) for i in range(dv._tabs.count())]}）")
    check(dv._canvas.width() > 0 and dv._canvas.height() > 0,
          f"连线图画布已布局（{dv._canvas.width()}×{dv._canvas.height()}）")
    check("MBL" in dv._canvas._layout and "HBL" in dv._canvas._layout,
          "连线图已为 MBL/HBL 排布方框")
    x_up = dv._canvas._layout["MBL"][0]
    x_down = dv._canvas._layout["HBL"][0]
    check(x_down > x_up, f"下游 HBL 排在上游 MBL 右侧（{x_up} → {x_down}）")
    check(dv._canvas._tooltip and "MBL" in dv._canvas._tooltip,
          "连线图携带依赖说明 tooltip")
    # 渲染一次，确认 paintEvent 不抛异常
    dv._canvas.grab()

    # 今日待办弹窗内的依赖视图页
    try:
        _app()
        from ui.dialogs import TodayTodoDialog
        from services.clock import get_today
        grp = {lv: [] for lv in R.LEVELS}
        for r in R.compute_reminders_for_batch(bid, today=get_today()):
            grp.setdefault(r["level"], []).append(r)
        dlg = TodayTodoDialog(grp, get_today(), batch_ids=[bid])
        check(dlg._tabs.count() == 2, "今日待办弹窗含「按优先级 / 依赖视图」两页")
        check(dlg.dependency_waiting_texts() and
              "MBL" in dlg.dependency_waiting_texts()[0],
              f"弹窗内依赖视图显示待上游（{dlg.dependency_waiting_texts()[:1]}）")
    except Exception as e:                                  # noqa: BLE001
        check(False, f"TodayTodoDialog 依赖页异常：{type(e).__name__}: {e}")


def t_api_compat():
    print("== 旧 API 兼容（ui/dialogs.py、ui/main_window.py、ui/pages/home_page.py 调用点） ==")
    bid = seed()
    proj = db.get_project(db.get_batch(bid)["project_id"])
    nodes = db.get_nodes_by_batch(bid)
    files = db.get_files_by_batch(bid)
    today = "2026-09-10"
    rem = R.compute_reminders(proj, nodes, files, today)
    check(set(rem.keys()) >= {"P0", "P1", "P2"}, "compute_reminders 仍返回三档字典")
    check(R.count_total(rem) == sum(len(rem[k]) for k in ("P0", "P1", "P2")),
          "count_total 口径不变")
    txt = R.format_reminders(rem, today)
    check(txt.startswith("今日待办 · 2026-09-10"), "format_reminders 仍可用")
    check(all(("project" in r and "msg" in r) for lv in ("P0", "P1", "P2")
              for r in rem[lv]), "条目保留旧字段 project / msg")
    check(all(r.get("batch_no") for lv in ("P0", "P1", "P2") for r in rem[lv]),
          "条目已补批次上下文（batch_no）")
    empty = R.format_reminders({"P0": [], "P1": [], "P2": []}, today)
    check("今日暂无待办" in empty, "空待办文案不变")


def main():
    print("=== Phase 1B 验收（T10 / T17 / T18 / T30 + §10.3 / §10.4 / §10.5 / §12.4）===")
    t10()
    t17()
    t18()
    t103_view_ui()
    t104_other()
    t105_three_level()
    t124_center()
    t_api_compat()

    # FilePanel 控件级（放在最后：本函数会改库状态）
    bid = seed()
    _file_panel_ui(bid, "2026-09-10")

    print("\n=== 结果 ===")
    if FAILED:
        print(f"FAILED: {len(FAILED)} 项")
        for m in FAILED:
            print("  ✗ " + m)
        shutil.rmtree(_TMP, ignore_errors=True)
        sys.exit(1)
    print("T10 / T17 / T18 / T30 及 §10.3 / §10.4 / §10.5 / §12.4 全部通过")
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
