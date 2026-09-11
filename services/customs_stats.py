"""报关要素统计（《多式联运.md》§14 T30：报关行/报关方式可录并可筛选/统计）。

数据落在 `batch_routes.customs_broker` / `batch_routes.customs_mode`
（批次管理对话框已可录入，见 `ui/batch_dialogs.py:158-165`、`ui/batch_dialogs.py:450-451`）。
本模块只做**只读聚合**，供报告层（`services/reporting.py` / `ui/pages/report_page.py`）
与筛选控件调用，不写库、不改动那两个文件。

对外 API（报告层接线用）：
    customs_broker_options(include_all=True) -> [{"value", "label", "count"}]
    customs_mode_options(include_all=True)   -> 同上
    customs_mode_stats(batch_ids=None, include_cancelled=False) -> {
        "total_batches", "with_customs", "missing_customs",
        "by_mode": [{value,label,count,running,completed,cancelled}],
        "by_broker": [...],
        "by_batch": [{batch_id, batch_no, batch_name, project_id, project_name,
                      status, customs_broker, customs_mode}],
        "matrix": [{broker, mode, count}],
        "options": {"broker": [...], "mode": [...]},
    }
    batch_ids_by_customs(broker=None, mode=None, project_id=None, status=None,
                         include_cancelled=False) -> [batch_id, ...]
    customs_filter_label(broker=None, mode=None) -> str
"""

import db

MODE_UNSET_LABEL = "（未填）"
BROKER_UNSET_LABEL = "（未填）"

# 批次状态分组（与 services/batches.py 的口径一致）
ACTIVE_STATES = ("draft", "ready", "running")
DONE_STATES = ("completed", "closed")


def _norm(v):
    v = (v or "").strip() if isinstance(v, str) else v
    return v or None


def _batch_rows(include_cancelled=False, project_id=None):
    """取批次 + 线路 + 项目名的一行式视图。"""
    conn = db.get_conn()
    sql = ("SELECT b.batch_id, b.batch_no, b.batch_name, b.project_id, b.status, "
           "b.planned_date, r.customs_broker, r.customs_mode, p.project_name "
           "FROM batches b "
           "LEFT JOIN batch_routes r ON r.batch_id = b.batch_id "
           "LEFT JOIN projects p ON p.project_id = b.project_id WHERE 1=1")
    args = []
    if not include_cancelled:
        sql += " AND b.status != 'cancelled'"
    if project_id:
        sql += " AND b.project_id = ?"
        args.append(project_id)
    sql += " ORDER BY b.project_id, b.created_at"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _selected_rows(batch_ids=None, include_cancelled=False, project_id=None):
    rows = _batch_rows(include_cancelled=include_cancelled, project_id=project_id)
    if batch_ids is None:
        return rows
    want = set(batch_ids)
    return [r for r in rows if r["batch_id"] in want]


def _options(rows, field, unset_label):
    """按出现次数降序的选项列表。"""
    counts = {}
    for r in rows:
        v = _norm(r.get(field))
        counts[v] = counts.get(v, 0) + 1
    items = [{"value": v, "label": (v if v else unset_label), "count": c}
             for v, c in counts.items()]
    # 已填值在前（按 count 降序、名称升序），未填兜底最后
    items.sort(key=lambda x: (x["value"] is None, -x["count"], x["label"]))
    return items


def _group_stats(rows, field, unset_label):
    buckets = {}
    for r in rows:
        v = _norm(r.get(field))
        b = buckets.setdefault(v, {"value": v, "label": v or unset_label,
                                   "count": 0, "running": 0,
                                   "completed": 0, "cancelled": 0,
                                   "batch_ids": []})
        b["count"] += 1
        b["batch_ids"].append(r["batch_id"])
        st = r.get("status")
        if st in ACTIVE_STATES:
            b["running"] += 1
        elif st in DONE_STATES:
            b["completed"] += 1
        elif st == "cancelled":
            b["cancelled"] += 1
    out = sorted(buckets.values(),
                 key=lambda x: (x["value"] is None, -x["count"], x["label"]))
    return out


def customs_broker_options(include_all=True, project_id=None,
                           include_unset=True, include_cancelled=False):
    """报关行选项（去重 + 出现次数），供报告筛选下拉。

    include_all=True 时首项为 `{"value": None, "label": "全部报关行"}`。
    """
    rows = _batch_rows(include_cancelled=include_cancelled, project_id=project_id)
    items = _options(rows, "customs_broker", BROKER_UNSET_LABEL)
    if not include_unset:
        items = [i for i in items if i["value"] is not None]
    if include_all:
        items = [{"value": None, "label": "全部报关行",
                  "count": len(rows)}] + items
    return items


