"""船期变更处理（《多式联运.md》§5.4 D34）：A/B/C/D 四类自动重排 + 影响预览 + 留痕。
纯离线，人工登记。自动变更只写 batch_schedule_changes + op_log，不写 shift_history。

既然节点计划日期由 §5.3 从 ETD/ETA 唯一推导，四类的差异实际上就是：
  A 同幅度 → 整体平移（重算全区分段）
  B 仅 ETA → DOME 不动（锚点 ETD 未变），OVERSEA 顺移，海运时长重算
  C 仅 ETD → DOME 顺移 + SEA.start 变，OVERSEA 不动
  D 组合 = C 再 B
统一走 schedule2.recompute_batch_schedule（只覆盖未冻结节点），由锚点变化自然得到。

§5.4 执行细则：
  1 冻结保护（Done / 已填实际完成日不移动），冲突给出三选项
  2 联动重算：节点计划日期 → 单证 due_date（锚 node_key）→ 申报截止（装船前 24/48h）
  3 免箱期/免堆期不自动顺延，仅提示人工复核
  4 留痕：batch_schedule_changes + op_log，**不写 shift_history**
  5 回退：不做一键撤销，按“再次登记原船期”执行
  6 人工微调仍走手动位移（写 shift_history，source=manual）
  7 KPI：变更次数 / 平均影响天数 / 变更后新增逾期数（见 services/reporting.py）
"""

from datetime import timedelta

import db
from services import schedule2
from services.schedule2 import ScheduleError, _parse, _fmt


def classify(old_etd, old_eta, new_etd, new_eta):
    old_etd, old_eta, new_etd, new_eta = (_parse(x) for x in (old_etd, old_eta, new_etd, new_eta))
    d_etd = (new_etd - old_etd).days
    d_eta = (new_eta - old_eta).days
    if d_etd == d_eta:
        return "A", d_etd, d_eta
    if d_etd == 0:
        return "B", d_etd, d_eta
    if d_eta == 0:
        return "C", d_etd, d_eta
    return "D", d_etd, d_eta


CLASS_DESC = {
    "A": "整船期同幅平移（批内相对间隔不变，海运时长不变）",
    "B": "仅 ETA 变化 → 境外段顺移 + 海运时长重算，境内不动",
    "C": "仅 ETD 变化 → 境内段平移 + 海运 start 变更，境外不动",
    "D": "ETD/ETA 均变且幅度不同 → 境内随 ETD、境外随 ETA 组合重排",
}


def _frozen(n):
    return n.get("status") == "Done" or bool(n.get("actual_completion_date"))


def _affected_count(batch_id):
    nodes = db.get_nodes_by_batch(batch_id)
    frozen = [n for n in nodes if _frozen(n)]
    return len(nodes) - len(frozen)


def _node_changes(batch_id, new_etd, new_eta):
    """逐节点计划日期变化（仅未冻结节点会实际变更）。"""
    nodes = db.get_nodes_by_batch(batch_id)
    plan = schedule2.compute_plan(new_etd, new_eta, nodes)
    changed, frozen_kept = [], []
    for n in nodes:
        k = n["node_key"]
        ns, ne = plan[k]
        old = (n.get("plan_start"), n.get("plan_end"))
        if old == (ns, ne):
            continue
        item = {"node_key": k, "node_name": n["node_name"], "seq": n["seq"],
                "old_start": old[0], "old_end": old[1],
                "new_start": ns, "new_end": ne, "frozen": _frozen(n),
                "delta_days": (_parse(ns) - _parse(old[0])).days if old[0] else None}
        if _frozen(n):
            frozen_kept.append(item)      # 冻结保护：不动，但需提示冲突
        else:
            changed.append(item)
    return changed, frozen_kept


def _doc_due_changes(batch_id, new_etd, new_eta):
    """按新锚点推算各单证 due 变化（不写库，仅预览）。"""
    nodes = db.get_nodes_by_batch(batch_id)
    plan = schedule2.compute_plan(new_etd, new_eta, nodes)
    out = []
    for f in db.get_files_by_batch(batch_id):
        rule = f.get("due_rule")
        if rule == "loading_before_hours":
            import math
            base = plan.get("LOADING")
            if not base:
                continue
            days = max(1, math.ceil(int(f.get("due_hours") or 24) / 24.0))
            new_due = (_parse(base[0]) - timedelta(days=days)).isoformat()
        else:
            key = f.get("due_node_key")
            if not key or key not in plan:
                continue
            new_due = plan[key][1] if f.get("due_type") == "node_end" else plan[key][0]
        if new_due != f.get("due_date"):
            out.append({"doc_name": f["doc_name"], "doc_type_key": f.get("doc_type_key"),
                        "old_due": f.get("due_date"), "new_due": new_due,
                        "baseline_source": f.get("baseline_source")})
    return out


