#!/usr/bin/env python3
"""
批次化迁移工具 (多式联运升级 Phase 1A)　按《多式联运.md》§7
把「旧 12 节点 / 无批次（pre-batch）schema」的存量库无感迁移到批次化 V2 schema。

用法:
  python tools/migrate_batches.py --check     # 预检查，不修改数据
  python tools/migrate_batches.py --apply     # 执行迁移（先备份 + 文件锁 + 事务）
  python tools/migrate_batches.py --rollback  # 从最近备份恢复（交互确认）

可被环境变量 ILTS_DB_PATH 覆盖数据库路径（用于测试/演示，避免误动线上库）。
"""

import os
import sys
import argparse
import sqlite3
import shutil
from datetime import datetime
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────────────────────
DB_PATH = os.environ.get("ILTS_DB_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "logistics.db")
LOCK_PATH = DB_PATH + ".migrate.lock"

# §7.3 nodes 重建目标结构（与 db.py SCHEMA_SQL 的 nodes 一致）
NODES_V2_DDL = """
CREATE TABLE IF NOT EXISTS nodes (
    batch_id                TEXT NOT NULL,
    node_id                 INTEGER NOT NULL,
    node_key                TEXT NOT NULL,
    node_name               TEXT NOT NULL,
    role_label              TEXT NOT NULL DEFAULT '',
    seq                     INTEGER NOT NULL DEFAULT 0,
    area                    TEXT NOT NULL DEFAULT 'DOME',
    calendar_mode           TEXT NOT NULL DEFAULT 'NATURAL',
    is_key_node             INTEGER NOT NULL DEFAULT 0,
    default_duration        INTEGER NOT NULL DEFAULT 1,
    duration                INTEGER NOT NULL DEFAULT 1,
    plan_start              TEXT,
    plan_end                TEXT,
    is_delayed              INTEGER NOT NULL DEFAULT 0,
    delay_days              INTEGER NOT NULL DEFAULT 0,
    status                  TEXT NOT NULL DEFAULT 'Pending',
    actual_completion_date  TEXT,
    remark                  TEXT,
    PRIMARY KEY (batch_id, node_id),
    UNIQUE (batch_id, node_key)
)
"""

# 旧 12 节点 → 新 node_key（§5.2 迁移映射表）
OLD_12_TO_NEW = {
    1: "EXPORT_CUSTOMS", 2: "GATE_IN", 3: "LASHING", 4: "LOADING",
    5: "SEA_TRANSIT", 6: "D_O_COLLECT", 7: "IMPORT_LICENSE",
    8: "IMPORT_CUSTOMS", 9: "CUSTOMS_INSPECT", 10: "STORAGE_FEE",
    11: "INLAND_TRANSPORT", 12: "SITE_DELIVERY",
}

# 新增节点（旧 12 节点迁移后补 Pending；seq 由迁移阶段重排）
NEW_NODES_TO_ADD = [
    {"node_key": "EMPTY_PICKUP",   "node_name": "提空箱", "area": "DOME",   "duration": 1, "seq": 1},
    {"node_key": "ARRIVAL_NOTICE", "node_name": "到港通知", "area": "OVERSEA", "duration": 1, "seq": 6},
    {"node_key": "EMPTY_RETURN",   "node_name": "空箱归还", "area": "OVERSEA", "duration": 1, "seq": 15},
]

# 需迁移的存量业务表（校验 batch_id 全覆盖）
BIZ_TABLES = ["nodes", "files", "cargo_items"]


def _now():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00")


# ─────────────────────────────────────────────────────────────────────────────
# 文件锁 / 备份
# ─────────────────────────────────────────────────────────────────────────────

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
    bak = f"{DB_PATH}.bak.{ts}"
    n = 2
    while os.path.exists(bak):
        bak = f"{DB_PATH}.bak.{ts}.{n}"
        n += 1
    shutil.copy2(DB_PATH, bak)
    print(f"[OK] 数据库已备份 → {bak}")
    return bak


def _schema_version(conn):
    cur = conn.cursor()
    try:
        cur.execute("SELECT value FROM settings WHERE key='schema_version'")
        row = cur.fetchone()
        return int(row[0]) if row and row[0] else 1
    except sqlite3.OperationalError:
        return 1


def _table_names(conn):
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {r[0] for r in cur.fetchall()}


def _has_column(cur, table, col):
    cur.execute(f"PRAGMA table_info({table})")
    return any(r[1] == col for r in cur.fetchall())


def _add_column(cur, table, col, typ):
    if not _has_column(cur, table, col):
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        print(f"[+col] {table}.{col}")
    else:
        print(f"[skip] {table}.{col} 已存在")


# ─────────────────────────────────────────────────────────────────────────────
# 迁移步骤（§7）
# ─────────────────────────────────────────────────────────────────────────────

def create_new_tables(conn):
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS batches (
        batch_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
        batch_no TEXT NOT NULL, batch_name TEXT,
        booking_no TEXT, mbl_no TEXT, hbl_nos TEXT,
        status TEXT NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft','ready','running','completed','closed','cancelled')),
        planned_date TEXT, actual_etd TEXT, actual_eta TEXT,
        actual_delivery TEXT, empty_returned_at TEXT,
        cancel_reason TEXT, closed_at TEXT, created_at TEXT NOT NULL,
        UNIQUE (project_id, batch_no)
    );
    CREATE INDEX IF NOT EXISTS idx_batch_project ON batches(project_id, status);

    CREATE TABLE IF NOT EXISTS batch_routes (
        route_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL UNIQUE,
        mode_primary TEXT NOT NULL DEFAULT 'SEA',
        mode_chain TEXT NOT NULL DEFAULT '["SEA"]',
        country TEXT, template_key TEXT, template_snapshot TEXT,
        export_port TEXT, etd TEXT, eta TEXT, vessel_id TEXT,
        customs_broker TEXT, customs_mode TEXT, release_mode TEXT,
        container_pickup_location TEXT, empty_return_location TEXT,
        free_demurrage_until TEXT, free_detention_until TEXT,
        enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS containers (
        container_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL,
        container_no TEXT NOT NULL, seal_no TEXT, container_type TEXT,
        pickup_at TEXT, return_due TEXT, returned_at TEXT, note TEXT,
        UNIQUE (batch_id, container_no)
    );
    CREATE TABLE IF NOT EXISTS container_batch_link (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        container_id TEXT NOT NULL, batch_id TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'PRIMARY', note TEXT,
        UNIQUE (container_id, batch_id)
    );

    CREATE TABLE IF NOT EXISTS parties (
        party_id TEXT PRIMARY KEY, party_name TEXT NOT NULL,
        address TEXT, contact TEXT, tax_id TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS batch_parties (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT NOT NULL, party_id TEXT NOT NULL,
        role TEXT NOT NULL, notes TEXT,
        UNIQUE (batch_id, party_id, role)
    );

    CREATE TABLE IF NOT EXISTS batch_schedule_changes (
        change_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL,
        old_etd TEXT, old_eta TEXT, new_etd TEXT, new_eta TEXT,
        rule_class TEXT, reason TEXT, source TEXT, affected_nodes TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS migration_status (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, status TEXT NOT NULL,
        detail TEXT, started_at TEXT, finished_at TEXT
    );

    CREATE TABLE IF NOT EXISTS vessel (
        vessel_id TEXT PRIMARY KEY, project_id TEXT, vessel_name TEXT,
        batch_id TEXT, voyage_sequence INTEGER DEFAULT 1,
        etd TEXT, eta TEXT
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT
    );
    """)
    conn.commit()
    print("[OK] 新表就绪")


def add_columns(conn):
    cur = conn.cursor()
    _add_column(cur, "projects", "project_no", "TEXT")
    _add_column(cur, "projects", "customer_id", "TEXT")
    _add_column(cur, "nodes", "batch_id", "TEXT")
    _add_column(cur, "nodes", "node_key", "TEXT")
    _add_column(cur, "files", "batch_id", "TEXT")
    _add_column(cur, "files", "node_key", "TEXT")
    _add_column(cur, "files", "due_node_key", "TEXT")
    _add_column(cur, "cargo_items", "batch_id", "TEXT")
    _add_column(cur, "cargo_items", "container_type", "TEXT")
    _add_column(cur, "vessel", "batch_id", "TEXT")
    _add_column(cur, "vessel", "voyage_sequence", "INTEGER DEFAULT 1")
    _add_column(cur, "shift_history", "batch_id", "TEXT")
    _add_column(cur, "shift_history", "node_key", "TEXT")
    _add_column(cur, "op_log", "batch_id", "TEXT")
    _add_column(cur, "op_log", "scope", "TEXT")
    _add_column(cur, "op_log", "node_key", "TEXT")
    conn.commit()
    print("[OK] 存量表加列完成")


def _unique_pno(used, pid):
    """生成项目内唯一 project_no：优先 pid 前 8 位字母数字，冲突则扩展，仍冲突加序号。"""
    alnum = [c for c in str(pid) if c.isalnum()]
    if not alnum:
        alnum = list("0" * 8)
    base = "P-" + "".join(alnum)[:8].ljust(8, "0")
    cand = base
    n = 2
    while cand in used:
        # 尝试扩展以保留更多区分字符
        if len(alnum) > 8 and n <= len(alnum) - 7:
            cand = "P-" + "".join(alnum[:8 + n - 2])
        else:
            cand = f"{base}-{n}"
        n += 1
    used.add(cand)
    return cand


def _batch_status(status):
    return "closed" if status == "Completed" else ("running" if status == "Active" else "ready")


def migrate_core(conn, cur):
    """生成批次 + 回填 batch_id/node_key + 集装箱迁移。位于单个事务内。"""
    from uuid import uuid4
    now = _now()
    cur.execute("SELECT project_id,status,etd,eta,export_port,vessel_name FROM projects")
    projects = cur.fetchall()

    proj_to_batch = {}
    used_pno = set()
    # 已存在批次的项目，直接复用（幂等：重复执行只补缺）
    cur.execute("SELECT project_id, batch_id, batch_no FROM batches")
    existing = cur.fetchall()
    for pid_old, bid_old, bno_old in existing:
        proj_to_batch[pid_old] = bid_old
        if bno_old:
            used_pno.add(bno_old.split("-B")[0])

    for pid, status, etd, eta, export_port, vessel_name in projects:
        if pid in proj_to_batch:
            continue  # 已迁移，跳过批次创建
        pno = _unique_pno(used_pno, pid)
        cur.execute("UPDATE projects SET project_no=? WHERE project_id=?", (pno, pid))
        bid = str(uuid4())
        bno = f"{pno}-B01"
        cur.execute(
            "INSERT INTO batches(batch_id,project_id,batch_no,batch_name,status,planned_date,"
            "actual_etd,actual_eta,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (bid, pid, bno, "B01", _batch_status(status), etd, etd, eta, now))
        cur.execute(
            "INSERT INTO batch_routes(route_id,batch_id,mode_primary,mode_chain,"
            "country,export_port,etd,eta,created_at)"
            " VALUES(?,?,'SEA','[\"SEA\"]',?,?,?,?,?)",
            (str(uuid4()), bid, None, export_port, etd, eta, now))
        if vessel_name:
            cur.execute(
                "UPDATE vessel SET batch_id=?, voyage_sequence=1 "
                "WHERE project_id=? AND (batch_id IS NULL OR batch_id='')",
                (bid, pid))
        proj_to_batch[pid] = bid

    # nodes 是否还是旧结构（含 project_id）。首次迁移为 True；§7.3 主键重建后
    # 变为 False，此后的重复执行必须走 batch_id 路径，否则报 no such column。
    _ncols = {r[1] for r in cur.execute("PRAGMA table_info(nodes)").fetchall()}
    has_pid = "project_id" in _ncols

    # nodes: node_key 映射 + batch_id（仅填补空值，幂等）
    for pid, bid in proj_to_batch.items():
        if has_pid:
            cur.execute("SELECT node_id FROM nodes WHERE project_id=?", (pid,))
            for (nid,) in cur.fetchall():
                nk = OLD_12_TO_NEW.get(nid)
                cur.execute(
                    "UPDATE nodes SET batch_id=?, node_key=? WHERE project_id=? AND node_id=? "
                    "AND (batch_id IS NULL OR batch_id='')",
                    (bid, nk, pid, nid))
            # 已映射但空 batch_id 的节点（重复运行时仍可能补 batch_id）
            cur.execute(
                "UPDATE nodes SET batch_id=? WHERE project_id=? "
                "AND (batch_id IS NULL OR batch_id='')",
                (bid, pid))
        # 新增 3 节点（Pending），避免与旧 12 序列冲突；幂等（已存在则跳过）
        if has_pid:
            cur.execute("SELECT MAX(seq), MAX(node_id) FROM nodes WHERE project_id=?", (pid,))
        else:
            cur.execute("SELECT MAX(seq), MAX(node_id) FROM nodes WHERE batch_id=?", (bid,))
        row = cur.fetchone()
        next_seq = (row[0] or 0) + 1
        next_nid = (row[1] or 0) + 1
        for m in NEW_NODES_TO_ADD:
            if has_pid:
                cur.execute(
                    "INSERT INTO nodes(node_id,project_id,batch_id,node_key,seq,node_name,"
                    "area,duration,status) SELECT ?,?,?,?,?,?,?,?,'Pending'"
                    " WHERE 0=(SELECT COUNT(*) FROM nodes WHERE project_id=? AND node_key=?)",
                    (next_nid, pid, bid, m["node_key"], next_seq, m["node_name"],
                     m["area"], m["duration"], pid, m["node_key"]))
            else:
                cur.execute(
                    "INSERT INTO nodes(node_id,batch_id,node_key,seq,node_name,"
                    "area,calendar_mode,is_key_node,default_duration,duration,status,role_label) "
                    "SELECT ?,?,?,?,?,?,?,?,?,?,'Pending','' "
                    " WHERE 0=(SELECT COUNT(*) FROM nodes WHERE batch_id=? AND node_key=?)",
                    (next_nid, bid, m["node_key"], next_seq, m["node_name"],
                     m["area"], m.get("calendar_mode", "NATURAL"),
                     1 if m.get("key_node") else 0, m["duration"], m["duration"],
                     bid, m["node_key"]))
            next_seq += 1
            next_nid += 1

    # files: batch_id + node_key（用节点映射，仅填补空值）
    for pid, bid in proj_to_batch.items():
        if has_pid:
            cur.execute(
                "UPDATE files SET batch_id=?, node_key=(SELECT node_key FROM nodes n "
                "WHERE n.project_id=files.project_id AND n.node_id=files.node_id) "
                "WHERE project_id=? AND (batch_id IS NULL OR batch_id='')",
                (bid, pid))
        else:
            cur.execute(
                "UPDATE files SET batch_id=?, node_key=(SELECT node_key FROM nodes n "
                "WHERE n.batch_id=files.batch_id AND n.node_id=files.node_id) "
                "WHERE project_id=? AND (batch_id IS NULL OR batch_id='')",
                (bid, pid))

    # cargo_items: batch_id（仅填补空值）
    for pid, bid in proj_to_batch.items():
        cur.execute(
            "UPDATE cargo_items SET batch_id=? WHERE project_id=? AND (batch_id IS NULL OR batch_id='')",
            (bid, pid))

    # op_log: batch_id + scope
    for pid, bid in proj_to_batch.items():
        cur.execute(
            "UPDATE op_log SET batch_id=?, scope='project' WHERE project_id=? AND (batch_id IS NULL OR batch_id='')",
            (bid, pid))
        cur.execute(
            "UPDATE op_log SET scope='system' WHERE project_id IS NULL AND (scope IS NULL OR scope='')")

    # shift_history: batch_id
    for pid, bid in proj_to_batch.items():
        cur.execute(
            "UPDATE shift_history SET batch_id=? WHERE project_id=? AND (batch_id IS NULL OR batch_id='')",
            (bid, pid))
        if has_pid:
            cur.execute(
                "UPDATE shift_history SET node_key=(SELECT node_key FROM nodes n "
                "WHERE n.project_id=shift_history.project_id AND n.node_id=shift_history.node_id) "
                "WHERE node_key IS NULL")
        else:
            cur.execute(
                "UPDATE shift_history SET node_key=(SELECT node_key FROM nodes n "
                "WHERE n.batch_id=shift_history.batch_id AND n.node_id=shift_history.node_id) "
                "WHERE node_key IS NULL")

    # 集装箱迁移：cargo_items.container_no 去重
    container_count = 0
    for pid, bid in proj_to_batch.items():
        cur.execute(
            "SELECT DISTINCT container_no,seal_no,container_type FROM cargo_items "
            "WHERE project_id=? AND batch_id=? AND container_no IS NOT NULL AND container_no!=''",
            (pid, bid))
        for cno, seal, ctype in cur.fetchall():
            cur.execute(
                "INSERT OR IGNORE INTO containers(container_id,batch_id,container_no,seal_no,"
                "container_type) VALUES(?,?,?,?,?)",
                (str(uuid4()), bid, cno, seal, ctype))
            # 幂等：复用实际落库的 container_id 建关联
            cur.execute(
                "SELECT container_id FROM containers WHERE batch_id=? AND container_no=?",
                (bid, cno))
            row = cur.fetchone()
            cid = row[0] if row else None
            if cid:
                cur.execute(
                    "INSERT OR IGNORE INTO container_batch_link(container_id,batch_id,role) "
                    "VALUES(?,?,'PRIMARY')", (cid, bid))
                container_count += 1
    return projects, container_count


def validate(conn, cur, n_projects):
    problems = []
    for t in BIZ_TABLES:
        cur.execute(f"SELECT COUNT(*) FROM {t} WHERE batch_id IS NULL")
        c = cur.fetchone()[0]
        if c:
            problems.append(f"{t}: {c} 行 batch_id 为空")
    cur.execute("SELECT COUNT(*) FROM batches")
    b = cur.fetchone()[0]
    if b != n_projects:
        problems.append(f"batches({b}) != projects({n_projects})")
    cur.execute("SELECT COUNT(*) FROM nodes WHERE node_key IS NULL OR node_key=''")
    if cur.fetchone()[0]:
        problems.append("仍有节点 node_key 为空")
    cur.execute("SELECT COUNT(*) FROM files WHERE node_key IS NULL OR node_key=''")
    if cur.fetchone()[0]:
        problems.append("仍有单证 node_key 为空（无归属节点的单证，属正常）")
    return problems


# ─────────────────────────────────────────────────────────────────────────────
# 命令
# ─────────────────────────────────────────────────────────────────────────────

def _rebuild_nodes_v2(conn):
    """§7.3：`nodes` 主键重建。

    旧库的 nodes 主键是 (project_id, node_id)，且缺 calendar_mode /
    default_duration / is_key_node 等新列。此处整表重建为
    `nodes_v2(batch_id, node_id, node_key, ...)` —— 主键 (batch_id, node_id)、
    唯一键 (batch_id, node_key)，与 db.py 的新结构完全一致。

    必须在所有「引用 nodes.project_id」的语句（files.node_key 回填等）之后执行。
    幂等：已是新结构则跳过。
    """
    cur = conn.cursor()
    cols = {r[1] for r in cur.execute("PRAGMA table_info(nodes)").fetchall()}
    if not cols:
        return None
    if {"default_duration", "calendar_mode", "is_key_node"} <= cols:
        return "already"          # 已是新结构（重复迁移）

    try:
        import sys as _sys
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _root not in _sys.path:
            _sys.path.insert(0, _root)
        from services import node_template as _nt
        tmpl = {n["node_key"]: n for n in _nt.template()}
    except Exception:
        tmpl = {}

    old_rows = None
    sel = [c for c in ("node_id", "batch_id", "node_key", "node_name", "role_label", "seq",
                       "area", "duration", "plan_start", "plan_end", "is_delayed",
                       "delay_days", "status", "actual_completion_date", "remark")
           if c in cols]
    idx = {c: i for i, c in enumerate(sel)}
    old_rows = cur.execute(f"SELECT {', '.join(sel)} FROM nodes").fetchall()

    def g(row, name, default=None):
        i = idx.get(name)
        return row[i] if i is not None else default

    cur.execute("ALTER TABLE nodes RENAME TO nodes_old")
    cur.execute(NODES_V2_DDL)
    for row in old_rows:
        key = g(row, "node_key")
        t = tmpl.get(key, {})
        # §5.2 顺序变化提示：节点顺序按新模板重排 —— node_id/seq 一律取模板序，
        # 未知 key（模板外）保留原值，保证不丢数据。
        new_id = t.get("node_id") or g(row, "node_id")
        new_seq = t.get("seq") or g(row, "seq") or 0
        cur.execute(
            "INSERT INTO nodes (batch_id, node_id, node_key, node_name, role_label, seq, "
            "area, calendar_mode, is_key_node, default_duration, duration, plan_start, "
            "plan_end, is_delayed, delay_days, status, actual_completion_date, remark) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (g(row, "batch_id"), new_id, key,
             g(row, "node_name") or t.get("node_name") or key,
             g(row, "role_label") or t.get("role_label") or "",
             new_seq,
             g(row, "area") or t.get("area") or "DOME",
             t.get("calendar_mode", "NATURAL"),
             1 if t.get("key_node") else 0,
             g(row, "duration") or t.get("default_duration") or 1,
             g(row, "duration") or t.get("default_duration") or 1,
             g(row, "plan_start"), g(row, "plan_end"),
             g(row, "is_delayed", 0) or 0, g(row, "delay_days", 0) or 0,
             g(row, "status") or "Pending", g(row, "actual_completion_date"),
             g(row, "remark") or ""))
    cur.execute("DROP TABLE nodes_old")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_node_batch_seq ON nodes(batch_id, seq)")

    # node_id 已重排 → 把引用方按 node_key 重新指向新 node_id（幂等：正确时为空操作）
    def _has_col(table, col):
        try:
            return col in {r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()}
        except sqlite3.OperationalError:
            return False

    remaps = [
        ("files", "node_id", "node_key"),
        ("files", "due_node_id", "due_node_key"),
        ("shift_history", "node_id", "node_key"),
    ]
    for table, id_col, key_col in remaps:
        if not (_has_col(table, id_col) and _has_col(table, key_col)):
            continue
        cur.execute(
            f"UPDATE {table} SET {id_col}=(SELECT n.node_id FROM nodes n "
            f"WHERE n.batch_id={table}.batch_id AND n.node_key={table}.{key_col}) "
            f"WHERE {key_col} IS NOT NULL")

    conn.commit()
    return len(old_rows)


def do_check():
    print(f"=== 迁移预检查 ===\n数据库: {DB_PATH}")
    if not os.path.exists(DB_PATH):
        print(f"[ERROR] 数据库不存在: {DB_PATH}")
        return 1
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        ver = _schema_version(conn)
        tables = _table_names(conn)
        print(f"schema_version: {ver}  |  new tables 已存在: {bool(tables & {'batches','batch_routes'})}")
        if ver >= 2:
            print("[OK] 已处于 V2，无需迁移")
        for t in ["projects", "nodes", "files", "cargo_items"]:
            if t in tables:
                cur.execute(f"SELECT COUNT(*) FROM {t}")
                print(f"  - {t}: {cur.fetchone()[0]} 行")
        # 检查是否已有 node_key 数据（已批次化则 warning）
        if "nodes" in tables and _has_column(cur, "nodes", "node_key"):
            cur.execute("SELECT COUNT(*) FROM nodes WHERE node_key IS NOT NULL AND node_key!=''")
            if cur.fetchone()[0]:
                print("[WARN] nodes 已含 node_key——库可能已批次化；请确认是否真的需要迁移")

        # §6.8 数据质量校验：--check 与 UI 保存同源（services.validation）
        if ver >= 2 or (tables & {"batches", "nodes"}):
            try:
                import sys as _sys
                _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                if _root not in _sys.path:
                    _sys.path.insert(0, _root)
                saved = os.environ.get("ILTS_DB_PATH")
                os.environ["ILTS_DB_PATH"] = DB_PATH
                import importlib
                import db as _db
                importlib.reload(_db)
                from services import validation as _vd
                problems = _vd.validate_all()
                if problems:
                    n = sum(len(v) for v in problems.values())
                    print(f"[WARN] §6.8 数据质量校验发现 {n} 项问题（不阻断迁移）：")
                    for pid, msgs in list(problems.items())[:5]:
                        for m in msgs[:4]:
                            print(f"        · {pid} {m}")
                    if n > 20:
                        print(f"        （仅展示前 20 条，共 {n} 条）")
                else:
                    print("[OK] §6.8 数据质量校验通过（13 项）")
                if saved is None:
                    os.environ.pop("ILTS_DB_PATH", None)
                else:
                    os.environ["ILTS_DB_PATH"] = saved
            except Exception as e:
                print(f"[SKIP] §6.8 校验未执行：{e}")

        print("\n预检查通过（可执行 --apply）")
        return 0
    finally:
        conn.close()


def _recompute_plans(conn):
    """§7.13：按 §5.3 用新模板与锚点重算所有未冻结节点的计划日期。

    冻结保护：status='Done' 或已填 actual_completion_date 的节点不动。
    返回被更新的节点行数（不写 batch_schedule_changes）。
    """
    try:
        import sys as _sys
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _root not in _sys.path:
            _sys.path.insert(0, _root)
        os.environ["ILTS_DB_PATH"] = DB_PATH
        import importlib
        import db as _db
        importlib.reload(_db)
        from services import schedule2 as _s2
    except Exception as e:
        print(f"[SKIP] 计划日期重算未执行：{e}")
        return 0

    cur = conn.cursor()
    n = 0
    # 注意：本连接未设 row_factory，行是 tuple，需显式取列
    batches = cur.execute("SELECT batch_id FROM batches").fetchall()
    for (batch_id,) in batches:
        route = cur.execute("SELECT etd, eta FROM batch_routes WHERE batch_id=?",
                            (batch_id,)).fetchone()
        if not route or not route[0] or not route[1]:
            continue
        rows = cur.execute(
            "SELECT node_id, node_key, area, duration, default_duration, calendar_mode, "
            "status, actual_completion_date, plan_start, plan_end "
            "FROM nodes WHERE batch_id=? ORDER BY seq", (batch_id,)).fetchall()
        if not rows:
            continue
        nodes = [{"node_id": r[0], "node_key": r[1], "area": r[2], "duration": r[3],
                  "default_duration": r[4], "calendar_mode": r[5], "status": r[6],
                  "actual_completion_date": r[7], "plan_start": r[8], "plan_end": r[9]}
                 for r in rows]
        try:
            plan = _s2.compute_plan(route[0], route[1], nodes)
        except Exception:
            continue
        for nd in nodes:
            if nd.get("status") == "Done" or nd.get("actual_completion_date"):
                continue          # 冻结保护
            new = plan.get(nd["node_key"])
            if not new:
                continue
            if (nd.get("plan_start"), nd.get("plan_end")) != new:
                cur.execute(
                    "UPDATE nodes SET plan_start=?, plan_end=? "
                    "WHERE batch_id=? AND node_id=?",
                    (new[0], new[1], batch_id, nd["node_id"]))
                n += 1
    conn.commit()
    return n


def do_apply():
    print(f"=== 执行批次化迁移 (Phase 1A) ===\n数据库: {DB_PATH}")
    if not os.path.exists(DB_PATH):
        print(f"[ERROR] 数据库不存在: {DB_PATH}")
        return 1
    if not acquire_lock():
        return 1
    bak = backup_db()
    if not bak:
        release_lock()
        return 1

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    started = _now()
    try:
        # 先建表（含 migration_status），再记 running
        create_new_tables(conn)
        add_columns(conn)
        cur.execute("DELETE FROM migration_status WHERE name='batches_v2'")
        cur.execute(
            "INSERT OR IGNORE INTO migration_status(name,status,started_at) VALUES('batches_v2','running',?)",
            (started,))
        conn.commit()

        cur.execute("SELECT COUNT(*) FROM projects")
        n_projects = cur.fetchone()[0]
        if n_projects == 0:
            print("[WARN] projects 为空，迁移无项目可处理")

        projects, container_count = migrate_core(conn, cur)

        problems = validate(conn, cur, n_projects)
        if problems:
            conn.rollback()
            cur.execute(
                "UPDATE migration_status SET status='failed',finished_at=?,detail=? WHERE name='batches_v2'",
                (_now(), "校验失败: " + "; ".join(problems)))
            conn.commit()
            print("=== 迁移失败，事务已回滚 ===")
            for p in problems:
                print("  ✗ " + p)
            print(f"备份保留: {bak}")
            return 1

        conn.commit()  # 迁移成功提交

        # §7.3 nodes 主键重建（必须在所有引用 nodes.project_id 的语句之后）
        rebuilt = _rebuild_nodes_v2(conn)
        if isinstance(rebuilt, int):
            print(f"[OK] nodes 主键重建完成：{rebuilt} 行 → (batch_id, node_id)"
                  f" + UNIQUE(batch_id, node_key)")

        # §7.13 计划日期：按 §5.3 用新模板与锚点重算一次所有未冻结节点
        #（旧 12 节点计划日期可能变化）；迁移本身不视为船期变更，不写
        # batch_schedule_changes。
        recomputed = _recompute_plans(conn)

        cur.execute(
            "UPDATE migration_status SET status='completed',finished_at=?,detail=? WHERE name='batches_v2'",
            (_now(), f"{n_projects} 项目, {container_count} 集装箱迁移"))
        cur.execute("INSERT INTO settings(key,value) VALUES('schema_version','2') "
                    "ON CONFLICT(key) DO UPDATE SET value='2'")
        conn.commit()

        print("\n=== 迁移成功 ===")
        print(f"  项目: {n_projects} | 集装箱: {container_count} | schema_version=2")
        print(f"  备份: {bak}")
        print(f"  计划日期: 已按 §5.3 用新模板重算 {recomputed} 个未冻结节点")
        print("  提示 1: 节点顺序按新模板重排（集港/返场提前到出口报关之前），"
              "计划日期已按 §5.3 重算，请复核受影响批次")
        print("  提示 2: 客户/货主主档为空，请到【批次管理】补录；"
              "旧项目按 §5.5.4 保留为空、不回溯阻断历史已提交单证")
        print("  提示 3: 免堆期/免箱期、报关行/报关方式、换单方式留空待补")
        return 0
    except Exception as e:
        conn.rollback()
        try:
            cur.execute(
                "UPDATE migration_status SET status='failed',finished_at=?,detail=? WHERE name='batches_v2'",
                (_now(), str(e)))
            conn.commit()
        except Exception:
            pass
        print("=== 迁移失败，事务已回滚 ===")
        print(f"  错误: {e}")
        print(f"  备份: {bak}")
        return 1
    finally:
        conn.close()
        release_lock()


def do_rollback():
    print("=== 迁移回滚 ===")
    db_dir = os.path.dirname(DB_PATH)
    base = os.path.basename(DB_PATH)
    backups = sorted(   # 升序 → 最旧为 true 迁移前快照
        (f for f in os.listdir(db_dir) if f.startswith(base + ".bak.")), reverse=False)
    if not backups:
        print("[ERROR] 未找到备份文件")
        return 1
    chosen = os.path.join(db_dir, backups[0])
    print(f"将用最初备份（迁移前快照）恢复: {chosen}")
    if len(backups) > 1:
        print(f"  （另存 {len(backups)-1} 份较新备份；如仅需撤销最近一次可另选）")
    ans = input("确认用该备份覆盖当前库？(y/N) ").strip().lower()
    if ans != "y":
        print("已取消")
        return 0
    shutil.copy2(chosen, DB_PATH)
    # 清除 WAL/journal sidecar，避免回滚后旧 WAL 被重放、重新引入迁移 schema
    for ext in ("-wal", "-shm", "-journal"):
        side = DB_PATH + ext
        if os.path.exists(side):
            os.remove(side)
    release_lock()
    print(f"[OK] 已从 {chosen} 恢复")
    return 0


def main():
    ap = argparse.ArgumentParser(description="批次化迁移工具 (多式联运 Phase 1A)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="预检查，不修改数据")
    g.add_argument("--apply", action="store_true", help="执行迁移（先备份 + 锁 + 事务）")
    g.add_argument("--rollback", action="store_true", help="从最近备份恢复")
    args = ap.parse_args()
    Path(os.path.dirname(DB_PATH)).mkdir(parents=True, exist_ok=True)
    if args.check:
        return do_check()
    if args.apply:
        return do_apply()
    return do_rollback()


if __name__ == "__main__":
    sys.exit(main())