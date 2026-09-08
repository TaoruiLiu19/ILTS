"""
SQLite 数据访问层 — 单连接串行写库，即时 commit。
"""

import sqlite3
import os
from datetime import date

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


# ── Settings ──

def get_setting(key, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))
    conn.commit()
