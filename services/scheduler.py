"""
排程算法（模板通用：境内串行 → 海运 → 境外串行）

优化方案 D2 核心：
  · shift_node：全环节「推迟 / 提前」统一入口，days 可正（推迟）可负（提前），覆盖 12 节点。
  · 三子链锚点分离：境内锚定 ETD、境外锚定 ETA，海运是两侧锚点的桥。
      - 境内 1–4   位移：本节点→节点4 + 海运 start 联动（ETD 变，ETA 不变，海运时长重算）
      - 海运 5     位移：海运 end / ETA 联动 + 境外全段 6–12（ETD 不变，海运时长重算）
      - 境外 6–12  位移：本节点→12（ETD / ETA / 境内均不动，口岸段整段平移）
  · 四守卫：① 提前不早于今日；② Done 节点保护；③ 链连续性（锚点不漂移）；④ delay_days 净位移可负 + shift_history 留痕可撤销。
"""

from datetime import date, timedelta

AREA_ORDER = ("DOME", "SEA", "OVERSEA")


class ShiftError(Exception):
    """位移被守卫拦截（提示用，message 面向操作员）"""


def _parse(s):
    if isinstance(s, date):
        return s
    if not s:
        return None
    y, m, d = str(s).split("-")
    return date(int(y), int(m), int(d))


def _fmt(dt):
    return dt.strftime("%Y-%m-%d")


# ── 基础排程（建项目时跑一次） ──

def generate_schedule(etd, eta, nodes_cfg):
    """
    nodes_cfg: [{id, dur, area}, ...] 按 seq 升序
    返回 {node_id: (start_str, end_str)}
    """
    etd = _parse(etd)
    eta = _parse(eta)
    if eta <= etd:
        raise ValueError("ETA 必须晚于 ETD 至少 1 天")

    plan = {}

    dome = [n for n in nodes_cfg if n["area"] == "DOME"]
    cursor = etd
    for n in reversed(dome):
        end = cursor
        start = cursor - timedelta(days=n["dur"])
        plan[n["id"]] = (_fmt(start), _fmt(end))
        cursor = start

    sea = [n for n in nodes_cfg if n["area"] == "SEA"][0]
    plan[sea["id"]] = (_fmt(etd), _fmt(eta))

    oversea = [n for n in nodes_cfg if n["area"] == "OVERSEA"]
    cursor = eta
    for n in oversea:
        start = cursor
        end = cursor + timedelta(days=n["dur"])
        plan[n["id"]] = (_fmt(start), _fmt(end))
        cursor = end

    return plan


# ── 位移（D2 核心） ──

def _node_list(nodes):
    """浅拷贝并补齐缺省字段，避免污染调用方原对象"""
    out = []
    for n in nodes:
        c = dict(n)
        c.setdefault("delay_days", 0)
        c.setdefault("is_delayed", 0)
        c.setdefault("status", "Pending")
        c.setdefault("actual_completion_date", None)
        c.setdefault("duration", 0)
        out.append(c)
    return out


def _chain_index(nodes):
    """全局串行链序：境内(1..4) → 海运(5) → 境外(6..12)"""
    return {n["node_id"]: i for i, n in enumerate(
        sorted(nodes, key=lambda x: (AREA_ORDER.index(x["area"]), x["seq"])))}


def _shift_span(n, days):
    n["plan_start"] = _fmt(_parse(n["plan_start"]) + timedelta(days=days))
    n["plan_end"] = _fmt(_parse(n["plan_end"]) + timedelta(days=days))


def _sea_duration(sea):
    return (_parse(sea["plan_end"]) - _parse(sea["plan_start"])).days


