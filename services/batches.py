"""
批次状态机 / 项目状态推导 / actual_* 推导 / 单证前置校验（《多式联运.md》§8、§6.10、§10.5）。
"""

import db
from services import node_template as nt


# ── 批次状态推导 ──

def batch_has_running_start(batch_id):
    """running 最小触发集：任一节点 Active 或任一单证已提交。"""
    nodes = db.get_nodes_by_batch(batch_id)
    files = db.get_files_by_batch(batch_id)
    if any(n.get("status") == "Active" for n in nodes):
        return True
    if any(f.get("status") == "submitted" for f in files):
        return True
    return False


def batch_ready(batch_id):
    """ready：已选线路（有 route.etd/eta）且资料就绪但未开始执行。"""
    route = db.get_route(batch_id)
    return bool(route and route.get("etd") and route.get("eta"))


def batch_boxes_returned(batch_id):
    """重箱已装 / 空箱已还：以 EMPTY_RETURN 节点完成或 actual empty_returned_at 判定。"""
    node = db.node_by_key(batch_id, nt.EMPTY_RETURN)
    if node and node.get("status") == "Done":
        return True
    b = db.get_batch(batch_id)
    return bool(b and b.get("empty_returned_at"))


def batch_complete_candidate(batch_id):
    """completed 建议条件：关键节点全 Done + 必填单证齐 + 箱已还。提示人工确认。"""
    key_nodes = db.get_nodes_by_batch(batch_id)
    for n in key_nodes:
        if n.get("is_key_node") and n.get("status") != "Done":
            return None, "关键节点尚未全部完成"
    files = db.get_files_by_batch(batch_id)
    req = [f for f in files if f.get("doc_type") == "required" and f.get("status") != "submitted"]
    if req:
        return None, f"尚有 {len(req)} 份必填单证未提交"
    if not batch_boxes_returned(batch_id):
        return None, "重箱/空箱尚未归还"
    return True, None


def derive_batch_status(batch_id, announce=False):
    """由当前数据推导 recommended status（不强制更写，供 UI 展示与该批次日志）。"""
    b = db.get_batch(batch_id)
    if not b:
        return None
    cur = b["status"]
    if cur == "cancelled":
        return cur
    if cur == "completed" or cur == "closed":
        return cur
    ok, reason = batch_complete_candidate(batch_id)
    if ok:
        return "completed"
    if batch_has_running_start(batch_id):
        return "running"
    if batch_ready(batch_id):
        return "ready"
    return "draft"


# ── §8 状态推进（自动推导 + 人工确认） ──

# 自动可写入的状态：completed 必须人工确认，故不在自动集合内
AUTO_STATES = ("draft", "ready", "running")


class BatchStateError(RuntimeError):
    """批次状态迁移非法。"""


def sync_batch_status(batch_id):
    """把批次推进到「自动可判定」的状态（draft/ready/running）。

    §8 要点：
      · `running` 最小触发集 = 任一节点 Active 或任一单证 submitted（仅录草稿不触发）。
      · `completed` **不自动写入**，只返回建议，必须走 confirm_complete() 人工确认。
    返回 dict：{status, changed, suggest_complete, reason}
    """
    b = db.get_batch(batch_id)
    if not b:
        return {"status": None, "changed": False, "suggest_complete": False, "reason": None}
    if b["status"] in ("cancelled", "closed", "completed"):
        return {"status": b["status"], "changed": False,
                "suggest_complete": False, "reason": None}

    ok, reason = batch_complete_candidate(batch_id)
    target = b["status"]
    if ok:
        target = "completed"          # 仅作为建议
    elif batch_has_running_start(batch_id):
        target = "running"
    elif batch_ready(batch_id):
        target = "ready"
    else:
        target = "draft"

    if target == "completed":
        return {"status": b["status"], "changed": False,
                "suggest_complete": True, "reason": None}
    if target != b["status"]:
        db.update_batch(batch_id, status=target)
        return {"status": target, "changed": True,
                "suggest_complete": False, "reason": reason}
    return {"status": b["status"], "changed": False, "suggest_complete": False, "reason": reason}


def advance_on_node_or_file(batch_id):
    """节点/单证变更后的状态推进钩子（running 最小触发集接线点）。"""
    return sync_batch_status(batch_id)


