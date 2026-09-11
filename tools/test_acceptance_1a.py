#!/usr/bin/env python3
"""Phase 1A 验收用例（《多式联运.md》§14：T35–T40）。
覆盖：客户/货主建模、单证前置校验、税号校验、计划日期规则、
船期变更 A/B/C/D、变更隔离与留痕。离屏运行，用隔离临时库。
用法: python tools/test_acceptance_1a.py
"""
import os
import sys
import tempfile
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db

_TMP = tempfile.mkdtemp(prefix="opt_1a_")
db.DB_PATH = os.path.join(_TMP, "t.db")
db._conn = None
db.init_db()

from services import node_template as nt
from services import schedule2
from services.schedule_change import classify, apply, ScheduleError
from services.batches import missing_roles, importer_tax_missing
from mock_data import DEMO_PROJECT, build_project

FAILED = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")
    if not cond:
        FAILED.append(msg)


def d(s):
    return date(*map(int, s.split("-")))


_pid_counter = 0


def _seed():
    """基于 DEMO_PROJECT 克隆一个新项目+默认批次，返回 batch_id。"""
    global _pid_counter
    _pid_counter += 1
    import copy
    proj = copy.deepcopy(DEMO_PROJECT)
    proj["project_id"] = f"{DEMO_PROJECT['project_id']}-acc{_pid_counter}"
    return build_project(proj)["batch_id"]


def calc(batch_id):
    """返回 {node_key:(start,end)} 由当前库节点计算。"""
    nodes = db.get_nodes_by_batch(batch_id)
    return {n["node_key"]: (n["plan_start"], n["plan_end"]) for n in nodes}


def node_plan(batch_id, node_key):
    n = db.node_by_key(batch_id, node_key)
    return (n["plan_start"], n["plan_end"])


def shift_days(a, b):
    return (d(b) - d(a)).days


