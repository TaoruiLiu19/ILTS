"""
SQLite 数据访问层 — 单连接串行写库，即时 commit。
V2：批次化（batch）口径，见《多式联运.md》§6。节点/单证/货物/班轮/位移/日志全部挂批次，
业务规则按 node_key 定位。旧的项目级函数委托到该项目「当前/默认批次」，保持历史调用不破。
"""

import sqlite3
import os
from datetime import date
from uuid import uuid4

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "logistics.db")

_conn = None
_SCHEMA_VERSION = "2"


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
    project_no              TEXT,                -- 项目号（可编辑，旧数据由种子生成）
    project_name            TEXT NOT NULL,
    country                 TEXT NOT NULL,
    export_port             TEXT,
    customer_id             TEXT,                -- 项目级默认客户（报告默认筛选），批次可覆盖
    status                  TEXT NOT NULL CHECK (status IN ('Active','Completed','Cancelled')),
    etd                     TEXT,                -- 兼容字段（= 默认批次线路 ETD）
    eta                     TEXT,                -- 兼容字段
    buffer_days             INTEGER NOT NULL DEFAULT 4,
    actual_completion_date  TEXT,
    current_batch_id        TEXT,                -- 「当前批次」上下文
    create_date             TEXT NOT NULL,
    last_alert_date         TEXT,
    last_visited_page       TEXT,
    last_opened_project_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_proj_status ON projects(status);

-- 批次 = 一次订舱 + 一次发运（§6.1）
CREATE TABLE IF NOT EXISTS batches (
    batch_id        TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    batch_no        TEXT NOT NULL,               -- {project_no}-B01（项目内唯一）
    batch_name      TEXT,                        -- 默认 B01，可重命名
    booking_no      TEXT,
    mbl_no          TEXT,
    hbl_nos         TEXT,                        -- HBL 号 JSON 数组
    status          TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','ready','running','completed','closed','cancelled')),
    planned_date    TEXT,                        -- 预计发运日 ≡ batch_routes.etd（§6.1 D17）
    actual_etd      TEXT, actual_eta TEXT,
    actual_delivery TEXT, empty_returned_at TEXT,
    previous_status TEXT,                        -- 取消前状态（§8 取消→恢复迁移表）
    cancel_reason   TEXT, closed_at TEXT, created_at TEXT NOT NULL,
    UNIQUE (project_id, batch_no)
);
CREATE INDEX IF NOT EXISTS idx_batch_project ON batches(project_id, status);

-- 线路方案实例（1 批次 1 条，§6.2）
CREATE TABLE IF NOT EXISTS batch_routes (
    route_id        TEXT PRIMARY KEY,
    batch_id        TEXT NOT NULL UNIQUE,
    mode_primary    TEXT NOT NULL DEFAULT 'SEA',
    mode_chain      TEXT NOT NULL DEFAULT '["SEA"]',
    country         TEXT, template_key TEXT, template_snapshot TEXT,
    export_port     TEXT,                        -- 出发国内港口 key
    etd             TEXT, eta TEXT,
    vessel_id       TEXT,
    customs_broker  TEXT, customs_mode TEXT,
    release_mode    TEXT,                        -- ORIGINAL/TELEX/SWB
    container_pickup_location TEXT, empty_return_location TEXT,
    free_demurrage_until TEXT, free_detention_until TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);

-- 集装箱（1 批次 N 柜；§6.3）
CREATE TABLE IF NOT EXISTS containers (
    container_id    TEXT PRIMARY KEY,
    batch_id        TEXT NOT NULL,
    container_no    TEXT NOT NULL,
    seal_no         TEXT,
    container_type  TEXT,
    pickup_at       TEXT,
    return_due      TEXT,
    returned_at     TEXT,
    note            TEXT,
    UNIQUE (batch_id, container_no)
);
-- 拼箱/混装柜预留（§6.3 D29）：一期恒 PRIMARY
CREATE TABLE IF NOT EXISTS container_batch_link (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    container_id TEXT NOT NULL,
    batch_id     TEXT NOT NULL,
    role         TEXT NOT NULL DEFAULT 'PRIMARY',
    note         TEXT,
    UNIQUE (container_id, batch_id)
);

-- 节点（批次级，node_key 稳定标识；§6.5）
CREATE TABLE IF NOT EXISTS nodes (
    batch_id                TEXT NOT NULL,
    node_id                 INTEGER NOT NULL,    -- 批次内展示序 1..15
    node_key                TEXT NOT NULL,       -- 稳定业务标识（规则一律用 key）
    node_name               TEXT NOT NULL,
    role_label              TEXT NOT NULL,
    seq                     INTEGER NOT NULL,
    area                    TEXT NOT NULL CHECK (area IN ('DOME','SEA','OVERSEA')),
    calendar_mode           TEXT NOT NULL DEFAULT 'NATURAL',
    is_key_node             INTEGER NOT NULL DEFAULT 0,
    default_duration        INTEGER NOT NULL,
    duration                INTEGER NOT NULL,
    plan_start              TEXT,
    plan_end                TEXT,
    is_delayed              INTEGER NOT NULL DEFAULT 0,
    delay_days              INTEGER NOT NULL DEFAULT 0,
    status                  TEXT NOT NULL DEFAULT 'Pending'
        CHECK (status IN ('Pending','Active','Overdue','Done')),
    actual_completion_date  TEXT,
    remark                  TEXT,
    PRIMARY KEY (batch_id, node_id),
    UNIQUE (batch_id, node_key)
);
CREATE INDEX IF NOT EXISTS idx_node_batch_seq ON nodes(batch_id, seq);