def confirm_complete(batch_id, operator_note=""):
    """人工确认完成（§8：completed 需人工确认）。"""
    ok, reason = batch_complete_candidate(batch_id)
    if not ok:
        return False, reason
    db.update_batch(batch_id, status="completed")
    b = db.get_batch(batch_id)
    try:
        from services.oplog import record
        record("batch_complete_confirm", b["project_id"], batch_id=batch_id,
               subject=b.get("batch_no") or "批次", scope="batch",
               detail=operator_note or "人工确认批次完成")
    except Exception:
        pass
    update_project_status(b["project_id"])
    return True, None


# 兼容旧名（§10.5）：语义已改为「人工确认」，不再静默落库
def suggest_complete(batch_id, operator_note=""):
    """达到完成条件时由操作员确认。返回 (True, None) 或 (False, 原因)。"""
    return confirm_complete(batch_id, operator_note=operator_note)


def close_batch(batch_id):
    """§8：仅 completed 可关闭；本期不产生归档文件。"""
    b = db.get_batch(batch_id)
    if not b:
        raise BatchStateError("批次不存在")
    if b["status"] != "completed":
        raise BatchStateError(
            f"仅「待确认完成」的批次可关闭，当前状态：{state_label(b['status'])}")
    db.update_batch(batch_id, status="closed", closed_at=db._now())
    try:
        from services.oplog import record
        record("batch_close", b["project_id"], batch_id=batch_id,
               subject=b.get("batch_no") or "批次", scope="batch", detail="关闭批次")
    except Exception:
        pass
    update_project_status(b["project_id"])
    return True


def cancel_batch(batch_id, reason=""):
    """§8/D14：取消批次（全模块默认隐藏，保留审计）。

    记 previous_status 以支持恢复（取消→恢复迁移表）。
    """
    b = db.get_batch(batch_id)
    if not b:
        raise BatchStateError("批次不存在")
    if b["status"] == "cancelled":
        return False
    db.update_batch(batch_id, status="cancelled",
                    previous_status=b["status"], cancel_reason=reason or "")
    try:
        from services.oplog import record
        record("batch_cancel", b["project_id"], batch_id=batch_id,
               subject=b.get("batch_no") or "批次", scope="batch",
               detail=f"取消批次（原状态 {state_label(b['status'])}）"
                      + (f" · 原因：{reason}" if reason else ""))
    except Exception:
        pass
    _retarget_current_batch(b["project_id"], exclude=batch_id)
    update_project_status(b["project_id"])
    return True


def restore_batch(batch_id, reason=""):
    """§8：恢复至取消前状态（迁移表 draft/ready/running/completed → 原状态）。

    从 `completed` 恢复时回到 `running`，**重新触发一次「人工确认完成」**，
    避免隐式复归（§8 取消→恢复迁移表附注）。
    """
    b = db.get_batch(batch_id)
    if not b:
        raise BatchStateError("批次不存在")
    if b["status"] != "cancelled":
        raise BatchStateError("该批次未处于已取消状态")
    prev = b.get("previous_status") or "draft"
    if prev not in ("draft", "ready", "running", "completed", "closed"):
        prev = "draft"
    re_confirm = prev in ("completed", "closed")
    target = "running" if re_confirm else prev
    db.update_batch(batch_id, status=target, previous_status=None, cancel_reason=None)
    try:
        from services.oplog import record
        record("batch_restore", b["project_id"], batch_id=batch_id,
               subject=b.get("batch_no") or "批次", scope="batch",
               detail=f"恢复批次 → {state_label(target)}"
                      + ("（原为待确认完成，需重新人工确认完成）" if re_confirm else "")
                      + (f" · 原因：{reason}" if reason else ""))
    except Exception:
        pass
    update_project_status(b["project_id"])
    return {"status": target, "re_confirm_required": re_confirm}


def _retarget_current_batch(project_id, exclude=None):
    """当前批次被取消后，把 current_batch_id 改指向另一个启用批次。"""
    batches = db.get_batches(project_id, include_cancelled=False)
    nxt = next((x for x in batches if x["batch_id"] != exclude), None)
    if nxt:
        db.update_project(project_id, current_batch_id=nxt["batch_id"])


