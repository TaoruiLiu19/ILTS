"""
报告聚合层 —— 纯逻辑、无 Qt 依赖，供离线单测。

职责：把 projects / nodes / files / cargo / vessel / op_log 组合为一份
「报告模型」，预览与 Word/txt 导出共享同一模型（同源一致）。
所有检索均为 project_id + 时间范围双条件；「全部项目」也逐项目聚合后合并。

两级口径（《多式联运.md》§11 / T9）：
  · 标题：单批次 → `【项目名 · B01】操作日报/周报`；全项目 → 保持公司日期制式原名
  · 筛选：项目 → 批次 → 客户（全部 / 单项目 / 单批次 / 单客户）
  · 未完成清单：批次列（批次号+名称）+ 客户列 + 柜号列
  · 操作动态：批次维度（scope='project' 的项目级事件批次列显示「—」）
  · 概览统计：双标（项目数 / 批次数）
  · 批次 KPI：按时率、平均周期、单证按时提交率、免箱期超期占比、船期变更影响（D34）
  · 「数据不足」：KPI 分子分母为空时显示「数据不足」，**绝不按 0 统计**（§6.10 D31）
"""

from datetime import date, timedelta

import db
from services.oplog import display_detail

MAX_OVERDUE = 2   # 逾期 ≤2 天 → 黄（超过则红）
MAX_MISSING = 1   # 必填缺失 ≤1 → 黄（超过则红）

HIGH_KW = ["报关", "截关", "截单", "船司", "提单", "AMS", "ISF", "清关", "查验"]
MID_KW = ["集港", "装船", "捆扎", "堆存"]
OVER_WEIGHT_KG = 100_000   # >100t
OVER_SIDE_M = 36.0         # >36m
CONCENTRATE_K = 3          # 下周同日 ≥K 节点 → 集中日


def _parse(s):
    if isinstance(s, date):
        return s
    if not s:
        return None
    y, m, d = str(s).split("-")
    return date(int(y), int(m), int(d))


def _fmt(d):
    return d.strftime("%Y-%m-%d")


def _monthday(d):
    return d.strftime("%m-%d")


# ── 优先级关键词（待办排序） ──

def priority_of(node_name):
    name = node_name or ""
    for kw in HIGH_KW:
        if kw in name:
            return 0
    for kw in MID_KW:
        if kw in name:
            return 1
    return 2


PRIO_LABEL = {0: "高", 1: "中", 2: "常规"}


def week_range(ref_date):
    """ref_date 所在自然周 [周一, 周日]"""
    monday = ref_date - timedelta(days=ref_date.weekday())
    return monday, monday + timedelta(days=6)


def next_week_range(ref_date):
    """ref_date 的下周自然周 [下周一, 下周日]（周报『下周待办』窗口）"""
    monday, _ = week_range(ref_date)
    return monday + timedelta(days=7), monday + timedelta(days=13)


# ── 健康度 ──

def health_level(project, nodes, files, ref_date):
    overdue_days = 0
    for n in nodes:
        if n["status"] == "Done":
            continue
        pe = _parse(n.get("plan_end"))
        if pe and ref_date > pe:
            overdue_days = max(overdue_days, (ref_date - pe).days)
    missing = sum(1 for f in files
                  if f["doc_type"] == "required" and f["status"] != "submitted")
    if overdue_days >= MAX_OVERDUE + 1 or missing >= MAX_MISSING + 1:
        return "红"
    if overdue_days == 0 and missing == 0:
        return "绿"
    return "黄"


def _is_all(v):
    return v in (None, "", "all", "全部")


def _active_filtered(project_filter):
    """按筛选返回报告范围内的项目列表（全部 / 单项目）。

    「全部」= 进行中 + 已取消（D30/T32：全部批次被取消的项目**留在列表**并标注
    「已取消」，只是不进「已完成」页）。已取消项目下默认没有启用批次，
    因此不会污染批次数与各批次表格。
    """
    if _is_all(project_filter):
        return db.get_projects_by_status("Active") + db.get_projects_by_status("Cancelled")
    p = db.get_project(project_filter)
    return [p] if p else []


def _project_names(ids):
    return {pid: (db.get_project(pid) or {}).get("project_name", pid)
            for pid in ids}


# ── 二级（批次）与三级（客户）筛选上下文（§11 T9） ──
#
# 报告口径： 项目 → 批次 → 客户
#   project_filter  : None/'all'/项目 id
#   batch_filter    : None/'all'/批次 id（单批次 → 报告标题带【项目名 · B01】）
#   customer_filter : None/'all'（全部） / '未填写'（无客户角色） / party_id（单客户）

class ReportScope:
    """报告筛选上下文：把「项目 / 批次 / 客户」三维筛选固化下来，
    供各聚合函数与展示层共用，避免各处重复判定。"""

    def __init__(self, project_filter=None, batch_filter=None, customer_filter=None,
                 customs_broker=None, customs_mode=None):
        self.project_filter = project_filter
        self.batch_filter = batch_filter
        self.customer_filter = customer_filter
        # §T30 报关要素维度（报关行 / 报关方式），None = 不限
        self.customs_broker = customs_broker or None
        self.customs_mode = customs_mode or None
        self.single_batch_id = _norm_sel(batch_filter)
        self.single_customer_id = _norm_sel(customer_filter)
        self.customer_unfilled = customer_filter == CUSTOMER_UNFILLED
        self._batch_cache = {}
        self._party_cache = {}
        self._label_cache = {}
        self._customs_cache = {}

    # 批次维度
    def batches(self, project_id):
        """报告范围内的该批次列表（默认排除已取消；§8 取消批次全模块隐藏）。"""
        if project_id not in self._batch_cache:
            self._batch_cache[project_id] = db.get_batches(project_id)
        rows = self._batch_cache[project_id]
        if self.single_batch_id:
            rows = [b for b in rows if b["batch_id"] == self.single_batch_id]
        return rows

    def batch_ids(self, project_id):
        return [b["batch_id"] for b in self.batches(project_id)]

    def allows_batch(self, project_id, batch_id):
        """拒绝范围外的批次，防止越权读到未筛选批次的节点/单证/日志。

        「项目级」行（batch_id 为空，如 op_log scope='project' 的立项/关项事件）
        不属于任何批次，单批次筛选时仍随项目带出（否则报告会漏掉项目级动态）。
        """
        if not self.single_batch_id:
            return True
        if not batch_id:
            return True
        return batch_id == self.single_batch_id

    def nodes(self, project_id, batch_id=None):
        if not self.allows_batch(project_id, batch_id):
            return []
        return db.get_nodes(project_id, batch_id=batch_id)

    def files(self, project_id, batch_id=None):
        if not self.allows_batch(project_id, batch_id):
            return []
        rows = db.get_files(project_id, batch_id=batch_id)
        if not (self.single_customer_id or self.customer_unfilled):
            return rows
        return [f for f in rows if self.customer_ok(f.get("batch_id"))]

    def all_nodes(self, project_id):
        """范围内全部节点（跨批次合并，供健康度/风险/周对比/待办统计）。

        同时受批次筛选与客户筛选约束，保证「同口径」。
        """
        out = []
        for b in self.batches(project_id):
            if not self.batch_ok(b["batch_id"]):
                continue
            out.extend(db.get_nodes(project_id, batch_id=b["batch_id"]))
        return out

    def all_files(self, project_id):
        out = []
        for b in self.batches(project_id):
            if not self.batch_ok(b["batch_id"]):
                continue
            out.extend(self.files(project_id, batch_id=b["batch_id"]))
        return out

    # 客户维度
    def customer_ok(self, batch_id):
        if not (self.single_customer_id or self.customer_unfilled):
            return True
        if not batch_id:
            return False
        name = customer_of(batch_id)
        if self.customer_unfilled:
            return name == CUSTOMER_UNFILLED
        return self._party_id_of(batch_id) == self.single_customer_id

    def _party_id_of(self, batch_id):
        if batch_id not in self._party_cache:
            rows = db.get_batch_parties(batch_id, "CUSTOMER")
            self._party_cache[batch_id] = rows[0]["party_id"] if rows else None
        return self._party_cache[batch_id]

    # §T30 报关要素维度
    def _customs_of(self, batch_id):
        if batch_id not in self._customs_cache:
            try:
                from services import customs_stats as _cs
                self._customs_cache[batch_id] = _cs.customs_of_batch(batch_id)
            except Exception:
                self._customs_cache[batch_id] = {}
        return self._customs_cache[batch_id] or {}

    def customs_ok(self, batch_id):
        if not (self.customs_broker or self.customs_mode):
            return True
        if not batch_id:
            return False
        c = self._customs_of(batch_id)
        if self.customs_broker and (c.get("customs_broker") or None) != self.customs_broker:
            return False
        if self.customs_mode and (c.get("customs_mode") or None) != self.customs_mode:
            return False
        return True

    @property
    def customs_scoped(self):
        return bool(self.customs_broker or self.customs_mode)

    def batch_ok(self, batch_id):
        """批次是否落在客户/报关要素筛选范围内（供概览计数、KPI 取样）。"""
        if not (self.single_batch_id or self.single_customer_id
                or self.customer_unfilled or self.customs_scoped):
            return True
        if self.customs_scoped and not self.customs_ok(batch_id):
            return False
        return self.customer_ok(batch_id)

    @property
    def batch_scoped(self):
        return bool(self.single_batch_id)

    @property
    def customer_scoped(self):
        return bool(self.single_customer_id or self.customer_unfilled)

    def label(self, batch_id):
        """批次号+名称（op_log 的批次列用）。"""
        if not batch_id:
            return "—"
        if batch_id not in self._label_cache:
            self._label_cache[batch_id] = batch_label(db.get_batch(batch_id))
        return self._label_cache[batch_id]


