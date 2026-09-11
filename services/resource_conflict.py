"""
跨批次资源冲突扫描 —— 供看板「甘特工作台」在多批次合并甘特里标红冲突段、底部冲突条列明细。

为什么放在 services 层：冲突是**数据口径**问题（同一港口/报关行/船期/免期被多个批次同时占用），
不依赖任何 UI；甘特工作台、报告导出、未来提醒都可复用同一份判定。

五条规则（同一对批次之间可同时命中多条）：
  1. PORT_WINDOW    high    同 export_port → 境内段（area='DOME' 节点并集）区间重叠
  2. CUSTOMS_BROKER high    同 customs_broker（非空）→ EXPORT_CUSTOMS 节点区间重叠
  3. VESSEL_VOYAGE  high    同 vessel_name + voyage（都非空）→ SEA_TRANSIT 区间重叠
  4. FREE_TIME      medium  批次内自检：免堆期 < STORAGE_FEE/CUSTOMS_INSPECT 计划完成，
                            或免箱期 < EMPTY_RETURN 计划完成（逾期 ≥3 天升级为 high）
  5. DEST_STORAGE   low     同目的国 → CUSTOMS_INSPECT/STORAGE_FEE 境外堆存查验窗口重叠

约定：
  · **只读**：本模块只经 db 的 get_* 取数，绝不写库（不调用任何 db.update_*/db.insert_*）。
  · 日期一律容错解析，None/空串/坏串只跳过该节点，不抛异常（甘特整屏不能因一条脏数据崩掉）。
  · 同一对批次同一 kind 只产出一条（重叠段合并后取最宽的一段），避免冲突条刷屏。
"""

from datetime import date, datetime, timedelta
from itertools import combinations

import db
from services import node_template as nt
from services.clock import get_today

# ── 冲突类型（kind）──
PORT_WINDOW = "PORT_WINDOW"
CUSTOMS_BROKER = "CUSTOMS_BROKER"
VESSEL_VOYAGE = "VESSEL_VOYAGE"
FREE_TIME = "FREE_TIME"
DEST_STORAGE = "DEST_STORAGE"

# ── 严重度 ──
LEVEL_HIGH = "high"
LEVEL_MEDIUM = "medium"
LEVEL_LOW = "low"
_LEVEL_RANK = {LEVEL_HIGH: 0, LEVEL_MEDIUM: 1, LEVEL_LOW: 2}

# 免期逾期 ≥ 该天数视为 high（1~2 天尚可通过催办补救）
HIGH_OVERDUE_DAYS = 3

# today 参数当前只用于「过滤已完全过去的窗口」，默认关闭：
# 甘特工作台需要复盘历史冲突，且多批次合并视图常看已过去的批次对。
_FILTER_PAST = False

# 规则 4 的两个免期字段 → (中文名, 被其约束的后续节点)
_FREE_CHECKS = (
    ("free_demurrage_until", "免堆期", (nt.STORAGE_FEE, nt.CUSTOMS_INSPECT)),
    ("free_detention_until", "免箱期", (nt.EMPTY_RETURN,)),
)

# 规则 5 参与的境外堆存/查验节点
_DEST_STORAGE_KEYS = (nt.CUSTOMS_INSPECT, nt.STORAGE_FEE)


# ══════════════════ 日期与区间工具 ══════════════════

