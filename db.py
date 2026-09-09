"""
SQLite 数据访问层 — 单连接串行写库，即时 commit。
"""

import sqlite3
import os
from datetime import date
from uuid import uuid4

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "logistics.db")

_conn = None


def get_conn():
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(DB_PATH)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    project_id              TEXT PRIMARY KEY,
    project_name            TEXT NOT NULL,
    country                 TEXT NOT NULL,
    export_port             TEXT,
    status                  TEXT NOT NULL CHECK (status IN ('Active','Completed')),
    etd                     TEXT NOT NULL,
    eta                     TEXT NOT NULL,
    buffer_days             INTEGER NOT NULL DEFAULT 4,
    actual_completion_date  TEXT,
    create_date             TEXT NOT NULL,
    last_alert_date         TEXT,
    last_visited_page       TEXT,
    last_opened_project_id  TEXT
);

CREATE TABLE IF NOT EXISTS nodes (
    project_id              TEXT NOT NULL,
    node_id                 INTEGER NOT NULL,
    node_name               TEXT NOT NULL,
    role_label              TEXT NOT NULL,
    seq                     INTEGER NOT NULL,
    area                    TEXT NOT NULL CHECK (area IN ('DOME','SEA','OVERSEA')),
    default_duration        INTEGER NOT NULL,
    duration                INTEGER NOT NULL,
    plan_start              TEXT NOT NULL,
    plan_end                TEXT NOT NULL,
    is_delayed              INTEGER NOT NULL DEFAULT 0,
    delay_days              INTEGER NOT NULL DEFAULT 0,
    status                  TEXT NOT NULL DEFAULT 'Pending'
        CHECK (status IN ('Pending','Active','Overdue','Done')),
    actual_completion_date  TEXT,
    remark                  TEXT,
    PRIMARY KEY (project_id, node_id)
);

CREATE TABLE IF NOT EXISTS files (
    file_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id              TEXT NOT NULL,
    node_id                 INTEGER,
    doc_name                TEXT NOT NULL,
    doc_type                TEXT NOT NULL CHECK (doc_type IN ('required','optional')),
    owner_dept              TEXT,
    copies                  INTEGER,
    due_node_id             INTEGER,
    due_type                TEXT CHECK (due_type IN ('node_start','node_end', NULL)),
    remind_before_days      INTEGER DEFAULT 3,
    status                  TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','submitted')),
    due_date                TEXT,
    submitted_date          TEXT,
    owner                   TEXT,
    note                    TEXT,
    is_default              INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);