def shift_node(nodes, from_node_id, days, today=None):
    """
    days > 0 推迟；days < 0 提前。
    返回 (updated_nodes, new_etd, new_eta, affected_node_ids)
    """
    days = int(days)
    nodes = _node_list(nodes)
    node_map = {n["node_id"]: n for n in nodes}
    fr = node_map.get(from_node_id)
    if fr is None:
        raise ShiftError(f"未找到节点 {from_node_id}")
    if days == 0:
        sea = next((n for n in nodes if n["area"] == "SEA"), fr)
        return nodes, sea["plan_start"], sea["plan_end"], []

    if today is None:
        from services.clock import get_today
        today = get_today()
    elif isinstance(today, str):
        today = _parse(today)

    # 记录原始 start，供「不早于今日」守卫判断是否把未来节点推入过去
    orig_start = {n["node_id"]: _parse(n["plan_start"]) for n in nodes}

    def _guard_done(win):
        done = [n for n in win if n.get("status") == "Done"]
        if done:
            names = "、".join(f"节点{n['node_id']}{n['node_name']}" for n in done)
            raise ShiftError(f"已完成节点不可位移：{names}")

    def _finish(win, etd_node, eta_node):
        """统一收尾：守卫 → 净位移记账 → 返回"""
        _guard_past(win, days, today, orig_start)
        _guard_collision(win, days, today)
        for n in win:
            n["delay_days"] = n.get("delay_days", 0) + days
            n["is_delayed"] = 1 if n["delay_days"] else 0
        return nodes, etd_node["plan_start"], eta_node["plan_end"], [n["node_id"] for n in win]

    def _guard_past(win, d, t, orig):
        """守卫①：提前把『本在未来开始』的节点推入过去（早于今日）→ 拦截"""
        if d >= 0:
            return
        for n in win:
            if n.get("status") == "Done":
                continue
            new_s = _parse(n["plan_start"])
            old_s = orig.get(n["node_id"])
            if old_s is not None and old_s >= t and new_s < t:
                raise ShiftError(
                    f"节点{n['node_id']}{n['node_name']} 提前后将早于今日"
                    f"({t.strftime('%Y-%m-%d')})，已拦截")

    def _guard_collision(win, d, t):
        """守卫①⑤：窗口头节点提前撞到前序已完成节点 → 拦截"""
        if d >= 0:
            return
        chain = _chain_index(nodes)
        head = min(win, key=lambda n: chain[n["node_id"]])
        pos = chain[head["node_id"]]
        prev = next((n for n in nodes if chain[n["node_id"]] == pos - 1), None)
        if prev and (prev.get("status") == "Done" or prev.get("actual_completion_date")):
            if _parse(head["plan_start"]) < _parse(prev["plan_end"]):
                raise ShiftError(
                    f"节点{head['node_id']}{head['node_name']} 提前将撞到已完成节点"
                    f"{prev['node_id']}{prev['node_name']}（{prev['plan_end']} 完成），已拦截")

    area = fr["area"]

    if area == "DOME":
        dome_win = [n for n in nodes
                    if n["area"] == "DOME" and n["seq"] >= fr["seq"]]
        sea = next(n for n in nodes if n["area"] == "SEA")
        win = dome_win + [sea]
        _guard_done(win)                      # 守卫②：已完成节点不参与位移
        for n in dome_win:
            _shift_span(n, days)
        # 守卫③：链连续性 — 境内末(节点4 end) 与 海运 start 同步，锚点不漂移
        sea["plan_start"] = dome_win[-1]["plan_end"]   # ETD = 海运 start
        if _sea_duration(sea) < 1:
            raise ShiftError("位移后海运段时长不足 1 天，请检查 ETD/ETA 安排")
        sea["duration"] = _sea_duration(sea)
        return _finish(win, sea, sea)

    if area == "SEA":
        sea = fr
        over_win = [n for n in nodes if n["area"] == "OVERSEA"]
        win = [sea] + over_win
        _guard_done(win)
        # 海运 start 锚定境内末（ETD）不动，只移动 ETA 端
        sea["plan_end"] = _fmt(_parse(sea["plan_end"]) + timedelta(days=days))
        if _sea_duration(sea) < 1:
            raise ShiftError("位移后海运段时长不足 1 天（ETA 早于 ETD），已拦截")
        sea["duration"] = _sea_duration(sea)
        for n in over_win:
            _shift_span(n, days)
        return _finish(win, sea, sea)

    # OVERSEA：口岸操作整段平移，境内/海运/ETD/ETA 均不动
    over_win = [n for n in nodes
                if n["area"] == "OVERSEA" and n["seq"] >= fr["seq"]]
    win = over_win
    _guard_done(win)
    for n in over_win:
        _shift_span(n, days)
    sea = next(n for n in nodes if n["area"] == "SEA")
    return _finish(win, sea, sea)