def customs_mode_options(include_all=True, project_id=None,
                         include_unset=True, include_cancelled=False):
    """报关方式选项（去重 + 出现次数），供报告筛选下拉。"""
    rows = _batch_rows(include_cancelled=include_cancelled, project_id=project_id)
    items = _options(rows, "customs_mode", MODE_UNSET_LABEL)
    if not include_unset:
        items = [i for i in items if i["value"] is not None]
    if include_all:
        items = [{"value": None, "label": "全部报关方式",
                  "count": len(rows)}] + items
    return items


def customs_mode_stats(batch_ids=None, include_cancelled=False, project_id=None):
    """报关行/报关方式统计（T30「可筛选/统计」的统计口径）。

    batch_ids=None → 全部批次。取消批次默认排除（§11 取消批次全隐藏）。
    """
    rows = _selected_rows(batch_ids, include_cancelled=include_cancelled,
                          project_id=project_id)
    total = len(rows)
    with_c = len([r for r in rows if _norm(r.get("customs_mode"))])
    with_b = len([r for r in rows if _norm(r.get("customs_broker"))])

    matrix = {}
    for r in rows:
        b = _norm(r.get("customs_broker"))
        m = _norm(r.get("customs_mode"))
        matrix[(b, m)] = matrix.get((b, m), 0) + 1

    by_batch = [{
        "batch_id": r["batch_id"], "batch_no": r["batch_no"],
        "batch_name": r.get("batch_name"), "project_id": r["project_id"],
        "project_name": r.get("project_name"), "status": r["status"],
        "customs_broker": _norm(r.get("customs_broker")),
        "customs_mode": _norm(r.get("customs_mode")),
        "planned_date": r.get("planned_date"),
    } for r in rows]

    return {
        "total_batches": total,
        "with_customs_broker": with_b,
        "with_customs_mode": with_c,
        "with_customs": len([r for r in rows
                             if _norm(r.get("customs_broker"))
                             or _norm(r.get("customs_mode"))]),
        "missing_customs": len([r for r in rows
                                if not _norm(r.get("customs_broker"))
                                and not _norm(r.get("customs_mode"))]),
        "by_mode": _group_stats(rows, "customs_mode", MODE_UNSET_LABEL),
        "by_broker": _group_stats(rows, "customs_broker", BROKER_UNSET_LABEL),
        "by_batch": by_batch,
        "matrix": [{"broker": b, "broker_label": b or BROKER_UNSET_LABEL,
                    "mode": m, "mode_label": m or MODE_UNSET_LABEL, "count": c}
                   for (b, m), c in sorted(matrix.items(),
                                           key=lambda kv: (-kv[1],
                                                           kv[0][0] or "",
                                                           kv[0][1] or ""))],
        "options": {
            "broker": _options(rows, "customs_broker", BROKER_UNSET_LABEL),
            "mode": _options(rows, "customs_mode", MODE_UNSET_LABEL),
        },
    }


def batch_ids_by_customs(broker=None, mode=None, project_id=None, status=None,
                         include_cancelled=False):
    """筛选：按报关行/报关方式（可组合）返回 batch_id 列表。

    broker/mode 传 None 表示「全部」；传 "" 表示「未填」。
    status 可传状态字符串或状态列表。
    """
    rows = _batch_rows(include_cancelled=include_cancelled, project_id=project_id)
    want_status = None
    if status is not None:
        want_status = {status} if isinstance(status, str) else set(status)
    out = []
    for r in rows:
        if broker is not None and (_norm(r.get("customs_broker")) or "") != broker:
            continue
        if mode is not None and (_norm(r.get("customs_mode")) or "") != mode:
            continue
        if want_status is not None and r["status"] not in want_status:
            continue
        out.append(r["batch_id"])
    return out


def customs_of_batch(batch_id):
    """单批次报关要素（报告标题/明细列用）。"""
    route = db.get_route(batch_id) or {}
    return {"batch_id": batch_id,
            "customs_broker": _norm(route.get("customs_broker")),
            "customs_mode": _norm(route.get("customs_mode")),
            "release_mode": _norm(route.get("release_mode"))}


def customs_filter_label(broker=None, mode=None):
    """报告筛选条件文案；未筛选返回 ''。"""
    parts = []
    if broker:
        parts.append(f"报关行：{broker}")
    if mode:
        parts.append(f"报关方式：{mode}")
    return " · ".join(parts)
