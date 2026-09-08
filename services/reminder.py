"""
提醒引擎：全量重算 + 缓冲策略 + 分组 + 去重
"""

from datetime import date, timedelta

from services.node_status import compute_node_status


def _parse(s):
    if isinstance(s, date):
        return s
    if not s:
        return None
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))


def compute_reminders(project, nodes, files, today=None):
    """
    返回 {level: [reminder_dict, ...]}
    level: 'P0' / 'P1' / 'P2'
    """
    if today is None:
        from services.clock import get_today
        today = get_today()
    elif isinstance(today, str):
        today = _parse(today)

    reminders = {"P0": [], "P1": [], "P2": []}
    proj_name = project["project_name"]
    buffer_days = project.get("buffer_days", 4)

    for n in nodes:
        st = compute_node_status(n, today)
        plan_end = _parse(n.get("plan_end"))
        plan_start = _parse(n.get("plan_start"))

        if st == "Overdue":
            overdue_days = (today - plan_end).days
            msg = f"节点⑨{n['node_name']} 已逾期 {overdue_days} 天"
            if n["node_id"] in (9, 10) and overdue_days > 0:
                msg += f"（已消耗缓冲 {min(overdue_days, buffer_days)} 天，预留 {buffer_days} 天）"
            reminders["P0"].append({"project": proj_name, "type": "NODE_OVERDUE", "msg": msg,
                                     "node_id": n["node_id"]})

        elif st == "Active" and plan_end == today:
            reminders["P1"].append({"project": proj_name, "type": "NODE_END_TODAY",
                                    "msg": f"节点⑨{n['node_name']} 今日截止", "node_id": n["node_id"]})

        elif st == "Active" and plan_start == today:
            reminders["P2"].append({"project": proj_name, "type": "NODE_START_TODAY",
                                   "msg": f"节点⑨{n['node_name']} 今日启动", "node_id": n["node_id"]})

    for f in files:
        if f.get("status") == "submitted":
            continue
        if f["doc_type"] != "required":
            continue

        due = _parse(f.get("due_date"))
        nid = f.get("node_id")
        remind_days = f.get("remind_before_days", 3)

        node_obj = next((n for n in nodes if n["node_id"] == nid), None)
        node_st = compute_node_status(node_obj, today) if node_obj else None

        if due and today > due:
            late_days = (today - due).days
            reminders["P0"].append({"project": proj_name, "type": "FILE_LATE",
                                    "msg": f"单证《{f['doc_name']}》超建议提交日 {late_days} 天",
                                    "file_id": f.get("file_id")})

        elif node_st in ("Active", "Overdue") and f.get("status", "pending") == "pending":
            reminders["P0"].append({"project": proj_name, "type": "FILE_MISSING_IN_ACTIVE",
                                    "msg": f"必备单证《{f['doc_name']}》未提交（节点进行中）",
                                    "file_id": f.get("file_id")})

        elif due and today >= due - timedelta(days=remind_days) and today <= due:
            reminders["P1"].append({"project": proj_name, "type": "FILE_MISSING_LATE",
                                    "msg": f"必备单证《{f['doc_name']}》临近提交日（{due.strftime('%m-%d')}）",
                                    "file_id": f.get("file_id")})

    return reminders


def format_reminders(all_reminders, today=None):
    """格式化今日待办弹窗文本"""
    if today is None:
        from services.clock import get_today
        today = get_today()
    elif isinstance(today, str):
        today = _parse(today)

    lines = [f"📋 今日待办 · {today.strftime('%Y-%m-%d')}", ""]

    p0 = all_reminders.get("P0", [])
    p1 = all_reminders.get("P1", [])
    p2 = all_reminders.get("P2", [])

    if not p0 and not p1 and not p2:
        lines.append("✅ 今日暂无待办，一切正常。")
        return "\n".join(lines)

    if p0:
        lines.append("【需处理】")
        for r in p0:
            lines.append(f" · {r['project']}：{r['msg']}")
        lines.append("")

    if p1:
        lines.append("【进行中提醒】")
        for r in p1:
            lines.append(f" · {r['project']}：{r['msg']}")
        lines.append("")

    if p2:
        lines.append("【今日启动】")
        for r in p2:
            lines.append(f" · {r['project']}：{r['msg']}")

    return "\n".join(lines)


def count_total(reminders):
    return len(reminders.get("P0", [])) + len(reminders.get("P1", [])) + len(reminders.get("P2", []))