def update_project_status(project_id):
    """三态推导（D30）：
    Active（存在 draft/ready/running 批次）、Completed（全部 closed 且至少一个）、
    Cancelled（全部 cancelled）。
    混合 closed + cancelled（无启用批次）时按 Completed 处理（项目已完成过工作，
    进「已完成」页），并在报告中标注存在取消批次。
    """
    batches = db.get_batches(project_id, include_cancelled=True)
    if not batches:
        return None
    active_states = {"draft", "ready", "running"}
    cancelled = [b for b in batches if b["status"] == "cancelled"]
    closed = [b for b in batches if b["status"] == "closed"]
    remaining = [b for b in batches if b["status"] in active_states]

    if remaining:
        new = "Active"
    elif cancelled and not closed:
        new = "Cancelled"          # 全部批次均已取消
    else:
        new = "Completed"          # 全部 closed，或 closed + cancelled 混合
    db.update_project(project_id, status=new)
    return new


# ── actual_* 自动推导（§6.10 D31） ──

def derive_actuals(batch_id, project_id, overwrite=False):
    """由节点实际完成日自动回填批次 actual_*（不覆盖手工值）。返回更新项。"""
    b = db.get_batch(batch_id)
    if not b:
        return []
    updates = {}
    fallback = {nt.ARRIVAL_NOTICE: nt.D_O_COLLECT}
    for field, key in nt.ACTUAL_SOURCE.items():
        node = db.node_by_key(batch_id, key)
        val = node.get("actual_completion_date") if node else None
        if not val and key in fallback:
            fb = db.node_by_key(batch_id, fallback[key])
            val = fb.get("actual_completion_date") if fb else None
        if val and (overwrite or not b.get(field)):
            updates[field] = val
    if updates:
        db.update_batch(batch_id, **updates)
        return list(updates)
    return []


def actual_eta(batch_id):
    node = db.node_by_key(batch_id, nt.ARRIVAL_NOTICE)
    if node and node.get("actual_completion_date"):
        return node["actual_completion_date"]
    fb = db.node_by_key(batch_id, nt.D_O_COLLECT)
    return fb.get("actual_completion_date") if fb else None


def on_node_completed(batch_id, node_id):
    """节点完成后的联动（§6.10 D31）：自动回填 actual_*，还空箱同时写 containers.returned_at。
    仅在字段为空时回填（手工覆盖优先，不被自动值冲掉）。"""
    node = db.get_conn().execute(
        "SELECT * FROM nodes WHERE batch_id=? AND node_id=?", (batch_id, node_id)).fetchone()
    if not node:
        return []
    key = node["node_key"]
    if key == nt.EMPTY_RETURN:
        _sync_containers_returned(batch_id,
                                  node["actual_completion_date"] or db.today_str())
    b = db.get_batch(batch_id)
    if not b:
        return []
    return derive_actuals(batch_id, b["project_id"], overwrite=False)


def _sync_containers_returned(batch_id, when):
    """还空箱节点完成 → 回填所有未填 returned_at 的柜（§3.4/§6.10）。"""
    if not when:
        return
    for c in db.get_containers(batch_id):
        if not c.get("returned_at"):
            db.update_container(c["container_id"], returned_at=when)


ACTUAL_FIELDS = ("actual_etd", "actual_eta", "actual_delivery", "empty_returned_at")


def override_actual(batch_id, field, value, reason=""):
    """手工覆盖 actual_*（可覆盖自动推导值）。必填原因并写 op_log（§6.10/D31）。"""
    if field not in ACTUAL_FIELDS:
        raise ValueError(f"未知实际值字段：{field}")
    if not reason:
        raise ValueError("手工覆盖实际值必须填写原因（§6.10）")
    db.assert_batch_writable(batch_id)          # §8：取消批次禁止任何写入
    b = db.get_batch(batch_id)
    if not b:
        raise ValueError("批次不存在")
    old = b.get(field)
    db.update_batch(batch_id, **{field: value})
    try:
        from services.oplog import record
        record("batch_actual_override", b["project_id"], batch_id=batch_id,
               subject=b.get("batch_no") or "批次", scope="batch",
               detail=f"{field}: {old or '（空）'} → {value or '（清空）'} · 原因：{reason}")
    except Exception:
        pass
    return {"field": field, "old": old, "new": value, "reason": reason}


# ── 单证前置校验（§10.5 D32） ──

# 单证名关键词 → 必填客户角色
DOC_ROLE_RULES = {
    ('出口报关单', '进口证', '进口税费', '非自动进口许可证', '许可证'):
        {'SHIPPER', 'IMPORTER'},
    ('提单', '海运提单', '到货通知', 'D/O', '提货单', '换单'):
        {'SHIPPER', 'CONSIGNEE'},
}


