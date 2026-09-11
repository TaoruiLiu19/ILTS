#!/usr/bin/env python3
"""
多式联运统一模型迁移工具（数据模型升级 阶段0+1） 按《数据模型升级.md》§5

把现有「批次化 V2（batch_routes 单线路 + 批次级 nodes/files）」存量库，
平滑升级到「Project → Batch → Route → Leg → Node」统一多式联运模型：

  · 新增统一模型表（locations / route_templates / route_template_legs / routes /
    route_legs / node_defs / doc_definitions / file_records / submission_logs /
    charges / subcontracts / insurances）——由 db.init_db() 幂等执行 DDL。
  · 每个批次建立「1 条默认线路 + 1 个默认海运 sea leg」。
  · 节点回填锚点列：route_id / leg_id / node_def_id / planned_date / actual_date。
  · 播种统一主数据：node_defs（按运输方式）、doc_definitions（单证定义）、
    locations（青岛/上海/瑟佩蒂巴示例）、REQUIRED_DOCS 线路模板种子。
  · 兼容：既有 V2 接口、既有 nodes/files 表结构与返回结构完全不变，前端零改动。

用法:
  python tools/migrate_multimodal.py --check     # 预检，不修改数据
  python tools/migrate_multimodal.py --apply     # 执行迁移（先备份 + 文件锁 + 事务）
  python tools/migrate_multimodal.py --rollback  # 从最近备份恢复（交互确认）

可被环境变量 ILTS_DB_PATH 覆盖数据库路径（用于测试/演示，避免误动线上库）。
"""

import os
import sys
import argparse
import sqlite3
import shutil
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DB_PATH = os.environ.get("ILTS_DB_PATH") or os.path.join(ROOT, "data", "logistics.db")
LOCK_PATH = DB_PATH + ".mmigrate.lock"

# 迁移守卫键：跑通一次即记录，防止重复/误跑（幂等由唯一约束兜底）
_GUARD_KEY = "multimodal_migrated_v1"


def _now():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00")


# ── 文件锁 / 备份 ──
def acquire_lock():
    if os.path.exists(LOCK_PATH):
        try:
            with open(LOCK_PATH, "r", encoding="utf-8") as f:
                pid = f.read().strip()
        except OSError:
            pid = "?"
        print(f"[ERROR] 迁移已被锁定 (PID {pid})。若无迁移进行中，请删除 {LOCK_PATH} 后重试")
        return False
    with open(LOCK_PATH, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return True


def release_lock():
    if os.path.exists(LOCK_PATH):
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass


def backup_db():
    if not os.path.exists(DB_PATH):
        print(f"[ERROR] 未找到数据库: {DB_PATH}")
        return None
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = f"{DB_PATH}.mmbak.{ts}"
    n = 2
    while os.path.exists(bak):
        bak = f"{DB_PATH}.mmbak.{ts}.{n}"
        n += 1
    shutil.copy2(DB_PATH, bak)
    print(f"[OK] 数据库已备份 → {bak}")
    return bak


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _table_names(conn):
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _has_column(conn, table, col):
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))


# ── 主数据种子 ──

