"""
验证 services.resource_conflict.scan_batch_conflicts（甘特工作台跨批次资源冲突扫描）

覆盖：
  A 五条规则命中（PORT_WINDOW / CUSTOMS_BROKER / VESSEL_VOYAGE / FREE_TIME / DEST_STORAGE）
  B window 落在预期区间（按 DB 原始节点计划独立重算）、level / batches / node_keys / message
  C 边界：0 批次 / 1 批次 → 空列表；batch_ids 过滤；today 参数不改变默认口径
  D 只读：扫描前后 projects/batches/nodes/files/op_log 行数不变
  E 容错：plan_start/plan_end 为 None 或坏串不抛异常，只丢该条冲突

隔离临时库，绝不触碰 data/logistics.db。
"""

import os
import sys
import shutil
import tempfile
import inspect

os.environ["QT_QPA_PLATFORM"] = "offscreen"          # 与本仓库其它验收脚本保持一致
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:                                                 # Windows 控制台默认 GBK，中文/emoji 会炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from datetime import date, timedelta                # noqa: E402

import db                                           # noqa: E402

# ★ 隔离临时库：必须在 import 任何 services 之前设置 DB_PATH
_TMP = tempfile.mkdtemp(prefix="conflict_")
db.DB_PATH = os.path.join(_TMP, "t.db")
db._conn = None
db.init_db()

from mock_data import DEMO_PROJECT, build_project                  # noqa: E402
from services import batches as batches_svc                        # noqa: E402
from services import node_template as nt                           # noqa: E402
from services import resource_conflict as rc                       # noqa: E402

FAILED = []
RANK = {"high": 0, "medium": 1, "low": 2}   # 本脚本自持的严重度序，不借用被测模块私有表


def check(msg, cond, detail=""):
    """msg 在前（与仓库既有脚本一致），detail 可选，便于失败时看实际值。"""
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}{('  · ' + str(detail)) if detail else ''}")
    if not cond:
        FAILED.append(msg)


def shift(day_str, days):
    """'YYYY-MM-DD' ± 天数 → 'YYYY-MM-DD'。"""
    y, m, d = map(int, day_str.split("-"))
    return (date(y, m, d) + timedelta(days=days)).isoformat()


# ══════════════════ 1. 造数据：B01（完整）+ B02（同港口/同报关行/同船同航次） ══════════════════

r = build_project(DEMO_PROJECT)
PID = r["project_id"]
B01 = r["batch_id"]

b02_row = db.create_batch(PID)                     # 只写 batches 一行，无线路/节点
B02 = b02_row["batch_id"]
db.upsert_route(B02, mode_primary="SEA", mode_chain='["SEA"]',
                country="BR", export_port="QD",    # 与 B01 同出口港 → PORT_WINDOW
                customs_broker="中外运报关行",      # 与 B01 同报关行 → CUSTOMS_BROKER
                etd="2026-09-14", eta="2026-10-25")  # 比 B01 各早 1 天，保证区间重叠
res_gen = batches_svc.ensure_batch_nodes(PID, B02)
# B01 也补上报关行（mock_data 未录），并让 B01/B02 同船同航次
db.update_route(B01, customs_broker="中外运报关行")
db.upsert_vessel(PID, vessel_name="COSCO INTEGRITY", voyage="V0123", batch_id=B01)
db.upsert_vessel(PID, vessel_name="COSCO INTEGRITY", voyage="V0123", batch_id=B02)

nodes01 = {n["node_key"]: n for n in db.get_nodes_by_batch(B01)}
nodes02 = {n["node_key"]: n for n in db.get_nodes_by_batch(B02)}

# FREE_TIME 造点（口径按需求：截止日 < 后续节点 plan_end 才算逾期）
#   B01：免堆期早于 STORAGE_FEE 完成日 4 天（≥3 天 → high）
#   B02：免箱期早于 EMPTY_RETURN 完成日 1 天（<3 天 → medium）；免堆期给远期日 → 不命中
CUT_DEMURRAGE_01 = shift(nodes01[nt.STORAGE_FEE]["plan_end"], -4)
CUT_DETENTION_02 = shift(nodes02[nt.EMPTY_RETURN]["plan_end"], -1)
db.update_route(B01, free_demurrage_until=CUT_DEMURRAGE_01, free_detention_until=None)
db.update_route(B02, free_demurrage_until="2027-01-31", free_detention_until=CUT_DETENTION_02)