-- 单证任务（批次级）
CREATE TABLE IF NOT EXISTS files (
    file_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id            TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    node_id             INTEGER,                 -- 展示序（兼容）
    node_key            TEXT,                    -- 锚点节点（稳定）
    doc_name            TEXT NOT NULL,
    doc_type            TEXT NOT NULL CHECK (doc_type IN ('required','optional')),
    owner_dept          TEXT,
    copies              INTEGER,
    due_node_id         INTEGER,
    due_node_key        TEXT,
    due_type            TEXT CHECK (due_type IN ('node_start','node_end', NULL)),
    due_rule            TEXT,                    -- §10.2：node_before/node_after/loading_before_hours
    due_hours           INTEGER,                 -- §10.2 小时级截止（如 AMS 装船前 24h）
    baseline_source     TEXT,                    -- §10.2 基准来源备注（合规争议留证）
    remind_before_days  INTEGER DEFAULT 3,
    status              TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','submitted')),
    due_date            TEXT,
    submitted_date      TEXT,
    owner               TEXT,
    note                TEXT,
    is_default          INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_files_batch ON files(batch_id, node_key);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- 货物台账（批次级）
CREATE TABLE IF NOT EXISTS cargo_items (
    item_id      TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL,
    batch_id     TEXT NOT NULL,
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
    container_type TEXT                -- 20GP/40HQ/40RF…
);
CREATE INDEX IF NOT EXISTS idx_cargo_batch ON cargo_items(batch_id);

-- 班轮（批次级，支持多程）
CREATE TABLE IF NOT EXISTS vessel (
    vessel_id    TEXT PRIMARY KEY,
    batch_id     TEXT NOT NULL,
    voyage_sequence INTEGER NOT NULL DEFAULT 1,
    vessel_name  TEXT,
    imo          TEXT,
    voyage       TEXT,
    carrier      TEXT,
    mmsi         TEXT,
    UNIQUE (batch_id, voyage_sequence)
);

-- 船位历史（批次级）
CREATE TABLE IF NOT EXISTS vessel_positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    TEXT NOT NULL,
    lat         REAL,
    lon         REAL,
    actual_eta  TEXT,
    note        TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vp_batch ON vessel_positions(batch_id, id);

-- 人工位移历史（仅手动位移写入；船期变更不写这里，见 batch_schedule_changes）
CREATE TABLE IF NOT EXISTS shift_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    TEXT NOT NULL,
    node_id     INTEGER NOT NULL,
    node_key    TEXT,
    source      TEXT NOT NULL DEFAULT 'manual',  -- manual / schedule_change
    delta       INTEGER NOT NULL,                -- 正值推迟 / 负值提前
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sh_batch ON shift_history(batch_id, id);

-- 操作日志（批次级为主，scope 区分 project/batch/system）
CREATE TABLE IF NOT EXISTS op_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT NOT NULL,
    batch_id    TEXT,
    node_id     INTEGER,
    node_key    TEXT,
    scope       TEXT NOT NULL DEFAULT 'batch',
    kind        TEXT NOT NULL,
    subject     TEXT NOT NULL DEFAULT '',
    detail      TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opl_batch ON op_log(batch_id, created_at);
CREATE INDEX IF NOT EXISTS idx_opl_project ON op_log(project_id, created_at);

-- 船期变更记录（§6.12 D34）：不写入 shift_history
CREATE TABLE IF NOT EXISTS batch_schedule_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    TEXT NOT NULL,
    old_etd TEXT, old_eta TEXT, new_etd TEXT, new_eta TEXT,
    rule_class TEXT, reason TEXT, source TEXT,
    affected_nodes INTEGER,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sched_batch ON batch_schedule_changes(batch_id, created_at);

-- 客户/货主（§6.11 D32）
CREATE TABLE IF NOT EXISTS parties (
    party_id    TEXT PRIMARY KEY,
    party_name  TEXT NOT NULL,
    name_en     TEXT,
    country     TEXT,
    address     TEXT,
    contact_name TEXT, phone TEXT, email TEXT,
    tax_id      TEXT,
    tax_id_type TEXT,                 -- CNPJ/CPF/VAT/EIN/OTHER
    remark      TEXT,
    created_at  TEXT NOT NULL, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_party_name ON parties(party_name);

CREATE TABLE IF NOT EXISTS party_roles (
    party_id TEXT NOT NULL,
    role     TEXT NOT NULL,            -- CUSTOMER/SHIPPER/CONSIGNEE/NOTIFY/IMPORTER
    PRIMARY KEY (party_id, role)
);

CREATE TABLE IF NOT EXISTS batch_parties (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    role     TEXT NOT NULL,
    party_id TEXT NOT NULL,
    seq      INTEGER DEFAULT 1,
    UNIQUE (batch_id, role, party_id)
);
CREATE INDEX IF NOT EXISTS idx_batch_parties ON batch_parties(batch_id, role);

-- 迁移状态（§6.9 D 风险2）
CREATE TABLE IF NOT EXISTS migration_status (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL,     -- pending/running/failed/completed
    detail      TEXT,
    started_at  TEXT,
    finished_at TEXT
);

-- 换线变更记录（§8 D18）：running 后线路只读，换线写本表 + op_log + 影响清单
CREATE TABLE IF NOT EXISTS batch_route_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    TEXT NOT NULL,
    old_snapshot TEXT,             -- 旧线路 JSON（含 mode/port/country/etd/eta）
    new_snapshot TEXT,             -- 新线路 JSON
    impact      TEXT,              -- 影响清单 JSON（新增/删除/改名/顺序/日期）
    reason      TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_route_changes_batch ON batch_route_changes(batch_id, created_at);

-- 单证类型主数据（§10.1 单证字典）
CREATE TABLE IF NOT EXISTS doc_types (
    type_key        TEXT PRIMARY KEY,   -- 稳定 key（规则一律用 key）
    type_name       TEXT NOT NULL,
    category        TEXT,               -- 出口/海运/进口/申报/保险…
    baseline_source TEXT,               -- 默认基准来源备注（§10.2）
    default_before_days INTEGER,        -- 单证类型默认提前量
    required_roles  TEXT,               -- 角色依赖声明（JSON 数组，§6.11）
    seq             INTEGER
);

-- 提前量多维度规则（§10.2 D23）：(单证类型, 目的国, 承运人) → 提前量
CREATE TABLE IF NOT EXISTS reminder_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_type    TEXT,                  -- 单证类型 key；空 = 任意
    country     TEXT,                  -- 目的国；空 = 任意
    carrier     TEXT,                  -- 承运人；空 = 任意
    before_days INTEGER,               -- 提前量（天）
    before_hours INTEGER,              -- 提前量（小时，装船前 N 小时场景）
    baseline_source TEXT,              -- 基准来源备注
    note        TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_reminder_rules ON reminder_rules(doc_type, country, carrier);

-- 单证依赖链（§10.3 D23）：上游未满足 → 下游「待上游」
CREATE TABLE IF NOT EXISTS doc_dependencies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    up_doc_key   TEXT NOT NULL,        -- 上游单证类型 key
    down_doc_key TEXT NOT NULL,        -- 下游单证类型 key
    note         TEXT,
    UNIQUE (up_doc_key, down_doc_key)
);

-- 保险单（§3.10）：保险公司/保单号/保额/保险起止日期 → 保险到期提醒
CREATE TABLE IF NOT EXISTS insurance_policies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    TEXT NOT NULL,
    company     TEXT,
    policy_no   TEXT,
    amount      REAL,
    start_date  TEXT,
    end_date    TEXT,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_insurance_batch ON insurance_policies(batch_id);
"""


def init_db():
    conn = get_conn()
    # 干净重建守卫：schema_version 非目标版本且无 batches 表时，重建干净库（演示数据可再播种）
    existing = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='batches'").fetchone()
    if existing:
        conn.executescript(SCHEMA_SQL)
    else:
        _rebuild_clean(conn)
    _ensure_columns(conn)
    conn.commit()
    # §10.1/10.2/10.3 单证字典与规则主数据（幂等播种）
    try:
        from services import docdict as _dd
        _dd.seed()
    except Exception:
        pass


# 已存在库的增列（CREATE TABLE IF NOT EXISTS 不会补列）——幂等
_ADDED_COLUMNS = {
    "batches": [
        ("planned_date", "TEXT"),
        ("previous_status", "TEXT"),
    ],
    "files": [
        ("due_rule", "TEXT"),
        ("due_hours", "INTEGER"),
        ("baseline_source", "TEXT"),
    ],
    "nodes": [
        ("last_recompute_at", "TEXT"),
    ],
}


def ensure_schema():
    """外部（工具/测试）可显式调用的幂等建表+补列入口。"""
    conn = get_conn()
    conn.executescript(SCHEMA_SQL)
    _ensure_columns(conn)
    conn.commit()


def _ensure_columns(conn):
    for table, cols in _ADDED_COLUMNS.items():
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not row:
            continue
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, ddl in cols:
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def _rebuild_clean(conn):
    """旧库（无 batches 表）→ 彻底清空业务表重建，避免残余旧主键/列冲突。"""
    owned = ["batch_schedule_changes", "batch_route_changes", "batch_parties",
             "party_roles", "parties", "doc_dependencies", "reminder_rules",
             "doc_types", "insurance_policies",
             "container_batch_link", "containers", "vessel_positions", "vessel",
             "cargo_items", "files", "nodes", "shift_history", "op_log", "batches",
             "batch_routes", "projects", "settings", "migration_status"]
    for t in owned:
        try:
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        except sqlite3.OperationalError:
            pass
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.executescript(SCHEMA_SQL)


def today_str():
    from services.clock import get_today_str
    return get_today_str()


def _now():
    from services.clock import get_now_str
    return get_now_str("%Y-%m-%dT%H:%M:%S+08:00")


# ══════════════════ 批次上下文 ══════════════════

DEFAULT_ROUTE_PRIMARY = "SEA"
DEFAULT_ROUTE_CHAIN = '["SEA"]'

def next_batch_no(project_id):
    """下一个空闲批次号 {project_no}-Bnn（项目内唯一）。"""
    conn = get_conn()
    row = conn.execute("SELECT project_no FROM projects WHERE project_id=?",
                       (project_id,)).fetchone()
    pno = (row["project_no"] if row else None) or "P"
    used = {r["batch_no"] for r in conn.execute(
        "SELECT batch_no FROM batches WHERE project_id=?", (project_id,)).fetchall()}
    n = len(used) + 1
    while f"{pno}-B{n:02d}" in used:
        n += 1
    return f"{pno}-B{n:02d}"


def create_batch(project_id, batch_no=None, batch_name=None, copy_from=None):
    """新建批次（§12.1 新增批次）。copy_from 给定时复制线路/节点/单证模板。"""
    if copy_from:
        from services import batches as _bsvc
        return _bsvc.copy_batch(project_id, copy_from, new_batch_no=batch_no,
                                batch_name=batch_name)
    batch_no = batch_no or next_batch_no(project_id)
    return create_default_batch(project_id, batch_no)


def create_default_batch(project_id, batch_no=None):
    """为项目创建默认批次 B01（幂等）。返回 batch dict。"""
    conn = get_conn()
    row = conn.execute("SELECT project_no FROM projects WHERE project_id=?", (project_id,)).fetchone()
    pno = row["project_no"] if row else None
    if not batch_no:
        batch_no = (pno or "P") + "-B01"
    row = conn.execute(
        "SELECT * FROM batches WHERE project_id=? AND batch_no=?",
        (project_id, batch_no)).fetchone()
    if row:
        b = dict(row)
        _ensure_current_batch(project_id, b["batch_id"])
        return b
    batch_id = f"batch-{uuid4().hex[:10]}"
    conn.execute(
        "INSERT INTO batches (batch_id, project_id, batch_no, batch_name, status, created_at) "
        "VALUES (?,?,?,?, 'draft', ?)",
        (batch_id, project_id, batch_no, batch_no.split("-")[-1], _now()))
    conn.commit()
    _ensure_current_batch(project_id, batch_id)
    return get_batch(batch_id)


def _ensure_current_batch(project_id, batch_id):
    conn = get_conn()
    cur = conn.execute("SELECT current_batch_id FROM projects WHERE project_id=?",
                       (project_id,)).fetchone()
    if not cur or not cur["current_batch_id"]:
        update_project(project_id, current_batch_id=batch_id)


def current_batch_id(project_id):
    """项目「当前批次」；无则取第一个启用批次；再无则创建默认 B01。"""
    conn = get_conn()
    proj = conn.execute("SELECT current_batch_id FROM projects WHERE project_id=?",
                        (project_id,)).fetchone()
    if proj and proj["current_batch_id"]:
        b = get_batch(proj["current_batch_id"])
        if b and b["status"] != "cancelled":
            return b["batch_id"]
    row = conn.execute(
        "SELECT batch_id FROM batches WHERE project_id=? AND status!='cancelled' "
        "ORDER BY created_at LIMIT 1", (project_id,)).fetchone()
    if row:
        return row["batch_id"]
    return create_default_batch(project_id)["batch_id"]


def default_batch(project_id):
    return get_batch(current_batch_id(project_id))


def _resolve_batch(project_id, batch_id=None):
    return batch_id or current_batch_id(project_id)


def resolve_batch_optional(project_id, batch_id=None):
    """只读解析批次：不存在则返回 None，**不创建**默认批次。
    供只读/查询路径使用，避免「查询不存在的项目」时凭空造出批次。"""
    if batch_id:
        return batch_id
    conn = get_conn()
    proj = conn.execute("SELECT current_batch_id FROM projects WHERE project_id=?",
                        (project_id,)).fetchone()
    if proj and proj["current_batch_id"]:
        b = get_batch(proj["current_batch_id"])
        if b and b["status"] != "cancelled":
            return b["batch_id"]
    row = conn.execute(
        "SELECT batch_id FROM batches WHERE project_id=? AND status!='cancelled' "
        "ORDER BY created_at LIMIT 1", (project_id,)).fetchone()
    return row["batch_id"] if row else None


class BatchCancelledError(RuntimeError):
    """批次已取消 → 禁止任何写入（§8 取消期间，后端拒绝而非仅隐藏）。"""


def assert_batch_writable(batch_id):
    """批次写入守卫：已取消批次拒绝一切业务写入（§8）。
    批次自身的 status 变更（取消/恢复）不受此限制，见 update_batch。"""
    if not batch_id:
        return
    row = get_conn().execute("SELECT status FROM batches WHERE batch_id=?",
                             (batch_id,)).fetchone()
    if row and row["status"] == "cancelled":
        raise BatchCancelledError(
            "批次已取消，禁止写入；如需修改请先在批次列表恢复该批次（§8 取消期间）。")


# ── Batches ──

def insert_batch(batch: dict):
    return create_default_batch(batch["project_id"], batch.get("batch_no"))


def get_batch(batch_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
    return dict(row) if row else None


def get_batches(project_id, include_cancelled=False):
    conn = get_conn()
    sql = "SELECT * FROM batches WHERE project_id=?"
    args = [project_id]
    if not include_cancelled:
        sql += " AND status!='cancelled'"
    sql += " ORDER BY created_at"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def update_batch(batch_id, **kw):
    conn = get_conn()
    if kw:
        sets = ", ".join(f"{k}=?" for k in kw)
        conn.execute(f"UPDATE batches SET {sets} WHERE batch_id=?", (*kw.values(), batch_id))
        conn.commit()


def count_batches(project_id):
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) AS c FROM batches WHERE project_id=? AND status!='cancelled'",
                       (project_id,)).fetchone()
    return row["c"]


def delete_project_cascade(project_id):
    """删除项目及其全部批次级数据（保存校验失败时回滚用）。"""
    conn = get_conn()
    batch_ids = [r["batch_id"] for r in conn.execute(
        "SELECT batch_id FROM batches WHERE project_id=?", (project_id,)).fetchall()]
    for bid in batch_ids:
        for sql in ("DELETE FROM files WHERE batch_id=?",
                    "DELETE FROM nodes WHERE batch_id=?",
                    "DELETE FROM containers WHERE batch_id=?",
                    "DELETE FROM container_batch_link WHERE batch_id=?",
                    "DELETE FROM cargo_items WHERE batch_id=?",
                    "DELETE FROM vessel WHERE batch_id=?",
                    "DELETE FROM vessel_positions WHERE batch_id=?",
                    "DELETE FROM shift_history WHERE batch_id=?",
                    "DELETE FROM batch_parties WHERE batch_id=?",
                    "DELETE FROM batch_schedule_changes WHERE batch_id=?",
                    "DELETE FROM batch_route_changes WHERE batch_id=?",
                    "DELETE FROM insurance_policies WHERE batch_id=?",
                    "DELETE FROM batch_routes WHERE batch_id=?",
                    "DELETE FROM batches WHERE batch_id=?"):
            conn.execute(sql, (bid,))
    conn.execute("DELETE FROM op_log WHERE project_id=?", (project_id,))
    conn.execute("DELETE FROM projects WHERE project_id=?", (project_id,))
    conn.commit()


# ── Batch routes ──

def upsert_route(batch_id, **kw):
    assert_batch_writable(batch_id)
    # §9 占位红线：后端硬拒绝未启用线路（不依赖 UI 置灰）
    from services.modes import validate_route_modes, DEFAULT_PRIMARY, DEFAULT_CHAIN
    validate_route_modes(kw.get("mode_primary", DEFAULT_PRIMARY),
                         kw.get("mode_chain", DEFAULT_CHAIN))
    kw.setdefault("mode_primary", DEFAULT_PRIMARY)
    kw.setdefault("mode_chain", DEFAULT_CHAIN)
    conn = get_conn()
    row = conn.execute("SELECT route_id FROM batch_routes WHERE batch_id=?",
                       (batch_id,)).fetchone()
    if row:
        sets = ", ".join(f"{k}=?" for k in kw)
        conn.execute(f"UPDATE batch_routes SET {sets} WHERE batch_id=?",
                     (*kw.values(), batch_id))
        _sync_batch_planned_date(batch_id)
        return row["route_id"]
    route_id = f"route-{uuid4().hex[:10]}"
    cols = ", ".join(kw.keys())
    qs = ", ".join("?" * len(kw))
    conn.execute(f"INSERT INTO batch_routes ({cols}, route_id, batch_id, created_at) "
                 f"VALUES ({qs}, ?, ?, ?)",
                 (*kw.values(), route_id, batch_id, _now()))
    conn.commit()
    _sync_batch_planned_date(batch_id)
    return route_id


def _sync_batch_planned_date(batch_id):
    """planned_date ≡ batch_routes.etd（D17 同一值）。"""
    conn = get_conn()
    row = conn.execute("SELECT project_id FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
    if not row:
        return
    r = conn.execute("SELECT etd FROM batch_routes WHERE batch_id=?", (batch_id,)).fetchone()
    if r:
        conn.execute("UPDATE batches SET planned_date=? WHERE batch_id=?", (r["etd"], batch_id))
        conn.commit()


def get_route(batch_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM batch_routes WHERE batch_id=?", (batch_id,)).fetchone()
    return dict(row) if row else None


def update_route(batch_id, **kw):
    assert_batch_writable(batch_id)
    from services.modes import validate_route_modes
    validate_route_modes(kw.get("mode_primary"), kw.get("mode_chain"))
    conn = get_conn()
    row = conn.execute("SELECT route_id FROM batch_routes WHERE batch_id=?",
                       (batch_id,)).fetchone()
    if not row:
        # 尚无线路方案 → 直接建（upsert 语义，避免静默无效）
        kw.setdefault("mode_primary", DEFAULT_ROUTE_PRIMARY)
        kw.setdefault("mode_chain", DEFAULT_ROUTE_CHAIN)
        rid = f"route-{uuid4().hex[:10]}"
        cols = ", ".join(kw.keys())
        qs = ", ".join("?" * len(kw))
        conn.execute(f"INSERT INTO batch_routes ({cols}, route_id, batch_id, created_at) "
                     f"VALUES ({qs}, ?, ?, ?)", (*kw.values(), rid, batch_id, _now()))
        conn.commit()
        _sync_batch_planned_date(batch_id)
        return
    if kw:
        sets = ", ".join(f"{k}=?" for k in kw)
        conn.execute(f"UPDATE batch_routes SET {sets} WHERE batch_id=?", (*kw.values(), batch_id))
        conn.commit()
        _sync_batch_planned_date(batch_id)


# ── Containers ──

def insert_container(batch_id, container_no, **kw):
    assert_batch_writable(batch_id)
    conn = get_conn()
    cid = f"ctn-{uuid4().hex[:10]}"
    conn.execute(
        "INSERT INTO containers (container_id, batch_id, container_no, seal_no, "
        "container_type, pickup_at, return_due, returned_at, note) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (cid, batch_id, container_no, kw.get("seal_no"), kw.get("container_type"),
         kw.get("pickup_at"), kw.get("return_due"), kw.get("returned_at"), kw.get("note")))
    conn.commit()
    return cid


def update_container(container_id, **kw):
    row = get_conn().execute("SELECT batch_id FROM containers WHERE container_id=?",
                             (container_id,)).fetchone()
    if row:
        assert_batch_writable(row["batch_id"])
    conn = get_conn()
    if kw:
        sets = ", ".join(f"{k}=?" for k in kw)
        conn.execute(f"UPDATE containers SET {sets} WHERE container_id=?", (*kw.values(), container_id))
        conn.commit()


def get_containers(batch_id):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM containers WHERE batch_id=? ORDER BY container_no",
                        (batch_id,)).fetchall()
    return [dict(r) for r in rows]


# ── Link containers to batch (一期恒 PRIMARY) ──

def link_container(batch_id, container_id, role="PRIMARY"):
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO container_batch_link (container_id, batch_id, role) "
                 "VALUES (?,?,?)", (container_id, batch_id, role))
    conn.commit()


# ── Parties 客户/货主 ──

def insert_party(**kw):
    conn = get_conn()
    pid = kw.get("party_id") or f"party-{uuid4().hex[:10]}"
    conn.execute(
        "INSERT INTO parties (party_id, party_name, name_en, country, address, "
        "contact_name, phone, email, tax_id, tax_id_type, remark, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, kw["party_name"], kw.get("name_en"), kw.get("country"), kw.get("address"),
         kw.get("contact_name"), kw.get("phone"), kw.get("email"), kw.get("tax_id"),
         kw.get("tax_id_type"), kw.get("remark"), _now()))
    for role in kw.get("roles") or []:
        conn.execute("INSERT OR IGNORE INTO party_roles (party_id, role) VALUES (?,?)",
                     (pid, role))
    conn.commit()
    return pid


def update_party(party_id, **kw):
    conn = get_conn()
    sets = ", ".join(f"{k}=?" for k in kw if k in (
        "party_name", "name_en", "country", "address", "contact_name", "phone",
        "email", "tax_id", "tax_id_type", "remark"))
    vals = [kw[k] for k in kw if k in (
        "party_name", "name_en", "country", "address", "contact_name", "phone",
        "email", "tax_id", "tax_id_type", "remark")]
    if sets:
        conn.execute(f"UPDATE parties SET {sets}, updated_at=? WHERE party_id=?",
                     (*vals, _now(), party_id))
    if "roles" in kw:
        conn.execute("DELETE FROM party_roles WHERE party_id=?", (party_id,))
        for role in kw["roles"]:
            conn.execute("INSERT OR IGNORE INTO party_roles (party_id, role) VALUES (?,?)",
                         (party_id, role))
    conn.commit()


def get_party(party_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM parties WHERE party_id=?", (party_id,)).fetchone()
    if not row:
        return None
    p = dict(row)
    p["roles"] = [r["role"] for r in conn.execute(
        "SELECT role FROM party_roles WHERE party_id=?", (party_id,)).fetchall()]
    return p


def list_parties(keyword=None):
    conn = get_conn()
    sql = "SELECT * FROM parties WHERE 1=1"
    args = []
    if keyword:
        sql += " AND (party_name LIKE ? OR name_en LIKE ?)"
        args += [f"%{keyword}%", f"%{keyword}%"]
    sql += " ORDER BY party_name"
    rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def bind_batch_party(batch_id, role, party_id, seq=1):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO batch_parties (batch_id, role, party_id, seq) VALUES (?,?,?,?)",
        (batch_id, role, party_id, seq))
    conn.commit()


def unbind_batch_party(batch_id, role, party_id):
    conn = get_conn()
    conn.execute("DELETE FROM batch_parties WHERE batch_id=? AND role=? AND party_id=?",
                 (batch_id, role, party_id))
    conn.commit()


def get_batch_parties(batch_id, role=None):
    conn = get_conn()
    sql = ("SELECT bp.id, bp.role, bp.seq, p.* FROM batch_parties bp "
           "JOIN parties p ON p.party_id=bp.party_id WHERE bp.batch_id=?")
    args = [batch_id]
    if role:
        sql += " AND bp.role=?"
        args.append(role)
    sql += " ORDER BY bp.role, bp.seq"
    rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def set_batch_parties(batch_id, role, party_ids):
    conn = get_conn()
    conn.execute("DELETE FROM batch_parties WHERE batch_id=? AND role=?", (batch_id, role))
    for i, pid in enumerate(party_ids, start=1):
        conn.execute(
            "INSERT INTO batch_parties (batch_id, role, party_id, seq) VALUES (?,?,?,?)",
            (batch_id, role, pid, i))
    conn.commit()


# ── Route changes（§8 换线留痕） ──

def insert_route_change(batch_id, **kw):
    assert_batch_writable(batch_id)
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO batch_route_changes (batch_id, old_snapshot, new_snapshot, impact, "
        "reason, created_at) VALUES (?,?,?,?,?,?)",
        (batch_id, kw.get("old_snapshot"), kw.get("new_snapshot"), kw.get("impact"),
         kw.get("reason"), kw.get("created_at") or _now()))
    conn.commit()
    return cur.lastrowid


def get_route_changes(batch_id, limit=50):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM batch_route_changes WHERE batch_id=? ORDER BY id DESC LIMIT ?",
        (batch_id, limit)).fetchall()
    return [dict(r) for r in rows]


# ── Ship schedule changes ──

def insert_schedule_change(batch_id, **kw):
    assert_batch_writable(batch_id)
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO batch_schedule_changes (batch_id, old_etd, old_eta, new_etd, new_eta, "
        "rule_class, reason, source, affected_nodes, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (batch_id, kw.get("old_etd"), kw.get("old_eta"), kw.get("new_etd"),
         kw.get("new_eta"), kw.get("rule_class"), kw.get("reason"), kw.get("source"),
         kw.get("affected_nodes"), kw.get("created_at") or _now()))
    conn.commit()
    return cur.lastrowid


def get_schedule_changes(batch_id, limit=50):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM batch_schedule_changes WHERE batch_id=? ORDER BY id DESC LIMIT ?",
        (batch_id, limit)).fetchall()
    return [dict(r) for r in rows]


# ── Projects ──

def ensure_project_no(project_id, project_no=None):
    conn = get_conn()
    proj = get_project(project_id)
    if not proj:
        return None
    if proj.get("project_no"):
        return proj["project_no"]
    if not project_no:
        project_no = f"P-{project_id[:8].upper():0>8}"
    conn.execute("UPDATE projects SET project_no=? WHERE project_id=?",
                 (project_no, project_id))
    conn.commit()
    return project_no


def set_project_no(project_id, new_no, rename_batches=True):
    """修改项目号（§5.1/T14）：项目号可改，**批次号随之更新**。

    规则：批次号形如 {project_no}-Bnn，改项目号时把前缀一并替换；
    若新批次号与本项目已有批次号冲突则拒绝。
    """
    new_no = (new_no or "").strip()
    if not new_no:
        raise ValueError("项目号不能为空")
    conn = get_conn()
    proj = get_project(project_id)
    if not proj:
        raise ValueError("项目不存在")
    old_no = proj.get("project_no")
    if not rename_batches or not old_no:
        conn.execute("UPDATE projects SET project_no=? WHERE project_id=?",
                     (new_no, project_id))
        conn.commit()
        return new_no

    rows = [dict(r) for r in conn.execute(
        "SELECT batch_id, batch_no FROM batches WHERE project_id=?", (project_id,)).fetchall()]
    planned = {}
    for r in rows:
        no = r["batch_no"] or ""
        planned[r["batch_id"]] = (new_no + no[len(old_no):]) if no.startswith(old_no) else no
    dup = [v for v in planned.values() if
           conn.execute("SELECT 1 FROM batches WHERE project_id=? AND batch_no=?",
                        (project_id, v)).fetchone()
           and planned.get(
               next((k for k, vv in planned.items() if vv == v), None)) != v]
    if len(set(planned.values())) != len(planned):
        raise ValueError(f"改号后批次号冲突：{sorted(set(planned.values()))}")
    for bid, no in planned.items():
        conn.execute("UPDATE batches SET batch_no=? WHERE batch_id=?", (no, bid))
    conn.execute("UPDATE projects SET project_no=? WHERE project_id=?", (new_no, project_id))
    conn.commit()
    return new_no


def insert_project(proj: dict):
    conn = get_conn()
    project_no = ensure_project_no(proj["project_id"], proj.get("project_no"))
    conn.execute(
        "INSERT INTO projects (project_id, project_no, project_name, country, export_port, "
        "customer_id, status, etd, eta, buffer_days, create_date) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (proj["project_id"], project_no, proj["project_name"], proj["country"],
         proj.get("export_port"), proj.get("customer_id"), proj.get("status", "Active"),
         proj.get("etd"), proj.get("eta"), proj.get("buffer_days", 4), today_str()))
    conn.commit()


def update_project(project_id, **kw):
    conn = get_conn()
    if kw:
        sets = ", ".join(f"{k}=?" for k in kw)
        conn.execute(f"UPDATE projects SET {sets} WHERE project_id=?", (*kw.values(), project_id))
        conn.commit()


def get_project(project_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
    if not row:
        return None
    p = dict(row)
    # 兼容旧调用：仅当已有批次时，把项目级 etd/eta 降级为默认批次线路（避免建库期递归）
    if not p.get("etd"):
        bst = conn.execute("SELECT batch_id FROM batches WHERE project_id=? AND status!='cancelled' "
                           "ORDER BY created_at LIMIT 1", (project_id,)).fetchone()
        if bst:
            r = conn.execute("SELECT etd, eta FROM batch_routes WHERE batch_id=?", (bst["batch_id"],)).fetchone()
            if r:
                p["etd"], p["eta"] = r["etd"], r["eta"]
    return p


def get_projects_by_status(status):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM projects WHERE status=? ORDER BY create_date", (status,)).fetchall()
    return [dict(r) for r in rows]


def count_projects(status):
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) AS c FROM projects WHERE status=?", (status,)).fetchone()
    return row["c"]


# ── Nodes ──

def _derive_node_key(node_id):
    """node_key 缺省时的回退推导（node_id == 模板序，见 services.node_template）。
    旧调用方只传 node_id 时不得写入 NULL（nodes.node_key 为 NOT NULL）。"""
    from services import node_template as _nt
    t = _nt.by_id(node_id)
    if t:
        return t["node_key"]
    return f"NODE_{node_id}"


def insert_nodes(project_id, nodes: list, batch_id=None):
    batch_id = _resolve_batch(project_id, batch_id)
    assert_batch_writable(batch_id)
    conn = get_conn()
    for n in nodes:
        conn.execute(
            "INSERT INTO nodes (batch_id, node_id, node_key, node_name, role_label, seq, area, "
            "calendar_mode, is_key_node, default_duration, duration, plan_start, plan_end, "
            "status, actual_completion_date, is_delayed, delay_days, remark) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (batch_id, n["node_id"], n.get("node_key") or _derive_node_key(n["node_id"]),
             n["node_name"], n["role_label"],
             n["seq"], n["area"], n.get("calendar_mode", "NATURAL"),
             1 if n.get("is_key_node") or n.get("key_node") else 0,
             n.get("default_duration", n["duration"]), n["duration"],
             n.get("plan_start"), n.get("plan_end"),
             n.get("status", "Pending"), n.get("actual_completion_date"),
             n.get("is_delayed", 0), n.get("delay_days", 0), n.get("remark", "")))
    conn.commit()


def get_nodes(project_id, batch_id=None):
    batch_id = _resolve_batch(project_id, batch_id)
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM nodes WHERE batch_id=? ORDER BY seq", (batch_id,)).fetchall()
    return [dict(r) for r in rows]


def get_nodes_by_batch(batch_id):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM nodes WHERE batch_id=? ORDER BY seq", (batch_id,)).fetchall()
    return [dict(r) for r in rows]


def node_by_key(batch_id, node_key):
    conn = get_conn()
    row = conn.execute("SELECT * FROM nodes WHERE batch_id=? AND node_key=?",
                       (batch_id, node_key)).fetchone()
    return dict(row) if row else None


def update_node(project_id, node_id, batch_id=None, **kw):
    batch_id = _resolve_batch(project_id, batch_id)
    assert_batch_writable(batch_id)
    conn = get_conn()
    if kw:
        sets = ", ".join(f"{k}=?" for k in kw)
        conn.execute(f"UPDATE nodes SET {sets} WHERE batch_id=? AND node_id=?",
                     (*kw.values(), batch_id, node_id))
        conn.commit()
    # 节点完成 → 自动回填批次 actual_*（§6.10 D31）；失败不影响主流程
    if kw.get("status") == "Done":
        try:
            from services import batches as _bsvc
            _bsvc.on_node_completed(batch_id, node_id)
        except Exception:
            pass


# ── Cargo items ──

_CARGO_COLS = ("seq", "item_name", "qty", "unit", "dim_l", "dim_w", "dim_h",
               "weight_kg", "gross_m3", "over_flag", "od_type", "marks",
               "packaging", "container_no", "seal_no", "container_type")


def insert_cargo_items(project_id, items: list, batch_id=None):
    batch_id = _resolve_batch(project_id, batch_id)
    assert_batch_writable(batch_id)
    conn = get_conn()
    for i, it in enumerate(items, start=1):
        item_id = it.get("item_id") or f"item-{uuid4().hex[:10]}"
        conn.execute(
            "INSERT INTO cargo_items (item_id, project_id, batch_id, seq, item_name, qty, unit, "
            "dim_l, dim_w, dim_h, weight_kg, gross_m3, over_flag, od_type, marks, packaging, "
            "container_no, seal_no, container_type) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, project_id, batch_id, it.get("seq", i), it["item_name"],
             it.get("qty", 1), it.get("unit"), it.get("dim_l"), it.get("dim_w"),
             it.get("dim_h"), it.get("weight_kg"), it.get("gross_m3"),
             it.get("over_flag", 0), it.get("od_type"), it.get("marks"),
             it.get("packaging"), it.get("container_no"), it.get("seal_no"),
             it.get("container_type")))
    conn.commit()


def get_cargo_items(project_id, batch_id=None):
    batch_id = _resolve_batch(project_id, batch_id)
    conn = get_conn()
    rows = conn.execute("SELECT * FROM cargo_items WHERE batch_id=? ORDER BY seq, item_id",
                        (batch_id,)).fetchall()
    return [dict(r) for r in rows]


def update_cargo_item(item_id, **kw):
    conn = get_conn()
    if kw:
        sets = ", ".join(f"{k}=?" for k in kw)
        conn.execute(f"UPDATE cargo_items SET {sets} WHERE item_id=?", (*kw.values(), item_id))
        conn.commit()


def delete_cargo_item(item_id):
    conn = get_conn()
    conn.execute("DELETE FROM cargo_items WHERE item_id=?", (item_id,))
    conn.commit()


def delete_cargo_items(project_id, batch_id=None):
    batch_id = _resolve_batch(project_id, batch_id)
    conn = get_conn()
    conn.execute("DELETE FROM cargo_items WHERE batch_id=?", (batch_id,))
    conn.commit()


# ── Vessel ──

def upsert_vessel(project_id, vessel_name=None, imo=None, voyage=None,
                  carrier=None, mmsi=None, batch_id=None):
    batch_id = _resolve_batch(project_id, batch_id)
    assert_batch_writable(batch_id)
    conn = get_conn()
    row = conn.execute("SELECT * FROM vessel WHERE batch_id=? AND voyage_sequence=1",
                       (batch_id,)).fetchone()
    if row:
        conn.execute(
            "UPDATE vessel SET vessel_name=?, imo=?, voyage=?, carrier=?, mmsi=? "
            "WHERE batch_id=? AND voyage_sequence=1",
            (vessel_name, imo, voyage, carrier, mmsi, batch_id))
        vessel_id = row["vessel_id"]
    else:
        vessel_id = f"ves-{uuid4().hex[:8]}"
        conn.execute(
            "INSERT INTO vessel (vessel_id, batch_id, voyage_sequence, vessel_name, imo, "
            "voyage, carrier, mmsi) VALUES (?,?,1,?,?,?,?,?)",
            (vessel_id, batch_id, vessel_name, imo, voyage, carrier, mmsi))
    conn.commit()
    return get_vessel(project_id, batch_id)


def get_vessel(project_id, batch_id=None):
    batch_id = _resolve_batch(project_id, batch_id)
    conn = get_conn()
    row = conn.execute("SELECT * FROM vessel WHERE batch_id=? AND voyage_sequence=1",
                       (batch_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    # 兼容层：vessel 表是批次级（无 project_id 列），调用方常按项目取用
    d["project_id"] = project_id
    return d


def insert_vessel_position(project_id, lat=None, lon=None, actual_eta=None, note=None,
                           batch_id=None):
    batch_id = _resolve_batch(project_id, batch_id)
    assert_batch_writable(batch_id)
    from services.clock import get_now_str
    conn = get_conn()
    conn.execute(
        "INSERT INTO vessel_positions (batch_id, lat, lon, actual_eta, note, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (batch_id, lat, lon, actual_eta, note, _now()))
    conn.commit()


def get_vessel_positions(project_id, limit=10, batch_id=None):
    batch_id = _resolve_batch(project_id, batch_id)
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM vessel_positions WHERE batch_id=? ORDER BY id DESC LIMIT ?",
        (batch_id, limit)).fetchall()
    return [dict(r) for r in rows]


# ── Shift history ──

def insert_shift_history(project_id, node_ids, delta, created_at, batch_id=None, node_keys=None):
    batch_id = _resolve_batch(project_id, batch_id)
    assert_batch_writable(batch_id)
    conn = get_conn()
    for i, nid in enumerate(node_ids):
        conn.execute(
            "INSERT INTO shift_history (batch_id, node_id, node_key, source, delta, created_at) "
            "VALUES (?,?,?, 'manual', ?, ?)",
            (batch_id, nid, (node_keys or {}).get(nid), delta, created_at))
    conn.commit()


def get_shift_history(project_id, limit=20, batch_id=None):
    batch_id = _resolve_batch(project_id, batch_id)
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM shift_history WHERE batch_id=? ORDER BY id DESC LIMIT ?",
        (batch_id, limit)).fetchall()
    return [dict(r) for r in rows]


def last_shift_group(project_id, batch_id=None):
    batch_id = resolve_batch_optional(project_id, batch_id)
    if not batch_id:
        return []
    conn = get_conn()
    row = conn.execute(
        "SELECT created_at FROM shift_history WHERE batch_id=? ORDER BY id DESC LIMIT 1",
        (batch_id,)).fetchone()
    if not row:
        return []
    rows = conn.execute(
        "SELECT * FROM shift_history WHERE batch_id=? AND created_at=? ORDER BY node_id",
        (batch_id, row["created_at"])).fetchall()
    return [dict(r) for r in rows]


# ── Files due recompute（位移联动） ──

def recompute_files_due(project_id, plan_by_key, batch_id=None):
    """plan_by_key: {node_key: (start_str, end_str)}；按锚点 node_key 重算 files.due_date"""
    batch_id = _resolve_batch(project_id, batch_id)
    assert_batch_writable(batch_id)
    conn = get_conn()
    files = conn.execute(
        "SELECT * FROM files WHERE batch_id=? AND due_node_key IS NOT NULL "
        "AND due_type IS NOT NULL", (batch_id,)).fetchall()
    for f in files:
        # §10.2 小时级截止（装船前 N 小时）：锚装船节点，不走 start/end
        if f["due_rule"] == "loading_before_hours":
            from services import node_template as _nt
            import math
            from datetime import date as _date, timedelta as _td
            base = plan_by_key.get(_nt.LOADING)
            if base:
                days = max(1, math.ceil(int(f["due_hours"] or 24) / 24.0))
                d0 = _date(*map(int, base[0].split("-")))
                conn.execute("UPDATE files SET due_date=? WHERE file_id=?",
                             ((d0 - _td(days=days)).isoformat(), f["file_id"]))
            continue
        key = f["due_node_key"]
        if key not in plan_by_key:
            continue
        start_s, end_s = plan_by_key[key]
        due = end_s if f["due_type"] == "node_end" else start_s
        conn.execute("UPDATE files SET due_date=? WHERE file_id=?", (due, f["file_id"]))
    conn.commit()
    return len(files)


# ── Files ──

def insert_files(project_id, files: list, batch_id=None):
    batch_id = _resolve_batch(project_id, batch_id)
    assert_batch_writable(batch_id)
    conn = get_conn()
    for f in files:
        conn.execute(
            "INSERT INTO files (batch_id, project_id, node_id, node_key, doc_name, doc_type, "
            "owner_dept, copies, due_node_id, due_node_key, due_type, due_rule, due_hours, "
            "baseline_source, remind_before_days, status, due_date, note, is_default) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending',?,?, 1)",
            (batch_id, project_id, f.get("node_id"), f.get("node_key"), f["doc_name"],
             f["doc_type"], f.get("owner_dept"), f.get("copies"), f.get("due_node_id"),
             f.get("due_node_key"), f.get("due_type"), f.get("due_rule"), f.get("due_hours"),
             f.get("baseline_source"), f.get("remind_before_days", 3),
             f.get("due_date"), f.get("note", "")))
    conn.commit()


def get_files(project_id, batch_id=None):
    batch_id = _resolve_batch(project_id, batch_id)
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM files WHERE batch_id=? ORDER BY node_id, file_id", (batch_id,)).fetchall()
    return [dict(r) for r in rows]


def get_files_by_batch(batch_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM files WHERE batch_id=? ORDER BY node_id, file_id", (batch_id,)).fetchall()
    return [dict(r) for r in rows]


def update_file(file_id, **kw):
    row = get_conn().execute("SELECT batch_id FROM files WHERE file_id=?", (file_id,)).fetchone()
    if row:
        assert_batch_writable(row["batch_id"])
    conn = get_conn()
    if kw:
        sets = ", ".join(f"{k}=?" for k in kw)
        conn.execute(f"UPDATE files SET {sets} WHERE file_id=?", (*kw.values(), file_id))
        conn.commit()


# ── Op log ──

# §6.6 时间戳统一 ISO 8601 +08:00；旧行可能是 'YYYY-MM-DD HH:MM'，比较/排序时归一化到分钟。
_TS_EXPR = "substr(replace(created_at,'T',' '),1,16)"


def _norm_ts(s):
    """把任意时间戳写法归一为 'YYYY-MM-DD HH:MM'，供与 _TS_EXPR 比较。"""
    if not s:
        return s
    return str(s).replace("T", " ")[:16]


def normalize_ts(value):
    """把任意时间戳写法归一为 ISO 8601 +08:00（§6.6）。
    系统全域为北京时间：无偏移的时间戳按北京时间解释。非法输入原样返回，
    交由 §6.8 校验（item 10）暴露为可视问题。"""
    if not value:
        return value
    s = str(value).strip().replace("T", " ")
    s = s[:19]
    if "+" in s[10:] or s.endswith("Z"):
        return str(value).strip()
    if len(s) == 16:            # YYYY-MM-DD HH:MM
        s += ":00"
    import re as _re
    if not _re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", s):
        return str(value).strip()
    return s.replace(" ", "T") + "+08:00"


def insert_op_log(project_id, kind, subject="", detail=None, node_id=None,
                  batch_id=None, node_key=None, scope="batch", created_at=None):
    if created_at is None:
        created_at = _now()
    else:
        created_at = normalize_ts(created_at)
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO op_log (project_id, batch_id, node_id, node_key, scope, kind, subject, "
        "detail, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (project_id, batch_id, node_id, node_key, scope, kind, subject, detail, created_at))
    conn.commit()
    return cur.lastrowid


def get_op_log_range(project_id, start=None, end=None, limit=1000, batch_id=None):
    conn = get_conn()
    sql = "SELECT * FROM op_log WHERE project_id=?"
    args = [project_id]
    if batch_id:
        sql += " AND (batch_id=? OR batch_id IS NULL)"
        args.append(batch_id)
    if start:
        sql += f" AND {_TS_EXPR} >= ?"
        args.append(_norm_ts(start))
    if end:
        sql += f" AND {_TS_EXPR} <= ?"
        args.append(_norm_ts(end))
    sql += " ORDER BY id ASC"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def get_op_log_all(start=None, end=None, limit=2000):
    conn = get_conn()
    sql = "SELECT * FROM op_log WHERE 1=1"
    args = []
    if start:
        sql += f" AND {_TS_EXPR} >= ?"
        args.append(_norm_ts(start))
    if end:
        sql += f" AND {_TS_EXPR} <= ?"
        args.append(_norm_ts(end))
    sql += " ORDER BY id ASC"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def delete_op_log(cond):
    conn = get_conn()
    cond = dict(cond)
    sql = "DELETE FROM op_log WHERE 1=1"
    args = []
    day = cond.pop("created_day", None)
    if day:
        sql += " AND created_at LIKE ?"
        args.append(day + "%")
    for k, v in cond.items():
        if isinstance(v, (tuple, list, set)):
            v = list(v)
            if not v:
                continue
            sql += f" AND {k} IN ({','.join('?' * len(v))})"
            args.extend(v)
        else:
            sql += f" AND {k}=?"
            args.append(v)
    cur = conn.execute(sql, args)
    conn.commit()
    return cur.rowcount


def last_file_actions(project_id, batch_id=None):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM op_log WHERE project_id=? AND kind IN ('file_submit','file_withdraw') "
        f"ORDER BY {_TS_EXPR} DESC, id DESC", (project_id,)).fetchall()
    out = {}
    for r in rows:
        key = r["subject"] or ""
        if key not in out:
            out[key] = dict(r)
    return out


# ── 单证类型主数据（§10.1） ──

def list_doc_types():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM doc_types ORDER BY seq, type_key").fetchall()
    return [dict(r) for r in rows]


def get_doc_type(type_key):
    conn = get_conn()
    row = conn.execute("SELECT * FROM doc_types WHERE type_key=?", (type_key,)).fetchone()
    return dict(row) if row else None


def upsert_doc_type(type_key, **kw):
    conn = get_conn()
    cols = ("type_name", "category", "baseline_source", "default_before_days",
            "required_roles", "seq")
    vals = [kw.get(c) for c in cols]
    conn.execute(
        "INSERT OR REPLACE INTO doc_types (type_key, type_name, category, baseline_source, "
        "default_before_days, required_roles, seq) VALUES (?,?,?,?,?,?,?)",
        (type_key, *vals))
    conn.commit()


# ── 提醒提前量规则（§10.2 D23：(单证类型, 目的国, 承运人)） ──

def list_reminder_rules(enabled_only=True):
    conn = get_conn()
    sql = "SELECT * FROM reminder_rules"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY id"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def insert_reminder_rule(doc_type=None, country=None, carrier=None, before_days=None,
                         before_hours=None, baseline_source=None, note=None):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO reminder_rules (doc_type, country, carrier, before_days, before_hours, "
        "baseline_source, note) VALUES (?,?,?,?,?,?,?)",
        (doc_type, country, carrier, before_days, before_hours, baseline_source, note))
    conn.commit()
    return cur.lastrowid


def delete_reminder_rule(rule_id):
    conn = get_conn()
    conn.execute("DELETE FROM reminder_rules WHERE id=?", (rule_id,))
    conn.commit()


def set_reminder_rules(rules):
    """整体替换规则集（幂等播种用）。rules: [dict, ...]"""
    conn = get_conn()
    conn.execute("DELETE FROM reminder_rules")
    for r in rules:
        conn.execute(
            "INSERT INTO reminder_rules (doc_type, country, carrier, before_days, before_hours, "
            "baseline_source, note, enabled) VALUES (?,?,?,?,?,?,?,1)",
            (r.get("doc_type"), r.get("country"), r.get("carrier"), r.get("before_days"),
             r.get("before_hours"), r.get("baseline_source"), r.get("note")))
    conn.commit()


# ── 单证依赖链（§10.3 D23） ──

def list_doc_dependencies():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM doc_dependencies ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def set_doc_dependencies(edges):
    """整体替换依赖边（幂等播种用，含无环校验）。edges: [(up, down), ...]"""
    _assert_acyclic(edges)
    conn = get_conn()
    conn.execute("DELETE FROM doc_dependencies")
    for up, down in edges:
        conn.execute("INSERT OR IGNORE INTO doc_dependencies (up_doc_key, down_doc_key) "
                     "VALUES (?,?)", (up, down))
    conn.commit()


def _assert_acyclic(edges):
    """§15 依赖链误配应对：依赖校验（无环）。"""
    graph = {}
    for up, down in edges:
        graph.setdefault(up, []).append(down)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {}

    def visit(n):
        color[n] = GRAY
        for m in graph.get(n, []):
            if color.get(m) == GRAY:
                raise ValueError(f"单证依赖链存在环：{n} → {m}")
            if color.get(m, WHITE) == WHITE:
                visit(m)
        color[n] = BLACK

    for n in list(graph):
        if color.get(n, WHITE) == WHITE:
            visit(n)


def upstream_docs(doc_key, doc_key_to_name=None):
    """返回该单证的全部直接上游（key 列表）。"""
    conn = get_conn()
    rows = conn.execute("SELECT up_doc_key FROM doc_dependencies WHERE down_doc_key=?",
                        (doc_key,)).fetchall()
    return [r["up_doc_key"] for r in rows]


# ── 保险单（§3.10） ──

def upsert_insurance(batch_id, company=None, policy_no=None, amount=None,
                     start_date=None, end_date=None, note=None):
    assert_batch_writable(batch_id)
    conn = get_conn()
    row = conn.execute("SELECT id FROM insurance_policies WHERE batch_id=? LIMIT 1",
                       (batch_id,)).fetchone()
    if row:
        conn.execute(
            "UPDATE insurance_policies SET company=?, policy_no=?, amount=?, start_date=?, "
            "end_date=?, note=? WHERE id=?",
            (company, policy_no, amount, start_date, end_date, note, row["id"]))
        conn.commit()
        return row["id"]
    cur = conn.execute(
        "INSERT INTO insurance_policies (batch_id, company, policy_no, amount, start_date, "
        "end_date, note) VALUES (?,?,?,?,?,?,?)",
        (batch_id, company, policy_no, amount, start_date, end_date, note))
    conn.commit()
    return cur.lastrowid


def get_insurance(batch_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM insurance_policies WHERE batch_id=? LIMIT 1",
                       (batch_id,)).fetchone()
    return dict(row) if row else None


# ── Settings ──

def get_setting(key, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))
    conn.commit()