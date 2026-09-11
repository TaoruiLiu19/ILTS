"""
节点计划日期计算（《多式联运.md》§5.3 D33）—— 唯一计划来源入口。

锚点与三段排布（唯一算法）：
  · 海运 SEA_TRANSIT：start = ETD，end = ETA，duration = ETA − ETD
  · 境内 DOME：倒排（cursor = ETD，按模板顺序反向遍历）
  · 境外 OVERSEA：顺排（cursor = ETA，按模板顺序正向遍历）
日历模式：NATURAL 连续 / WORKDAY 跳过周六周日。
冻结：status='Done' 或已填 actual_completion_date 的节点不参与重算。
重算：仅覆盖 Pending / Active 节点；只把「发生变化」的行写 op_log(kind=schedule_recompute)。
"""

from datetime import date, timedelta

import db
from services import node_template as nt


class ScheduleError(Exception):
    pass


def _parse(s):
    if isinstance(s, date):
        return s
    if not s:
        return None
    y, m, d = str(s).split("-")
    return date(int(y), int(m), int(d))


def _fmt(dt):
    return dt.strftime("%Y-%m-%d")


def _back(date_, n_days, workday):
    d = date_
    while n_days > 0:
        d = d - timedelta(days=1)
        if workday and d.weekday() >= 5:
            continue
        n_days -= 1
    return d


def _fwd(start, n_days, workday):
    d = start
    while n_days > 0:
        d = d + timedelta(days=1)
        if workday and d.weekday() >= 5:
            continue
        n_days -= 1
    return d


def _duration(n):
    return int(n.get("duration") or n.get("default_duration") or 1)


def _mode(n):
    return "WORKDAY" if n.get("calendar_mode") == "WORKDAY" else "NATURAL"


def compute_plan(etd, eta, nodes):
    """nodes: 批次节点（含 node_key/area/duration/calendar_mode）。
    返回 {node_key: (start_str, end_str)}（全量，含冻结节点亦给理论值）。"""
    etd = _parse(etd)
    eta = _parse(eta)
    if not etd or not eta:
        raise ScheduleError("缺少 ETD/ETA，无法计算计划日期")
    if eta <= etd:
        raise ScheduleError("ETA 必须晚于 ETD 至少 1 天")

    plan = {}
    dome = [n for n in nodes if n["area"] == "DOME"]
    oversea = [n for n in nodes if n["area"] == "OVERSEA"]
    sea = [n for n in nodes if n["area"] == "SEA"][0]

    plan[nt.SEA_TRANSIT] = (_fmt(etd), _fmt(eta))

    # 境内倒排
    cursor = etd
    for n in reversed(dome):
        end = cursor
        start = _back(end, _duration(n), _mode(n) == "WORKDAY")
        plan[n["node_key"]] = (_fmt(start), _fmt(end))
        cursor = start

    # 境外顺排
    cursor = eta
    for n in oversea:
        start = cursor
        end = _fwd(start, _duration(n), _mode(n) == "WORKDAY")
        plan[n["node_key"]] = (_fmt(start), _fmt(end))
        cursor = end

    return plan


def _frozen(n):
    return n.get("status") == "Done" or bool(n.get("actual_completion_date"))


def recompute_batch_schedule(project_id, batch_id=None, reason=""):
    """唯一重算入口：覆盖非冻结节点，记录变化行。
    返回 {"plan": {key:(s,e)}, "changed": [node_key...]}
    """
    batch_id = db._resolve_batch(project_id, batch_id)
    route = db.get_route(batch_id)
    if not route or not route.get("etd"):
        return {"plan": {}, "changed": []}
    nodes = db.get_nodes_by_batch(batch_id)
    if not nodes:
        return {"plan": {}, "changed": []}

    plan = compute_plan(route["etd"], route["eta"], nodes)
    changed = []

    for n in nodes:
        if _frozen(n):
            continue
        new_start, new_end = plan[n["node_key"]]
        if (n.get("plan_start"), n.get("plan_end")) != (new_start, new_end):
            from services.clock import get_now_str
            db.update_node(project_id, n["node_id"], batch_id=batch_id,
                           plan_start=new_start, plan_end=new_end)
            changed.append(n["node_key"])
            _log_recompute(project_id, batch_id, n, new_start, new_end, reason)

    # 批次状态：计划日期变化不影响当前记录是否 running
    return {"plan": plan, "changed": changed}


def _log_recompute(project_id, batch_id, n, new_start, new_end, reason):
    try:
        from services.oplog import record
        why = reason or "计划日期计算"
        record("schedule_recompute", project_id, batch_id=batch_id,
               node_key=n["node_key"], node_id=n["node_id"],
               subject=n["node_name"],
               detail=f"{why} · {n.get('plan_start') or '-'} → {new_start} / "
                      f"{n.get('plan_end') or '-'} → {new_end}")
    except Exception:
        pass


def result_summary(project_id, batch_id=None):
    """供 UI「计划日期」只读视图用：返回 node_key+日期+标记。"""
    batch_id = db._resolve_batch(project_id, batch_id)
    nodes = db.get_nodes_by_batch(batch_id)
    route = db.get_route(batch_id)
    out = []
    for n in nodes:
        out.append({
            "node_id": n["node_id"], "seq": n["seq"], "node_key": n["node_key"],
            "node_name": n["node_name"], "area": n["area"],
            "plan_start": n.get("plan_start"), "plan_end": n.get("plan_end"),
            "status": n.get("status"),
            "frozen": _frozen(n),
            "mode": n.get("calendar_mode"),
        })
    return {"batch_id": batch_id, "etd": route.get("etd") if route else None,
            "eta": route.get("eta") if route else None, "nodes": out}