check(f"B01 节点 15 个（{db.get_batch(B01)['batch_no']}）", len(nodes01) == 15)
check(f"B02 节点由 ensure_batch_nodes 生成 15 个（created={res_gen['created']}）",
      res_gen["created"] and len(nodes02) == 15)
check("两个批次均为启用中（get_batches 可见）",
      {b["batch_id"] for b in db.get_batches(PID)} == {B01, B02})


# ══════════════════ 2. 独立重算预期区间 ══════════════════

def span(batch_id, keys):
    """按 DB 原始节点计划取该批次若干 node_key 的整体跨度 (min start, max end)。"""
    by_key = {n["node_key"]: n for n in db.get_nodes_by_batch(batch_id)}
    spans = [(by_key[k]["plan_start"], by_key[k]["plan_end"])
             for k in keys if k in by_key and by_key[k]["plan_start"] and by_key[k]["plan_end"]]
    return (min(s for s, _ in spans), max(e for _, e in spans)) if spans else None


def expect_overlap(keys):
    """两批按 keys 的整体跨度的闭区间交集（字符串 YYYY-MM-DD 可直接比较）。"""
    a, b = span(B01, keys), span(B02, keys)
    if not a or not b:
        return None
    s, e = max(a[0], b[0]), min(a[1], b[1])
    return (s, e) if s <= e else None


DOME_KEYS = [n["node_key"] for n in db.get_nodes_by_batch(B01) if n["area"] == "DOME"]
OVERSEA_KEYS = [nt.CUSTOMS_INSPECT, nt.STORAGE_FEE]
EXPECT = {
    rc.PORT_WINDOW: expect_overlap(DOME_KEYS),
    rc.CUSTOMS_BROKER: expect_overlap([nt.EXPORT_CUSTOMS]),
    rc.VESSEL_VOYAGE: expect_overlap([nt.SEA_TRANSIT]),
    rc.DEST_STORAGE: expect_overlap(OVERSEA_KEYS),
}


# ══════════════════ 3. 只读护栏：只允许扫描自身发生，写库动作都在护栏之外 ══════════════════

def row_counts():
    conn = db.get_conn()
    return {t: conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
            for t in ("projects", "batches", "nodes", "files", "op_log")}


def read_only_scan(label, *args, **kwargs):
    """扫描前后比对 projects/batches/nodes/files/op_log 行数：必须完全一致。"""
    b = row_counts()
    out = rc.scan_batch_conflicts(*args, **kwargs)
    a = row_counts()
    check(f"只读：{label} 前后行数不变（含 op_log）", a == b, f"{b} → {a}")
    return out


# ══════════════════ 4. 主扫描 ══════════════════

res = read_only_scan("全项目扫描", PID)
by_kind = {}
for c in res:
    by_kind.setdefault(c["kind"], []).append(c)

print("\n  ── 扫描结果明细 ──")
for c in res:
    print(f"    [{c['level']:<6}] {c['kind']:<15} {c['resource']}  "
          f"{c['window'][0]}~{c['window'][1]}  batches={len(c['batches'])}  {c['message']}")
print()

check("签名与需求一致 (project_id, batch_ids=None, today=None, include_intra=False)",
      str(inspect.signature(rc.scan_batch_conflicts)) ==
      "(project_id, batch_ids=None, today=None, include_intra=False)")
check("单批次 + include_intra=True → 返回 FREE_TIME 自检（甘特单批次视图要用）",
      isinstance(rc.scan_batch_conflicts(PID, [B01], include_intra=True), list)
      and all(c["kind"] == rc.FREE_TIME
              for c in rc.scan_batch_conflicts(PID, [B01], include_intra=True)))
check("单批次默认仍为 []（跨批次口径不变）",
      rc.scan_batch_conflicts(PID, [B01]) == [])
check("扫描返回非空 list[dict]", isinstance(res, list) and res and all(isinstance(c, dict) for c in res))
check("命中全部五类规则（缺哪类见下）",
      set(by_kind) == {rc.PORT_WINDOW, rc.CUSTOMS_BROKER, rc.VESSEL_VOYAGE,
                       rc.FREE_TIME, rc.DEST_STORAGE},
      f"实际={sorted(by_kind)}")

