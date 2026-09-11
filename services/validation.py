"""数据质量校验（《多式联运.md》§6.8）。

`validate_batch()` 在 UI 保存与 `--check` 中均执行。
13 项，分两档：
  · **blocking**（保存即阻断）：1 ETD≤ETA、2 出发港合法、3 目的国模板存在、
    4 批次号项目内唯一、5 node_key 在模板内且不重复、8 柜号格式/唯一、
    9 关键节点齐、10 时间戳合法偏移、13 计划日期三条不变量
  · **advisory**（提示不阻断）：6 单证 due 落在锚点区间、7 免箱/免堆期 ≥ ETA、
    11 必填客户角色、12 目的国税号

为什么 11/12 不阻断保存：§5.5 规则 2/4 规定缺角色只阻断**相关单证的提交**，
且旧项目客户资料为空时「保留为空、不回溯阻断」。若保存批次就被卡死，
将无法录入任何历史项目（§5.5.4）。
"""

import re
from datetime import date

import db
from services import node_template as nt

# ISO 8601 强制 +08:00（§6.6）
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(\+08:00)$")
# 柜号：4 位字母 + 7 位数字
_CONTAINER_RE = re.compile(r"^[A-Z]{4}\d{7}$")

BLOCKING = "blocking"
ADVISORY = "advisory"


class ValidationError(RuntimeError):
    """保存被阻断（§6.8/§12.7）：携带全部阻断问题清单。"""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("；".join(self.problems))


def _d(s):
    if not s:
        return None
    try:
        y, m, dd = str(s)[:10].split("-")
        return date(int(y), int(m), int(dd))
    except Exception:
        return None


