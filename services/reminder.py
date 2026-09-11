"""提醒引擎（《多式联运.md》§10.2 / §10.3 / §10.4 / §10.5 / §12.4）。

三级结构：**项目 → 批次 → 节点**（§10.5）。
提醒级别（沿用旧 API 的三档，UI 侧文案为「需处理 / 进行中提醒 / 今日启动」）：
    P0 = 需处理（逾期、缺失、免箱期已到/前 1 天、申报已过截止）
    P1 = 进行中提醒（临近、免箱期前 3 天、到货通知、保险到期、依赖等待）
    P2 = 今日启动（节点今日启动）

提醒类型（§10.4 新增）：
    DETENTION_DUE        免箱期倒计时（前 3 天 / 前 1 天，高优先级置顶）— T17
    DEMURRAGE_DUE        免堆期倒计时（同上）
    CONTAINER_RETURN_DUE 单柜还箱截止（containers.return_due）
    ARRIVAL_NOTICE       到货通知 AN 到达即提醒
    INSURANCE_EXPIRY     保险到期（保单 end_date）
    DECLARATION_DUE      申报截止（AMS/ISF/ENS，装船前 N 小时）
    DEP_WAITING          依赖等待（§10.3 上游未满足）
以及沿用的 NODE_OVERDUE / NODE_END_TODAY / NODE_START_TODAY /
FILE_LATE / FILE_MISSING_IN_ACTIVE / FILE_MISSING_LATE。

向后兼容（旧调用点，勿改签名）：
    compute_reminders(project, nodes, files, today=None) -> {"P0": [...], "P1": [...], "P2": [...]}
    format_reminders(all_reminders, today=None) -> str
    count_total(reminders) -> int
条目仍带 `project` / `msg` / `type` / `node_id|file_id`，另加批次与依赖上下文字段。
"""

from datetime import date, datetime, timedelta

from services import docdict
from services.node_status import compute_node_status
from services.node_template import BUFFER_HINT, LOADING

# ── 级别 ──
LEVELS = ("P0", "P1", "P2")
LEVEL_LABEL = {"P0": "需处理", "P1": "进行中提醒", "P2": "今日启动"}

# ── 提醒类型 ──
T_NODE_OVERDUE = "NODE_OVERDUE"
T_NODE_END_TODAY = "NODE_END_TODAY"
T_NODE_START_TODAY = "NODE_START_TODAY"
T_FILE_LATE = "FILE_LATE"
T_FILE_MISSING_ACTIVE = "FILE_MISSING_IN_ACTIVE"
T_FILE_NEAR = "FILE_MISSING_LATE"
T_DETENTION = "DETENTION_DUE"
T_DEMURRAGE = "DEMURRAGE_DUE"
T_CONTAINER_RETURN = "CONTAINER_RETURN_DUE"
T_ARRIVAL_NOTICE = "ARRIVAL_NOTICE"
T_INSURANCE = "INSURANCE_EXPIRY"
T_DECLARATION = "DECLARATION_DUE"
T_DEP_WAITING = "DEP_WAITING"

# §10.4 免箱期/免堆期：前 3 天、前 1 天，高优先级且置顶
DETENTION_LEAD_DAYS = (3, 1)
# 保险到期预警梯度（§10.4「保险到期」）
INSURANCE_LEAD_DAYS = (30, 15, 7, 3, 1, 0)
# 免箱期 = 需处理级别的提前量（前 1 天及已到）
DETENTION_P0_DAYS = 1

# §12.4 提醒中心固定分节（顺序即展示顺序）
SECTION_ORDER = ("DETENTION", "ARRIVAL", "DECLARATION", "DEPENDENCY", "OTHER")
SECTION_LABEL = {
    "DETENTION": "免箱期倒计时",
    "ARRIVAL": "到货通知",
    "DECLARATION": "申报截止",
    "DEPENDENCY": "依赖等待",
    "OTHER": "其他待办",
}
SECTION_ICON = {
    "DETENTION": "clock",
    "ARRIVAL": "vessel",
    "DECLARATION": "customs",
    "DEPENDENCY": "link",
    "OTHER": "file",
}
# 提醒类型 → 提醒中心分节
TYPE_SECTION = {
    T_DETENTION: "DETENTION",
    T_DEMURRAGE: "DETENTION",
    T_CONTAINER_RETURN: "DETENTION",
    T_ARRIVAL_NOTICE: "ARRIVAL",
    T_DECLARATION: "DECLARATION",
    T_DEP_WAITING: "DEPENDENCY",
}
# 允许在提醒中心「临时忽略」的类型（§15：可临时忽略并留痕）——由调用方留痕
IGNORABLE_TYPES = (T_DEP_WAITING,)

_HOUR = timedelta(hours=1)


# ── 通用小工具 ──

def _parse(s):
    if isinstance(s, date):
        return s
    if not s:
        return None
    txt = str(s).strip()
    if not txt:
        return None
    try:
        return date(*map(int, txt[:10].split("-")))
    except (ValueError, TypeError):
        return None