# ── 条目结构 ──
FIELDS = {"kind", "level", "resource", "window", "batches", "node_keys", "message"}
check("每条字段齐全且类型/取值合法",
      all(set(c) == FIELDS and c["level"] in ("high", "medium", "low")
          and isinstance(c["window"], tuple) and len(c["window"]) == 2
          and c["window"][0] <= c["window"][1]
          and isinstance(c["resource"], str) and c["resource"]
          and isinstance(c["batches"], list) and len(c["batches"]) >= 1
          and isinstance(c["node_keys"], list) and isinstance(c["message"], str)
          and c["message"] for c in res))
check("跨批次规则 batches ≥2；FREE_TIME 为单批次自检 =1",
      all((len(c["batches"]) == 1) == (c["kind"] == rc.FREE_TIME) for c in res))
check("排序：严重度非递减，同级别按窗口开始日非递减",
      all((RANK[res[i]["level"]], res[i]["window"][0]) <=
          (RANK[res[i + 1]["level"]], res[i + 1]["window"][0])
          for i in range(len(res) - 1)))
check("同一对批次同一 kind 同一资源只产出一条",
      len({(c["kind"], c["resource"], tuple(sorted(c["batches"]))) for c in res}) == len(res))
check("node_keys 无重复且均为真实 node_key",
      all(len(c["node_keys"]) == len(set(c["node_keys"]))
          and all(k in nodes01 for k in c["node_keys"]) for c in res))

# ── 规则 1：PORT_WINDOW ──
print("\n  ── 规则 1 PORT_WINDOW（同出口港境内段重叠） ──")
recs = by_kind.get(rc.PORT_WINDOW, [])
check("命中 1 条（B01/B02 同出口港 QD）", len(recs) == 1)
if recs:
    c = recs[0]
    check("level=high", c["level"] == "high")
    check("resource=青岛港（港 key 翻译为人类可读名）", c["resource"] == "青岛港", c["resource"])
    check(f"batches 数量 2 且为 {{B01,B02}}", len(c["batches"]) == 2 and set(c["batches"]) == {B01, B02})
    check(f"window 落在预期重叠区间 {EXPECT[rc.PORT_WINDOW]}", c["window"] == EXPECT[rc.PORT_WINDOW],
          f"实际={c['window']}")
    check("node_keys 命中的均为 DOME 节点",
          c["node_keys"] and all(nodes01[k]["area"] == "DOME" for k in c["node_keys"]),
          f"keys={c['node_keys']}")
    check("message 含两个批次号与各自窗口",
          db.get_batch(B01)["batch_no"] in c["message"] and db.get_batch(B02)["batch_no"] in c["message"]
          and "~" in c["message"])

# ── 规则 2：CUSTOMS_BROKER ──
print("\n  ── 规则 2 CUSTOMS_BROKER（同报关行出口报关窗口重叠） ──")
recs = by_kind.get(rc.CUSTOMS_BROKER, [])
check("命中 1 条（同报关行 中外运报关行）", len(recs) == 1)
if recs:
    c = recs[0]
    check("level=high", c["level"] == "high")
    check("resource=中外运报关行", c["resource"] == "中外运报关行", c["resource"])
    check("batches 数量 2 且为 {B01,B02}", len(c["batches"]) == 2 and set(c["batches"]) == {B01, B02})
    check(f"window 落在预期重叠区间 {EXPECT[rc.CUSTOMS_BROKER]}",
          c["window"] == EXPECT[rc.CUSTOMS_BROKER], f"实际={c['window']}")
    check("node_keys == ['EXPORT_CUSTOMS']", c["node_keys"] == [nt.EXPORT_CUSTOMS], f"{c['node_keys']}")

# ── 规则 3：VESSEL_VOYAGE ──
print("\n  ── 规则 3 VESSEL_VOYAGE（同船同航次海上运输重叠） ──")
recs = by_kind.get(rc.VESSEL_VOYAGE, [])
check("命中 1 条（COSCO INTEGRITY V0123）", len(recs) == 1)
if recs:
    c = recs[0]
    check("level=high", c["level"] == "high")
    check("resource=COSCO INTEGRITY V0123", c["resource"] == "COSCO INTEGRITY V0123", c["resource"])
    check("batches 数量 2 且为 {B01,B02}", len(c["batches"]) == 2 and set(c["batches"]) == {B01, B02})
    check(f"window 落在预期重叠区间 {EXPECT[rc.VESSEL_VOYAGE]}",
          c["window"] == EXPECT[rc.VESSEL_VOYAGE], f"实际={c['window']}")
    check("node_keys == ['SEA_TRANSIT']", c["node_keys"] == [nt.SEA_TRANSIT], f"{c['node_keys']}")