def main():
    print("== T35 客户/货主建模 ==")
    bid = _seed()
    shipper = db.insert_party(party_name="青岛腾达物流", country="CN", roles=["SHIPPER", "IMPORTER", "CONSIGNEE"])
    notify1 = db.insert_party(party_name="宁波通报一", country="CN", roles=["NOTIFY"])
    notify2 = db.insert_party(party_name="上海通报二", country="CN", roles=["NOTIFY"])
    db.bind_batch_party(bid, "SHIPPER", shipper)
    db.bind_batch_party(bid, "IMPORTER", shipper)
    db.bind_batch_party(bid, "CONSIGNEE", shipper)
    db.bind_batch_party(bid, "NOTIFY", notify1)
    db.bind_batch_party(bid, "NOTIFY", notify2)
    parts = db.get_batch_parties(bid)
    roles = {p["role"] for p in parts}
    check({"SHIPPER", "IMPORTER", "CONSIGNEE", "NOTIFY"} <= roles, "四类角色均可绑定")
    check(len([p for p in parts if p["role"] == ("NOTIFY")]) == 2, "通知方支持多条目")
    same_company_multi = len({p["party_id"] for p in parts if p["party_id"] == shipper}) == 1 and \
        {"SHIPPER", "IMPORTER", "CONSIGNEE"} <= {p["role"] for p in parts if p["party_id"] == shipper}
    check(same_company_multi, "同一公司可同时承担多角色")

    print("== T36 单证前置校验 ==")
    bid2 = _seed()
    check(missing_roles(bid2, "出口报关单") == {"SHIPPER", "IMPORTER"}, "缺 SHIPPER/IMPORTER 时报关单不可提交")
    check(missing_roles(bid2, "到货通知") == {"SHIPPER", "CONSIGNEE"}, "缺 SHIPPER/CONSIGNEE 时 AN 不可提交")
    check(missing_roles(bid2, "海运提单") == {"SHIPPER", "CONSIGNEE"}, "缺 SHIPPER/CONSIGNEE 时提单不可提交")
    check(missing_roles(bid2, "D/O") == {"SHIPPER", "CONSIGNEE"}, "缺 SHIPPER/CONSIGNEE 时 D/O 不可提交")
    s2 = db.insert_party(party_name="集港发货人", country="CN", roles=["SHIPPER"])
    db.bind_batch_party(bid2, "SHIPPER", s2)
    check(missing_roles(bid2, "出口报关单") == {"IMPORTER"}, "仅补 SHIPPER 后仍缺 IMPORTER")
    imp2 = db.insert_party(party_name="巴西进口方", country="BR", roles=["IMPORTER"])
    db.bind_batch_party(bid2, "IMPORTER", imp2)
    check(missing_roles(bid2, "出口报关单") == set(), "补齐角色后报关单可提交")
    cons2 = db.insert_party(party_name="圣保罗收货人", country="BR", roles=["CONSIGNEE"])
    db.bind_batch_party(bid2, "CONSIGNEE", cons2)
    check(missing_roles(bid2, "到货通知") == set(), "补齐角色后 AN 可提交")

    print("== T37 税号校验 ==")
    bid3 = _seed()
    imp3 = db.insert_party(party_name="巴西进口方-无税号", country="BR", roles=["IMPORTER"])
    db.bind_batch_party(bid3, "IMPORTER", imp3)
    check(importer_tax_missing(bid3, "BR") is True, "目的国要求税号而进口商未填 → 阻断")
    imp3b = db.insert_party(party_name="巴西进口方-有税号", country="BR", roles=["IMPORTER"],
                            tax_id="12.345.678/0001-90", tax_id_type="CNPJ")
    db.bind_batch_party(bid3, "IMPORTER", imp3b)
    check(importer_tax_missing(bid3, "BR") is False, "进口商填了 CNPJ → 放行")

    print("== T38 计划日期规则（§5.3 三条不变量） ==")
    bid4 = _seed()
    route = db.get_route(bid4)
    etd, eta = route["etd"], route["eta"]
    p = calc(bid4)
    dome_nodes = [n for n in db.get_nodes_by_batch(bid4) if n["area"] == "DOME"]
    dome_last = max(dome_nodes, key=lambda n: d(n["plan_end"]))
    check(dome_last["plan_end"] == etd, f"境内末 end({dome_last['plan_end']}) == ETD({etd})")
    check(p[nt.SEA_TRANSIT][0] == etd, "海运 start == ETD")
    check(p[nt.SEA_TRANSIT][1] == eta, "海运 end == ETA")
    oversea_first = min((n for n in db.get_nodes_by_batch(bid4) if n["area"] == "OVERSEA"),
                        key=lambda n: d(n["plan_start"]))
    check(oversea_first["plan_start"] == eta, f"境外首 start({oversea_first['plan_start']}) == ETA({eta})")
    sea_dur = (d(eta) - d(etd)).days
    check((d(p[nt.SEA_TRANSIT][1]) - d(p[nt.SEA_TRANSIT][0])).days == sea_dur, "海运 duration = ETA−ETD")

    # 冻结保护：Done 节点不改其日期
    tf = db.node_by_key(bid4, nt.LOADING)
    db.update_node(bid4, tf["node_id"], status="Done", actual_completion_date=tf["plan_end"])
    before = node_plan(bid4, nt.LOADING)
    res = schedule2.recompute_batch_schedule(bid4, batch_id=bid4, reason="测试冻结")
    after = node_plan(bid4, nt.LOADING)
    check(before == after, "Done 节点被冻结，重算后计划日期不变")
    check(nt.LOADING not in {c.get("node_key") or c.get("key") for c in res.get("changed", [])},
          "Done 节点不参与变更列表")

    print("== T39 船期变更 A/B/C/D ==")
    # classify 纯分类
    check(classify("2026-09-01", "2026-09-20", "2026-09-06", "2026-09-25")[0] == "A", "同幅度=A")
    check(classify("2026-09-01", "2026-09-20", "2026-09-01", "2026-09-23")[0] == "B", "仅ETA=B")
    check(classify("2026-09-01", "2026-09-20", "2026-09-03", "2026-09-20")[0] == "C", "仅ETD=C")
    check(classify("2026-09-01", "2026-09-20", "2026-09-03", "2026-09-26")[0] == "D", "组合=D")

    # 实际重排：顺序跑 A→B→C→D
    bid5 = _seed()
    r0 = db.get_route(bid5)
    e0, a0 = r0["etd"], r0["eta"]
    pA0 = calc(bid5)
    dom = [k for k, v in pA0.items() if db.node_by_key(bid5, k)["area"] == "DOME"]

    # A→B→C→D 顺序跑。因 EXPORT_CUSTOMS/IMPORT_CUSTOMS 为 WORKDAY（§5.3 跳过周末），
    # 逐节点"日历天数平移"会跨周末而非恒等 delta；正确判定 = 对比 compute_plan 在新锚点
    # 的理论值是否全部落库，且不变段节点日期保持原样。
    def expect_match(batch, e, a):
        nodes = db.get_nodes_by_batch(batch)
        want = schedule2.compute_plan(e, a, nodes)
        got = {n["node_key"]: (n["plan_start"], n["plan_end"]) for n in nodes}
        return got == want

    # A：ETD/ETA 同幅度 +5
    resA = apply(bid5, bid5, d_add(e0, 5), d_add(a0, 5), reason="A整体平移")
    pA = calc(bid5)
    ose_key = [k for k in pA if db.node_by_key(bid5, k)["area"] == "OVERSEA"]
    dom_key = [k for k in pA if db.node_by_key(bid5, k)["area"] == "DOME"]
    check(expect_match(bid5, d_add(e0, 5), d_add(a0, 5)),
          "A 类：全部节点落库 == §5.3 按新锚点理论值")
    check(shift_days(pA[nt.SEA_TRANSIT][1], pA[nt.SEA_TRANSIT][0]) ==
          shift_days(pA0[nt.SEA_TRANSIT][1], pA0[nt.SEA_TRANSIT][0]), "A 类：海运时长不变")

    # B：仅 ETA +3（ETD 不变）→ 境内段锚点未动，境内节点日期保持
    e1 = d_add(e0, 5)
    a1 = d_add(a0, 5)
    resB = apply(bid5, bid5, e1, d_add(a1, 3), reason="B仅ETA")
    pB = calc(bid5)
    dome_ok = all(pB[k] == pA[k] for k in dom_key)
    check(dome_ok, "B 类：境内段锚点不动，节点日期完全不变")
    check(expect_match(bid5, e1, d_add(a1, 3)), "B 类：境外段按 §5.3 重算落库")

    # C：仅 ETD +2（ETA 不变）→ 境外段锚点未动，境外节点保持
    e2 = d_add(e1, 2)
    a2 = d_add(a1, 3)
    resC = apply(bid5, bid5, e2, a2, reason="C仅ETD")
    pC = calc(bid5)
    oversea_okC = all(pC[k] == pB[k] for k in ose_key)
    check(oversea_okC, "C 类：境外段锚点不动，节点日期完全不变")
    check(expect_match(bid5, e2, a2), "C 类：境内段按 §5.3 重算落库")

    # D：组合（ETD+1、ETA-4）→ 两段均重算
    e3 = d_add(e2, 1)
    a3 = d_add(a2, -4)
    cls_d = classify(e2, a2, e3, a3)[0]
    check(cls_d == "D", f"D 类：组合变更识别（{cls_d}）")
    resD = apply(bid5, bid5, e3, a3, reason="D组合")
    pD = calc(bid5)
    check(expect_match(bid5, e3, a3), "D 类：整体按 §5.3 重算落库（境内/境外各自随锚点）")

    print("== T40 变更隔离与留痕 ==")
    scs = db.get_schedule_changes(bid5, limit=10)
    check(len(scs) >= 4, f"batch_schedule_changes 记录 {len(scs)} 次变更（A/B/C/D）")
    check(all(s["rule_class"] in ("A", "B", "C", "D") for s in scs), "变更均标注类别")
    hist = db.get_shift_history(bid5, limit=50, batch_id=bid5)
    check(len(hist) == 0, f"shift_history 不被船期变更污染（当前 {len(hist)} 条）")
    changes_from_sea = [s for s in scs if s["reason"].startswith("A") or s["reason"].startswith("B") or
                        s["reason"].startswith("C") or s["reason"].startswith("D")]
    check(len(changes_from_sea) == 4, "4 次变更都来自船期调整")
    check("不自动顺延" in resD["alert"], "免堆期/免箱期仅提示不自动顺延")

    # ETA ≤ ETD 防护
    try:
        apply(bid5, bid5, d_add(e3, 1), e3, reason="非法")
        check(False, "ETA≤ETD 应被拦截")
    except ScheduleError:
        check(True, "ETA≤ETD 被拦截")

    print("\n=== 结果 ===")
    if FAILED:
        print(f"FAILED: {len(FAILED)} 项")
        for m in FAILED:
            print("  ✗ " + m)
        sys.exit(1)
    print("T35–T40 全部通过")


def d_add(s, n):
    return (d(s) + timedelta(days=n)).isoformat()


if __name__ == "__main__":
    main()