def _parse_dt(s):
    """解析 ISO 时间戳；无时间部分时按当日 00:00 处理。"""
    if isinstance(s, datetime):
        return s
    if isinstance(s, date):
        return datetime(s.year, s.month, s.day)
    if not s:
        return None
    txt = str(s).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%d"):
        try:
            return datetime.strptime(txt[:26], fmt)
        except ValueError:
            continue
    d = _parse(txt)
    return datetime(d.year, d.month, d.day) if d else None


def _day_diff(due, today):
    """due − today 的天数差（负数 = 已过截止）。"""
    return (due - today).days


def _now(today):
    """给定业务日期时，「当前时刻」。

    纯 date 口径（自测/报告传入 YYYY-MM-DD）→ 返回 None，表示只按日粒度判定，
    小时级截止用「截止日当天」作为触发日（date 比较按 <= 处理），避免把
    「当日 00:00」算成已过截止而误报 P0。
    """
    if isinstance(today, datetime):
        return today
    return None


def _midnight(d):
    return datetime(d.year, d.month, d.day)


def _batch_ref(batch):
    if not batch:
        return {"batch_id": None, "batch_no": None, "batch_name": None, "batch_status": None}
    return {"batch_id": batch.get("batch_id"),
            "batch_no": batch.get("batch_no"),
            "batch_name": batch.get("batch_name"),
            "batch_status": batch.get("status")}


def _mk(level, rtype, msg, project_name, batch=None, pinned=False,
        remind_on=None, **extra):
    """构造统一结构的提醒条目（同时保留旧字段名）。"""
    ref = _batch_ref(batch)
    item = {
        "id": None,                       # 后面统一编号
        "level": level,
        "type": rtype,
        "msg": msg,
        "project": project_name,
        "project_id": extra.pop("project_id", None),
        "pinned": bool(pinned),
        "section": TYPE_SECTION.get(rtype, "OTHER"),
        "remind_on": remind_on,
        **ref,
    }
    item.update(extra)
    return item


def _assign_ids(items):
    for i, it in enumerate(items, start=1):
        if not it.get("id"):
            it["id"] = f"rmd-{i}"
    return items


def _sort_key(it):
    """免箱期等高优先级置顶（§10.4），其余按（级别 → 日期 → 类型）稳定排序。"""
    lvl = LEVELS.index(it["level"]) if it.get("level") in LEVELS else len(LEVELS)
    rd = it.get("remind_on")
    dd = _parse(rd) if rd else None
    return (0 if it.get("pinned") else 1, lvl, dd or date.max,
            str(it.get("type") or ""), str(it.get("batch_no") or ""),
            it.get("file_id") if it.get("file_id") is not None else 0)


def sort_reminders(items):
    """统一排序：置顶项 → 级别 → 日期 → 类型 → 批次。原地排序并返回。"""
    items.sort(key=_sort_key)
    return _assign_ids(items)


# ── 旧版逐节点/逐单证逻辑（保持 msg 不变，保证回归脚本口径一致） ──

def legacy_node_reminders(project, nodes, today, batch=None):
    out = []
    proj_name = project["project_name"]
    buffer_days = project.get("buffer_days", 4)
    for n in nodes:
        st = compute_node_status(n, today)
        plan_end = _parse(n.get("plan_end"))
        plan_start = _parse(n.get("plan_start"))
        base = {"node_id": n["node_id"], "node_key": n.get("node_key")}

        if st == "Overdue":
            overdue_days = (today - plan_end).days
            msg = f"{n['node_name']} 已逾期 {overdue_days} 天"
            if n.get("node_key") in BUFFER_HINT and overdue_days > 0:
                msg += f"（已消耗缓冲 {min(overdue_days, buffer_days)} 天，预留 {buffer_days} 天）"
            out.append(_mk("P0", T_NODE_OVERDUE, msg, proj_name, batch, **base))
        elif st == "Active" and plan_end == today:
            out.append(_mk("P1", T_NODE_END_TODAY, f"{n['node_name']} 今日截止",
                           proj_name, batch, **base))
        elif st == "Active" and plan_start == today:
            out.append(_mk("P2", T_NODE_START_TODAY, f"{n['node_name']} 今日启动",
                           proj_name, batch, **base))
    return out


def legacy_file_reminders(project, nodes, files, today, batch=None):
    out = []
    proj_name = project["project_name"]
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
            out.append(_mk("P0", T_FILE_LATE,
                           f"单证《{f['doc_name']}》超建议提交日 {late_days} 天",
                           proj_name, batch, file_id=f.get("file_id")))
        elif node_st in ("Active", "Overdue") and f.get("status", "pending") == "pending":
            out.append(_mk("P0", T_FILE_MISSING_ACTIVE,
                           f"必备单证《{f['doc_name']}》未提交（节点进行中）",
                           proj_name, batch, file_id=f.get("file_id")))
        elif due and today >= due - timedelta(days=remind_days) and today <= due:
            out.append(_mk("P1", T_FILE_NEAR,
                           f"必备单证《{f['doc_name']}》临近提交日（{due.strftime('%m-%d')}）",
                           proj_name, batch, file_id=f.get("file_id")))
    return out


# ── 提前量（§10.2） ──