def missing_roles(batch_id, doc):
    """返回该单证因缺客户角色而不可提交的角色集合（空 = 可提交）。"""
    name = doc
    for keywords, roles in DOC_ROLE_RULES.items():
        if any(k in name for k in keywords):
            present = {p["role"] for p in db.get_batch_parties(batch_id) if p["role"] != "NOTIFY"}
            missing = roles - present
            if missing:
                return missing
            break
    return set()


def importer_tax_missing(batch_id, country_code):
    """目的国要求税号（BR=CNPJ/CPF）而 IMPORTER 缺 tax_id → 返回 True。"""
    from config import get_country
    tmpl = get_country(country_code)
    if not tmpl.get("importer_tax_id_required"):
        return False
    importers = db.get_batch_parties(batch_id, "IMPORTER")
    if not importers:
        return True
    return not any(p.get("tax_id") for p in importers if p.get("tax_id"))


def state_label(status):
    return {"draft": "草稿", "ready": "已就绪", "running": "进行中",
            "completed": "待确认完成", "closed": "已完成", "cancelled": "已取消"}.get(status, status)


# ── §8 换线（线路只读后走变更记录） ──

# 线路相关字段（写入 batch_route_changes 快照）
_ROUTE_SNAPSHOT_FIELDS = ("mode_primary", "mode_chain", "country", "template_key",
                          "export_port", "etd", "eta", "customs_broker", "customs_mode",
                          "release_mode", "container_pickup_location",
                          "empty_return_location", "free_demurrage_until",
                          "free_detention_until")


def route_locked(batch_id):
    """§8/D7：`running` 之后线路只读（只能走 change_route 登记变更）。"""
    b = db.get_batch(batch_id)
    return bool(b and b["status"] in ("running", "completed", "closed"))


def _node_fingerprint(batch_id):
    return [{"node_key": n["node_key"], "node_name": n["node_name"], "seq": n["seq"]}
            for n in db.get_nodes_by_batch(batch_id)]


def route_impact(batch_id, new_route):
    """生成换线影响清单（§8）：新增/删除/改名/顺序变化 + ETD 变化时的复核提示。"""
    old = db.get_route(batch_id) or {}
    impact = {"added": [], "removed": [], "renamed": [], "reordered": [],
              "etd_changed": False, "eta_changed": False, "fields": {}}

    for f in _ROUTE_SNAPSHOT_FIELDS:
        if f in new_route and (new_route[f] or None) != (old.get(f) or None):
            impact["fields"][f] = {"old": old.get(f), "new": new_route[f]}

    impact["etd_changed"] = impact["fields"].get("etd") is not None
    impact["eta_changed"] = impact["fields"].get("eta") is not None

    # 节点模板差异（一期模板固定，通常为空；换目的国模板时才会出现）
    old_nodes = _node_fingerprint(batch_id)
    new_tmpl = (new_route.get("template_key") or new_route.get("country") or "").strip()
    new_nodes = old_nodes
    if new_tmpl and new_tmpl != (old.get("template_key") or old.get("country") or ""):
        try:
            from templates import load_template  # 预留：目的国模板切换
            new_nodes = load_template(new_tmpl)
        except Exception:
            new_nodes = old_nodes

    old_by_key = {n["node_key"]: n for n in old_nodes}
    new_by_key = {n["node_key"]: n for n in new_nodes}
    impact["added"] = [k for k in new_by_key if k not in old_by_key]
    impact["removed"] = [k for k in old_by_key if k not in new_by_key]
    impact["renamed"] = [{"node_key": k, "old": old_by_key[k]["node_name"],
                          "new": new_by_key[k]["node_name"]}
                         for k in old_by_key if k in new_by_key
                         and old_by_key[k]["node_name"] != new_by_key[k]["node_name"]]
    # 顺序变化：以 node_key 为稳定标识比较「在链路中的位置」。
    # 注意不得用 zip 比较 —— 长度不同时 zip 会静默截断，把「多了/少了节点」误判为无顺序变化。
    old_pos = {n["node_key"]: i for i, n in enumerate(old_nodes)}
    new_pos = {n["node_key"]: i for i, n in enumerate(new_nodes)}
    for i, n in enumerate(new_nodes):
        k = n["node_key"]
        if k in old_pos and old_pos[k] != i:
            impact["reordered"].append(k)
    if impact["reordered"]:
        impact["reordered"].sort(key=lambda k: new_pos.get(k, 0))

    # ETD 变化 → 未冻结节点计划日期需复核
    if impact["etd_changed"] or impact["eta_changed"]:
        nodes = db.get_nodes_by_batch(batch_id)
        pending = [n for n in nodes
                   if n.get("status") != "Done" and not n.get("actual_completion_date")]
        impact["nodes_need_recheck"] = len(pending)
    else:
        impact["nodes_need_recheck"] = 0
    return impact


