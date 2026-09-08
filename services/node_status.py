"""
节点状态机：依据 plan_start/plan_end/status/today 计算实时状态。
"""

from datetime import date


def _parse(s):
    if isinstance(s, date):
        return s
    if not s:
        return None
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))


def compute_node_status(node, today=None):
    from services.clock import get_today
    if today is None:
        today = get_today()
    else:
        today = _parse(today) if isinstance(today, str) else today

    if node.get("status") == "Done":
        return "Done"

    plan_end = _parse(node.get("plan_end"))
    plan_start = _parse(node.get("plan_start"))

    if plan_end and today > plan_end:
        return "Overdue"
    if plan_start and plan_end and plan_start <= today <= plan_end:
        return "Active"
    return "Pending"


def compute_all_status(nodes, today=None):
    """返回 {node_id: status_str}"""
    return {n["node_id"]: compute_node_status(n, today) for n in nodes}


def get_current_node(nodes, today=None):
    """返回当前 Active 节点，若无则返回第一个 Pending"""
    from services.clock import get_today
    if today is None:
        today = get_today()
    for n in sorted(nodes, key=lambda x: x["seq"]):
        st = compute_node_status(n, today)
        if st == "Active":
            return n
    for n in sorted(nodes, key=lambda x: x["seq"]):
        st = compute_node_status(n, today)
        if st == "Pending":
            return n
    return None