def carrier_of(batch_id):
    """承运人：优先 `vessel.carrier`，回退船名（规则演示常按承运人公司名配置）。"""
    row = None
    try:
        row = db_get_vessel(batch_id)
    except Exception:
        row = None
    if row:
        return (row.get("carrier") or row.get("vessel_name") or None)
    return None


def db_get_vessel(batch_id):
    import db
    row = db.get_conn().execute(
        "SELECT vessel_name, carrier FROM vessel WHERE batch_id=? AND voyage_sequence=1",
        (batch_id,)).fetchone()
    return dict(row) if row else None


# §10.2 规则键 = (单证类型, 目的国, 承运人)，空 = 任意。
# 匹配按「特异性」取最高（而非回落链顺序）：三个维度全部非空则最特异，
# 只有类型最泛；因此 (类型,空,承运人) 会胜过 (类型,空,空)，而 (类型,空,空) 胜过全局。
# 特异性相同时按 §10.2 行文顺序（目的国默认 → 承运人默认 → 类型默认）取先者。
_LV_EXACT = "规则（类型+目的国+承运人）"
_LV_COUNTRY = "目的国默认"
_LV_CARRIER = "承运人默认"
_LV_TYPE = "单证类型默认"
_LV_COUNTRY_ONLY = "目的国默认"
_LV_GLOBAL = "全局默认"
# 特异性得分相同时的取舍顺序（§10.2 行文：目的国默认 → 承运人默认 → 单证类型默认 → 全局）
_SPEC_RANK = {_LV_COUNTRY: 1, _LV_COUNTRY_ONLY: 1, _LV_CARRIER: 2,
              _LV_TYPE: 3, _LV_GLOBAL: 4}


def _specificity(rule, doc_key, country, carrier):
    """返回 (命中层级, 特异性得分)；未命中返回 (None, -1)。分数越高越具体。

    规则里**非空**的每个维度都必须在查询值上成立，否则该规则不适用
    （避免「目的国命中但承运人不匹配」的规则被误用，§15 依赖/规则误配思路）。
    """
    rt, rc, rr = rule.get("doc_type"), rule.get("country"), rule.get("carrier")

    def eq(a, b):
        return (a or None) == (b or None)

    if rt is not None and not eq(rt, doc_key):
        return None, -1
    if rc is not None and not eq(rc, country):
        return None, -1
    if rr is not None and not eq(rr, carrier):
        return None, -1

    score = 0
    if rt is not None:
        score += 4
    if rc is not None:
        score += 2
    if rr is not None:
        score += 1
    if score == 0:
        return _LV_GLOBAL, 0
    if rt is not None and rc is not None and rr is not None:
        return _LV_EXACT, score                     # 7
    if rt is not None and rc is not None:
        return _LV_COUNTRY, score                   # 6
    if rt is not None and rr is not None:
        return _LV_CARRIER, score                   # 5
    if rt is not None:
        return _LV_TYPE, score                      # 4
    if rc is not None:
        return _LV_COUNTRY_ONLY, score              # 2
    return _LV_CARRIER, score                       # 1（无类型、无国、有承运人）


def lead_time_for(doc_key, country=None, carrier=None):
    """§10.2 提前量：按「(单证类型, 目的国, 承运人)」匹配已启用规则。

    维度取值：命中的维度越多越具体；同分时按 §10.2 行文顺序
    （目的国默认 → 承运人默认 → 单证类型默认 → 全局默认）取先者。
    未命中任何规则 → 全局默认（最后一条 (None,None,None) 规则或内建 3 天）。

    返回 {before_days, before_hours, baseline_source, source, rule_id}
    """
    import db
    rules = db.list_reminder_rules()
    best, best_score, best_rank = None, -1, 99
    for r in rules:
        lvl, score = _specificity(r, doc_key, country, carrier)
        if lvl is None:
            continue
        rank = _SPEC_RANK.get(lvl, 9)
        if score > best_score or (score == best_score and rank < best_rank):
            best, best_score, best_rank = (r, lvl), score, rank

    if not best:
        return {"before_days": docdict.GLOBAL_DEFAULT_DAYS, "before_hours": None,
                "baseline_source": "系统全局默认", "source": "全局默认（内建）",
                "rule_id": None}
    rule, lvl = best
    return {"before_days": rule.get("before_days"),
            "before_hours": rule.get("before_hours"),
            "baseline_source": rule.get("baseline_source"), "source": lvl,
            "rule_id": rule.get("id"),
            "note": rule.get("note")}


# ── 批次级提醒（新增主体） ──