CREATE INDEX IF NOT EXISTS idx_files_project ON files(project_id, node_id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- 优化方案 D1 · 货物台账（挂在 projects 下，1 项目 N 货项）
CREATE TABLE IF NOT EXISTS cargo_items (
    item_id      TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL,
    seq          INTEGER,
    item_name    TEXT NOT NULL,
    qty          INTEGER DEFAULT 1,
    unit         TEXT,
    dim_l        REAL, dim_w REAL, dim_h REAL,
    weight_kg    REAL,
    gross_m3     REAL,
    over_flag    INTEGER DEFAULT 0,
    od_type      TEXT,
    marks        TEXT,
    packaging    TEXT,
    container_no TEXT,
    seal_no      TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);
CREATE INDEX IF NOT EXISTS idx_cargo_project ON cargo_items(project_id);

-- 优化方案 D1 · 班轮（1 项目 1 船）
CREATE TABLE IF NOT EXISTS vessel (
    vessel_id   TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL UNIQUE,
    vessel_name TEXT,
    imo         TEXT,
    voyage      TEXT,
    carrier     TEXT,
    mmsi        TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

-- 优化方案 D1 · 船位历史（手动登记，本地回放用）
CREATE TABLE IF NOT EXISTS vessel_positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT NOT NULL,
    lat         REAL,
    lon         REAL,
    actual_eta  TEXT,
    note        TEXT,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);
CREATE INDEX IF NOT EXISTS idx_vp_project ON vessel_positions(project_id, id);

-- 优化方案 D2 · 位移历史（回退 / 撤销用）
CREATE TABLE IF NOT EXISTS shift_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT NOT NULL,
    node_id     INTEGER NOT NULL,
    delta       INTEGER NOT NULL,        -- 正值推迟 / 负值提前
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sh_project ON shift_history(project_id, id);

-- 日报/周报 · 操作日志（报告时间线 + 审计留痕）
CREATE TABLE IF NOT EXISTS op_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT NOT NULL,
    node_id     INTEGER,
    kind        TEXT NOT NULL,
    subject     TEXT NOT NULL DEFAULT '',
    detail      TEXT,
    valid       INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opl_project ON op_log(project_id, created_at);
"""


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def today_str():
    from services.clock import get_today_str
    return get_today_str()


# ── Projects ──

def insert_project(proj: dict):
    conn = get_conn()
    conn.execute(
        "INSERT INTO projects (project_id, project_name, country, export_port, status, "
        "etd, eta, buffer_days, create_date) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (proj["project_id"], proj["project_name"], proj.get("country", "BR"),
         proj.get("export_port"), "Active",
         proj["etd"], proj["eta"], proj.get("buffer_days", 4), today_str())
    )
    conn.commit()


def update_project(project_id, **kw):
    conn = get_conn()
    sets = ", ".join(f"{k}=?" for k in kw)
    conn.execute(f"UPDATE projects SET {sets} WHERE project_id=?", (*kw.values(), project_id))
    conn.commit()


def get_project(project_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
    return dict(row) if row else None


def get_projects_by_status(status):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM projects WHERE status=? ORDER BY etd", (status,)).fetchall()
    return [dict(r) for r in rows]


def count_projects(status):
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) AS c FROM projects WHERE status=?", (status,)).fetchone()
    return row["c"]


# ── Nodes ──

def insert_nodes(project_id, nodes: list):
    conn = get_conn()
    for n in nodes:
        conn.execute(
            "INSERT INTO nodes (project_id, node_id, node_name, role_label, seq, area, "
            "default_duration, duration, plan_start, plan_end, status, remark) "
            "VALUES (?,?,?,?,?,?,?,?,?,?, 'Pending',?)",
            (project_id, n["node_id"], n["node_name"], n["role_label"], n["seq"], n["area"],
             n.get("default_duration", n["duration"]), n["duration"],
             n["plan_start"], n["plan_end"], n.get("remark", ""))
        )
    conn.commit()


def get_nodes(project_id):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM nodes WHERE project_id=? ORDER BY seq", (project_id,)).fetchall()
    return [dict(r) for r in rows]


def update_node(project_id, node_id, **kw):
    conn = get_conn()
    sets = ", ".join(f"{k}=?" for k in kw)
    conn.execute(f"UPDATE nodes SET {sets} WHERE project_id=? AND node_id=?",
                 (*kw.values(), project_id, node_id))
    conn.commit()


# ── Cargo items ──

_CARGO_COLS = ("seq", "item_name", "qty", "unit", "dim_l", "dim_w", "dim_h",
               "weight_kg", "gross_m3", "over_flag", "od_type", "marks",
               "packaging", "container_no", "seal_no")


def insert_cargo_items(project_id, items: list):
    """items: [{item_name, qty, ...}]，自动补 item_id/seq/over_flag"""
    conn = get_conn()
    for i, it in enumerate(items, start=1):
        item_id = it.get("item_id") or f"item-{uuid4().hex[:10]}"
        conn.execute(
            "INSERT INTO cargo_items (item_id, project_id, seq, item_name, qty, unit, "
            "dim_l, dim_w, dim_h, weight_kg, gross_m3, over_flag, od_type, marks, "
            "packaging, container_no, seal_no) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, project_id, it.get("seq", i), it["item_name"], it.get("qty", 1),
             it.get("unit"), it.get("dim_l"), it.get("dim_w"), it.get("dim_h"),
             it.get("weight_kg"), it.get("gross_m3"), it.get("over_flag", 0),
             it.get("od_type"), it.get("marks"), it.get("packaging"),
             it.get("container_no"), it.get("seal_no"))
        )
    conn.commit()


def get_cargo_items(project_id):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM cargo_items WHERE project_id=? ORDER BY seq, item_id",
                        (project_id,)).fetchall()
    return [dict(r) for r in rows]


def update_cargo_item(item_id, **kw):
    conn = get_conn()
    if kw:
        sets = ", ".join(f"{k}=?" for k in kw)
        conn.execute(f"UPDATE cargo_items SET {sets} WHERE item_id=?",
                     (*kw.values(), item_id))
        conn.commit()


def delete_cargo_item(item_id):
    conn = get_conn()
    conn.execute("DELETE FROM cargo_items WHERE item_id=?", (item_id,))
    conn.commit()


def delete_cargo_items(project_id):
    conn = get_conn()
    conn.execute("DELETE FROM cargo_items WHERE project_id=?", (project_id,))
    conn.commit()


# ── Vessel ──

def upsert_vessel(project_id, vessel_name=None, imo=None, voyage=None,
                  carrier=None, mmsi=None):
    """1 项目 1 船：有则更新，无则插入。返回 vessel dict。"""
    conn = get_conn()
    row = conn.execute("SELECT * FROM vessel WHERE project_id=?",
                       (project_id,)).fetchone()
    if row:
        conn.execute(
            "UPDATE vessel SET vessel_name=?, imo=?, voyage=?, carrier=?, mmsi=? "
            "WHERE project_id=?",
            (vessel_name, imo, voyage, carrier, mmsi, project_id))
        vessel_id = row["vessel_id"]
    else:
        vessel_id = f"ves-{uuid4().hex[:8]}"
        conn.execute(
            "INSERT INTO vessel (vessel_id, project_id, vessel_name, imo, voyage, "
            "carrier, mmsi) VALUES (?,?,?,?,?,?,?)",
            (vessel_id, project_id, vessel_name, imo, voyage, carrier, mmsi))
    conn.commit()
    return get_vessel(project_id)


def get_vessel(project_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM vessel WHERE project_id=?",
                       (project_id,)).fetchone()
    return dict(row) if row else None


# ── Vessel positions ──

def insert_vessel_position(project_id, lat=None, lon=None, actual_eta=None, note=None):
    from datetime import datetime
    conn = get_conn()
    conn.execute(
        "INSERT INTO vessel_positions (project_id, lat, lon, actual_eta, note, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (project_id, lat, lon, actual_eta, note,
         datetime.now().strftime("%Y-%m-%d %H:%M")))
    conn.commit()


def get_vessel_positions(project_id, limit=10):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM vessel_positions WHERE project_id=? ORDER BY id DESC LIMIT ?",
        (project_id, limit)).fetchall()
    return [dict(r) for r in rows]


# ── Shift history ──

def insert_shift_history(project_id, node_ids, delta, created_at):
    conn = get_conn()
    for nid in node_ids:
        conn.execute(
            "INSERT INTO shift_history (project_id, node_id, delta, created_at) "
            "VALUES (?,?,?,?)", (project_id, nid, delta, created_at))
    conn.commit()


def get_shift_history(project_id, limit=20):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM shift_history WHERE project_id=? ORDER BY id DESC LIMIT ?",
        (project_id, limit)).fetchall()
    return [dict(r) for r in rows]


def last_shift_group(project_id):
    """最近一次位移的全部记录（同 created_at 分组），无则返回 []"""
    conn = get_conn()
    row = conn.execute(
        "SELECT created_at FROM shift_history WHERE project_id=? "
        "ORDER BY id DESC LIMIT 1", (project_id,)).fetchone()
    if not row:
        return []
    rows = conn.execute(
        "SELECT * FROM shift_history WHERE project_id=? AND created_at=? "
        "ORDER BY node_id", (project_id, row["created_at"])).fetchall()
    return [dict(r) for r in rows]


# ── Files due recompute（位移联动） ──

def recompute_files_due(project_id, plan):
    """plan: {node_id: (start_str, end_str)}；按锚点重算 files.due_date"""
    conn = get_conn()
    files = conn.execute(
        "SELECT * FROM files WHERE project_id=? AND due_node_id IS NOT NULL "
        "AND due_type IS NOT NULL", (project_id,)).fetchall()
    for f in files:
        nid = f["due_node_id"]
        if nid not in plan:
            continue
        start_s, end_s = plan[nid]
        due = end_s if f["due_type"] == "node_end" else start_s
        conn.execute("UPDATE files SET due_date=? WHERE file_id=?",
                     (due, f["file_id"]))
    conn.commit()
    return len(files)


# ── Files ──

def insert_files(project_id, files: list):
    conn = get_conn()
    for f in files:
        conn.execute(
            "INSERT INTO files (project_id, node_id, doc_name, doc_type, owner_dept, copies, "
            "due_node_id, due_type, remind_before_days, status, due_date, note, is_default) "
            "VALUES (?,?,?,?,?,?,?,?,?, 'pending',?,?, 1)",
            (project_id, f.get("node_id"), f["doc_name"], f["doc_type"],
             f.get("owner_dept"), f.get("copies"),
             f.get("due_node_id"), f.get("due_type"), f.get("remind_before_days", 3),
             f.get("due_date"), f.get("note", ""))
        )
    conn.commit()


def get_files(project_id):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM files WHERE project_id=? ORDER BY node_id, file_id", (project_id,)).fetchall()
    return [dict(r) for r in rows]


def update_file(file_id, **kw):
    conn = get_conn()
    sets = ", ".join(f"{k}=?" for k in kw)
    conn.execute(f"UPDATE files SET {sets} WHERE file_id=?", (*kw.values(), file_id))
    conn.commit()


# ── Op log（操作日志 · 报告时间线/审计） ──

def insert_op_log(project_id, kind, subject="", detail=None, node_id=None,
                   valid=1, created_at=None):
    """写入一条操作日志。created_at 缺省取真实当下（分钟级）。"""
    if created_at is None:
        from datetime import datetime
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO op_log (project_id, node_id, kind, subject, detail, valid, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (project_id, node_id, kind, subject, detail, int(valid), created_at))
    conn.commit()
    return cur.lastrowid


def get_op_log_range(project_id, start=None, end=None, valid_only=True, limit=1000):
    """按 project_id + 时间区间（双条件，含边界）过滤；默认仅有效行。"""
    conn = get_conn()
    sql = "SELECT * FROM op_log WHERE project_id=?"
    args = [project_id]
    if start:
        sql += " AND created_at >= ?"
        args.append(start)
    if end:
        sql += " AND created_at <= ?"
        args.append(end)
    if valid_only:
        sql += " AND valid=1"
    sql += " ORDER BY id ASC"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def get_op_log_all(start=None, end=None, valid_only=True, limit=2000):
    """全项目操作日志（跨项目不混行：仍以 start/end 过滤，供报告聚合各项目后合并）。"""
    conn = get_conn()
    sql = "SELECT * FROM op_log WHERE 1=1"
    args = []
    if start:
        sql += " AND created_at >= ?"
        args.append(start)
    if end:
        sql += " AND created_at <= ?"
        args.append(end)
    if valid_only:
        sql += " AND valid=1"
    sql += " ORDER BY id ASC"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def invalidate_op_log(cond_kwargs):
    """把满足 {kind, subject, project_id, created_date?} 的有效行置 0（审计保留）。
    用于『写前收敛』：提交覆盖、位移当日净收敛等。"""
    conn = get_conn()
    if "created_day" in cond_kwargs and cond_kwargs["created_day"]:
        # 按当日（created_at LIKE 'YYYY-MM-DD%'）收敛
        day = cond_kwargs.pop("created_day")
        cond_kwargs["created_at_like"] = day + "%"
    if "created_at_like" in cond_kwargs and cond_kwargs["created_at_like"]:
        like = cond_kwargs.pop("created_at_like")
        sql = "UPDATE op_log SET valid=0 WHERE valid=1 AND created_at LIKE ?"
        args = [like]
        for k, v in cond_kwargs.items():
            sql += f" AND {k}=?"
            args.append(v)
        conn.execute(sql, args)
        conn.commit()


# ── Settings ──

def get_setting(key, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))
    conn.commit()