# 按运输方式的标准节点模板（§3.7 node_defs），node_key 沿用既有 15 节点稳定标识
SEA_NODE_DEFS = [
    # (mode, key, name, phase, work_item, default_anchor_level)
    ("sea", "EMPTY_PICKUP",    "提空箱",              "启动", "货物组织", "node"),
    ("sea", "LASHING",         "装箱/加固（捆扎固定）", "启动", "货物组织", "node"),
    ("sea", "GATE_IN",         "返场/集港（还重箱）",   "策划", "运输组织", "node"),
    ("sea", "EXPORT_CUSTOMS",  "办理出口报关",        "策划", "合规申报", "node"),
    ("sea", "LOADING",         "实施货物装船",        "实施", "运输执行", "node"),
    ("sea", "SEA_TRANSIT",     "进行海上运输",        "实施", "运输执行", "node"),
    ("sea", "ARRIVAL_NOTICE",  "收到到货通知",        "实施", "运输执行", "node"),
    ("sea", "D_O_COLLECT",     "到港换单/领取 D/O",   "实施", "到港换单", "node"),
    ("sea", "IMPORT_LICENSE",  "申请进口许可 LI",     "实施", "合规申报", "node"),
    ("sea", "IMPORT_CUSTOMS",  "进行进口清关申报",    "实施", "合规申报", "node"),
    ("sea", "CUSTOMS_INSPECT", "执行海关查验/放行",   "实施", "合规申报", "node"),
    ("sea", "STORAGE_FEE",     "支付临时堆存费",      "管控", "费用管理", "node"),
    ("sea", "INLAND_TRANSPORT", "安排境外内陆运输",   "管控", "运输组织", "node"),
    ("sea", "SITE_DELIVERY",   "完成工地交付",        "收尾", "交付",     "node"),
    ("sea", "EMPTY_RETURN",    "还空箱",             "收尾", "空箱管理", "node"),
]
ROAD_NODE_DEFS = [
    ("road", "ROAD_PICKUP",   "安排提货",   "启动", "货物组织", "node"),
    ("road", "ROAD_LOAD",     "装车",       "策划", "运输组织", "node"),
    ("road", "ROAD_TRANSIT",  "陆路运输",   "实施", "运输执行", "node"),
    ("road", "ROAD_DELIVERY", "送达交付",   "收尾", "交付",     "node"),
]

# 单证定义种子（§3.9 doc_definitions），含各级别锚点示例
DOC_DEFS = [
    # (code, name, anchor_level, required, phase, work_item)
    ("MASTER_CONTRACT", "主合同",           "project", 1, "启动", "合同"),
    ("PROJECT_MANUAL",  "项目手册",         "project", 1, "启动", "手册"),
    ("WBS",             "WBS / 项目预算",   "project", 1, "启动", "预算"),
    ("DAILY_REPORT",    "项目日报",         "project", 1, "管控", "汇报"),
    ("TRACKING_TABLE",  "物流动态跟踪表",   "project", 1, "管控", "汇报"),
    ("PROGRESS_REPORT", "项目进度报告",     "project", 1, "管控", "汇报"),
    ("BATCH_PLAN",      "批次实施计划",     "batch",   1, "策划", "计划"),
    ("STAFF_PLAN",      "人员安排计划",     "batch",   1, "策划", "计划"),
    ("COST_CONFIRM",    "费用确认单",       "batch",   1, "管控", "费用"),
    ("MULTIMODAL_BL",   "多式联运提单(主单)", "route",  1, "实施", "单证"),
    ("FULL_CARGO_INS",  "全程保险",         "route",   1, "策划", "保险"),
    ("FULL_TRACKING",   "全程跟踪方案",     "route",   1, "实施", "跟踪"),
    ("LEG_WAYBILL",     "段级运单",         "leg",     1, "实施", "单证"),
    ("LEG_CARRIER_CT",  "段级承运合同",     "leg",     1, "策划", "合同"),
    ("LEG_INSURANCE",   "段级保险",         "leg",     1, "策划", "保险"),
    ("CUSTOMS_DECL",    "报关单",           "node",    1, "策划", "合规申报"),
    ("SO_LETTER",       "SO 订舱确认",      "node",    1, "策划", "订舱"),
    ("VGM",             "VGM 重量验证",     "node",    1, "策划", "申报"),
    ("PACKING_LIST",    "装箱单",           "node",    1, "策划", "单证"),
    ("DELIVERY_NOTE",   "到货/交付单",      "node",    1, "收尾", "交付"),
]