def preview(project_id, batch_id, new_etd, new_eta):
    """返回变更影响预览 dict（不写库）。"""
    route = db.get_route(batch_id)
    if not route or not route.get("etd"):
        raise ScheduleError("批次尚未设定船期，无法变更")
    if not new_etd or not new_eta:
        raise ScheduleError("请填写新 ETD 与新 ETA")
    if _parse(new_eta) <= _parse(new_etd):
        raise ScheduleError("新 ETA 必须晚于新 ETD")

    cls, d_etd, d_eta = classify(route["etd"], route["eta"], new_etd, new_eta)
    nodes = db.get_nodes_by_batch(batch_id)
    changed, frozen_kept = _node_changes(batch_id, new_etd, new_eta)
    doc_changes = _doc_due_changes(batch_id, new_etd, new_eta)

    route_obj = db.get_route(batch_id)
    body = {
        "project_id": project_id, "batch_id": batch_id,
        "class": cls, "class_desc": CLASS_DESC.get(cls, ""),
        "old_etd": route["etd"], "old_eta": route["eta"],
        "new_etd": new_etd, "new_eta": new_eta,
        "delta_etd": d_etd, "delta_eta": d_eta,
        "affected_nodes": len(changed),
        "sea_duration_old": (_parse(route["eta"]) - _parse(route["etd"])).days,
        "sea_duration": (_parse(new_eta) - _parse(new_etd)).days,
        "node_changes": changed,            # 受影响节点清单 + 逐节点日期变化
        "frozen_conflicts": frozen_kept,    # 冻结保护：需人工处理
        "doc_changes": doc_changes,         # 单证 due 变化
        "free_demurrage_until": route_obj.get("free_demurrage_until"),
        "free_detention_until": route_obj.get("free_detention_until"),
        "free_time_recheck": True,          # §5.4 细则3：不自动顺延，仅提示复核
    }
    # 冲突三选项（§5.3）：有冻结节点会与新计划重叠时给出
    if frozen_kept:
        body["conflict_options"] = [
            "①仅重算未开始节点（推荐）",
            "②整体平移并列出“需压缩/人工处理”的节点",
            "③放弃本次变更",
        ]
    return body


def apply(project_id, batch_id, new_etd, new_eta, reason="", source="", confirm=True):
    """执行船期变更：更新线路锚点 → 重算计划日期 → 联动单证 due → 留痕。
    返回 dict：{body, changed, doc_changes, alert}。"""
    route = db.get_route(batch_id)
    if not route:
        raise ScheduleError("批次缺少线路方案")
    body = preview(project_id, batch_id, new_etd, new_eta)
    old_etd, old_eta = route["etd"], route["eta"]

    db.update_route(batch_id, etd=new_etd, eta=new_eta)
    db.update_project(project_id, etd=new_etd, eta=new_eta)

    # §5.4 细则2：节点计划日期重算（仅未冻结节点）
    res = schedule2.recompute_batch_schedule(
        project_id, batch_id=batch_id,
        reason=f"船期变更（{body['class']}类）{old_etd}/{old_eta}→{new_etd}/{new_eta}")

    # §5.4 细则2：单证 due_date（锚 node_key）与申报截止（装船前 24/48h）同步重算
    plan = {n["node_key"]: (n["plan_start"], n["plan_end"])
            for n in db.get_nodes_by_batch(batch_id)}
    due_changed = db.recompute_files_due(project_id, plan, batch_id=batch_id)

    db.insert_schedule_change(
        batch_id, old_etd=old_etd, old_eta=old_eta, new_etd=new_etd, new_eta=new_eta,
        rule_class=body["class"], reason=reason or "船期调整", source=source or "人工登记",
        affected_nodes=body["affected_nodes"])

    from services.oplog import record
    record("batch_schedule_change", project_id, batch_id=batch_id,
           subject=f"线路 {route.get('export_port') or ''}", scope="batch",
           detail=(f"{body['class']}类 · ETD {old_etd}→{new_etd} · ETA {old_eta}→{new_eta}"
                   f" · 影响 {len(res['changed'])} 个节点 · 单证 due 重算 {due_changed} 项"
                   f" · 原因: {reason or '未填'}"))

    # §5.4 细则3：免堆期/免箱期不自动顺延
    alert = (f"船期已按 {body['class']} 类重排：影响 {len(res['changed'])} 个节点、"
             f"重算 {due_changed} 份单证截止日。"
             f"免堆期/免箱期不自动顺延，请与船公司/堆场复核后人工调整。")
    if body.get("frozen_conflicts"):
        alert += f" 另有 {len(body['frozen_conflicts'])} 个已完成节点已冻结、未移动，请复核。"
    return {"body": body, "changed": res["changed"], "doc_changes": body["doc_changes"],
            "due_recomputed": due_changed, "alert": alert}