def compute_reminders_for_batch(batch_id, today=None, add_legacy=True,
                                sections=None, include_done_batch=False):
    """批次级提醒（§10.2/§10.3/§10.4）。

    today: date / 'YYYY-MM-DD' / datetime；None → services.clock.get_today()
    add_legacy: 是否附带沿用旧口径的节点/单证提醒（默认 True）
    返回：已排序的提醒 dict 列表（免箱期等高优先级置顶）。
    """
    import db

    if today is None:
        from services.clock import get_today
        today = get_today()
    elif isinstance(today, str):
        today = _parse(today)
    today_date = today.date() if isinstance(today, datetime) else today

    batch = db.get_batch(batch_id)
    if not batch:
        return []
    project = db.get_project(batch["project_id"]) or {}
    proj_name = project.get("project_name") or batch["project_id"]
    if batch.get("status") == "cancelled" and not include_done_batch:
        return []

    route = db.get_route(batch_id) or {}
    nodes = db.get_nodes_by_batch(batch_id)
    files = db.get_files_by_batch(batch_id)
    country = route.get("country") or project.get("country")
    carrier = carrier_of(batch_id)

    out = []
    if add_legacy:
        out += legacy_node_reminders(project, nodes, today_date, batch)
        out += legacy_file_reminders(project, nodes, files, today_date, batch)

    out += _detention_reminders(batch, route, today_date, proj_name, project)
    out += _arrival_notice_reminders(batch, nodes, route, files, today_date, proj_name)
    out += _insurance_reminders(batch, today_date, proj_name)
    out += _declaration_reminders(batch, route, files, today_date, proj_name)
    out += _dependency_reminders(batch, files, today_date, proj_name)

    if sections:
        want = set(sections)
        out = [r for r in out if r["section"] in want]
    return sort_reminders(out)


# 「启用中批次」= 未取消、未完成关闭（§10.5 徽标口径）
ENABLED_STATES = ("draft", "ready", "running")


def _level_groups(items):
    g = {lv: [] for lv in LEVELS}
    for it in items:
        g.setdefault(it["level"], []).append(it)
    return g


def compute_reminders_for_project(project_id, today=None, enabled_only=True,
                                  include_cancelled=False):
    """项目级：按批次聚合（§10.5 三级汇总的中间层）。"""
    import db
    if today is None:
        from services.clock import get_today
        today = get_today()
    elif isinstance(today, str):
        today = _parse(today)

    batches = db.get_batches(project_id, include_cancelled=include_cancelled)
    if enabled_only:
        batches = [b for b in batches if b["status"] in ENABLED_STATES]
    items = []
    for b in batches:
        items += compute_reminders_for_batch(b["batch_id"], today)
    return sort_reminders(items)


def compute_reminders_three_level(project_ids=None, today=None,
                                  enabled_only=True, include_cancelled=False):
    """三级汇总（§10.5）：**项目 → 批次 → 节点**。

    返回：
      {
        "today": "YYYY-MM-DD",
        "projects": [{
            "project_id", "project_name", "country", "status",
            "batch_count", "enabled_batch_count",
            "total", "counts": {"P0","P1","P2"},
            "levels": {"P0":[...], "P1":[...], "P2":[...]},
            "batches": [{
                "batch_id","batch_no","batch_name","batch_status","planned_date",
                "total","counts","levels",
                "nodes": [{"node_id","node_key","node_name","total","items":[...]}],
                "reminders": [...]
            }],
        }],
        "totals": {"P0","P1","P2","total","projects","batches"},
        "badge": {...same as totals...},     # 徽标口径 = 启用中批次待办合计
      }
    """
    import db
    if today is None:
        from services.clock import get_today
        today = get_today()
    elif isinstance(today, str):
        today = _parse(today)

    if project_ids is None:
        rows = db.get_projects_by_status("Active") + db.get_projects_by_status("Completed")
        project_ids = [p["project_id"] for p in rows]

    projects_out = []
    t_counts = {lv: 0 for lv in LEVELS}
    t_total = 0
    n_batches = 0

    for pid in project_ids:
        proj = db.get_project(pid)
        if not proj:
            continue
        batches = db.get_batches(pid, include_cancelled=include_cancelled)
        if enabled_only:
            batches = [b for b in batches if b["status"] in ENABLED_STATES]

        b_out = []
        p_items = []
        for b in batches:
            items = compute_reminders_for_batch(b["batch_id"], today)
            p_items += items
            b_out.append(_batch_rollup(b, items))
            n_batches += 1
        sort_reminders(p_items)
        levels = _level_groups(p_items)
        counts = {lv: len(levels[lv]) for lv in LEVELS}
        for lv in LEVELS:
            t_counts[lv] += counts[lv]
        t_total += len(p_items)

        projects_out.append({
            "project_id": pid,
            "project_name": proj.get("project_name"),
            "country": proj.get("country"),
            "status": proj.get("status"),
            "batch_count": len(db.get_batches(pid, include_cancelled=include_cancelled)),
            "enabled_batch_count": len(batches),
            "total": len(p_items),
            "counts": counts,
            "levels": levels,
            "batches": b_out,
        })

    projects_out.sort(key=lambda p: (-p["total"], str(p["project_name"])))
    totals = {**t_counts, "total": t_total, "projects": len(projects_out),
              "batches": n_batches}
    return {
        "today": (today.date() if isinstance(today, datetime) else today).isoformat(),
        "projects": projects_out,
        "totals": totals,
        # §10.5 徽标口径 = 启用中批次待办合计
        "badge": dict(totals),
    }