def _norm_sel(v):
    """把下拉框的占位值（全部/空）归一到 None。"""
    return None if _is_all(v) else v


CUSTOMER_UNFILLED = "未填写"


def batch_label(batch):
    """批次列：`批次号+名称`（名称与号码末段重复时只显示一次）。"""
    if not batch:
        return "—"
    no = batch.get("batch_no") or batch.get("batch_id") or "—"
    nm = (batch.get("batch_name") or "").strip()
    if not nm or nm == no or no.endswith("-" + nm):
        return no
    return f"{no} · {nm}"


def customer_of(batch_id, project=None):
    """批次客户（§5.5 D32）：批次级 CUSTOMER 角色 → 项目级默认客户 → 「未填写」。"""
    if batch_id:
        rows = db.get_batch_parties(batch_id, "CUSTOMER")
        if rows:
            p = rows[0]
            return p.get("party_name") or p.get("name_en") or CUSTOMER_UNFILLED
        b = db.get_batch(batch_id)
        if b:
            proj = db.get_project(b["project_id"])
            if proj and proj.get("customer_id"):
                c = db.get_party(proj["customer_id"])
                if c:
                    return c.get("party_name") or CUSTOMER_UNFILLED
    if project and project.get("customer_id"):
        c = db.get_party(project["customer_id"])
        if c:
            return c.get("party_name") or CUSTOMER_UNFILLED
    return CUSTOMER_UNFILLED


def customer_options():
    """客户筛选下拉：全部 / 未填写 / 单客户（按名称排序）。"""
    out = [{"id": None, "name": "全部客户"},
           {"id": CUSTOMER_UNFILLED, "name": CUSTOMER_UNFILLED}]
    for p in db.list_parties():
        out.append({"id": p["party_id"], "name": p["party_name"]})
    return out


def _container_nos(batch_id):
    if not batch_id:
        return []
    return [c.get("container_no") for c in db.get_containers(batch_id) if c.get("container_no")]


def _fmt_rate(hit, total):
    """比率口径：分母为空 → 「数据不足」（绝不按 0 统计，§6.10 D31）。"""
    if not total:
        return NO_DATA, 0, 0
    return f"{hit / total * 100:.0f}%", hit, total


NO_DATA = "数据不足"


# ── 总体概览 ──

def overview(projects, ref_date, scope=None):
    """概览统计：双标（项目数 / 批次数），§11。"""
    scope = scope or ReportScope()
    total = len(projects)
    batch_count = 0
    health = {"绿": 0, "黄": 0, "红": 0}
    nodes_total = nodes_done = 0
    for p in projects:
        pid = p["project_id"]
        nodes = scope.all_nodes(pid)
        files = scope.all_files(pid)
        batch_count += len([b for b in scope.batches(pid) if scope.batch_ok(b["batch_id"])])
        health[health_level(p, nodes, files, ref_date)] += 1
        nodes_total += len(nodes)
        nodes_done += sum(1 for n in nodes
                          if n.get("status") == "Done" or n.get("actual_completion_date"))
    rate = f"{nodes_done / nodes_total * 100:.0f}%" if nodes_total else "—"
    return {
        "project_count": total,
        "batch_count": batch_count,
        "health": health,
        "nodes_total": nodes_total,
        "nodes_done": nodes_done,
        "completion_rate": rate,
    }


# ── 未完成清单 ──

def unfinished(projects, ref_date, scope=None):
    """未完成（已到期未完成）清单：含批次列 / 客户列 / 柜号列（§11）。"""
    scope = scope or ReportScope()
    out = []
    names = _project_names([p["project_id"] for p in projects])
    for p in projects:
        pid = p["project_id"]
        files = scope.all_files(pid)
        missing_by_batch_node = {}
        for f in files:
            if f["doc_type"] == "required" and f["status"] != "submitted" \
                    and f.get("node_id") is not None:
                missing_by_batch_node.setdefault(
                    (f.get("batch_id"), f["node_id"]), []).append(f["doc_name"])
        for b in scope.batches(pid):
            bid = b["batch_id"]
            if not scope.batch_ok(bid):        # 客户筛选（§5.5 D32）
                continue
            boxes = "、".join(_container_nos(bid)) or "—"
            cust = customer_of(bid, p)
            for n in db.get_nodes(pid, batch_id=bid):
                if n["status"] == "Done":
                    continue
                pe = _parse(n.get("plan_end"))
                if not pe or pe > ref_date:
                    continue
                od = (ref_date - pe).days
                out.append({
                    "project": names.get(pid, pid),
                    "batch_id": bid,
                    "batch": batch_label(b),
                    "customer": cust,
                    "container": boxes,
                    "node_id": n["node_id"],
                    "node_name": n["node_name"],
                    "plan_end": n["plan_end"],
                    "overdue_days": od,
                    "missing": missing_by_batch_node.get((bid, n["node_id"]), []),
                })
    out.sort(key=lambda x: -x["overdue_days"])
    return out


# ── 操作动态 ──