def change_route(batch_id, new_route: dict, reason=""):
    """登记换线（§8）：写 batch_route_changes + op_log + 影响清单。

    §8/D18：换线**不自动重排节点**，仅记录变更并给出影响清单提示。
    """
    b = db.get_batch(batch_id)
    if not b:
        raise BatchStateError("批次不存在")
    old = db.get_route(batch_id) or {}
    impact = route_impact(batch_id, new_route)

    import json
    db.insert_route_change(
        batch_id,
        old_snapshot=json.dumps({f: old.get(f) for f in _ROUTE_SNAPSHOT_FIELDS},
                                ensure_ascii=False, default=str),
        new_snapshot=json.dumps({f: new_route.get(f, old.get(f))
                                 for f in _ROUTE_SNAPSHOT_FIELDS},
                                ensure_ascii=False, default=str),
        impact=json.dumps(impact, ensure_ascii=False, default=str),
        reason=reason or "换线")

    # 换线不改节点计划日期（D18 仅提示）；仅同步线路字段本身
    upd = {k: v for k, v in new_route.items() if k in _ROUTE_SNAPSHOT_FIELDS}
    if upd:
        db.update_route(batch_id, **upd)

    try:
        from services.oplog import record
        bits = []
        if impact["added"]:
            bits.append(f"新增 {len(impact['added'])} 节点")
        if impact["removed"]:
            bits.append(f"删除 {len(impact['removed'])} 节点")
        if impact["renamed"]:
            bits.append(f"改名 {len(impact['renamed'])} 节点")
        if impact["reordered"]:
            bits.append(f"顺序变化 {len(impact['reordered'])} 节点")
        if impact["nodes_need_recheck"]:
            bits.append(f"以下 {impact['nodes_need_recheck']} 个节点计划日期需复核")
        record("route_change", b["project_id"], batch_id=batch_id,
               subject=b.get("batch_no") or "批次", scope="batch",
               detail="换线：" + ("；".join(bits) if bits else "线路字段变更")
                      + (f" · 原因：{reason}" if reason else ""))
    except Exception:
        pass
    return impact


# ── §8 复制批次（冻结模板快照） ──