def _batch_rollup(batch, items):
    """批次 → 节点 下钻（三级汇总的第三级）。"""
    by_node = {}
    others = []
    for it in items:
        nk, nid = it.get("node_key"), it.get("node_id")
        if nk is None and nid is None:
            others.append(it)
            continue
        key = nk if nk is not None else f"node:{nid}"
        slot = by_node.setdefault(key, {"node_key": nk, "node_id": nid,
                                        "node_name": None, "items": []})
        if nid is not None and slot["node_id"] is None:
            slot["node_id"] = nid
        slot["items"].append(it)

    # 补节点名（提醒本身不带名字时从库读，保证第三级可读）
    if by_node:
        nodes = {n["node_key"]: n for n in _nodes_of(batch["batch_id"])}
        for key, slot in by_node.items():
            node = nodes.get(slot.get("node_key"))
            slot["node_name"] = node["node_name"] if node else (
                it_name(slot["items"]) or slot.get("node_key"))

    levels = _level_groups(items)
    counts = {lv: len(levels[lv]) for lv in LEVELS}
    node_rows = []
    for slot in by_node.values():
        slot_items = slot["items"]
        node_rows.append({
            "node_id": slot["node_id"], "node_key": slot["node_key"],
            "node_name": slot["node_name"], "total": len(slot_items),
            "counts": {lv: len([x for x in slot_items if x["level"] == lv])
                       for lv in LEVELS},
            "items": slot_items,
        })
    node_rows.sort(key=lambda r: (r["node_id"] if r["node_id"] is not None else 999))
    if others:
        node_rows.append({"node_id": None, "node_key": None,
                          "node_name": "项目级 · 全程", "total": len(others),
                          "counts": {lv: len([x for x in others if x["level"] == lv])
                                     for lv in LEVELS},
                          "items": others})
    return {
        "batch_id": batch["batch_id"], "batch_no": batch.get("batch_no"),
        "batch_name": batch.get("batch_name"), "batch_status": batch.get("status"),
        "planned_date": batch.get("planned_date"),
        "total": len(items), "counts": counts, "levels": levels,
        "nodes": node_rows,
        "reminders": sort_reminders(list(items)),
    }


def _nodes_of(batch_id):
    import db
    return db.get_nodes_by_batch(batch_id)


def it_name(items):
    for it in items:
        if it.get("node_name"):
            return it["node_name"]
    return None


def badge_count(project_ids=None, today=None):
    """§10.5 徽标口径 = **启用中批次待办合计**。

    只统计 status ∈ (draft, ready, running) 的批次；取消/待确认完成/已完成/已关闭
    批次不计入。
    """
    data = compute_reminders_three_level(project_ids, today=today, enabled_only=True)
    return data["badge"]["total"]


def badge_count_by_project(project_ids=None, today=None):
    """逐项目徽标数（主看板/启动页展示用）。返回 {project_id: int}"""
    data = compute_reminders_three_level(project_ids, today=today, enabled_only=True)
    return {p["project_id"]: p["total"] for p in data["projects"]}


# ── §10.4 免箱期 / 免堆期倒计时（T17） ──

def _detention_reminders(batch, route, today, proj_name, project):
    out = []
    pairs = (("free_demurrage_until", T_DEMURRAGE, "免堆期"),
             ("free_detention_until", T_DETENTION, "免箱期"))
    for field, rtype, label in pairs:
        until = _parse(route.get(field))
        if not until:
            continue
        diff = _day_diff(until, today)
        if diff in DETENTION_LEAD_DAYS or diff <= 0:
            # 前 1 天及已到期/超期 = 需处理（P0）；前 3 天 = 进行中提醒（P1）
            if diff > DETENTION_P0_DAYS:
                level, tail = "P1", f"还有 {diff} 天"
            elif diff > 0:
                level, tail = "P0", f"还有 {diff} 天（{DETENTION_P0_DAYS} 天内）"
            elif diff == 0:
                level, tail = "P0", "今日到期"
            else:
                level, tail = "P0", f"已超期 {-diff} 天"
            msg = f"{label}截止 {until.isoformat()}（{tail}），请优先安排提箱/还箱"
            out.append(_mk(level, rtype, msg, proj_name, batch, pinned=True,
                           remind_on=until.isoformat(), due_date=until.isoformat(),
                           days_left=diff, field=field,
                           baseline_source="承运人免箱/免堆期条款"))
    # 单柜还箱截止（T26 提/还箱日）——与免箱期同一倒计时口径（前 3 天 / 前 1 天）
    import db
    for c in db.get_containers(batch["batch_id"]):
        due = _parse(c.get("return_due"))
        if not due or c.get("returned_at"):
            continue
        diff = _day_diff(due, today)
        if diff > DETENTION_LEAD_DAYS[0]:
            continue                       # 还早（>3 天）不提醒
        if diff > DETENTION_P0_DAYS:
            level, tail = "P1", f"还有 {diff} 天"
        elif diff > 0:
            level, tail = "P0", f"还有 {diff} 天（{DETENTION_P0_DAYS} 天内）"
        elif diff == 0:
            level, tail = "P0", "今日到期"
        else:
            level, tail = "P0", f"已超期 {-diff} 天"
        out.append(_mk(level, T_CONTAINER_RETURN,
                       f"柜号 {c['container_no']} 还箱截止 {due.isoformat()}（{tail}）",
                       proj_name, batch, pinned=True, remind_on=due.isoformat(),
                       days_left=diff, container_id=c["container_id"],
                       baseline_source="堆场还箱时限"))
    return out