_KIND_LABEL = {
    "project_create": "新建项目", "project_close": "项目完结",
    "node_shift": "推迟/提前", "node_unshift": "撤销位移", "node_done": "自动完成",
    "file_submit": "提交单证", "file_withdraw": "撤交单证",
    "vessel_position": "船位登记", "cargo_edit": "货物变更",
    # 批次化（1A/1C）后新增的动作
    "batch_create": "新建批次", "batch_edit": "批次变更",
    "batch_status": "批次状态", "batch_complete": "确认完成",
    "batch_close": "关闭批次", "batch_cancel": "取消批次", "batch_restore": "恢复批次",
    "batch_copy": "复制批次", "batch_actual_override": "实际值覆盖",
    "route_change": "换线", "batch_schedule_change": "船期变更",
    "schedule_recompute": "计划重算", "party_bind": "客户绑定",
    "file_dependency_block": "依赖阻断",
}


def _activity_rows(project_ids, start, end, names, scope=None):
    """区间内所有日志（逐项目聚合后合并，跨项目不混行）。

    §11：op_log 增加批次维度；`scope='project'` 的项目级事件批次列显示「—」。
    """
    scope = scope or ReportScope()
    rows = []
    for pid in project_ids:
        for r in db.get_op_log_range(pid, start=start, end=end):
            if not scope.allows_batch(pid, r.get("batch_id")):
                continue
            if scope.customer_scoped and not scope.customer_ok(r.get("batch_id")):
                continue
            rows.append(r)
    rows.sort(key=lambda r: r["created_at"])
    enriched = []
    for r in rows:
        project_scoped = (r.get("scope") == "project") or not r.get("batch_id")
        enriched.append({
            "project_id": r["project_id"],
            "project": names.get(r["project_id"], r["project_id"]),
            "batch_id": r.get("batch_id"),
            "batch": "—" if project_scoped else scope.label(r.get("batch_id")),
            "project_scoped": project_scoped,
            "kind": r["kind"],
            "kind_label": _KIND_LABEL.get(r["kind"], r["kind"]),
            "subject": r["subject"] or "",
            "detail": display_detail(r["detail"] or ""),
            "created_at": r["created_at"],
            "date": r["created_at"][:10],
            "hour": r["created_at"][11:13],
        })
    return _converge_file_ops(enriched)


def _converge_file_ops(items):
    """
    单证类收敛：同一 (项目, 单证, 日期) 只保留当天最后一条提交/撤销。
    数据库本身已按「同项目·同单证·同日一行」落库，这里再做一次防御性收敛，
    保证报告「操作动态」不会因历史库数据重复而重复展示；**跨天不折叠**。
    """
    if not items:
        return items
    last = {}
    for it in items:
        if it["kind"] in ("file_submit", "file_withdraw"):
            last[(it["project_id"], it["subject"], it["date"])] = it
    out = []
    placed = set()
    for it in items:
        if it["kind"] in ("file_submit", "file_withdraw"):
            key = (it["project_id"], it["subject"], it["date"])
            if key in placed:
                continue
            placed.add(key)
            out.append(last[key])
        else:
            out.append(it)
    out.sort(key=lambda r: r["created_at"])
    return out


def last_file_states(projects, scope=None):
    """每张单证的**最新状态**（跨天取最后一次提交/撤销）。

    数据来源：`files.status/submitted_date`（当前态）+ `op_log` 最后一条单证动作（时间/动作）。
    返回按项目分组的最新状态行，供报告「单证最新状态」板块（含批次维度）。
    """
    scope = scope or ReportScope()
    out = []
    for p in projects:
        pid = p["project_id"]
        pname = p.get("project_name") or pid
        last = db.last_file_actions(pid)
        for b in scope.batches(pid):
            if not scope.batch_ok(b["batch_id"]):
                continue
            for f in scope.files(pid, b["batch_id"]):
                act = last.get(f["doc_name"])
                submitted = f.get("status") == "submitted"
                if act:
                    kind_label = "提交" if act["kind"] == "file_submit" else "撤销"
                    at = act["created_at"]
                else:
                    # 老数据无日志：退回单证自身时间戳
                    kind_label = "提交" if submitted else "—"
                    at = (f.get("submitted_date") or "")[:16] if submitted else ""
                out.append({
                    "project": pname,
                    "batch": batch_label(b),
                    "node_id": f.get("node_id"),
                    "doc_name": f["doc_name"],
                    "doc_type": f["doc_type"],
                    "state": "已提交" if submitted else "未提交",
                    "action": kind_label,
                    "at": at,
                })
    return out


def activity_daily(projects, ref_date, names, scope=None):
    return _activity_rows([p["project_id"] for p in projects],
                          ref_date.strftime("%Y-%m-%d 00:00"),
                          ref_date.strftime("%Y-%m-%d 23:59"), names, scope)


def activity_weekly(projects, week_start, week_end, names, scope=None):
    """weekly raw rows（供按天分组）。"""
    return _activity_rows([p["project_id"] for p in projects],
                          week_start.strftime("%Y-%m-%d 00:00"),
                          week_end.strftime("%Y-%m-%d 23:59"), names, scope)


# ── 待办（日报：下一个工作日；周报：下周自然周） ──

def _todo_nodes(projects, wanted_days, scope=None):
    """wanted_days: [date,...] 期望 plan_start 落在这些天；返回 [ (day, node, project) ]"""
    scope = scope or ReportScope()
    wanted = {_fmt(d) for d in wanted_days}
    out = []
    for p in projects:
        pid = p["project_id"]
        for n in scope.all_nodes(pid):
            if n["status"] == "Done":
                continue
            if n.get("plan_start") in wanted:
                out.append((n["plan_start"], n, p))
    out.sort(key=lambda x: (x[0], priority_of(x[1]["node_name"]), x[1]["node_id"]))
    return out


def _pending_docs_by_node(projects, scope=None):
    """{(project_id, batch_id): {node_id: [未提交的必填单证名, ...]}}（待办表格的「单证」列）"""
    scope = scope or ReportScope()
    out = {}
    for p in projects:
        pid = p["project_id"]
        for b in scope.batches(pid):
            if not scope.batch_ok(b["batch_id"]):
                continue
            by_node = {}
            for f in scope.files(pid, b["batch_id"]):
                if f.get("doc_type") != "required":
                    continue
                if f.get("status") == "submitted":
                    continue
                nid = f.get("node_id")
                if nid is None:
                    continue
                by_node.setdefault(nid, []).append(f["doc_name"])
            out[(pid, b["batch_id"])] = by_node
    return out


def _todo_item(day_s, n, p, names, pending, label_fn, scope=None):
    """label_fn 接收 date 对象，返回该行的日期展示文案。"""
    scope = scope or ReportScope()
    bid = n.get("batch_id")
    docs = (pending.get((p["project_id"], bid)) or {}).get(n["node_id"]) or []
    prio = priority_of(n["node_name"])
    return {
        "day": day_s, "day_label": label_fn(_parse(day_s)),
        "project": names.get(p["project_id"], p["project_id"]),
        "batch": scope.label(bid),
        "node": f"节点{n['node_id']} {n['node_name']}",
        "prio": prio,
        "prio_label": PRIO_LABEL[prio],
        "docs": "、".join(docs) if docs else "—",
    }


def todo_daily(projects, ref_date, scope=None):
    scope = scope or ReportScope()
    names = _project_names([p["project_id"] for p in projects])
    pending = _pending_docs_by_node(projects, scope)
    tomorrow = ref_date + timedelta(days=1)
    days = [tomorrow]
    warning = None
    if tomorrow.weekday() in (5, 6):          # 明日为周六/日 → 周末提前预警
        next_mon = ref_date + timedelta((0 - ref_date.weekday()) % 7 or 7)
        days.append(next_mon)
        warning = ("明日为周末，已并入下周一计划，建议今日提前处理")
    items = [_todo_item(day_s, n, p, names, pending, _monthday, scope)
             for day_s, n, p in _todo_nodes(projects, days, scope)]
    return {"items": items, "warning": warning}