# ── 规则 4：FREE_TIME（批次内自检，两条：B01 免堆期 high / B02 免箱期 medium） ──
print("\n  ── 规则 4 FREE_TIME（批次内免期逾期自检） ──")
recs = by_kind.get(rc.FREE_TIME, [])
check("命中 2 条（B01 免堆期 + B02 免箱期）", len(recs) == 2, f"实际={len(recs)}")
rec01 = next((c for c in recs if c["batches"] == [B01]), None)
rec02 = next((c for c in recs if c["batches"] == [B02]), None)
check("存在 batches=[B01] 的条目", rec01 is not None)
check("存在 batches=[B02] 的条目", rec02 is not None)
if rec01:
    # 预期：逾期节点 = plan_end 晚于截止日的 CUSTOMS_INSPECT / STORAGE_FEE，window=(截止日, 最晚 plan_end)
    ends = [nodes01[k]["plan_end"] for k in (nt.STORAGE_FEE, nt.CUSTOMS_INSPECT)
            if CUT_DEMURRAGE_01 < nodes01[k]["plan_end"]]
    overdue = (date(*map(int, max(ends).split("-"))) - date(*map(int, CUT_DEMURRAGE_01.split("-")))).days
    check(f"B01: window=(免堆期截止日, 最晚逾期节点 {max(ends)})",
          rec01["window"] == (CUT_DEMURRAGE_01, max(ends)), f"实际={rec01['window']}")
    check(f"B01: 逾期 {overdue} 天 ≥3 → level=high", overdue >= 3 and rec01["level"] == "high",
          f"level={rec01['level']}")
    check("B01: resource=免堆期（免箱期未录，不产生条款）", rec01["resource"] == "免堆期",
          rec01["resource"])
    check("B01: node_keys 为逾期节点集合",
          set(rec01["node_keys"]) == {k for k in (nt.STORAGE_FEE, nt.CUSTOMS_INSPECT)
                                      if CUT_DEMURRAGE_01 < nodes01[k]["plan_end"]},
          f"{rec01['node_keys']}")
if rec02:
    end = nodes02[nt.EMPTY_RETURN]["plan_end"]
    overdue = (date(*map(int, end.split("-"))) - date(*map(int, CUT_DETENTION_02.split("-")))).days
    check(f"B02: window=(免箱期截止日, EMPTY_RETURN {end})",
          rec02["window"] == (CUT_DETENTION_02, end), f"实际={rec02['window']}")
    check(f"B02: 逾期 {overdue} 天 <3 → level=medium", overdue < 3 and rec02["level"] == "medium",
          f"level={rec02['level']}")
    check("B02: resource=免箱期", rec02["resource"] == "免箱期", rec02["resource"])
    check("B02: node_keys == ['EMPTY_RETURN']", rec02["node_keys"] == [nt.EMPTY_RETURN],
          f"{rec02['node_keys']}")
check("B02 免堆期取远期日 → 不产生第 3 条 FREE_TIME",
      not any(c["batches"] == [B02] and "免堆期" in c["resource"] for c in recs))

# ── 规则 5：DEST_STORAGE ──
print("\n  ── 规则 5 DEST_STORAGE（同目的国境外堆存/查验窗口重叠） ──")
recs = by_kind.get(rc.DEST_STORAGE, [])
if EXPECT[rc.DEST_STORAGE] is None:
    print("  SKIP  DEST_STORAGE：B01/B02 的 CUSTOMS_INSPECT+STORAGE_FEE 窗口无重叠（数据构造原因）")
else:
    check("命中 1 条（同目的国 BR）", len(recs) == 1)