def validate_batch(batch_id, detail=False):
    """返回 [(severity, message), ...]；detail=False 时只返回消息字符串列表。"""
    P = []

    def add(sev, msg):
        P.append((sev, msg))

    b = db.get_batch(batch_id)
    if not b:
        add(BLOCKING, "批次不存在")
        return P if detail else [m for _, m in P]

    route = db.get_route(batch_id) or {}
    nodes = db.get_nodes_by_batch(batch_id)
    etd, eta = _d(route.get("etd")), _d(route.get("eta"))

    # 1) ETD ≤ ETA
    if etd and eta and eta < etd:
        add(BLOCKING, f"ETD({route.get('etd')}) 晚于 ETA({route.get('eta')})")
    elif etd and eta and eta == etd:
        add(BLOCKING, "ETA 必须晚于 ETD 至少 1 天")

    # 2) 出发港合法性
    port = route.get("export_port")
    if port:
        try:
            from services import ports as _ports
            if not _ports.get_port(port):
                add(BLOCKING, f"出发港「{port}」不在国内海港字典中")
        except Exception:
            pass

    # 3) 目的国模板
    proj = db.get_project(b["project_id"]) or {}
    country = route.get("country") or proj.get("country")
    if country:
        from config import get_country
        if not get_country(country):
            add(BLOCKING, f"目的国「{country}」没有节点/单证模板")

    # 4) 批次号项目内唯一
    dup = db.get_conn().execute(
        "SELECT COUNT(*) AS c FROM batches WHERE project_id=? AND batch_no=? AND batch_id!=?",
        (b["project_id"], b["batch_no"], batch_id)).fetchone()
    if dup and dup["c"]:
        add(BLOCKING, f"批次号「{b['batch_no']}」在项目内重复")

    # 5) node_key 在模板内且不重复
    seen = set()
    for n in nodes:
        k = n.get("node_key")
        if not k:
            add(BLOCKING, f"节点 {n.get('node_id')} 缺 node_key")
        elif not nt.by_key(k):
            add(BLOCKING, f"节点 node_key「{k}」不在模板内")
        elif k in seen:
            add(BLOCKING, f"节点 node_key「{k}」重复")
        seen.add(k)

    # 9) 关键节点齐（模板完整性）
    missing_key = nt.KEY_NODES - {n.get("node_key") for n in nodes}
    if missing_key:
        add(BLOCKING, "缺少关键节点：" + "、".join(sorted(missing_key)))

    # 8) 柜号格式 + 批次内唯一
    seen_c = set()
    for c in db.get_containers(batch_id):
        no = (c.get("container_no") or "").strip().upper()
        if not _CONTAINER_RE.match(no):
            add(BLOCKING, f"柜号「{c.get('container_no')}」格式非法（应为 4 位字母 + 7 位数字）")
        elif no in seen_c:
            add(BLOCKING, f"柜号「{no}」在批次内重复")
        seen_c.add(no)

    # 10) 时间戳合法偏移（仅查本批次的日志行 + 批次自身）
    try:
        rows = db.get_conn().execute(
            "SELECT DISTINCT created_at AS v FROM op_log WHERE batch_id=? "
            "AND created_at IS NOT NULL LIMIT 50", (batch_id,)).fetchall()
    except Exception:
        rows = []
    rows = list(rows) + [{"v": b.get("created_at")}]
    for r in rows:
        v = r["v"]
        if v and not _TS_RE.match(str(v)):
            add(BLOCKING, f"时间戳「{v}」缺少合法 +08:00 偏移（§6.6）")
            break

    # 6) 单证截止日落在锚点节点区间内（advisory：手工覆盖时才可能越界）
    #    例外：due_rule='loading_before_hours'（AMS/ISF/ENS 等申报类）本就
    #    以「装船前 N 小时」为基准，截止日**必然早于**装船节点开始日，不适用区间校验。
    for f in db.get_files_by_batch(batch_id):
        if f.get("due_rule") == "loading_before_hours":
            continue
        due = _d(f.get("due_date"))
        if not due:
            continue
        key = f.get("due_node_key")
        node = next((n for n in nodes if n["node_key"] == key), None) if key else None
        if not node:
            continue
        s, e = _d(node.get("plan_start")), _d(node.get("plan_end"))
        if s and due < s:
            add(ADVISORY,
                f"单证《{f['doc_name']}》截止日 {f['due_date']} 早于锚点节点开始日 {node['plan_start']}")
        if e and due > e:
            add(ADVISORY,
                f"单证《{f['doc_name']}》截止日 {f['due_date']} 晚于锚点节点结束日 {node['plan_end']}")

    # 7) 免箱期/免堆期 ≥ ETA
    if eta:
        for field, label in (("free_demurrage_until", "免堆期"),
                             ("free_detention_until", "免箱期")):
            v = _d(route.get(field))
            if v and v < eta:
                add(ADVISORY, f"{label}截止 {route.get(field)} 早于 ETA {route.get('eta')}")

    # 11/12) 客户角色 + 税号（阻断的是单证提交，不是批次保存）
    from services import batches as _bsvc
    for doc_name in ("出口报关单", "海运提单", "到货通知 AN", "D/O 提货单"):
        miss = _bsvc.missing_roles(batch_id, doc_name)
        if miss:
            roles_cn = "、".join({"SHIPPER": "发货人", "CONSIGNEE": "收货人",
                                  "IMPORTER": "进口商"}.get(r, r) for r in sorted(miss))
            add(ADVISORY, f"单证《{doc_name}》缺必填角色：{roles_cn}（提交时将被阻断）")
    if country and _bsvc.importer_tax_missing(batch_id, country):
        add(ADVISORY, f"目的国「{country}」要求进口商税号，但 IMPORTER 未填 tax_id（提交报关单时将被阻断）")

    # 13) 计划日期三条不变量
    if etd and eta and nodes:
        sea = next((n for n in nodes if n["node_key"] == nt.SEA_TRANSIT), None)
        dome = [n for n in nodes if n["area"] == "DOME"]
        oversea = [n for n in nodes if n["area"] == "OVERSEA"]
        if sea:
            if _d(sea.get("plan_start")) != etd:
                add(BLOCKING,
                    f"不变量破坏：海运 start({sea.get('plan_start')}) != ETD({route.get('etd')})")
            if _d(sea.get("plan_end")) != eta:
                add(BLOCKING,
                    f"不变量破坏：海运 end({sea.get('plan_end')}) != ETA({route.get('eta')})")
            if dome:
                last = max(dome, key=lambda n: _d(n.get("plan_end")) or date.min)
                if _d(last.get("plan_end")) != etd:
                    add(BLOCKING,
                        f"不变量破坏：境内末节点 end({last.get('plan_end')}) != ETD({route.get('etd')})")
            if oversea:
                first = min(oversea, key=lambda n: _d(n.get("plan_start")) or date.max)
                if _d(first.get("plan_start")) != eta:
                    add(BLOCKING,
                        f"不变量破坏：境外首节点 start({first.get('plan_start')}) != ETA({route.get('eta')})")

    return P if detail else [m for _, m in P]


def blocking_problems(batch_id):
    return [m for sev, m in validate_batch(batch_id, detail=True) if sev == BLOCKING]


def advisory_problems(batch_id):
    return [m for sev, m in validate_batch(batch_id, detail=True) if sev == ADVISORY]


def validate_batch_or_raise(batch_id):
    """UI 保存路径入口：仅阻断级问题抛错（附全部问题）。"""
    probs = blocking_problems(batch_id)
    if probs:
        raise ValidationError(probs)
    return advisory_problems(batch_id)


def validate_all(raising=False):
    """全库校验：{project_id: [问题...]}（含阻断与提示，标注级别）。"""
    out = {}
    for p in db.get_conn().execute("SELECT project_id FROM projects").fetchall():
        pid = p["project_id"]
        for b in db.get_batches(pid, include_cancelled=True):
            probs = validate_batch(b["batch_id"], detail=True)
            if probs:
                out.setdefault(pid, []).extend(
                    [f"[{b['batch_no']}][{'阻断' if s == BLOCKING else '提示'}] {m}"
                     for s, m in probs])
    if raising and out:
        raise ValidationError([f"{k}: {v}" for k, v in out.items()])
    return out