def todo_weekly(projects, ref_date, scope=None):
    scope = scope or ReportScope()
    names = _project_names([p["project_id"] for p in projects])
    pending = _pending_docs_by_node(projects, scope)
    ws, we = next_week_range(ref_date)
    items = [_todo_item(day_s, n, p, names, pending,
                        lambda s: _parse(s).strftime("%m-%d %a"), scope)
             for day_s, n, p in _todo_nodes(projects, [ws + timedelta(days=i) for i in range(7)],
                                            scope)]
    return {"items": items, "warning": None}


# ── 风险（实时读当前 cargo/vessel 值判定） ──

def risks(projects, ref_date, weekly=True, scope=None):
    scope = scope or ReportScope()
    out = []
    # ① 超限：实时读 cargo 当前值
    for p in projects:
        for b in scope.batches(p["project_id"]):
            if not scope.batch_ok(b["batch_id"]):
                continue
            for it in db.get_cargo_items(p["project_id"], batch_id=b["batch_id"]):
                w = it.get("weight_kg") or 0
                side = max([it.get("dim_l") or 0, it.get("dim_w") or 0, it.get("dim_h") or 0])
                if it.get("over_flag") or w > OVER_WEIGHT_KG or side > OVER_SIDE_M:
                    out.append(f"因 {it['item_name']} 超限，需协调特种车/吊装能力")
                    break
    if not weekly:
        return out
    # ② 下周多任务集中日
    ws, we = next_week_range(ref_date)
    by_day = {}
    for p in projects:
        for n in scope.all_nodes(p["project_id"]):
            ps = n.get("plan_start")
            if ws.strftime("%Y-%m-%d") <= ps <= we.strftime("%Y-%m-%d") \
                    and n["status"] != "Done":
                by_day.setdefault(ps, 0)
                by_day[ps] += 1
    for day, cnt in sorted(by_day.items()):
        if cnt >= CONCENTRATE_K:
            out.append(f"多任务集中日（{_monthday(_parse(day))}）需提前排布资源")
    # ③ 集中到港（下周境外段 plan_start 集中）
    oversea_by_day = {}
    for p in projects:
        for n in scope.all_nodes(p["project_id"]):
            ps = n.get("plan_start")
            if ws.strftime("%Y-%m-%d") <= ps <= we.strftime("%Y-%m-%d") \
                    and n.get("area") == "OVERSEA":
                oversea_by_day.setdefault(ps, 0)
                oversea_by_day[ps] += 1
    for day, cnt in oversea_by_day.items():
        if cnt >= 2:
            out.append(f"（{_monthday(_parse(day))}）集中到港，注意压港/堆存风险")
    return out


# ── 周对比 ──

def _week_metrics(projects, ws, we, scope=None):
    scope = scope or ReportScope()
    overdue = 0
    missing = 0
    done_in_week = 0
    planned = 0
    for p in projects:
        pid = p["project_id"]
        nodes = scope.all_nodes(pid)
        files = scope.all_files(pid)
        overdue += sum(1 for n in nodes
                       if n["status"] != "Done" and _parse(n.get("plan_end"))
                       and we > _parse(n["plan_end"]))
        missing += sum(1 for f in files
                       if f["doc_type"] == "required" and f["status"] != "submitted")
        planned += sum(1 for n in nodes
                       if n.get("plan_start") and
                       ws.strftime("%Y-%m-%d") <= n["plan_start"] <= we.strftime("%Y-%m-%d"))
        done_in_week += sum(1 for n in nodes if (n.get("actual_completion_date")
                                                 and ws.strftime("%Y-%m-%d")
                                                 <= n["actual_completion_date"]
                                                 <= we.strftime("%Y-%m-%d")))
    return overdue, missing, planned, done_in_week


def weekly_compare(projects, ref_date, scope=None):
    """周对比：**同口径双标**（同样按项目/批次/客户筛选取样，§11）。"""
    scope = scope or ReportScope()
    cur_ws, cur_we = week_range(ref_date)
    prev_ws, prev_we = cur_ws - timedelta(days=7), cur_ws - timedelta(days=1)
    # 前一周有无「数据痕迹」：操作日志或节点完成（含计划节点完成），否则视为首份
    prev_rows = activity_weekly(projects, prev_ws, prev_we, {}, scope)
    prev_done = 0
    for p in projects:
        prev_done += sum(1 for n in scope.all_nodes(p["project_id"])
                         if (n.get("actual_completion_date")
                             and prev_ws.strftime("%Y-%m-%d")
                             <= n["actual_completion_date"] <= prev_we.strftime("%Y-%m-%d")))
    if not prev_rows and prev_done == 0:
        return None  # 调用方显示「前一周无数据，本次为首份周报」

    cur = _week_metrics(projects, cur_ws, cur_we, scope)
    prev = _week_metrics(projects, prev_ws, prev_we, scope)
    labels = ["本周完成节点", "本周待办节点", "逾期节点", "单证缺失"]
    rows = []
    for lbl, c, pr in zip(labels, cur, prev):
        sym = "↑" if c > pr else ("↓" if c < pr else "—")
        rows.append((lbl, pr, c, sym))
    # 改善/恶化结论
    done_sym = "改善" if cur[3] >= prev[3] else "回落"
    bad = "恶化" if (cur[0] > prev[0] or cur[2] > prev[2]) else "保持"
    note = f"本周完成节点 {done_sym}，逾期/缺失 {bad}"
    return {"rows": rows, "note": note,
            "scope": {"projects": len(projects),
                      "batches": sum(len(scope.batches(p["project_id"])) for p in projects)}}


# ── 批次 KPI（§11 / §5.4 D34 / T29） ──
#
# 口径（全部基于 §5.3 计划日期结果与 §6.10 actual_* 字段，报告不自行推算日期）：
#   ① 批次按时率      = actual_eta ≤ batch_routes.eta 的批次 ÷ 同时有 actual_eta 与 eta 的批次
#   ② 批次平均周期    = mean(actual_delivery − actual_etd)，仅取两者皆有值的批次
#   ③ 单证按时提交率  = status=submitted 且 submitted_date ≤ due_date 的必填单证 ÷ 有 due_date 的必填单证
#   ④ 免箱期超期占比  = 有 free_detention_until 且 empty_returned_at 晚于该日期的批次 ÷ 两者皆有值的批次
#   ⑤ 船期变更（D34）= batch_schedule_changes 条数 / 平均影响天数(max(|ΔETD|,|ΔETA|)) /
#                      变更后新增逾期节点数（plan_end 已过仍未完成，且该变更把计划往后推）
# 任何一项的取样集合为空 → 「数据不足」，不按 0 统计（§6.10 D31）。