# ── §10.4 到货通知 AN 到达即提醒 ──

def _arrival_notice_reminders(batch, nodes, route, files, today, proj_name):
    out = []
    an_node = next((n for n in nodes if n["node_key"] == "ARRIVAL_NOTICE"), None)
    an_file = next((f for f in files
                    if docdict.match_doc_type(f.get("doc_name")) == "ARRIVAL_NOTICE"
                    or f.get("node_key") == "ARRIVAL_NOTICE"), None)

    def fire(level, msg, extra=None):
        out.append(_mk(level, T_ARRIVAL_NOTICE, msg, proj_name, batch,
                       node_id=an_node["node_id"] if an_node else None,
                       node_key="ARRIVAL_NOTICE",
                       file_id=(an_file or {}).get("file_id"),
                       baseline_source="到港后即推送收货人", **(extra or {})))

    done = bool(an_node and an_node.get("status") == "Done")
    arrival = None
    if an_node:
        arrival = _parse(an_node.get("actual_completion_date"))
    if arrival is None:
        arrival = _parse(batch.get("actual_eta"))
    if arrival is None:
        arrival = _parse(route.get("eta"))

    if done or (arrival and arrival <= today):
        if an_file and an_file.get("status") != "submitted":
            fire("P1", f"已到港（{(arrival or today).isoformat()}），"
                       f"到货通知《{an_file['doc_name']}》待提交并推送收货人")
        elif done and arrival == today:
            fire("P1", f"到货通知已到达（{today.isoformat()}），请核对收货人信息")
        elif done:
            fire("P1", f"已到港（{arrival.isoformat()}），请确认到货通知已送达收货人")
    elif an_node and an_node.get("status") in ("Active", "Overdue"):
        # 到港换单节点进行中：AN 尚未到达，提前提示（不抢 P0）
        fire("P1", f"到港换单进行中，等待到货通知 AN 到达（计划 "
                   f"{an_node.get('plan_start')}）")
    return out


# ── §10.4 保险到期 ──

def _insurance_reminders(batch, today, proj_name):
    import db
    pol = db.get_insurance(batch["batch_id"])
    if not pol:
        return []
    end = _parse(pol.get("end_date"))
    if not end:
        return []
    diff = _day_diff(end, today)
    if diff not in INSURANCE_LEAD_DAYS and diff > 0:
        return []
    if diff < 0:
        level, tail = "P0", f"已过期 {-diff} 天"
    elif diff == 0:
        level, tail = "P0", "今日到期"
    elif diff <= 3:
        level, tail = "P1", f"还有 {diff} 天"
    else:
        level, tail = "P1", f"还有 {diff} 天"
    policy = pol.get("policy_no") or "（无保单号）"
    msg = f"保险单 {policy}（{pol.get('company') or '承保方未填'}）保险期至 "
    msg += f"{end.isoformat()}（{tail}），请及时续保"
    return [_mk(level, T_INSURANCE, msg, proj_name, batch,
                remind_on=end.isoformat(), due_date=end.isoformat(),
                days_left=diff, policy_no=pol.get("policy_no"),
                baseline_source="CIF 条款投保要求（保险到期提醒）")]


# ── §10.4 申报截止（AMS/ISF/ENS：装船前 N 小时） ──

DECLARATION_KEYS = ("AMS", "ISF", "ENS")


def declaration_deadline(batch_id, doc_key, files=None, route=None):
    """计算申报截止时刻。「装船前 N 小时」= 装船节点 plan_start − N 小时。

    返回 dict：{doc_key, deadline(datetime), due_date, due_hours, source, file_id,
                gate_mode: 'hours'|'days'|None}
    """
    import db
    files = files if files is not None else db.get_files_by_batch(batch_id)
    target = next((f for f in files
                   if docdict.match_doc_type(f.get("doc_name")) == doc_key), None)
    if not target:
        return None
    route = route if route is not None else (db.get_route(batch_id) or {})
    lead = lead_time_for(doc_key, country=route.get("country"),
                         carrier=carrier_of(batch_id))
    hours = target.get("due_hours") or lead.get("before_hours")
    days = lead.get("before_days")

    loading = db.node_by_key(batch_id, LOADING)
    base_day = _parse((loading or {}).get("plan_start"))
    deadline, gate = None, None
    if base_day and hours:
        deadline = _midnight(base_day) - timedelta(hours=int(hours))
        gate = "hours"
    elif target.get("due_date"):
        deadline = _midnight(_parse(target["due_date"]))
        gate = "days"
    elif base_day and days:
        deadline = _midnight(base_day) - timedelta(days=int(days))
        gate = "days"
    return {"doc_key": doc_key, "deadline": deadline, "gate_mode": gate,
            "due_hours": hours, "due_days": days,
            "due_date": deadline.date().isoformat() if deadline else None,
            "source": target.get("baseline_source") or lead.get("baseline_source"),
            "match_source": lead.get("source"),
            "file_id": target.get("file_id"),
            "doc_name": target.get("doc_name"),
            "status": target.get("status")}