# 示例线路模板种子（§3.4）：公路+海运+公路
ROUTE_TEMPLATES = [
    {"name": "公海公联运", "description": "门到门 road+sea+road", "legs": [
        {"seq": 1, "mode": "road", "origin_name": "青岛仓库"},
        {"seq": 2, "mode": "sea", "origin_name": "青岛", "dest_name": "Sepetiba"},
        {"seq": 3, "mode": "road", "origin_name": "Sepetiba", "dest_name": "工地"}],
     },
    {"name": "纯海运", "description": "门到港 sea", "legs": [
        {"seq": 1, "mode": "sea", "origin_name": "青岛", "dest_name": "Sepetiba"}],
     },
]

# 示例地点种子（§3.3）
LOCATION_SEEDS = [
    {"name": "青岛",   "unlocode": "CNQIN", "loc_type": "port",    "country": "CN"},
    {"name": "上海",   "unlocode": "CNSHA", "loc_type": "port",    "country": "CN"},
    {"name": "Sepetiba", "unlocode": "BRSSZ", "loc_type": "port",  "country": "BR"},
]


def _backfill_route_exists(conn, batch_id):
    return conn.execute("SELECT 1 FROM routes WHERE batch_id=? AND is_active=1 LIMIT 1",
                        (batch_id,)).fetchone() is not None


def _backfill_route(conn, batch_id, project_id, batch_no, br):
    """为一个批次建立默认线路 + 默认海运段；幂等（唯一 active 线路约束兜底）。"""
    if _backfill_route_exists(conn, batch_id):
        return conn.execute("SELECT route_id FROM routes WHERE batch_id=? AND is_active=1 "
                            "LIMIT 1", (batch_id,)).fetchone()["route_id"]
    # 以既有 batch_routes（限 SEA）为准回填线路名称与段信息
    rid_auto = _next_route_id(conn)
    conn.execute(
        "INSERT INTO routes (route_id, batch_id, route_name, status, is_active, created_at, "
        "updated_at) VALUES (?,?,?,'active',1,?,?)",
        (rid_auto, batch_id, f"{batch_no} 默认线路", _now(), _now()))
    leg_mode = "sea"
    leg_id = _next_leg_id(conn)
    origin = (br["export_port"] if br else None) or "青岛"
    dest = (br["eta_port"] if br and "eta_port" in br.keys() else None) or "Sepetiba"
    etd = br["etd"] if br and br["etd"] else None
    eta = br["eta"] if br and br["eta"] else None
    conn.execute(
        "INSERT INTO route_legs (leg_id, route_id, seq, mode, origin_name, dest_name, "
        "planned_etd, planned_eta, status, created_at, updated_at) "
        "VALUES (?,?,1,?,?,?,?,?,'pending',?,?)",
        (leg_id, rid_auto, leg_mode, origin, dest, etd, eta, _now(), _now()))
    return rid_auto


def _next_route_id(conn):
    r = conn.execute("SELECT COALESCE(MAX(route_id),0)+1 AS x FROM routes").fetchone()
    return r["x"]


def _next_leg_id(conn):
    r = conn.execute("SELECT COALESCE(MAX(leg_id),0)+1 AS x FROM route_legs").fetchone()
    return r["x"]


def _seed_node_defs(conn):
    done = 0
    for mode, key, name, phase, wi, anchor in SEA_NODE_DEFS + ROAD_NODE_DEFS:
        conn.execute(
            "INSERT INTO node_defs (mode, node_key, node_name, phase, work_item, "
            "default_anchor_level) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(mode, node_key) DO UPDATE SET "
            "node_name=excluded.node_name, phase=excluded.phase, "
            "work_item=excluded.work_item, default_anchor_level=excluded.default_anchor_level",
            (mode, key, name, phase, wi, anchor))
        done += 1
    return done


