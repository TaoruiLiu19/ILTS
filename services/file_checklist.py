"""
单证清单 bootstrap + due_date 计算
"""

from config import get_country, get_port


def compute_file_due(file_cfg, plan):
    """
    plan: {node_id: (start_str, end_str)}
    返回 due_date 字符串或 None（项目级周期型无固定 due）
    """
    nid = file_cfg.get("due_node_id")
    dtype = file_cfg.get("due_type")
    if nid and dtype == "node_end" and nid in plan:
        return plan[nid][1]
    if nid and dtype == "node_start" and nid in plan:
        return plan[nid][0]
    return None


def bootstrap(country_code, port_code, plan):
    """
    按模板展开单证清单，计算 due_date，追加港口平台备注。
    返回 files list，每项含 node_id/doc_name/doc_type/owner_dept/copies/
           due_node_id/due_type/remind_before_days/due_date/note
    """
    tmpl = get_country(country_code)
    if not tmpl:
        return []

    files = []

    for f in tmpl.get("files_project", []):
        due = compute_file_due(f, plan)
        files.append({
            "node_id": None,
            "doc_name": f["doc_name"],
            "doc_type": f["doc_type"],
            "owner_dept": f.get("owner_dept"),
            "copies": f.get("copies"),
            "due_node_id": f.get("due_node_id"),
            "due_type": f.get("due_type"),
            "remind_before_days": f.get("remind_before_days", 3),
            "due_date": due,
            "note": f.get("note", ""),
        })

    port = get_port(port_code)

    for f in tmpl.get("files_nodes", []):
        nid = f.get("node_id")
        file_cfg = {**f, "due_node_id": nid}
        due = compute_file_due(file_cfg, plan)
        note = f.get("note", "")
        if port and nid in port.get("platform_notes", {}):
            platform_note = port["platform_notes"][nid]
            note = f"{note} | {platform_note}" if note else platform_note
        files.append({
            "node_id": nid,
            "doc_name": f["doc_name"],
            "doc_type": f["doc_type"],
            "owner_dept": f.get("owner_dept"),
            "copies": f.get("copies"),
            "due_node_id": nid,
            "due_type": f.get("due_type"),
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