def _declaration_reminders(batch, route, files, today, proj_name):
    out = []
    now = _now(today)
    today_date = today.date() if isinstance(today, datetime) else today
    for key in DECLARATION_KEYS:
        info = declaration_deadline(batch["batch_id"], key, files=files, route=route)
        if not info or not info["deadline"]:
            continue
        f = next((x for x in files if x.get("file_id") == info["file_id"]), None)
        if f and f.get("status") == "submitted":
            continue

        dl = info["deadline"]
        # 提前量（天）：小时级封顶当天（ceil(hours/24)−1）；
        # 日级优先用文件自己的 remind_before_days，其次 before_days−1，最后默认 1 天。
        lead_days = 0
        if info["gate_mode"] == "hours" and info["due_hours"]:
            import math
            lead_days = max(0, int(math.ceil(int(info["due_hours"]) / 24.0)) - 1)
        else:
            rbd = (f or {}).get("remind_before_days")
            lead_days = int(rbd) if rbd else max(1, int(info["due_days"] or 3) - 1)

        if now is not None:
            passed = now >= dl
            delta_txt = _human_delta(now - dl) if passed else _human_delta(dl - now)
        else:
            # 日粒度：截止日当天（含）即视为「已到截止」
            passed = today_date >= dl.date()
            delta_txt = _human_delta(_midnight(dl.date()) - _midnight(today_date))

        if passed:
            level, tail = "P0", (f"已过截止 {delta_txt}" if now is not None
                                 else "已到截止日")
        elif now is not None and now >= dl - timedelta(days=lead_days):
            level, tail = "P1", f"距截止 {delta_txt}"
        elif now is None and lead_days and \
                (_midnight(dl.date()) - _midnight(today_date)).days <= lead_days:
            level, tail = "P1", f"距截止 {delta_txt}"
        else:
            continue

        src = info["source"] or ""
        msg = (f"申报截止：{info['doc_name']} 须于 {dl.strftime('%Y-%m-%d %H:%M')} 前提交"
               f"（{tail}）")
        if src:
            msg += f" · 依据：{src}"
        out.append(_mk(level, T_DECLARATION, msg, proj_name, batch,
                       remind_on=dl.date().isoformat(), file_id=info["file_id"],
                       doc_key=key, deadline=dl.strftime("%Y-%m-%d %H:%M"),
                       due_hours=info["due_hours"], gate_mode=info["gate_mode"],
                       match_source=info["match_source"],
                       baseline_source=src))
    return out


def _human_delta(td):
    secs = int(td.total_seconds())
    if secs < 0:
        secs = -secs
    h = secs // 3600
    if h >= 24:
        return f"{h // 24} 天 {h % 24} 小时"
    if h >= 1:
        return f"{h} 小时"
    return f"{max(1, secs // 60)} 分钟"


# ── §10.3 依赖等待提醒 ──

def _dependency_reminders(batch, files, today, proj_name):
    from services import doc_dependency as dep
    out = []
    ctx = dep.build_context(batch["batch_id"])
    for f in files:
        if f.get("status") == "submitted":
            continue
        key = docdict.match_doc_type(f.get("doc_name"))
        if not key:
            continue
        st = dep.status_for_doc(f.get("doc_name"), ctx, key)
        if not st["blocked"]:
            continue
        names = st["waiting_names"]
        out.append(_mk("P1", T_DEP_WAITING,
                       f"单证《{f['doc_name']}》{st['text']}",
                       proj_name, batch, file_id=f.get("file_id"),
                       doc_key=key, node_key=f.get("node_key"),
                       waiting_keys=st["waiting_keys"], waiting_names=names,
                       baseline_source="单证依赖链（§10.3）"))
    return out


# ── 兼容入口：旧签名（project, nodes, files） ──

def compute_reminders(project, nodes, files, today=None):
    """【兼容】旧签名入口，返回 {level: [items]}。

    仍按「传入的 nodes/files」计算（不查库），保证旧调用点与回归脚本口径一致；
    当这三者其实来自某个批次时，自动补上批次上下文（batch_id/batch_no…）。
    """
    if today is None:
        from services.clock import get_today
        today = get_today()
    elif isinstance(today, str):
        today = _parse(today)
    today_date = today.date() if isinstance(today, datetime) else today

    import db
    batch = None
    bid = None
    if nodes:
        bid = nodes[0].get("batch_id")
    if not bid and files:
        bid = files[0].get("batch_id")
    if not bid:
        bid = db.resolve_batch_optional(project["project_id"])
    if bid:
        batch = db.get_batch(bid)

    items = legacy_node_reminders(project, nodes, today_date, batch)
    items += legacy_file_reminders(project, nodes, files, today_date, batch)
    return _level_groups(sort_reminders(items))


