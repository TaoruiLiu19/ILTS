"""
报告聚合层 —— 纯逻辑、无 Qt 依赖，供离线单测。

职责：把 projects / nodes / files / cargo / vessel / op_log 组合为一份
「报告模型」，预览与 Word/txt 导出共享同一模型（同源一致）。
所有检索均为 project_id + 时间范围双条件；「全部项目」也逐项目聚合后合并。
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


def _active_filtered(project_filter):
    """按筛选返回活动项目列表（全部 / 单项目）。"""
    if project_filter in (None, "", "all", "全部"):
        return db.get_projects_by_status("Active")
    p = db.get_project(project_filter)
    return [p] if p else []


def _project_names(ids):
    return {pid: (db.get_project(pid) or {}).get("project_name", pid)
            for pid in ids}


# ── 总体概览 ──

def overview(projects, ref_date):
    total = len(projects)
    health = {"绿": 0, "黄": 0, "红": 0}
    nodes_total = nodes_done = 0
    for p in projects:
        nodes = db.get_nodes(p["project_id"])
        files = db.get_files(p["project_id"])
        health[health_level(p, nodes, files, ref_date)] += 1
        nodes_total += len(nodes)
        nodes_done += sum(1 for n in nodes
                          if n.get("status") == "Done" or n.get("actual_completion_date"))
    rate = f"{nodes_done / nodes_total * 100:.0f}%" if nodes_total else "—"
    return {
        "project_count": total,
        "health": health,
        "nodes_total": nodes_total,
        "nodes_done": nodes_done,
        "completion_rate": rate,
    }


# ── 未完成清单 ──

def unfinished(projects, ref_date):
    out = []
    names = _project_names([p["project_id"] for p in projects])
    for p in projects:
        pid = p["project_id"]
        files = db.get_files(pid)
        missing_by_node = {}
        for f in files:
            if f["doc_type"] == "required" and f["status"] != "submitted" \
                    and f.get("node_id") is not None:
                missing_by_node.setdefault(f["node_id"], []).append(f["doc_name"])
        for n in db.get_nodes(pid):
            if n["status"] == "Done":
                continue
            pe = _parse(n.get("plan_end"))
            if not pe or pe > ref_date:
                continue
            od = (ref_date - pe).days
            out.append({
                "project": names.get(pid, pid),
                "node_id": n["node_id"],
                "node_name": n["node_name"],
                "plan_end": n["plan_end"],
                "overdue_days": od,
                "missing": missing_by_node.get(n["node_id"], []),
            })
    out.sort(key=lambda x: -x["overdue_days"])
    return out


# ── 操作动态 ──

_KIND_LABEL = {
    "project_create": "新建项目", "project_close": "项目完结",
    "node_shift": "推迟/提前", "node_unshift": "撤销位移", "node_done": "自动完成",
    "file_submit": "提交单证", "file_withdraw": "撤交单证",
    "vessel_position": "船位登记", "cargo_edit": "货物变更",
}


def _activity_rows(project_ids, start, end, names):
    """区间内所有日志（逐项目聚合后合并，跨项目不混行）。"""
    rows = []
    for pid in project_ids:
        for r in db.get_op_log_range(pid, start=start, end=end):
            rows.append(r)
    rows.sort(key=lambda r: r["created_at"])
    enriched = []
    for r in rows:
        enriched.append({
            "project_id": r["project_id"],
            "project": names.get(r["project_id"], r["project_id"]),
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


def last_file_states(projects):
    """每张单证的**最新状态**（跨天取最后一次提交/撤销）。

    数据来源：`files.status/submitted_date`（当前态）+ `op_log` 最后一条单证动作（时间/动作）。
    返回按项目分组的最新状态行，供报告「单证最新状态」板块。
    """
    out = []
    for p in projects:
        pid = p["project_id"]
        pname = p.get("project_name") or pid
        last = db.last_file_actions(pid)
        for f in db.get_files(pid):
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
                "node_id": f.get("node_id"),
                "doc_name": f["doc_name"],
                "doc_type": f["doc_type"],
                "state": "已提交" if submitted else "未提交",
                "action": kind_label,
                "at": at,
            })
    return out


def activity_daily(projects, ref_date, names):
    return _activity_rows([p["project_id"] for p in projects],
                          ref_date.strftime("%Y-%m-%d 00:00"),
                          ref_date.strftime("%Y-%m-%d 23:59"), names)


def activity_weekly(projects, week_start, week_end, names):
    """weekly raw rows（供按天分组）。"""
    return _activity_rows([p["project_id"] for p in projects],
                          week_start.strftime("%Y-%m-%d 00:00"),
                          week_end.strftime("%Y-%m-%d 23:59"), names)


# ── 待办（日报：下一个工作日；周报：下周自然周） ──

def _todo_nodes(projects, wanted_days):
    """wanted_days: [date,...] 期望 plan_start 落在这些天；返回 [ (day, node, project) ]"""
    wanted = {_fmt(d) for d in wanted_days}
    out = []
    for p in projects:
        pid = p["project_id"]
        for n in db.get_nodes(pid):
            if n["status"] == "Done":
                continue
            if n.get("plan_start") in wanted:
                out.append((n["plan_start"], n, p))
    out.sort(key=lambda x: (x[0], priority_of(x[1]["node_name"]), x[1]["node_id"]))
    return out


def _pending_docs_by_node(projects):
    """{project_id: {node_id: [未提交的必填单证名, ...]}}（待办表格的「单证」列）"""
    out = {}
    for p in projects:
        pid = p["project_id"]
        by_node = {}
        for f in db.get_files(pid):
            if f.get("doc_type") != "required":
                continue
            if f.get("status") == "submitted":
                continue
            nid = f.get("node_id")
            if nid is None:
                continue
            by_node.setdefault(nid, []).append(f["doc_name"])
        out[pid] = by_node
    return out


def _todo_item(day_s, n, p, names, pending, label_fn):
    """label_fn 接收 date 对象，返回该行的日期展示文案。"""
    docs = (pending.get(p["project_id"]) or {}).get(n["node_id"]) or []
    prio = priority_of(n["node_name"])
    return {
        "day": day_s, "day_label": label_fn(_parse(day_s)),
        "project": names.get(p["project_id"], p["project_id"]),
        "node": f"节点{n['node_id']} {n['node_name']}",
        "prio": prio,
        "prio_label": PRIO_LABEL[prio],
        "docs": "、".join(docs) if docs else "—",
    }


def todo_daily(projects, ref_date):
    names = _project_names([p["project_id"] for p in projects])
    pending = _pending_docs_by_node(projects)
    tomorrow = ref_date + timedelta(days=1)
    days = [tomorrow]
    warning = None
    if tomorrow.weekday() in (5, 6):          # 明日为周六/日 → 周末提前预警
        next_mon = ref_date + timedelta((0 - ref_date.weekday()) % 7 or 7)
        days.append(next_mon)
        warning = ("明日为周末，已并入下周一计划，建议今日提前处理")
    items = [_todo_item(day_s, n, p, names, pending, _monthday)
             for day_s, n, p in _todo_nodes(projects, days)]
    return {"items": items, "warning": warning}


def todo_weekly(projects, ref_date):
    names = _project_names([p["project_id"] for p in projects])
    pending = _pending_docs_by_node(projects)
    ws, we = next_week_range(ref_date)
    items = [_todo_item(day_s, n, p, names, pending,
                        lambda s: _parse(s).strftime("%m-%d %a"))
             for day_s, n, p in _todo_nodes(projects, [ws + timedelta(days=i) for i in range(7)])]
    return {"items": items, "warning": None}


# ── 风险（实时读当前 cargo/vessel 值判定） ──

def risks(projects, ref_date, weekly=True):
    out = []
    # ① 超限：实时读 cargo 当前值
    for p in projects:
        for it in db.get_cargo_items(p["project_id"]):
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
        for n in db.get_nodes(p["project_id"]):
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
        for n in db.get_nodes(p["project_id"]):
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

def _week_metrics(projects, ws, we):
    overdue = 0
    missing = 0
    done_in_week = 0
    planned = 0
    for p in projects:
        pid = p["project_id"]
        nodes = db.get_nodes(pid)
        files = db.get_files(pid)
        overdue += sum(1 for n in nodes
                       if n["status"] != "Done" and _parse(n["plan_end"])
                       and we > _parse(n["plan_end"]))
        missing += sum(1 for f in files
                       if f["doc_type"] == "required" and f["status"] != "submitted")
        planned += sum(1 for n in nodes
                       if ws.strftime("%Y-%m-%d") <= n["plan_start"] <= we.strftime("%Y-%m-%d"))
        done_in_week += sum(1 for n in nodes if (n.get("actual_completion_date")
                                                 and ws.strftime("%Y-%m-%d")
                                                 <= n["actual_completion_date"]
                                                 <= we.strftime("%Y-%m-%d")))
    return overdue, missing, planned, done_in_week


def weekly_compare(projects, ref_date):
    cur_ws, cur_we = week_range(ref_date)
    prev_ws, prev_we = cur_ws - timedelta(days=7), cur_ws - timedelta(days=1)
    # 前一周有无「数据痕迹」：操作日志或节点完成（含计划节点完成），否则视为首份
    prev_rows = activity_weekly(projects, prev_ws, prev_we, {})
    prev_done = 0
    for p in projects:
        prev_done += sum(1 for n in db.get_nodes(p["project_id"])
                         if (n.get("actual_completion_date")
                             and prev_ws.strftime("%Y-%m-%d")
                             <= n["actual_completion_date"] <= prev_we.strftime("%Y-%m-%d")))
    if not prev_rows and prev_done == 0:
        return None  # 调用方显示「前一周无数据，本次为首份周报」

    cur = _week_metrics(projects, cur_ws, cur_we)
    prev = _week_metrics(projects, prev_ws, prev_we)
    labels = ["本周完成节点", "本周待办节点", "逾期节点", "单证缺失"]
    rows = []
    for lbl, c, pr in zip(labels, cur, prev):
        sym = "↑" if c > pr else ("↓" if c < pr else "—")
        rows.append((lbl, pr, c, sym))
    # 改善/恶化结论
    done_sym = "改善" if cur[3] >= prev[3] else "回落"
    bad = "恶化" if (cur[0] > prev[0] or cur[2] > prev[2]) else "保持"
    note = f"本周完成节点 {done_sym}，逾期/缺失 {bad}"
    return {"rows": rows, "note": note}


# ── 智能摘要（规则模板） ──

def summary_rules(ov, unfin, risks_items, todo_items, weekly):
    parts = []
    parts.append(f"本期共操作 {ov['project_count']} 个项目")
    reds = [k for k, v in ov["health"].items() if v and k == "红"]
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
                 report_no=None, generated_at=None):
    """组合完整报告模型。report_no 由导出层传入（预览可传 peek，不占流水）。"""
    from services.clock import get_today, get_now_str
    ref_date = _parse(ref_date)
    today = get_today()
    if generated_at is None:
        generated_at = get_now_str("%Y-%m-%d %H:%M")

    name_map = None
    single_project = None
    if project_filter not in (None, "", "all", "全部"):
        sp = db.get_project(project_filter)
        single_project = sp["project_name"] if sp else None

    projects = _active_filtered(project_filter)
    names = _project_names([p["project_id"] for p in projects])

    # 周期
    if kind == "weekly":
        ws, we = week_range(ref_date)
        start_s, end_s = _fmt(ws), _fmt(we)
        period_label = f"{_monthday(ws)} ~ {_monthday(we)}"
        todokey = "week"
    else:
        ws = we = ref_date
        start_s = end_s = _fmt(ref_date)
        period_label = _fmt(ref_date)
        todokey = "day"

    # 标题：统一为公司日期制式（不再带项目名）
    date_txt = (f"{ref_date.strftime('%Y')}年{ref_date.strftime('%m')}月"
                f"{ref_date.strftime('%d')}日")
    if kind == "weekly":
        start_date_txt = (f"{ws.strftime('%Y')}年{ws.strftime('%m')}月"
                          f"{ws.strftime('%d')}日")
        end_date_txt = (f"{we.strftime('%Y')}年{we.strftime('%m')}月"
                        f"{we.strftime('%d')}日")
        title = f"{start_date_txt}-{end_date_txt}物流操作周报"
    else:
        title = f"{date_txt}物流操作日报"

    ov = overview(projects, ref_date)
    unfin = unfinished(projects, ref_date)

    if kind == "daily":
        act = activity_daily(projects, ref_date, names)
        todo = todo_daily(projects, ref_date)
        acts = todo["warning"]
    else:
        act_rows = activity_weekly(projects, ws, we, names)
        todo = todo_weekly(projects, ref_date)
        acts = None

    risk_items = risks(projects, ref_date, weekly=(kind == "weekly"))

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
        "project_filter_label": single_project or "全部项目",
        "project_count": len(projects),
        "brief": brief,
        "overview": ov,
        "unfinished": unfin,
        "activity_daily": act if kind == "daily" else None,
        "activity_weekly": act_rows if kind == "weekly" else None,
        "activity_warning": acts,
        "file_states": last_file_states(projects),
        "todo": todo,
        "risks": risk_items,
        "weekly_compare": weekly_compare(projects, ref_date) if kind == "weekly" else None,
        "summary": summary_rules(ov, unfin, risk_items, todo["items"], kind == "weekly"),
    }
    return model


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

    # 总体概览
    ov = m["overview"]
    _h("总体概览")
    b.append({"t": "para", "text": f"进行中项目 {ov['project_count']} 个"})
    b.append({"t": "para", "text":
              f"节点完成率：{ov['nodes_done']}/{ov['nodes_total']} = {ov['completion_rate']}"})

    # 未完成清单
    _h("未完成清单")
    if not m["unfinished"]:
        b.append({"t": "para", "text": "暂无未完成且已到期的节点。"})
    else:
        unfin_blk = {"t": "table",
                     "header": ["项目", "节点", "计划结束", "逾期(天)", "缺失单证"],
                     "rows": [[
                         x["project"],
                         f"节点{x['node_id']} {x['node_name']}",
                         x["plan_end"],
                         str(x["overdue_days"]),
                         "、".join(x["missing"]) if x["missing"] else "—",
                     ] for x in m["unfinished"]],
                     "red": {i for i, x in enumerate(m["unfinished"]) if x["missing"]}}
        b.append(unfin_blk)

    # 操作动态
    _h("操作动态")
    if m["kind"] == "daily":
        items = m["activity_daily"] or []
        if not items:
            b.append({"t": "para", "text": "当日无操作记录。"})
        else:
            b.append({"t": "table",
                      "header": ["时间", "项目", "动作", "内容"],
                      "rows": [[f"{it['hour']} 时", it["project"],
                                it["kind_label"],
                                f"{it['subject']} {it['detail']}".strip()]
                               for it in items],
                      "red": set()})
        if m["activity_warning"]:
            b.append({"t": "note", "text": m["activity_warning"]})
    else:
        # 周报：按天分组
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
                    sample = "；".join(("{} {}".format(x["subject"], x["detail"])).strip()
                                      for x in rr[:3])
                    b.append({"t": "para", "text": f"{kind_label} {len(rr)} 次 · {sample}"})

    # 单证最新状态（每张单证只反映最后一次提交/撤销）
    _h("单证最新状态")
    states = m.get("file_states") or []
    if not states:
        b.append({"t": "para", "text": "暂无单证记录。"})
    else:
        b.append({"t": "table",
                  "header": ["项目", "节点", "单证", "类型", "当前状态", "最后动作", "时间"],
                  "rows": [[
                      x["project"],
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
                  "header": ["日期", "优先级", "项目", "节点", "单证"],
                  "rows": [[
                      it["day_label"], it["prio_label"], it["project"],
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

    # 周对比
    if m["kind"] == "weekly":
        _h("与上周对比")
        wc = m["weekly_compare"]
        if wc is None:
            b.append({"t": "note", "text": "前一周无数据，本次为首份周报。"})
        else:
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