if recs:
    c = recs[0]
    check("level=low", c["level"] == "low")
    check("resource 为目的国 + 境外堆存/查验窗口表述",
          c["resource"] == "巴西 境外堆存/查验窗口", c["resource"])
    check("batches 数量 2 且为 {B01,B02}", len(c["batches"]) == 2 and set(c["batches"]) == {B01, B02})
    check(f"window 落在预期重叠区间 {EXPECT[rc.DEST_STORAGE]}",
          c["window"] == EXPECT[rc.DEST_STORAGE], f"实际={c['window']}")
    check("node_keys 均为境外堆存/查验节点",
          set(c["node_keys"]) <= {nt.CUSTOMS_INSPECT, nt.STORAGE_FEE}, f"{c['node_keys']}")


# ══════════════════ 5. 边界：0/1 批次、batch_ids 过滤、today ══════════════════

print("\n  ── 边界与参数 ──")
check("只传 1 个批次 → []", read_only_scan("单批次扫描", PID, batch_ids=[B01]) == [])
check("不存在的项目（0 批次）→ []", rc.scan_batch_conflicts("no-such-project") == [])
check("batch_ids=[B01,B02] 与缺省结果一致",
      rc.scan_batch_conflicts(PID, batch_ids=[B01, B02]) == res)
check("batch_ids 含他项目/坏 id 时不炸且忽略",
      rc.scan_batch_conflicts(PID, batch_ids=[B01, B02, "ghost-batch"]) == res)
check("today 传过去的日期（默认不过滤）结果不变",
      len(rc.scan_batch_conflicts(PID, today=date(2027, 1, 1))) == len(res))
check("today 传坏值（字符串/None）不抛异常",
      len(rc.scan_batch_conflicts(PID, today="not-a-date")) == len(res)
      and len(rc.scan_batch_conflicts(PID, today=None)) == len(res))

# cancelled 批次默认排除：取消 B02 后默认扫描只剩 1 个启用批次 → []
batches_svc.cancel_batch(B02, reason="验证默认排除 cancelled")
check("取消 B02 后默认扫描 → []（只统计启用中批次）", rc.scan_batch_conflicts(PID) == [])
check("显式传已取消批次同样被忽略 → []",
      rc.scan_batch_conflicts(PID, batch_ids=[B01, B02]) == [])
batches_svc.restore_batch(B02, reason="恢复以继续后续检查")
check("恢复 B02 后冲突重新出现",
      read_only_scan("恢复后复扫", PID) == res)

# ══════════════════ 6. 容错：脏日期不抛异常 ══════════════════

print("\n  ── 脏数据容错 ──")
db.update_node(PID, nodes02[nt.SEA_TRANSIT]["node_id"], batch_id=B02,
               plan_start="not-a-date", plan_end=None)          # 坏串 + 空值
try:
    res_dirty = read_only_scan("脏数据（坏串/None）扫描", PID)
    no_raise = True
except Exception as e:                                          # noqa: BLE001
    res_dirty, no_raise = [], False
    print(f"      异常：{type(e).__name__}: {e}")
check("plan_start/plan_end 为坏串/None 时不抛异常", no_raise)
kinds_dirty = {c["kind"] for c in res_dirty}
check("只丢该节点相关冲突（VESSEL_VOYAGE 消失），其余规则仍命中",
      rc.VESSEL_VOYAGE not in kinds_dirty
      and {rc.PORT_WINDOW, rc.CUSTOMS_BROKER, rc.FREE_TIME, rc.DEST_STORAGE} <= kinds_dirty,
      f"kinds={sorted(kinds_dirty)}")
db.update_node(PID, nodes01[nt.LASHING]["node_id"], batch_id=B01, plan_start=None)  # 境内节点缺边
try:
    res_dirty2 = read_only_scan("单边缺失扫描", PID)
    no_raise2 = True
except Exception as e:                                          # noqa: BLE001
    res_dirty2, no_raise2 = [], False
    print(f"      异常：{type(e).__name__}: {e}")
check("单边缺失（只有 plan_end）不抛异常，PORT_WINDOW 仍按剩余节点判定", no_raise2 and bool(res_dirty2))


# ══════════════════ 收尾 ══════════════════

try:
    db.get_conn().close()
except Exception:
    pass
shutil.rmtree(_TMP, ignore_errors=True)

print("\n" + ("PASS ALL" if not FAILED else f"FAIL {len(FAILED)}"))
if FAILED:
    for f in FAILED:
        print("  - " + f)
sys.exit(0 if not FAILED else 1)
