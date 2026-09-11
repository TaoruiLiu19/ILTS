"""
单证清单 bootstrap + due_date 计算（按 node_key 锚定，对照《多式联运.md》§10）。
plan: {node_key: (start_str, end_str)}
"""

from config import get_country, get_port
from services.node_template import OLD_TO_KEY, by_key


def compute_file_due(file_cfg, plan):
    """计算单证截止日（§10.2：节点前 N 天 / 节点后 N 天 / 装船前 N 小时）。
    返回 (due_date, due_hours)：
      · due_rule='loading_before_hours' → 以装船节点 start 为基准，必要时回退节点前 due_hours/24 天；
        小时精度由 due_hours 承载，due_date 为对应日期（§6.6 业务日期为日粒度）。
      · 其余 → 按 due_node_key + due_type 锚定节点 start/end。
    """
    rule = file_cfg.get("due_rule")
    hours = file_cfg.get("due_hours")
    if rule == "loading_before_hours":
        from services.node_template import LOADING
        base = plan.get(LOADING)
        if base:
            base_day = base[0]
            if hours:
                # 日粒度回退：装船前 N 小时 → 至少提前 ceil(N/24) 天
                import math
                days = max(1, math.ceil(int(hours) / 24.0))
                from datetime import date, timedelta
                d0 = date(*map(int, base_day.split("-")))
                return (d0 - timedelta(days=days)).isoformat(), int(hours)
            return base_day, hours

    key = file_cfg.get("due_node_key") or file_cfg.get("node_key")
    dtype = file_cfg.get("due_type")
    if key and dtype == "node_end" and key in plan:
        return plan[key][1], hours
    if key and dtype == "node_start" and key in plan:
        return plan[key][0], hours
    return None, hours


def _node_id(key):
    n = by_key(key)
    return n["node_id"] if n else None


def bootstrap(country_code, port_code, plan):
    """展开单证清单，计算 due_date，追加港口平台备注（按旧节点序映射显示）。
    返回 files list（含 node_id 展示序 + node_key 稳定锚点 + §10.2 截止规则/基准来源）。"""
    tmpl = get_country(country_code)
    if not tmpl:
        return []

    files = []
    for f in tmpl.get("files_project", []):
        key = f.get("due_node_key")
        due, due_hours = compute_file_due(f, plan)
        files.append({
            "node_id": _node_id(key) if key else None,
            "node_key": key,
            "doc_name": f["doc_name"],
            "doc_type": f["doc_type"],
            "doc_type_key": f.get("doc_type_key"),
            "owner_dept": f.get("owner_dept"),
            "copies": f.get("copies"),
            "due_node_id": _node_id(key) if key else None,
            "due_node_key": key,
            "due_type": f.get("due_type"),
            "due_rule": f.get("due_rule"),
            "due_hours": due_hours,
            "baseline_source": f.get("baseline_source"),
            "remind_before_days": f.get("remind_before_days", 3),
            "due_date": due,
            "note": f.get("note", ""),
        })

    port = get_port(port_code)
    for f in tmpl.get("files_nodes", []):
        key = f.get("node_key")
        due, due_hours = compute_file_due(f, plan)
        note = f.get("note", "")
        # 港口平台备注按旧节点序映射展示（国内段 1-4）
        old_nid = {v: k for k, v in OLD_TO_KEY.items()}.get(key)
        if port and old_nid and old_nid in (port.get("platform_notes") or {}):
            pn = port["platform_notes"][old_nid]
            note = f"{note} | {pn}" if note else pn
        files.append({
            "node_id": _node_id(key),
            "node_key": key,
            "doc_name": f["doc_name"],
            "doc_type": f["doc_type"],
            "doc_type_key": f.get("doc_type_key"),
            "owner_dept": f.get("owner_dept"),
            "copies": f.get("copies"),
            "due_node_id": _node_id(key),
            "due_node_key": key,
            "due_type": f.get("due_type"),
            "due_rule": f.get("due_rule"),
            "due_hours": due_hours,
            "baseline_source": f.get("baseline_source"),
            "remind_before_days": f.get("remind_before_days", 3),
            "due_date": due,
            "note": note,
        })
    return files


def count_files(files):
    total = len(files)
    required = len([f for f in files if f["doc_type"] == "required"])
    pending = len([f for f in files if f.get("status", "pending") == "pending" and f["doc_type"] == "required"])
    submitted = len([f for f in files if f.get("status") == "submitted"])
    return {"total": total, "required": required, "pending": pending, "submitted": submitted}