def _batch_kpi_rows(projects, ref_date, scope):
    rows = []
    for p in projects:
        pid = p["project_id"]
        pname = p.get("project_name") or pid
        for b in scope.batches(pid):
            if not scope.batch_ok(b["batch_id"]):
                continue
            route = db.get_route(b["batch_id"]) or {}
            files = scope.files(pid, b["batch_id"])
            req = [f for f in files
                   if f.get("doc_type") == "required" and f.get("due_date")]
            files_ontime = sum(1 for f in req
                               if f.get("status") == "submitted"
                               and (f.get("submitted_date") or "")[:10] <= f["due_date"])
            rows.append({
                "project": pname,
                "batch_id": b["batch_id"],
                "batch": batch_label(b),
                "customer": customer_of(b["batch_id"], p),
                "status": b.get("status"),
                "etd": route.get("etd"),
                "eta": route.get("eta"),
                "actual_etd": b.get("actual_etd"),
                "actual_eta": b.get("actual_eta"),
                "actual_delivery": b.get("actual_delivery"),
                "empty_returned_at": b.get("empty_returned_at"),
                "free_detention_until": route.get("free_detention_until"),
                "files_required": len(req),
                "files_ontime": files_ontime,
                "schedule_changes": 0,
                "avg_impact_days": None,
                "new_overdue": 0,
            })
    return rows


def _apply_schedule_change_kpi(rows, ref_date):
    """D34 KPI：船期变更次数 / 平均影响天数 / 变更后新增逾期节点数。"""
    for r in rows:
        bid = r["batch_id"]
        changes = db.get_schedule_changes(bid, limit=500)
        r["schedule_changes"] = len(changes)
        impacts = []
        pushed_to = []
        for c in changes:
            d_etd = _day_delta(c.get("old_etd"), c.get("new_etd"))
            d_eta = _day_delta(c.get("old_eta"), c.get("new_eta"))
            deltas = [abs(x) for x in (d_etd, d_eta) if x is not None]
            if deltas:
                # 单次变更的「影响天数」= max(|ΔETD|, |ΔETA|)（A/B/C/D 四类统一口径）
                impacts.append(max(deltas))
            push = max([x for x in (d_etd, d_eta) if x is not None] + [0])
            if push > 0:
                pushed_to.append(push)
        r["avg_impact_days"] = (round(sum(impacts) / len(impacts), 1) if impacts else None)
        # 变更后新增逾期：该批次确有「往后推」的船期变更时，统计当前仍未完成且
        # 计划结束日已过参考日的节点数（节点日期只来自 §5.3 重算结果，报告不自行推算）
        if not pushed_to:
            r["new_overdue"] = 0
            continue
        r["new_overdue"] = sum(
            1 for n in db.get_nodes_by_batch(bid)
            if n.get("status") != "Done" and not n.get("actual_completion_date")
            and (lambda pe: bool(pe) and pe <= ref_date)(_parse(n.get("plan_end"))))
    return rows


def _day_delta(a, b):
    """b − a 的天数；任一为空返回 None（数据不足，不按 0 计）。"""
    da, dbb = _parse(a), _parse(b)
    if da is None or dbb is None:
        return None
    return (dbb - da).days


def batch_kpi(projects, ref_date, scope=None):
    """批次 KPI 汇总：返回 {items:[...], overall:{...}}。"""
    scope = scope or ReportScope()
    rows = _apply_schedule_change_kpi(_batch_kpi_rows(projects, ref_date, scope), ref_date)

    # ① 按时率：actual_eta ≤ eta
    eta_ok = [r for r in rows if r["actual_eta"] and r["eta"]]
    ontime_hit = sum(1 for r in eta_ok
                     if _day_delta(r["eta"], r["actual_eta"]) is not None
                     and _day_delta(r["eta"], r["actual_eta"]) <= 0)
    # ② 平均周期：actual_etd → actual_delivery
    cyc = [(_day_delta(r["actual_etd"], r["actual_delivery"]), r)
           for r in rows if r["actual_etd"] and r["actual_delivery"]]
    cyc_vals = [c for c, _ in cyc if c is not None]
    # ③ 单证按时提交率
    f_total = sum(r["files_required"] for r in rows)
    f_hit = sum(r["files_ontime"] for r in rows)
    # ④ 免箱期超期占比
    det = [r for r in rows if r["free_detention_until"] and r["empty_returned_at"]]
    det_over = sum(1 for r in det
                   if _day_delta(r["free_detention_until"], r["empty_returned_at"]) is not None
                   and _day_delta(r["free_detention_until"], r["empty_returned_at"]) > 0)
    # ⑤ 船期变更
    chg_impacts = [r["avg_impact_days"] for r in rows if r["avg_impact_days"] is not None]
    chg_new_overdue = sum(r["new_overdue"] for r in rows if r["schedule_changes"])

    rate_txt, hit, total = _fmt_rate(ontime_hit, len(eta_ok))
    cyc_txt = f"{round(sum(cyc_vals) / len(cyc_vals), 1)} 天" if cyc_vals else NO_DATA
    file_txt, fh, ft = _fmt_rate(f_hit, f_total)
    det_txt, dh, dt = _fmt_rate(det_over, len(det))

    overall = {
        "batch_total": len(rows),
        # ① 批次按时率
        "ontime_rate": rate_txt,
        "ontime_hit": hit,
        "ontime_sample": total,
        "ontime_nodata": total == 0,
        # ② 批次平均周期
        "avg_cycle": cyc_txt,
        "avg_cycle_sample": len(cyc_vals),
        "avg_cycle_nodata": not cyc_vals,
        # ③ 单证按时提交率
        "file_ontime_rate": file_txt,
        "file_ontime_hit": fh,
        "file_ontime_sample": ft,
        "file_ontime_nodata": ft == 0,
        # ④ 免箱期超期占比
        "detention_over_rate": det_txt,
        "detention_over_hit": dh,
        "detention_over_sample": dt,
        "detention_over_nodata": dt == 0,
        # ⑤ D34 船期变更
        "schedule_change_count": sum(r["schedule_changes"] for r in rows),
        "avg_impact_days": (round(sum(chg_impacts) / len(chg_impacts), 1)
                            if chg_impacts else NO_DATA),
        "avg_impact_nodata": not chg_impacts,
        "new_overdue_count": chg_new_overdue,
    }
    return {"items": rows, "overall": overall}


# ── 按客户分组（§5.5 D32 / T35：报告按客户筛选与分组统计） ──

# ── 报关要素统计（T30：报关行/报关方式可筛选、可统计） ──

def customs_stats(projects, scope=None):
    """报告范围内的报关要素统计：按报关方式 / 报关行分组计数。"""
    batch_ids = []
    for p in projects or []:
        for b in (scope.batches(p["project_id"]) if scope else db.get_batches(p["project_id"])):
            if scope and not scope.batch_ok(b["batch_id"]):
                continue
            batch_ids.append(b["batch_id"])
    try:
        from services import customs_stats as _cs
        s = _cs.customs_mode_stats(batch_ids)
    except Exception:
        return {"by_mode": [], "by_broker": [], "total_batches": 0, "missing_customs": 0}
    return s


def _customs_filter_label(scope):
    if not (scope and scope.customs_scoped):
        return None
    try:
        from services import customs_stats as _cs
        return _cs.customs_filter_label(scope.customs_broker, scope.customs_mode)
    except Exception:
        return None