# ── 高层服务：位移 → 全链路落库（nodes / project / shift_history / files due） ──

def apply_shift(project_id, from_node_id, days, oplog=True):
    """
    对项目执行一次位移并落库：
      1) shift_node 计算（含四守卫）
      2) 回写 nodes.plan_start/plan_end/duration/delay_days/is_delayed
      3) 回写 projects.etd/eta（ETA 联动）
      4) 写入 shift_history（可撤销留痕）
      5) 单证 due_date 跟随重算（锚 node_start/node_end）
    oplog=False 供撤销调用：撤销自身不再记 node_shift（由调用方记 node_unshift）。
    返回结果 dict；守卫拦截时抛 ShiftError（不产生任何写入）。
    """
    import db
    project = db.get_project(project_id)
    if not project:
        raise ShiftError("项目不存在")
    nodes = db.get_nodes(project_id)
    updated, new_etd, new_eta, affected = shift_node(nodes, from_node_id, days)

    for n in updated:
        db.update_node(project_id, n["node_id"],
                       plan_start=n["plan_start"],
                       plan_end=n["plan_end"],
                       duration=n.get("duration", 0),
                       delay_days=n.get("delay_days", 0),
                       is_delayed=n.get("is_delayed", 0))
    db.update_project(project_id, etd=new_etd, eta=new_eta)

    from services.clock import get_now_str
    created_at = get_now_str("%Y-%m-%d %H:%M:%S.%f")  # 微秒级：保证同组/同秒可区分
    db.insert_shift_history(project_id, affected, int(days), created_at)

    # 操作日志埋点（node_shift 主动调整）
    if oplog:
        try:
            from services.oplog import record as oplog_record
            oplog_record(
                "node_shift", project_id, node_id=from_node_id, subject=f"节点{from_node_id}",
                detail=f"{'推迟' if days > 0 else '提前'} {abs(days)} 天 · 影响 {len(affected)} 个节点",
                delta=int(days))
        except Exception:
            pass  # 日志失败不影响位移主流程

    plan = {n["node_id"]: (n["plan_start"], n["plan_end"]) for n in updated}
    due_changed = db.recompute_files_due(project_id, plan)

    return {
        "project_id": project_id,
        "from_node_id": from_node_id,
        "days": int(days),
        "new_etd": new_etd,
        "new_eta": new_eta,
        "affected": affected,
        "files_recomputed": due_changed,
        "note": (f"{'推迟' if days > 0 else '提前'} {abs(days)} 天 · 影响 {len(affected)} 个节点"
                 f" · 单证 due 重算 {due_changed} 项"),
    }


def undo_last_shift(project_id):
    """
    撤销最近一次位移：取 shift_history 最近一组（同 created_at），
    以组内最小 node_id 为起始节点反向位移 -delta。
    返回结果 dict；无历史或撤销被守卫拦截时抛 ShiftError。
    """
    import db
    group = db.last_shift_group(project_id)
    if not group:
        raise ShiftError("暂无位移历史可撤销")
    delta = group[0]["delta"]
    undone_id = min(r["id"] for r in group)
    from_node = min(r["node_id"] for r in group)
    # 撤销自身不再记 node_shift；由下方单独记 node_unshift（带 shift_history 主键）
    res = apply_shift(project_id, from_node, -delta, oplog=False)
    try:
        from services.oplog import record as oplog_record
        oplog_record(
            "node_unshift", project_id, node_id=from_node, subject=f"节点{from_node}",
            detail=f"撤销位移 #{undone_id}（恢复至原计划）")
    except Exception:
        pass
    res["undone"] = True
    res["note"] = f"已撤销上一步（还原 {abs(delta)} 天）"
    return res


# 兼容旧名：delay_nodes → shift_node（只支持推迟的旧接口废除，此处不再保留）