def format_reminders(all_reminders, today=None):
    """格式化今日待办文本（兼容旧调用；含三级归属前缀）。"""
    if today is None:
        from services.clock import get_today
        today = get_today()
    elif isinstance(today, str):
        today = _parse(today)

    lines = [f"今日待办 · {today.strftime('%Y-%m-%d')}", ""]
    p0 = all_reminders.get("P0", [])
    p1 = all_reminders.get("P1", [])
    p2 = all_reminders.get("P2", [])
    if not p0 and not p1 and not p2:
        lines.append("今日暂无待办，一切正常。")
        return "\n".join(lines)

    for level, title in (("P0", "【需处理】"), ("P1", "【进行中提醒】"),
                         ("P2", "【今日启动】")):
        items = all_reminders.get(level, [])
        if not items:
            continue
        lines.append(title)
        for r in items:
            lines.append(f" · {_owner_text(r)}：{r['msg']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _owner_text(r):
    """条目归属：项目 · 批次（有批次时）。"""
    proj = r.get("project") or ""
    bn = r.get("batch_no")
    return f"{proj} · {bn}" if bn and bn not in proj else proj


def count_total(reminders):
    """兼容旧 API：三档合计。"""
    return (len(reminders.get("P0", [])) + len(reminders.get("P1", []))
            + len(reminders.get("P2", [])))


def flatten(all_reminders):
    """{level: [...]} → 已排序的平铺列表。"""
    items = []
    for lv in LEVELS:
        items += list(all_reminders.get(lv, []) or [])
    return sort_reminders(items)


# ── §12.4 提醒中心视图模型 ──

def reminder_center(project_ids=None, today=None, batch_ids=None,
                    enabled_only=True, include_cancelled=False):
    """提醒中心（§12.4）：**按批次分组**，含固定四节
    免箱期倒计时 / 到货通知 / 申报截止 / 依赖等待（+ 其他待办）。

    返回 {
      "today", "totals", "badge",
      "groups": [{
          project_id, project_name, batch_id, batch_no, batch_name, batch_status,
          counts, total, sections: [{key,label,icon,count,items:[...]}]
      }],
      "sections_order": (...)
    }
    """
    import db
    if today is None:
        from services.clock import get_today
        today = get_today()
    elif isinstance(today, str):
        today = _parse(today)

    if batch_ids is not None:
        rows = [db.get_batch(b) for b in batch_ids]
        rows = [b for b in rows if b]
    else:
        if project_ids is None:
            rows = db.get_projects_by_status("Active") + db.get_projects_by_status("Completed")
            project_ids = [p["project_id"] for p in rows]
        rows = []
        for pid in project_ids:
            for b in db.get_batches(pid, include_cancelled=include_cancelled):
                if enabled_only and b["status"] not in ENABLED_STATES:
                    continue
                rows.append(b)

    groups = []
    t_counts = {lv: 0 for lv in LEVELS}
    t_total = 0
    only_empty = []
    for b in rows:
        items = compute_reminders_for_batch(b["batch_id"], today)
        if not items:
            only_empty.append(b)
            continue
        proj = db.get_project(b["project_id"]) or {}
        counts = {lv: len([x for x in items if x["level"] == lv]) for lv in LEVELS}
        for lv in LEVELS:
            t_counts[lv] += counts[lv]
        t_total += len(items)
        groups.append({
            "project_id": b["project_id"],
            "project_name": proj.get("project_name"),
            "batch_id": b["batch_id"], "batch_no": b.get("batch_no"),
            "batch_name": b.get("batch_name"), "batch_status": b.get("status"),
            "planned_date": b.get("planned_date"),
            "total": len(items), "counts": counts,
            "sections": _sections_of(items),
            "reminders": items,
        })

    groups.sort(key=lambda g: (g["batch_status"] == "running" and 0 or 1,
                               str(g["project_name"] or ""), str(g["batch_no"] or "")))
    totals = {**t_counts, "total": t_total, "batches": len(groups),
              "batches_with_todo": len(groups)}
    return {"today": (today.date() if isinstance(today, datetime) else today).isoformat(),
            "groups": groups, "totals": totals, "badge": dict(totals),
            "sections_order": SECTION_ORDER}


def _sections_of(items):
    buckets = {k: [] for k in SECTION_ORDER}
    for it in items:
        buckets[it.get("section", "OTHER")].append(it)
    out = []
    for k in SECTION_ORDER:
        rows = buckets.get(k) or []
        out.append({"key": k, "label": SECTION_LABEL[k], "icon": SECTION_ICON[k],
                    "count": len(rows), "levels": _level_groups(rows),
                    "items": rows})
    return out


def section_counts(batch_id, today=None):
    """单批次四节计数（批次详情页副标题用）。"""
    items = compute_reminders_for_batch(batch_id, today)
    return {s["key"]: s["count"] for s in _sections_of(items)}


def format_reminder_center(data):
    """提醒中心纯文本（离屏/自测可读输出）。"""
    lines = [f"提醒中心 · {data['today']}",
             f"启用中批次待办合计：{data['badge']['total']} 项"
             f"（需处理 {data['badge']['P0']} / 进行中 {data['badge']['P1']}"
             f" / 今日启动 {data['badge']['P2']}）"]
    if not data["groups"]:
        lines.append("暂无待办。")
        return "\n".join(lines)
    for g in data["groups"]:
        lines.append("")
        lines.append(f"【{g['project_name']} · {g['batch_no']}】共 {g['total']} 项")
        for s in g["sections"]:
            if not s["count"]:
                continue
            lines.append(f"  · {s['label']}（{s['count']}）")
            for it in s["items"]:
                lines.append(f"      - {it['msg']}")
    return "\n".join(lines)