def _to_date(value):
    """容错解析日期：date/datetime 直取，"YYYY-MM-DD"（含带时间/ISO 串）取前 10 位。

    为什么容错：plan_start/plan_end 由 schedule2 写成字符串，但空批次、人工清空、
    旧数据都可能为空或异常；本模块只做区间比较，解析失败一律返回 None 由调用方跳过。
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        y, m, d = str(value).strip()[:10].split("-")
        return date(int(y), int(m), int(d))
    except (AttributeError, TypeError, ValueError):
        return None


def _fmt(d):
    """date → "YYYY-MM-DD"；None → None。"""
    return d.isoformat() if d else None


def _mmdd(d):
    """message 里的短日期（对齐示例口径：B01 09-10~09-12）。"""
    return d.strftime("%m-%d") if d else "-"


def _node_span(node):
    """节点闭区间 (start, end)；缺边或倒挂（start > end）→ None（跳过该节点）。"""
    s, e = _to_date(node.get("plan_start")), _to_date(node.get("plan_end"))
    if s is None or e is None or e < s:
        return None
    return (s, e)


def _collapse(spans):
    """闭区间合并：重叠或相邻（相差 1 天）即并成一段，返回按开始排序的列表。

    为什么把相邻也算合并：境内节点是首尾相接排布的（一节点 end = 下一节点 start），
    若不合并，同一批次的 DOME 段会被切成 5 段，重叠判定与展示都会碎掉。
    """
    merged = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1] + timedelta(days=1):
            if e > merged[-1][1]:
                merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return merged


def _intersect(a, b):
    """闭区间交集；无交集 → None。"""
    s, e = max(a[0], b[0]), min(a[1], b[1])
    return (s, e) if s <= e else None


def _span_of(items):
    """一组 (key, span) 的整体跨度（最早开始 ~ 最晚结束）；空 → None。"""
    if not items:
        return None
    return (min(s[0] for _, s in items), max(s[1] for _, s in items))


def _best_overlap(items_a, items_b):
    """两批区间集合的重叠：返回 (最宽的重叠段, 涉及 node_key 列表)；无重叠 → (None, [])。

    为什么只取「最宽的一段」：同一对批次同一资源常有多个节点先后压在一起，
    逐段上报会把冲突条刷屏；需求要求同一对批次同一 kind 只出一条。
    """
    hits = []
    for _, sa in items_a:
        for _, sb in items_b:
            ov = _intersect(sa, sb)
            if ov:
                hits.append(ov)
    if not hits:
        return None, []

    # 先合并（相邻重叠段并成一段），再取跨度最大者；跨度相同取更早者，保证结果稳定。
    best = max(_collapse(hits), key=lambda x: (x[1] - x[0], -x[0].toordinal()))

    keys = []
    for key, span in list(items_a) + list(items_b):
        if key not in keys and _intersect(span, best):
            keys.append(key)
    return best, keys


def _items_of(facts, keys):
    """取某批次指定 node_key 的区间 → [(node_key, (start, end))]，无计划日期的节点自动跳过。"""
    out = []
    for key in keys:
        node = facts["by_key"].get(key)
        if not node:
            continue
        span = _node_span(node)
        if span:
            out.append((key, span))
    return out


def _items_of_area(facts, area):
    """取某批次某分区（DOME/SEA/OVERSEA）全部节点的区间。"""
    return _items_of(facts, [n["node_key"] for n in facts["nodes"] if n.get("area") == area])


# ══════════════════ 资源名与文案 ══════════════════

def _port_label(key):
    """出口港 key → 中文港名（"QD" → "青岛港"）；主数据缺失时原样返回，不丢冲突。"""
    try:
        from services.ports import get_port
        entry = get_port(key) or {}
    except Exception:
        entry = {}
    return entry.get("name") or key


def _country_label(code):
    """目的国代码 → 中文国名（"BR" → "巴西"）；主数据缺失时原样返回。"""
    try:
        from config import get_country
        entry = get_country(code) or {}
    except Exception:
        entry = {}
    return entry.get("name") or code


def _brief(facts, items):
    """message 片段：批次号 + 该批次自身窗口（如 "B01 09-08~09-15"）。"""
    span = _span_of(items)
    if not span:
        return facts["batch_no"]
    return f"{facts['batch_no']} {_mmdd(span[0])}~{_mmdd(span[1])}"


def _record(kind, level, resource, window, batches, node_keys, message):
    """统一冲突条目（UI 直接消费）；window 固定为 "YYYY-MM-DD" 闭区间元组。"""
    return {
        "kind": kind,
        "level": level,
        "resource": resource,
        "window": (_fmt(window[0]), _fmt(window[1])),
        "batches": list(batches),
        "node_keys": list(node_keys),
        "message": message,
    }


# ══════════════════ 批次上下文与分组 ══════════════════

def _batch_facts(project_id, batch, project_country=None):
    """单批次扫描上下文：节点（含 node_key 索引）、线路、班轮（含航次）。

    一次性取全，避免五条规则各查一遍库（甘特拖动时会被高频调用）。
    """
    batch_id = batch["batch_id"]
    nodes = db.get_nodes_by_batch(batch_id)
    route = db.get_route(batch_id) or {}
    return {
        "batch_id": batch_id,
        "batch_no": batch.get("batch_no") or batch_id,
        "nodes": nodes,
        "by_key": {n["node_key"]: n for n in nodes},
        "route": route,
        "vessel": db.get_vessel(project_id, batch_id) or {},
        # 目的国视角：线路未填时退回项目国别（规则 5 分组用）
        "dest_country": route.get("country") or project_country,
    }


def _groups(facts_list, keyfunc):
    """按资源键分组，只保留 ≥2 个批次的组（跨批次冲突的最小前提）。"""
    groups = {}
    for facts in facts_list:
        key = keyfunc(facts)
        if not key:
            continue
        groups.setdefault(key, []).append(facts)
    return {k: v for k, v in groups.items() if len(v) >= 2}


def _resolve_batches(project_id, batch_ids):
    """待扫描批次：batch_ids=None 取该项目全部启用中批次（不含 cancelled）。

    显式传入时同样过滤「不属于本项目 / 不存在 / 已取消」的 id：
    已取消批次不产生新冲突，混进来只会让甘特冲突条出现无法处置的幽灵项。
    """
    if batch_ids is None:
        return db.get_batches(project_id)
    out = []
    for batch_id in batch_ids:
        b = db.get_batch(batch_id)
        if not b or b.get("project_id") != project_id or b.get("status") == "cancelled":
            continue
        out.append(b)
    return out


# ══════════════════ 规则 1~5 ══════════════════

def _rule_port_window(facts_list):
    """规则 1（high）：同 export_port 的批次，境内段（area='DOME' 节点并集）区间重叠。"""
    out = []
    for port_key, group in _groups(facts_list, lambda f: f["route"].get("export_port")).items():
        for a, b in combinations(group, 2):
            ia, ib = _items_of_area(a, "DOME"), _items_of_area(b, "DOME")
            window, keys = _best_overlap(ia, ib)
            if not window:
                continue
            resource = _port_label(port_key)
            out.append(_record(
                PORT_WINDOW, LEVEL_HIGH, resource, window,
                [a["batch_id"], b["batch_id"]], keys,
                f"出口港 {resource} 境内作业窗口重叠：{_brief(a, ia)} ｜ {_brief(b, ib)}"))
    return out


def _rule_customs_broker(facts_list):
    """规则 2（high）：同报关行（非空）的批次，EXPORT_CUSTOMS 节点区间重叠。"""
    out = []
    for broker, group in _groups(facts_list, lambda f: f["route"].get("customs_broker")).items():
        for a, b in combinations(group, 2):
            ia, ib = _items_of(a, [nt.EXPORT_CUSTOMS]), _items_of(b, [nt.EXPORT_CUSTOMS])
            window, keys = _best_overlap(ia, ib)
            if not window:
                continue
            out.append(_record(
                CUSTOMS_BROKER, LEVEL_HIGH, broker, window,
                [a["batch_id"], b["batch_id"]], keys,
                f"出口报关窗口重叠：{broker} ｜ {_brief(a, ia)} ｜ {_brief(b, ib)}"))
    return out


def _rule_vessel_voyage(facts_list):
    """规则 3（high）：同船名 + 同航次（都非空）的批次，SEA_TRANSIT 区间重叠。

    为什么查 db.get_vessel：船名/航次在 vessel 表（批次级），不在 batch_routes。
    """
    def key_of(facts):
        vessel = facts["vessel"]
        name, voyage = vessel.get("vessel_name"), vessel.get("voyage")
        return f"{name} {voyage}" if name and voyage else None

    out = []
    for resource, group in _groups(facts_list, key_of).items():
        for a, b in combinations(group, 2):
            ia, ib = _items_of(a, [nt.SEA_TRANSIT]), _items_of(b, [nt.SEA_TRANSIT])
            window, keys = _best_overlap(ia, ib)
            if not window:
                continue
            out.append(_record(
                VESSEL_VOYAGE, LEVEL_HIGH, resource, window,
                [a["batch_id"], b["batch_id"]], keys,
                f"同船同航次海上运输区间重叠：{resource} ｜ {_brief(a, ia)} ｜ {_brief(b, ib)}"))
    return out


def _rule_free_time(facts_list):
    """规则 4（medium，逾期 ≥3 天升 high）：批次内自检 —— 免期截止日早于本批次后续节点计划。

    与规则 1/2/3/5 不同，这是**批次内**风险（`batches` 只含该批次自己），不参与跨批次配对；
    但入口仍受「≥2 个启用批次」约束（单批次项目按接口约定返回空列表）。
    同一批次的两个免期若都逾期，合并为一条（resource 形如 "免堆期、免箱期"），避免刷屏。
    """
    out = []
    for facts in facts_list:
        hits = []
        for field, label, node_keys in _FREE_CHECKS:
            cutoff = _to_date(facts["route"].get(field))
            if not cutoff:
                continue
            for node_key in node_keys:
                node = facts["by_key"].get(node_key)
                span = _node_span(node) if node else None
                if not span:
                    continue
                end = span[1]
                # 严格早于才算逾期：截止日当天仍可用，不算风险
                if cutoff < end:
                    hits.append({"label": label, "cutoff": cutoff, "end": end,
                                 "key": node_key, "node_name": node["node_name"],
                                 "overdue": (end - cutoff).days})
        if not hits:
            continue

        labels, keys = [], []
        for h in hits:
            if h["label"] not in labels:
                labels.append(h["label"])
            if h["key"] not in keys:
                keys.append(h["key"])
        window = (min(h["cutoff"] for h in hits), max(h["end"] for h in hits))
        overdue = max(h["overdue"] for h in hits)
        level = LEVEL_HIGH if overdue >= HIGH_OVERDUE_DAYS else LEVEL_MEDIUM
        message = "；".join(
            f"{h['label']}截止 {_fmt(h['cutoff'])} 早于 {h['node_name']}"
            f"计划完成 {_fmt(h['end'])}，逾期 {h['overdue']} 天" for h in hits)
        out.append(_record(
            FREE_TIME, level, "、".join(labels), window, [facts["batch_id"]], keys,
            f"{facts['batch_no']}：{message}"))
    return out


def _rule_dest_storage(facts_list):
    """规则 5（low）：同目的国视角下，境外堆存/查验窗口（CUSTOMS_INSPECT + STORAGE_FEE）重叠。

    说明：batch_routes 只有 country（目的国），**没有目的港字段**，
    故按目的国归组，resource 用「{国名} 境外堆存/查验窗口」表述（需求允许该表述）。
    """
    out = []
    for country, group in _groups(facts_list, lambda f: f["dest_country"]).items():
        for a, b in combinations(group, 2):
            ia, ib = _items_of(a, _DEST_STORAGE_KEYS), _items_of(b, _DEST_STORAGE_KEYS)
            window, keys = _best_overlap(ia, ib)
            if not window:
                continue
            resource = f"{_country_label(country)} 境外堆存/查验窗口"
            out.append(_record(
                DEST_STORAGE, LEVEL_LOW, resource, window,
                [a["batch_id"], b["batch_id"]], keys,
                f"{resource}重叠：{_brief(a, ia)} ｜ {_brief(b, ib)}"))
    return out


# ══════════════════ 汇总 ══════════════════

def _dedupe(conflicts):
    """同一对批次同一 kind 同一资源只保留一条 —— UI 冲突条不被重复项刷屏。

    正常路径下各规则按资源分组，天然不会重复；这里兜底（例如未来接入多程班轮时
    同一批次对可能从多个资源键命中同一 kind）。
    """
    kept = {}
    for c in conflicts:
        key = (c["kind"], c["resource"], tuple(sorted(c["batches"])))
        old = kept.get(key)
        if old is None:
            kept[key] = c
            continue
        starts = [d for d in (_to_date(old["window"][0]), _to_date(c["window"][0])) if d]
        ends = [d for d in (_to_date(old["window"][1]), _to_date(c["window"][1])) if d]
        old["window"] = (_fmt(min(starts)) if starts else None,
                         _fmt(max(ends)) if ends else None)
        for k in c["node_keys"]:
            if k not in old["node_keys"]:
                old["node_keys"].append(k)
    return list(kept.values())


def _sort_key(c):
    """排序：严重度（high → low）优先，其次窗口开始日期，再按 kind / 资源稳定次序。"""
    start = _to_date(c["window"][0]) or date.max
    return (_LEVEL_RANK.get(c["level"], 9), start, c["kind"], c["resource"])


def scan_batch_conflicts(project_id, batch_ids=None, today=None, include_intra=False):
    """扫描同一项目内多个批次的资源占用冲突。

    :param project_id: 项目 id（冲突一律限制在同一项目内比较）
    :param batch_ids: 待扫描批次；None → 该项目全部启用中批次（`db.get_batches`，不含 cancelled）
    :param today: `datetime.date`，缺省取 `services.clock.get_today()`；
                  当前仅预留「过滤已完全过去的窗口」，默认不过滤（见 `_FILTER_PAST`）
    :param include_intra: True 时即使只有 1 个批次也返回**批次内**自检项（FREE_TIME：
                          免堆/免箱截止日 vs 该批次自身后续节点计划）。单批次甘特视图用得到。
    :return: list[dict]，按严重度与开始日期排序，每项：
             {
               "kind": "PORT_WINDOW"|"CUSTOMS_BROKER"|"VESSEL_VOYAGE"|"FREE_TIME"|"DEST_STORAGE",
               "level": "high"|"medium"|"low",
               "resource": str,                  # 人类可读资源名，如 "青岛港" / "COSCO INTEGRITY V0123"
               "window": (start_str, end_str),   # "YYYY-MM-DD"，重叠区间闭区间
               "batches": [batch_id, ...],       # 参与批次（FREE_TIME 为单批次自检）
               "node_keys": [node_key, ...],     # 可用于在甘特图上定位（可能为空 list）
               "message": str,                   # 一句话中文说明
             }
             0 个启用批次 → []；1 个批次 → 默认 []（不构成跨批次冲突），
             传 include_intra=True 时只返回该批次的 FREE_TIME 自检项。
    """
    today = _to_date(today) or get_today()
    batches = _resolve_batches(project_id, batch_ids)
    if not batches:
        return []
    # 跨批次规则至少要两个批次；FREE_TIME 是批次内自检，include_intra=True 时单批次也给
    multi = len(batches) >= 2
    if not multi and not include_intra:
        return []

    project_country = (db.get_project(project_id) or {}).get("country")
    facts_list = [_batch_facts(project_id, b, project_country) for b in batches]

    conflicts = []
    if multi:
        conflicts += _rule_port_window(facts_list)
        conflicts += _rule_customs_broker(facts_list)
        conflicts += _rule_vessel_voyage(facts_list)
        conflicts += _rule_dest_storage(facts_list)
    conflicts += _rule_free_time(facts_list)

    conflicts = _dedupe(conflicts)
    if _FILTER_PAST:
        # 只丢「结束日已早于 today」的窗口；跨今天的窗口保留
        conflicts = [c for c in conflicts if (_to_date(c["window"][1]) or date.min) >= today]
    conflicts.sort(key=_sort_key)
    return conflicts