def copy_batch(project_id, source_batch_id, new_batch_no=None, batch_name=None,
               copy_containers=True, copy_cargo=True, copy_parties=True):
    """复制批次：批次信息 + 线路 + **冻结模板快照**（实例化副本）+ 柜/货物可选。

    不带：状态、日志、完成日期、船位（§8）。单证一律「未开始」（pending）。
    """
    src = db.get_batch(source_batch_id)
    if not src:
        raise BatchStateError("源批次不存在")
    src_route = db.get_route(source_batch_id) or {}

    if not new_batch_no:
        new_batch_no = db.next_batch_no(project_id)

    # 建批次（不带源状态/完成日期）
    conn = db.get_conn()
    from uuid import uuid4
    batch_id = f"batch-{uuid4().hex[:10]}"
    conn.execute(
        "INSERT INTO batches (batch_id, project_id, batch_no, batch_name, booking_no, "
        "mbl_no, hbl_nos, status, created_at) VALUES (?,?,?,?,?,?,?, 'draft', ?)",
        (batch_id, project_id, new_batch_no, batch_name or new_batch_no.split("-")[-1],
         src.get("booking_no"), src.get("mbl_no"), src.get("hbl_nos"), db._now()))
    conn.commit()

    # 线路（含冻结模板快照）
    import json
    route = {k: src_route.get(k) for k in _ROUTE_SNAPSHOT_FIELDS}
    route["template_snapshot"] = json.dumps(_node_fingerprint(source_batch_id),
                                            ensure_ascii=False)
    db.upsert_route(batch_id, **route)

    # 节点：实例化副本（冻结快照语义 —— 不随模板后续变更）
    from services import schedule2
    src_nodes = db.get_nodes_by_batch(source_batch_id)
    nodes = []
    for n in src_nodes:
        nodes.append({
            "node_id": n["node_id"], "node_key": n["node_key"],
            "node_name": n["node_name"], "role_label": n["role_label"],
            "seq": n["seq"], "area": n["area"],
            "calendar_mode": n.get("calendar_mode", "NATURAL"),
            "is_key_node": n.get("is_key_node", 0),
            "default_duration": n.get("default_duration", n.get("duration", 1)),
            "duration": n.get("duration", 1),
            "status": "Pending", "actual_completion_date": None,
            "is_delayed": 0, "delay_days": 0, "remark": n.get("remark", ""),
            "plan_start": None, "plan_end": None,
        })
    try:
        plan = schedule2.compute_plan(route.get("etd"), route.get("eta"), nodes)
        for n in nodes:
            n["plan_start"], n["plan_end"] = plan[n["node_key"]]
    except Exception:
        pass
    db.insert_nodes(project_id, nodes, batch_id)

    # 单证：一律「未开始」，due 由锚点重算
    src_files = db.get_files_by_batch(source_batch_id)
    files = []
    for f in src_files:
        files.append({**f, "status": "pending", "submitted_date": None,
                      "due_date": None, "file_id": None})
    if files:
        db.insert_files(project_id, files, batch_id)
    plan_map = {n["node_key"]: (n["plan_start"], n["plan_end"]) for n in nodes}
    db.recompute_files_due(project_id, plan_map, batch_id=batch_id)

    # 客户/货主
    if copy_parties:
        for p in db.get_batch_parties(source_batch_id):
            db.bind_batch_party(batch_id, p["role"], p["party_id"], p.get("seq") or 1)

    # 集装箱 + 关联（一期恒 PRIMARY）
    if copy_containers:
        for c in db.get_containers(source_batch_id):
            cid = db.insert_container(batch_id, c["container_no"], seal_no=c.get("seal_no"),
                                      container_type=c.get("container_type"),
                                      note=c.get("note"))
            db.link_container(batch_id, cid, role="PRIMARY")

    # 货物台账
    if copy_cargo:
        items = [{k: v for k, v in c.items() if k not in ("item_id", "batch_id")}
                 for c in db.get_cargo_items(project_id, source_batch_id)]
        if items:
            db.insert_cargo_items(project_id, items, batch_id)

    try:
        from services.oplog import record
        record("batch_create", project_id, batch_id=batch_id, subject=new_batch_no,
               scope="batch", detail=f"复制自批次 {src.get('batch_no')}（冻结模板快照）")
    except Exception:
        pass
    sync_batch_status(batch_id)
    update_project_status(project_id)
    return db.get_batch(batch_id)


# ── §12.1 批次指标 / 项目风险汇总（看板摘要卡与甘特工作台共用） ──

def batch_metrics(batch_id, today=None):
    """单批次汇总：线路 / 状态 / 发运日 / 单证完成率 / 待办数 / 逾期节点数。

    供「批次列表」表格与合并甘特的图例使用；纯只读。
    """
    if today is None:
        from services.clock import get_today
        today = get_today()
    from services.node_status import compute_node_status
    nodes = db.get_nodes_by_batch(batch_id)
    files = db.get_files_by_batch(batch_id)
    route = db.get_route(batch_id) or {}
    b = db.get_batch(batch_id) or {}
    n_done = sum(1 for n in nodes if n.get("status") == "Done")
    req = [f for f in files if f.get("doc_type") == "required"]
    submitted = sum(1 for f in req if f.get("status") == "submitted")
    overdue = sum(1 for n in nodes if compute_node_status(n, today) == "Overdue")
    pending = len(req) - submitted
    return {
        "batch_id": batch_id,
        "batch_no": b.get("batch_no") or "",
        "batch_name": b.get("batch_name") or "",
        "status": b.get("status") or "draft",
        "status_cn": state_label(b.get("status") or "draft"),
        "mode": route.get("mode_primary") or "SEA",
        "port": route.get("export_port") or "",
        "etd": route.get("etd") or "",
        "eta": route.get("eta") or "",
        "node_done": n_done, "node_total": len(nodes),
        "doc_rate": (submitted / len(req) * 100) if req else 0.0,
        "doc_total": len(req), "doc_submitted": submitted,
        "todo": pending, "overdue": overdue,
    }


