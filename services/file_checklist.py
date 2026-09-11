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


def is_project_level(cfg):
    """判断国家模板里的一条单证是否为**项目级**（不随批次各存一份）。

    判据：完全没有节点锚点（`due_node_key` / `due_type` / `due_rule` 都为空）。
    巴西模板里只有《项目日报》《物流动态跟踪表》《项目进度报告》是这种；
    《批次实施计划》《人员安排计划》虽然写在 `files_project`，但挂了 EXPORT_CUSTOMS 锚点，
    到期日按每个批次自己的报关节点算 → 仍然属于批次级。
    """
    return (not cfg.get("due_node_key")) and (not cfg.get("due_type")) \
        and (not cfg.get("due_rule")) and (not cfg.get("node_key"))


def bootstrap_project(country_code):
    """**项目级**单证清单（每个项目一份，不随批次复制）。

    返回结构与 `bootstrap()` 一致，便于 UI 把两类单证拼在同一个列表里渲染；
    但 `node_id/node_key/due_*` 一律为 None（没有节点锚点，也就没有到期日）。
    """
    tmpl = get_country(country_code)
    if not tmpl:
        return []
    out = []
    for f in tmpl.get("files_project", []):
        if not is_project_level(f):
            continue
        out.append({
            "node_id": None, "node_key": None,
            "doc_name": f["doc_name"], "doc_type": f["doc_type"],
            "doc_type_key": f.get("doc_type_key"), "owner_dept": f.get("owner_dept"),
            "copies": f.get("copies"),
            "due_node_id": None, "due_node_key": None, "due_type": None,
            "due_rule": None, "due_hours": None, "baseline_source": None,
            "remind_before_days": f.get("remind_before_days", 3),
            "due_date": None, "note": f.get("note", ""),
        })
    return out


def seed(project_id, country_code, port_code, plan, batch_id=None):
    """一次性写入「批次级 + 项目级」单证清单（生产路径的唯一入口）。

    · 批次级（票货单证，含节点锚点）→ `files` 表，按批次各一份；
    · 项目级（项目日报/进度报告等）→ `project_files` 表，同项目只一份（幂等）。

    返回 {"batch": n, "project": m} 便于调用方打印/断言。
    """
    import db
    batch_files = bootstrap(country_code, port_code, plan)
    if batch_files:
        db.insert_files(project_id, batch_files, batch_id)
    proj_files = bootstrap_project(country_code)
    if proj_files:
        db.insert_project_files(project_id, proj_files)
    return {"batch": len(batch_files), "project": len(proj_files)}


def bootstrap(country_code, port_code, plan):
    """展开**批次级**单证清单，计算 due_date，追加港口平台备注（按旧节点序映射显示）。

    项目级条目（无节点锚点）不在这里返回 —— 见 `bootstrap_project()`。
    返回 files list（含 node_id 展示序 + node_key 稳定锚点 + §10.2 截止规则/基准来源）。
    """
    tmpl = get_country(country_code)
    if not tmpl:
        return []

    files = []
    for f in tmpl.get("files_project", []):
        if is_project_level(f):
            continue                     # 项目级 → 走 bootstrap_project()，不按批次复制
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


def merge_scope(batch_files, project_files):
    """把「批次级 + 项目级」单证合成一个列表供 UI 渲染（项目级行打上 scope='project'）。

    项目级行的 `file_id` 已是 `pf-<n>` 形式（见 db.get_project_files），
    与 `files.file_id`（整数）不会撞车，故可安全混在一个列表里。
    """
    out = []
    for f in batch_files or []:
        row = dict(f)
        row.setdefault("scope", "batch")
        out.append(row)
    for f in project_files or []:
        row = dict(f)
        row["scope"] = "project"
        out.append(row)
    return out


def pending_required(batch_files, project_files=None):
    """**缺单证口径**（全局唯一）：批次级未提交必填 + 项目级未提交必填。

    项目级单证在同项目内只有一份，故不会因为批次变多而重复计数；
    卡片、工作台字牌、顶部统计都用这一个口径，避免三处各算一套。
    """
    n = count_files(batch_files or [])["pending"]
    if project_files:
        n += count_files(project_files)["pending"]
    return n


def submitted_required(batch_files, project_files=None):
    """**已交口径**（与 pending_required 配套）：批次级已交必填 + 项目级已交必填。"""
    a = count_files(batch_files or [])
    n = a["required"] - a["pending"]
    if project_files:
        b = count_files(project_files)
        n += b["required"] - b["pending"]
    return n