def _seed_doc_defs(conn):
    done = 0
    for code, name, anchor, required, phase, wi in DOC_DEFS:
        conn.execute(
            "INSERT INTO doc_definitions (doc_code, doc_name, default_anchor_level, required, "
            "phase, work_item, created_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(doc_code) DO UPDATE SET doc_name=excluded.doc_name, "
            "default_anchor_level=excluded.default_anchor_level, required=excluded.required, "
            "phase=excluded.phase, work_item=excluded.work_item",
            (code, name, anchor, required, phase, wi, _now()))
        done += 1
    return done


def _seed_locations(conn):
    done = 0
    for loc in LOCATION_SEEDS:
        cur = conn.execute(
            "INSERT INTO locations (unlocode, name, loc_type, country, created_at) VALUES (?,?,?,?,?)",
            (loc["unlocode"], loc["name"], loc["loc_type"], loc["country"], _now()))
        done += 1
    return done


def _seed_route_templates(conn):
    done = 0
    for tpl in ROUTE_TEMPLATES:
        row = conn.execute("SELECT template_id FROM route_templates WHERE template_name=?",
                           (tpl["name"],)).fetchone()
        if row:
            tid = row["template_id"]
        else:
            cur = conn.execute(
                "INSERT INTO route_templates (template_name, description, status, created_at) "
                "VALUES (?,?,'active',?)", (tpl["name"], tpl["description"], _now()))
            tid = cur.lastrowid
        for lg in tpl["legs"]:
            conn.execute(
                "INSERT INTO route_template_legs (template_id, seq, mode, origin_name, dest_name) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(template_id, seq) DO UPDATE SET mode=excluded.mode",
                (tid, lg["seq"], lg["mode"], lg.get("origin_name"), lg.get("dest_name")))
        done += 1
    return done


def _backfill_node_anchors(conn, batch_id, route_id, leg_id):
    """回填节点锚点：node_def_id（按 node_key 匹配 sea/road 模板）+ leg_id + planned/actual。"""
    nodes = conn.execute(
        "SELECT node_id, node_key, plan_start, actual_completion_date, status "
        "FROM nodes WHERE batch_id=? ", (batch_id,)).fetchall()
    ndef_by_key = {}
    for r in conn.execute("SELECT node_def_id, node_key FROM node_defs WHERE mode='sea'"):
        ndef_by_key[r["node_key"]] = r["node_def_id"]
    n = 0
    for nd in nodes:
        defid = ndef_by_key.get(nd["node_key"])
        conn.execute(
            "UPDATE nodes SET route_id=?, leg_id=?, node_def_id=?, planned_date=?, actual_date=? "
            "WHERE batch_id=? AND node_id=?",
            (route_id, leg_id, defid, nd["plan_start"], nd["actual_completion_date"],
             batch_id, nd["node_id"]))
        n += 1
    return n


# ── 迁移主流程 ──
def _scan(conn):
    """返回迁移前的统计诊断。"""
    d = {}
    d["projects"] = conn.execute("SELECT COUNT(*) AS c FROM projects").fetchone()["c"]
    d["batches"] = conn.execute("SELECT COUNT(*) AS c FROM batches").fetchone()["c"]
    d["nodes"] = conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()["c"]
    already = conn.execute("SELECT COUNT(*) AS c FROM nodes WHERE leg_id IS NOT NULL").fetchone()["c"]
    d["nodes_already_anchored"] = already
    d["routes"] = conn.execute("SELECT COUNT(*) AS c FROM routes").fetchone()["c"]
    d["file_records"] = conn.execute("SELECT COUNT(*) AS c FROM file_records").fetchone()["c"]
    d["node_defs"] = conn.execute("SELECT COUNT(*) AS c FROM node_defs").fetchone()["c"]
    d["doc_defs"] = conn.execute("SELECT COUNT(*) AS c FROM doc_definitions").fetchone()["c"]
    d["locations"] = conn.execute("SELECT COUNT(*) AS c FROM locations").fetchone()["c"]
    d["guard"] = conn.execute("SELECT 1 FROM settings WHERE key=?", (_GUARD_KEY,)).fetchone() is not None
    return d