def customer_groups(projects, ref_date, scope=None):
    """客户维度分组统计：项目数 / 批次数 / 未完成节点数 / 逾期节点数。

    客户为空按「未填写」归组（§5.5 规则 3）。
    """
    scope = scope or ReportScope()
    groups = {}
    for p in projects:
        pid = p["project_id"]
        pname = p.get("project_name") or pid
        for b in scope.batches(pid):
            if not scope.batch_ok(b["batch_id"]):
                continue
            name = customer_of(b["batch_id"], p)
            g = groups.setdefault(name, {"customer": name, "projects": set(),
                                         "batches": 0, "nodes_total": 0,
                                         "nodes_done": 0, "overdue": 0})
            g["projects"].add(pname)
            g["batches"] += 1
            for n in db.get_nodes(pid, batch_id=b["batch_id"]):
                g["nodes_total"] += 1
                if n.get("status") == "Done" or n.get("actual_completion_date"):
                    g["nodes_done"] += 1
                    continue
                pe = _parse(n.get("plan_end"))
                if pe and pe <= ref_date:
                    g["overdue"] += 1
    out = []
    for g in groups.values():
        g["projects"] = len(g["projects"])
        g["completion"] = (f"{g['nodes_done'] / g['nodes_total'] * 100:.0f}%"
                           if g["nodes_total"] else "—")
        out.append(g)
    out.sort(key=lambda x: (x["customer"] == CUSTOMER_UNFILLED, x["customer"]))
    return out


# ── 智能摘要（规则模板） ──

def summary_rules(ov, unfin, risks_items, todo_items, weekly):
    parts = []
    parts.append(f"本期共操作 {ov['project_count']} 个项目 / {ov.get('batch_count', 0)} 个批次")
    if ov["health"]["红"]:
        parts.append(f"{ov['health']['红']} 个出现延误预警")
    missing = sum(len(x["missing"]) for x in unfin)
    if missing:
        parts.append(f"{missing} 项必填单证缺失")
    if risks_items:
        parts.append(f"{len(risks_items)} 项风险提示")
    head = "，".join(parts) + "。"
    tail = []
    if unfin:
        tail.append(f"需优先处理「{unfin[0]['project']}」")
    if todo_items:
        tail.append(f"下一个工作日待办 {len(todo_items)} 项")
    tail_s = "；".join(tail) if tail else ""
    return head + tail_s


# ── 组装 ──

def _range_text(kind, ref_date, start_s, end_s):
    if kind == "daily":
        return f"日报周期：{_fmt(ref_date)}"
    return f"周报周期：{start_s} ~ {end_s}"


def build_report(kind, ref_date, project_filter=None, brief=False,
                 report_no=None, generated_at=None,
                 batch_filter=None, customer_filter=None,
                 customs_broker=None, customs_mode=None):
    """组合完整报告模型。report_no 由导出层传入（预览可传 peek，不占流水）。

    两级口径（§11）：
      · `batch_filter` 指定单批次 → 标题 `【项目名 · B01】操作日报/周报`，
        文件名由 report_exporter 追加 `_B01`；未指定（全项目）→ 保持原名。
      · `customer_filter` 支持「全部 / 未填写 / 单客户」（§5.5 D32）。
    """
    from services.clock import get_today, get_now_str
    ref_date = _parse(ref_date)
    today = get_today()
    if generated_at is None:
        generated_at = get_now_str("%Y-%m-%d %H:%M")

    single_project = None
    if not _is_all(project_filter):
        sp = db.get_project(project_filter)
        single_project = sp["project_name"] if sp else None

    projects = _active_filtered(project_filter)
    names = _project_names([p["project_id"] for p in projects])

    # 批次筛选：必须落在报告范围内的项目下，避免越权读取别的项目
    scope = ReportScope(project_filter=project_filter,
                        batch_filter=batch_filter,
                        customer_filter=customer_filter,
                        customs_broker=customs_broker,
                        customs_mode=customs_mode)
    bit = _resolve_batch_scope(projects, scope)
    single_batch_label = batch_label(bit) if bit else None
    # §11：只要筛选落到「单批次」，标题就必须带【项目名 · 批次号】。
    # 仅选批次而未选项目时（项目筛选=全部），项目名由该批次反解，
    # 否则标题会退回公司日期制式，丢掉「两级口径」。
    if bit and not single_project:
        bproj = db.get_project(bit.get("project_id"))
        single_project = bproj["project_name"] if bproj else None

    # 周期
    if kind == "weekly":
        ws, we = week_range(ref_date)
        start_s, end_s = _fmt(ws), _fmt(we)
        period_label = f"{_monthday(ws)} ~ {_monthday(we)}"
    else:
        ws = we = ref_date
        start_s = end_s = _fmt(ref_date)
        period_label = _fmt(ref_date)

    # 标题（§11）：单批次 → 【项目名 · B01】操作日报/周报；全项目 → 公司日期制式原名
    date_txt = (f"{ref_date.strftime('%Y')}年{ref_date.strftime('%m')}月"
                f"{ref_date.strftime('%d')}日")
    if kind == "weekly":
        start_date_txt = (f"{ws.strftime('%Y')}年{ws.strftime('%m')}月"
                          f"{ws.strftime('%d')}日")
        end_date_txt = (f"{we.strftime('%Y')}年{we.strftime('%m')}月"
                        f"{we.strftime('%d')}日")
        base_title = f"{start_date_txt}-{end_date_txt}物流操作周报"
    else:
        base_title = f"{date_txt}物流操作日报"
    if bit and single_project:
        # §11：标题用批次号（规则 `{项目号}-B01`，T13），与「未完成清单」批次列（号+名）区分
        batch_tag = bit.get("batch_no") or bit.get("batch_name") or single_batch_label
        title = f"【{single_project} · {batch_tag}】物流操作" + \
                ("周报" if kind == "weekly" else "日报")
    else:
        title = base_title

    ov = overview(projects, ref_date, scope)
    unfin = unfinished(projects, ref_date, scope)

    act = act_rows = None
    if kind == "daily":
        act = activity_daily(projects, ref_date, names, scope)
        todo = todo_daily(projects, ref_date, scope)
        acts = todo["warning"]
    else:
        act_rows = activity_weekly(projects, ws, we, names, scope)
        todo = todo_weekly(projects, ref_date, scope)
        acts = None

    risk_items = risks(projects, ref_date, weekly=(kind == "weekly"), scope=scope)
    kpi = batch_kpi(projects, ref_date, scope)
    groups = customer_groups(projects, ref_date, scope)

    filters = _filter_label(scope, single_project, single_batch_label)
    model = {
        "kind": kind,
        "title": title,
        "report_no": report_no or "RPT-·····",
        "ref_date": _fmt(ref_date),
        "generated_at": generated_at,
        "data_cutoff": generated_at,
        "historical": _fmt(ref_date) != _fmt(today),
        "period": {"start": start_s, "end": end_s, "label": period_label},
        "range_text": _range_text(kind, ref_date, start_s, end_s),
        "project_filter_label": filters,
        "project_count": len(projects),
        "batch_count": ov["batch_count"],
        "batch_filter": bit["batch_id"] if bit else None,
        "batch_filter_label": single_batch_label,
        "customer_filter": scope.single_customer_id or (
            CUSTOMER_UNFILLED if scope.customer_unfilled else None),
        "brief": brief,
        "overview": ov,
        "unfinished": unfin,
        "batch_kpi": kpi,
        "customer_groups": groups,
        "customs_stats": customs_stats(projects, scope),
        "customs_filter_label": _customs_filter_label(scope),
        "activity_daily": act if kind == "daily" else None,
        "activity_weekly": act_rows if kind == "weekly" else None,
        "activity_warning": acts,
        "file_states": last_file_states(projects, scope),
        "todo": todo,
        "risks": risk_items,
        "weekly_compare": (weekly_compare(projects, ref_date, scope)
                           if kind == "weekly" else None),
        "summary": summary_rules(ov, unfin, risk_items, todo["items"], kind == "weekly"),
    }
    return model