def project_risk_summary(project_id, today=None):
    """项目级风险汇总：N 批次 · 逾期节点 X · 待办单证 Y（含已取消的当前批次）。

    与看板卡片批次条右侧那行「N 批次 · 逾期节点 X · 待办单证 Y」同源。
    """
    batches = list(db.get_batches(project_id))
    cur = db.get_batch(db.current_batch_id(project_id))
    if cur and cur.get("status") == "cancelled" and \
            not any(b["batch_id"] == cur["batch_id"] for b in batches):
        batches.append(cur)
    overdue = todo = 0
    for bt in batches:
        m = batch_metrics(bt["batch_id"], today)
        overdue += m["overdue"]
        todo += m["todo"]
    return {"n_batch": len(batches), "overdue": overdue, "todo": todo}


# ── §12.1 空批次「补齐节点 + 单证」（按 15 节点标准模板实例化，幂等） ──

def ensure_batch_nodes(project_id, batch_id, reason="补齐批次线路"):
    """为**尚无任何节点**的批次按标准模板实例化 15 个节点并展开单证清单。

    背景：`db.create_batch()`（新增批次）只往 batches 表写一行，不建线路/节点/单证
    （只有 copy_batch 复制批次才建）。若后续不在「批次管理」填 ETD/ETA 并补齐节点，
    该批次永远 0 节点 → 看板甘特图为空。本函数是该补齐动作的唯一入口。

    幂等与安全：
      · 已有节点（哪怕 1 个）→ 原样返回，不重复插入；
      · 缺 ETD/ETA 或 ETA ≤ ETD → 不动作，返回原因；
      · 已取消批次 → 不动作（§8 取消期间禁止写入）。
    返回 {"created": bool, "nodes": int, "files": int, "reason": str|None}
    """
    batch = db.get_batch(batch_id)
    if not batch:
        return {"created": False, "nodes": 0, "files": 0, "reason": "批次不存在"}
    if batch.get("status") == "cancelled":
        return {"created": False, "nodes": 0, "files": 0, "reason": "批次已取消，禁止写入"}
    if db.get_nodes_by_batch(batch_id):
        return {"created": False, "nodes": 0, "files": 0, "reason": None}

    route = db.get_route(batch_id) or {}
    etd, eta = route.get("etd"), route.get("eta")
    if not etd or not eta:
        return {"created": False, "nodes": 0, "files": 0, "reason": "缺 ETD/ETA"}

    proj = db.get_project(project_id) or {}
    country = route.get("country") or proj.get("country")
    port = route.get("export_port") or proj.get("export_port")

    from services import schedule2
    from services.ports import merge_node_notes

    tpl = nt.template()
    try:
        plan = schedule2.compute_plan(etd, eta, tpl)
    except schedule2.ScheduleError as e:
        return {"created": False, "nodes": 0, "files": 0, "reason": str(e)}

    nodes = []
    for n in tpl:
        s, e = plan[n["node_key"]]
        nodes.append({
            "node_id": n["node_id"], "node_key": n["node_key"],
            "node_name": n["node_name"], "role_label": n["role_label"],
            "seq": n["seq"], "area": n["area"],
            "calendar_mode": n["calendar_mode"], "key_node": n["key_node"],
            "default_duration": n["default_duration"], "duration": n["duration"],
            "status": "Pending", "plan_start": s, "plan_end": e,
            "remark": merge_node_notes(n.get("remark", ""), port, n["node_id"]),
        })
    db.insert_nodes(project_id, nodes, batch_id)

    from services.file_checklist import seed as seed_files
    counts = seed_files(project_id, country, port, plan, batch_id=batch_id)

    sync_batch_status(batch_id)
    update_project_status(project_id)
    try:
        from services.oplog import record
        record("batch_edit", project_id, batch_id=batch_id,
               subject=batch.get("batch_no") or "批次",
               detail=f"{reason}：按 15 节点标准模板生成计划节点与单证清单"
                      f"（{len(nodes)} 节点 / {counts['batch']} 批次单证"
                      f" + {counts['project']} 项目级单证）")
    except Exception:
        pass
    return {"created": True, "nodes": len(nodes),
            "files": counts["batch"] + counts["project"],
            "batch_files": counts["batch"], "project_files": counts["project"],
            "reason": None}