def do_migrate(conn, dry=False, force_overwrite=False):
    """执行多式联运统一模型迁移。dry=True 时只打印诊断。返回结果统计 dict。"""
    stats = _scan(conn)
    if dry:
        print("[CHECK] 迁移前诊断：")
        for k, v in stats.items():
            print(f"         {k:24s} {v}")
        if stats["nodes_already_anchored"] and not stats["guard"]:
            print("[HINT] 已有节点带 leg_id，但守卫键未置——可能此前部分迁移中断，可安全重跑")
        return stats

    if stats["guard"] and not force_overwrite:
        print(f"[SKIP] 已迁移过（settings.{_GUARD_KEY}），幂等跳过；用 --force 可全量重灌种子")
        return stats

    # 1) 主数据种子
    nd = _seed_node_defs(conn)
    dd = _seed_doc_defs(conn)
    lo = _seed_locations(conn)
    tpl = _seed_route_templates(conn)
    print(f"[seed] node_defs {nd} / doc_definitions {dd} / locations {lo} / templates {tpl}")

    # 2) 每批次的默认线路 + 海运段 + 节点锚点
    batches = conn.execute(
        "SELECT b.batch_id, b.project_id, b.batch_no, br.etd, br.eta, br.export_port "
        "FROM batches b LEFT JOIN batch_routes br ON br.batch_id = b.batch_id").fetchall()
    n_route = n_leg = n_node = 0
    for b in batches:
        rid = _backfill_route(conn, b["batch_id"], b["project_id"], b["batch_no"],
                              {"etd": b["etd"], "eta": b["eta"], "export_port": b["export_port"]})
        leg = conn.execute("SELECT leg_id FROM route_legs WHERE route_id=?", (rid,)).fetchone()
        n_route += 1
        n_leg += 1
        n_node += _backfill_node_anchors(conn, b["batch_id"], rid, leg["leg_id"])
    print(f"[backfill] routes {n_route} / legs {n_leg} / nodes anchored {n_node}")

    # 3) 落守卫
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (_GUARD_KEY, _now()))
    conn.commit()
    print("[OK] 多式联运统一模型迁移完成")
    return {"seed_node_defs": nd, "seed_doc_defs": dd, "seed_locations": lo,
            "seed_templates": tpl, "routes": n_route, "legs": n_leg, "nodes": n_node}


def main():
    ap = argparse.ArgumentParser(description="多式联运统一模型迁移")
    ap.add_argument("--check", action="store_true", help="预检查，不修改数据")
    ap.add_argument("--apply", action="store_true", help="执行迁移（备份+锁+事务）")
    ap.add_argument("--rollback", action="store_true", help="从最近备份恢复（交互确认）")
    ap.add_argument("--force", action="store_true", help="忽略守卫键，重灌种子")
    ap.add_argument("--no-backup", action="store_true", help="跳过备份（测试用）")
    args = ap.parse_args()

    if args.check:
        conn = _connect()
        try:
            do_migrate(conn, dry=True)
        finally:
            conn.close()
        return

    if args.rollback:
        import glob
        baks = sorted(glob.glob(DB_PATH + ".mmbak.*"), reverse=True)
        if not baks:
            print("[ERROR] 没有找到 mmbak 备份，无法回滚")
            return
        latest = baks[0]
        ans = input(f"确认从 {latest} 恢复？（该操作会覆盖当前库）[y/N] ").strip().lower()
        if ans != "y":
            print("已取消")
            return
        shutil.copy2(latest, DB_PATH)
        print(f"[OK] 已从 {latest} 恢复")
        return

    if not args.apply:
        ap.print_help()
        return

    if not acquire_lock():
        return
    try:
        if not args.no_backup:
            backup_db()
        conn = _connect()
        try:
            do_migrate(conn, dry=False, force_overwrite=args.force)
        finally:
            conn.close()
    finally:
        release_lock()


if __name__ == "__main__":
    main()