def _resolve_batch_scope(projects, scope):
    """校验 batch_filter：仅接受报告范围内项目的批次；返回批次行或 None。"""
    if not scope.single_batch_id:
        return None
    for p in projects:
        for b in db.get_batches(p["project_id"], include_cancelled=True):
            if b["batch_id"] == scope.single_batch_id:
                scope._batch_cache.setdefault(p["project_id"], db.get_batches(p["project_id"]))
                return b
    return db.get_batch(scope.single_batch_id)


def _filter_label(scope, single_project, single_batch_label):
    """报告头部「项目:xxx」文案：项目 → 批次 → 客户 三级可读呈现。"""
    parts = [single_project or "全部项目"]
    if single_batch_label:
        parts.append(single_batch_label)
    if scope.customer_unfilled:
        parts.append(CUSTOMER_UNFILLED)
    elif scope.single_customer_id:
        p = db.get_party(scope.single_customer_id)
        parts.append((p or {}).get("party_name") or scope.single_customer_id)
    return " · ".join(parts)


# ── 同源排版中间表示（blocks）──
# 预览 HTML 与 Word/txt 导出都消费此中间表示，保证「所见即所得」。

_CN_NUM = "〇一二三四五六七八九十"


def _cn_no(n):
    """章节序号 → 中文数字（1→一 … 10→十、11→十一）"""
    if n <= 10:
        return _CN_NUM[n]
    if n < 20:
        return "十" + _CN_NUM[n - 10]
    return _CN_NUM[n // 10] + "十" + (_CN_NUM[n % 10] if n % 10 else "")


def blocks(model):
    """把报告模型转为统一块列表，供 html / docx / txt 三者渲染。"""
    b = []
    m = model
    _sec = [0]

    def _h(title):
        """自动递增章节号：条件板块被跳过时不会出现跳号（如无风险时周报不再从六跳到八）"""
        _sec[0] += 1
        b.append({"t": "h", "text": f"{_cn_no(_sec[0])}、{title}"})

    # 头部
    b.append({"t": "title", "text": m["title"]})
    b.append({"t": "para", "text":
              f"{m['report_no']} · 生成于 {m['generated_at']} · "
              f"数据截止至 {m['data_cutoff']}"})
    b.append({"t": "para", "text": f"范围:{m['range_text']} · 项目:{m['project_filter_label']}"})
    if m["historical"]:
        b.append({"t": "note",
                  "text": f"本报告为历史回溯数据（{m['ref_date']}）"})

    # 总体概览（§11 双标：项目数 / 批次数）
    ov = m["overview"]
    _h("总体概览")
    b.append({"t": "para", "text":
              f"项目数 {ov['project_count']} 个 · 批次数 {ov.get('batch_count', 0)} 个"})
    b.append({"t": "para", "text":
              f"节点完成率：{ov['nodes_done']}/{ov['nodes_total']} = {ov['completion_rate']}"})

    # 未完成清单（含批次列 / 客户列 / 柜号列）
    _h("未完成清单")
    if not m["unfinished"]:
        b.append({"t": "para", "text": "暂无未完成且已到期的节点。"})
    else:
        unfin_blk = {"t": "table",
                     "header": ["项目", "批次", "客户", "柜号", "节点",
                                "计划结束", "逾期(天)", "缺失单证"],
                     "rows": [[
                         x["project"],
                         x.get("batch") or "—",
                         x.get("customer") or CUSTOMER_UNFILLED,
                         x.get("container") or "—",
                         f"节点{x['node_id']} {x['node_name']}",
                         x["plan_end"],
                         str(x["overdue_days"]),
                         "、".join(x["missing"]) if x["missing"] else "—",
                     ] for x in m["unfinished"]],
                     "red": {i for i, x in enumerate(m["unfinished"]) if x["missing"]}}
        b.append(unfin_blk)

    # 批次 KPI（§11 / §5.4 D34 / T29）
    kpi = m.get("batch_kpi") or {}
    ko = kpi.get("overall") or {}
    if ko:
        _h("批次 KPI")
        b.append({"t": "para", "text":
                  f"① 批次按时率（actual_eta ≤ eta）：{ko['ontime_rate']}"
                  + ("" if ko["ontime_nodata"]
                     else f"（{ko['ontime_hit']}/{ko['ontime_sample']} 个批次）")
                  + f"　② 批次平均周期（actual_etd → actual_delivery）：{ko['avg_cycle']}"
                  + ("" if ko["avg_cycle_nodata"]
                     else f"（{ko['avg_cycle_sample']} 个批次样本）")})
        b.append({"t": "para", "text":
                  f"③ 单证按时提交率（submitted 且 submitted_date ≤ due_date）："
                  f"{ko['file_ontime_rate']}"
                  + ("" if ko["file_ontime_nodata"]
                     else f"（{ko['file_ontime_hit']}/{ko['file_ontime_sample']} 张必填单证）")
                  + f"　④ 免箱期超期批次占比：{ko['detention_over_rate']}"
                  + ("" if ko["detention_over_nodata"]
                     else f"（{ko['detention_over_hit']}/{ko['detention_over_sample']} 个批次）")})
        b.append({"t": "para", "text":
                  f"⑤ 船期变更（D34）：变更次数 {ko['schedule_change_count']} 次 · "
                  f"平均影响天数 {ko['avg_impact_days']} · "
                  f"变更后新增逾期节点 {ko['new_overdue_count']} 个"})
        if any(ko.get(k) for k in ("ontime_nodata", "avg_cycle_nodata",
                                   "file_ontime_nodata", "detention_over_nodata",
                                   "avg_impact_nodata")):
            b.append({"t": "note", "text":
                      "标「数据不足」的 KPI 表示取样集合为空（缺 actual_etd/actual_eta/"
                      "actual_delivery/empty_returned_at/单证 due_date 或船期变更记录），"
                      "按 §6.10 不按 0 统计。"})
        items = kpi.get("items") or []
        if items:
            b.append({"t": "table",
                      "header": ["项目", "批次", "客户", "实际 ETD", "实际 ETA",
                                 "实际交付", "免箱期截止", "还箱日", "船期变更(次)",
                                 "平均影响(天)", "变更后新增逾期"],
                      "rows": [[
                          r["project"], r["batch"], r["customer"],
                          r["actual_etd"] or "—", r["actual_eta"] or "—",
                          r["actual_delivery"] or "—",
                          r["free_detention_until"] or "—",
                          r["empty_returned_at"] or "—",
                          str(r["schedule_changes"]),
                          "—" if r["avg_impact_days"] is None else str(r["avg_impact_days"]),
                          str(r["new_overdue"]),
                      ] for r in items],
                      "red": {i for i, r in enumerate(items) if r["new_overdue"]}})

    # 报关要素统计（T30：报关行 / 报关方式可统计；筛选时仅列所选口径）
    cs = m.get("customs_stats") or {}
    if cs.get("total_batches"):
        _h("报关要素统计")
        parts = []
        if m.get("customs_filter_label"):
            parts.append(f"筛选口径：{m['customs_filter_label']}")
        parts.append(f"批次样本 {cs['total_batches']} 个")
        if cs.get("missing_customs"):
            parts.append(f"未填写报关要素 {cs['missing_customs']} 个")
        b.append({"t": "para", "text": " · ".join(parts)})
        if cs.get("by_mode"):
            b.append({"t": "table",
                      "header": ["报关方式", "批次数", "进行中", "已完成"],
                      "rows": [[x["label"], str(x["count"]), str(x.get("running", 0)),
                                str(x.get("completed", 0))] for x in cs["by_mode"]]})
        if cs.get("by_broker"):
            b.append({"t": "table",
                      "header": ["报关行", "批次数", "进行中", "已完成"],
                      "rows": [[x["label"], str(x["count"]), str(x.get("running", 0)),
                                str(x.get("completed", 0))] for x in cs["by_broker"]]})

    # 按客户分组（§5.5 D32：项目 → 批次 → 客户 三维可筛 + 按客户分组统计）
    groups = m.get("customer_groups") or []
    if groups:
        _h("按客户分组")
        b.append({"t": "para", "text": "客户为空按「未填写」归组（§5.5 D32）。"})
        b.append({"t": "table",
                  "header": ["客户", "项目数", "批次数", "节点数",
                             "已完成节点", "节点完成率", "逾期节点"],
                  "rows": [[
                      g["customer"], str(g["projects"]), str(g["batches"]),
                      str(g["nodes_total"]), str(g["nodes_done"]),
                      g["completion"], str(g["overdue"]),
                  ] for g in groups],
                  "red": {i for i, g in enumerate(groups) if g["overdue"]}})

    # 操作动态
    _h("操作动态")
    if m["kind"] == "daily":
        items = m["activity_daily"] or []
        if not items:
            b.append({"t": "para", "text": "当日无操作记录。"})
        else:
            b.append({"t": "table",
                      "header": ["时间", "项目", "批次", "动作", "内容"],
                      "rows": [[f"{it['hour']} 时", it["project"],
                                it.get("batch") or "—",
                                it["kind_label"],
                                f"{it['subject']} {it['detail']}".strip()]
                               for it in items],
                      "red": set()})
        if m["activity_warning"]:
            b.append({"t": "note", "text": m["activity_warning"]})
    else:
        # 周报：按天分组（批次维度并入内容前缀）
        rows = m["activity_weekly"] or []
        if not rows:
            b.append({"t": "para", "text": "本自然周无操作记录。"})
        else:
            for day in sorted({r["date"] for r in rows}):
                day_rows = [r for r in rows if r["date"] == day]
                b.append({"t": "sub", "text": f"{day}"})
                # 按 kind 聚合计数
                agg = {}
                for r in day_rows:
                    agg.setdefault(r["kind_label"], []).append(r)
                for kind_label, rr in agg.items():
                    sample = "；".join(
                        ("{} {}".format(
                            x["subject"],
                            (f"[{x['batch']}] " if x.get("batch") and x["batch"] != "—" else "")
                            + x["detail"])).strip()
                        for x in rr[:3])
                    b.append({"t": "para", "text": f"{kind_label} {len(rr)} 次 · {sample}"})

    # 单证最新状态（每张单证只反映最后一次提交/撤销）
    _h("单证最新状态")
    states = m.get("file_states") or []
    if not states:
        b.append({"t": "para", "text": "暂无单证记录。"})
    else:
        b.append({"t": "table",
                  "header": ["项目", "批次", "节点", "单证", "类型",
                             "当前状态", "最后动作", "时间"],
                  "rows": [[
                      x["project"],
                      x.get("batch") or "—",
                      f"节点{x['node_id']}" if x["node_id"] else "项目级",
                      x["doc_name"],
                      "必填" if x["doc_type"] == "required" else "可选",
                      x["state"],
                      x["action"],
                      x["at"] or "—",
                  ] for x in states],
                  "red": {i for i, x in enumerate(states)
                          if x["doc_type"] == "required" and x["state"] != "已提交"}})

    # 待办（日报：下一个工作日；周报：下周自然周）
    _h("下一个工作日待办" if m["kind"] == "daily" else "下周待办")
    todo = m["todo"]
    if not todo["items"]:
        b.append({"t": "para", "text":
                  "下一个工作日暂无计划内节点。" if m["kind"] == "daily"
                  else "下周窗口暂无计划内节点。"})
    else:
        b.append({"t": "table",
                  "header": ["日期", "优先级", "项目", "批次", "节点", "单证"],
                  "rows": [[
                      it["day_label"], it["prio_label"], it["project"],
                      it.get("batch") or "—",
                      it["node"], it.get("docs") or "—",
                  ] for it in todo["items"]],
                  "red": set()})
    if todo["warning"]:
        b.append({"t": "note", "text": todo["warning"]})

    # 摘要
    _h("本期摘要")
    b.append({"t": "para", "text": m["summary"]})

    # 风险
    if m["risks"]:
        _h("风险提示")
        for r in m["risks"]:
            b.append({"t": "para", "text": "· " + r})

    # 周对比（同口径双标）
    if m["kind"] == "weekly":
        _h("与上周对比")
        wc = m["weekly_compare"]
        if wc is None:
            b.append({"t": "note", "text": "前一周无数据，本次为首份周报。"})
        else:
            sc = wc.get("scope") or {}
            b.append({"t": "para", "text":
                      f"同口径对比：项目 {sc.get('projects', 0)} 个 · "
                      f"批次 {sc.get('batches', 0)} 个（与本期筛选一致）"})
            b.append({"t": "table",
                      "header": ["指标", "上周", "本周", "变化"],
                      "rows": [list(r) for r in wc["rows"]],
                      "red": set()})
            b.append({"t": "para", "text": "结论：" + wc["note"]})

    return b


def render_html(model):
    """预览：HTML 式排版，与 Word 同源（同 blocks）。"""
    from ui.theme import ACCENT, TEXT_SECONDARY, RED
    html = [f"<div style='font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;"
            f"font-size:13px;color:#1c1c1e;line-height:1.6;'>"]
    for blk in blocks(model):
        if blk["t"] == "title":
            html.append(f"<div style='font-size:17px;font-weight:600;margin:6px 0 2px;'>{_esc(blk['text'])}</div>")
        elif blk["t"] == "h":
            html.append(f"<div style='font-size:14px;font-weight:600;color:{ACCENT};"
                        f"margin:14px 0 4px;border-left:3px solid {ACCENT};padding-left:8px;'>{_esc(blk['text'])}</div>")
        elif blk["t"] == "sub":
            html.append(f"<div style='font-size:13px;font-weight:600;margin:8px 0 2px;'>{_esc(blk['text'])}</div>")
        elif blk["t"] == "note":
            html.append(f"<div style='background:#f2f2f7;border-radius:6px;padding:6px 10px;"
                        f"color:#8e8e93;font-size:12px;margin:4px 0;'>{_esc(blk['text'])}</div>")
        elif blk["t"] == "para":
            html.append(f"<div style='margin:2px 0;'>{_esc(blk['text'])}</div>")
        elif blk["t"] == "table":
            rows = blk["header"] and [blk["header"]] + blk["rows"]
            table = ["<table style='border-collapse:collapse;width:100%;margin:4px 0;font-size:12px;'>"]
            red_row_ids = blk.get("red") or set()
            for ri, r in enumerate(rows):
                tag = "th" if ri == 0 else "td"
                style = ("background:#f2f2f7;font-weight:600;text-align:left;"
                         if ri == 0 else
                         (f"color:{RED};" if (ri - 1) in red_row_ids else ""))
                cells = "".join(f"<{tag} style='border:1px solid #e5e5ea;padding:4px 8px;{style}'>{_esc(c)}</{tag}>" for c in r)
                table.append(f"<tr>{cells}</tr>")
            table.append("</table>")
            html.append("".join(table))
    html.append("</div>")
    return "".join(html)


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace("\n", "<br/>"))