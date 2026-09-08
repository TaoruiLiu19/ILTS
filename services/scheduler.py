"""
排程算法（模板通用：境内串行 → 海运 → 境外串行）
"""

from datetime import date, timedelta


def _parse(s):
    if isinstance(s, date):
        return s
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))


def _fmt(dt):
    return dt.strftime("%Y-%m-%d")


def generate_schedule(etd, eta, nodes_cfg):
    """
    nodes_cfg: [{id, dur, area}, ...] 按 seq 升序
    返回 {node_id: (start_str, end_str)}
    """
    etd = _parse(etd)
    eta = _parse(eta)
    if eta <= etd:
        raise ValueError("ETA 必须晚于 ETD 至少 1 天")

    plan = {}

    dome = [n for n in nodes_cfg if n["area"] == "DOME"]
    cursor = etd
    for n in reversed(dome):
        end = cursor
        start = cursor - timedelta(days=n["dur"])
        plan[n["id"]] = (_fmt(start), _fmt(end))
        cursor = start

    sea = [n for n in nodes_cfg if n["area"] == "SEA"][0]
    plan[sea["id"]] = (_fmt(etd), _fmt(eta))

    oversea = [n for n in nodes_cfg if n["area"] == "OVERSEA"]
    cursor = eta
    for n in oversea:
        start = cursor
        end = cursor + timedelta(days=n["dur"])
        plan[n["id"]] = (_fmt(start), _fmt(end))
        cursor = end

    return plan


def delay_nodes(nodes, from_node_id, delay_days):
    """
    推迟连锁：从 from_node_id 起，同区域及后续区域节点 +delay_days
    境内节点(1-4)推迟影响 ETD；境外节点(6-12)推迟影响 ETA。
    返回 (updated_nodes, new_etd, new_eta)
    """
    node_map = {n["node_id"]: n for n in nodes}
    areas_order = ["DOME", "SEA", "OVERSEA"]
    from_area = node_map[from_node_id]["area"]
    from_area_idx = areas_order.index(from_area)

    for n in nodes:
        n_idx = areas_order.index(n["area"])
        same_area_after = (n["area"] == from_area and n["seq"] >= node_map[from_node_id]["seq"])
        later_area = n_idx > from_area_idx
        if same_area_after or later_area:
            start = _parse(n["plan_start"]) + timedelta(days=delay_days)
            end = _parse(n["plan_end"]) + timedelta(days=delay_days)
            n["plan_start"] = _fmt(start)
            n["plan_end"] = _fmt(end)
            n["is_delayed"] = 1
            n["delay_days"] += delay_days

    sea_node = next(n for n in nodes if n["area"] == "SEA")
    new_etd = sea_node["plan_start"]
    new_eta = sea_node["plan_end"]
    return nodes, new_etd, new_eta
