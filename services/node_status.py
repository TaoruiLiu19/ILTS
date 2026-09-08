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


def sync_doc_completion(project_id):
    """
    自动完成规则（演示版无「人工确认完成」入口）：
      某节点「必填单证全部提交」且「已过 plan_end（今日 > plan_end）」→ 视为已完成 Done，
      逾期/红条/统计随之消失；必填单证被撤勾时回退为 Pending。
    """
    import db
    from services.clock import get_today_str
    today_s = get_today_str()
    nodes = db.get_nodes(project_id)
    files = db.get_files(project_id)
    changed = 0
    for n in nodes:
        nid = n["node_id"]
        req = [f for f in files
               if f.get("node_id") == nid and f.get("doc_type") == "required"]
        if not req:                     # 该节点无必填单证 → 不参与自动完成
            continue
        all_sub = all(f.get("status") == "submitted" for f in req)
        past = bool(n.get("plan_end")) and n["plan_end"] < today_s
        if n.get("status") != "Done" and past and all_sub:
            db.update_node(project_id, nid, status="Done",
                           actual_completion_date=today_s)
            changed += 1
        elif n.get("status") == "Done" and not all_sub:
            db.update_node(project_id, nid, status="Pending",
                           actual_completion_date=None)
            changed += 1
    return changed


def sync_active_projects():
    """对所有进行中项目执行自动完成同步（轻量，供刷新/待办前调用）"""
    import db
    total = 0
    for p in db.get_projects_by_status("Active"):
        total += sync_doc_completion(p["project_id"])
    return total
