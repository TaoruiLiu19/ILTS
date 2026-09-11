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

-- 项目级单证（§10.1 无节点锚点的条目：《项目日报》《物流动态跟踪表》《项目进度报告》）
-- 为什么单开一张表：这些单证与批次无关，旧口径按批次各复制一份 → 3 个批次 9 行、
-- 用户要提交 3 次、报告里重复出现。files 表结构与其 batch_id NOT NULL 不变量
-- （tools/migrate_batches.py 的「files.batch_id IS NULL 必须为 0」）保持不变，项目级行走本表。
CREATE TABLE IF NOT EXISTS project_files (
    project_file_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id         TEXT NOT NULL,
    doc_name           TEXT NOT NULL,
    doc_type           TEXT NOT NULL CHECK (doc_type IN ('required','optional')),
    owner_dept         TEXT,
    copies             INTEGER,
    remind_before_days INTEGER DEFAULT 3,
    status             TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','submitted')),
    submitted_date     TEXT,
    owner              TEXT,
    note               TEXT,
    is_default         INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_project_files_doc ON project_files(project_id, doc_name);

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

-- ═══════════════════════════════════════════════════════════════════════════
-- 多式联运统一模型（数据模型升级：Project → Batch → Route → Leg → Node）
-- 新增表全部独立存在，不与既有 V2 表冲突；既有 nodes/files 通过锚点列打通。
-- 纯海运 =「1 条线路 + 1 个 sea leg + 原节点」。
-- ═══════════════════════════════════════════════════════════════════════════

-- 3.3 地点库（多式联运统一地点）
CREATE TABLE IF NOT EXISTS locations (
    loc_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    unlocode        TEXT UNIQUE,          -- 如 CNSHA, BRSSZ
    name            TEXT NOT NULL,
    loc_type        TEXT NOT NULL DEFAULT 'port'
                    CHECK (loc_type IN ('port','airport','rail_station','terminal','warehouse','border')),
    country         TEXT,
    timezone        TEXT,
    created_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_locations_type ON locations(loc_type);

-- 3.4 线路模板（可复用运输产品）
CREATE TABLE IF NOT EXISTS route_templates (
    template_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    template_name   TEXT NOT NULL,
    description     TEXT,
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT
);
CREATE TABLE IF NOT EXISTS route_template_legs (
    template_leg_id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id     INTEGER NOT NULL REFERENCES route_templates(template_id),
    seq             INTEGER NOT NULL,
    mode            TEXT NOT NULL CHECK (mode IN ('sea','road','rail','air')),
    origin_name     TEXT,
    dest_name       TEXT,
    default_carrier_role TEXT,
    UNIQUE (template_id, seq)
);

-- 3.5 运输线路实例（候选/选中/执行）
CREATE TABLE IF NOT EXISTS routes (
    route_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        TEXT NOT NULL REFERENCES batches(batch_id),
    template_id     INTEGER REFERENCES route_templates(template_id),
    route_name      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'candidate'
                    CHECK (status IN ('candidate','selected','active','completed','cancelled')),
    is_active       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT,
    updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_routes_batch ON routes(batch_id);
-- 一个批次最多一条 active 线路（部分唯一索引）
CREATE UNIQUE INDEX IF NOT EXISTS idx_routes_active
ON routes(batch_id) WHERE is_active = 1 AND status NOT IN ('cancelled','completed');

-- 3.6 运输段（单一路径段）
CREATE TABLE IF NOT EXISTS route_legs (
    leg_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id        INTEGER NOT NULL REFERENCES routes(route_id),
    seq             INTEGER NOT NULL,
    mode            TEXT NOT NULL CHECK (mode IN ('sea','road','rail','air')),
    origin_name     TEXT,
    dest_name       TEXT,
    origin_loc_id   INTEGER REFERENCES locations(loc_id),
    dest_loc_id     INTEGER REFERENCES locations(loc_id),
    carrier_id      TEXT,
    vehicle_no      TEXT,
    voyage_flight_train TEXT,
    planned_etd     TEXT,
    planned_eta     TEXT,
    actual_etd      TEXT,
    actual_eta      TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','running','completed','cancelled')),
    created_at      TEXT,
    updated_at      TEXT,
    UNIQUE (route_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_route_legs_route ON route_legs(route_id);

-- 3.7 节点定义（按运输方式的标准节点模板）
CREATE TABLE IF NOT EXISTS node_defs (
    node_def_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    mode            TEXT NOT NULL CHECK (mode IN ('sea','road','rail','air','common')),
    node_key        TEXT NOT NULL,
    node_name       TEXT NOT NULL,
    phase           TEXT,
    work_item       TEXT,
    default_anchor_level TEXT NOT NULL DEFAULT 'node'
                        CHECK (default_anchor_level IN ('project','batch','route','leg','node')),
    required_docs   TEXT,
    UNIQUE (mode, node_key)
);
CREATE INDEX IF NOT EXISTS idx_node_defs_mode ON node_defs(mode);

-- 3.9 单证定义（统一主数据，缺省锚点级别）
CREATE TABLE IF NOT EXISTS doc_definitions (
    doc_def_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_code        TEXT NOT NULL UNIQUE,
    doc_name        TEXT NOT NULL,
    default_anchor_level TEXT NOT NULL DEFAULT 'batch'
                        CHECK (default_anchor_level IN ('project','batch','route','leg','node')),
    required        INTEGER NOT NULL DEFAULT 0,
    phase           TEXT,
    work_item       TEXT,
    due_node_key    TEXT,
    due_type        TEXT CHECK (due_type IN ('before_node','at_node','after_node')),
    created_at      TEXT
);

-- 3.10 文件记录（多式联运统一锚点；独立承载 file_path/hash/version，避免与前端单证清单冲突）
CREATE TABLE IF NOT EXISTS file_records (
    file_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_def_id      INTEGER NOT NULL REFERENCES doc_definitions(doc_def_id),
    anchor_level    TEXT NOT NULL CHECK (anchor_level IN ('project','batch','route','leg','node')),
    project_id      TEXT NOT NULL REFERENCES projects(project_id),
    batch_id        TEXT REFERENCES batches(batch_id),
    route_id        INTEGER REFERENCES routes(route_id),
    leg_id          INTEGER REFERENCES route_legs(leg_id),
    node_id         INTEGER,             -- 节点实例主键 = (batch_id, node_id)，跨批次不唯一，故不建 FK
    file_name       TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    file_hash       TEXT,
    version         INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','submitted','approved','rejected','superseded','deleted')),
    submitted_at    TEXT,
    submitted_by    TEXT,
    created_at      TEXT,
    updated_at      TEXT,
    CHECK (
        (anchor_level = 'project' AND project_id IS NOT NULL)
        OR (anchor_level = 'batch' AND project_id IS NOT NULL AND batch_id IS NOT NULL)
        OR (anchor_level = 'route' AND project_id IS NOT NULL AND batch_id IS NOT NULL AND route_id IS NOT NULL)
        OR (anchor_level = 'leg' AND project_id IS NOT NULL AND batch_id IS NOT NULL AND route_id IS NOT NULL AND leg_id IS NOT NULL)
        OR (anchor_level = 'node' AND project_id IS NOT NULL AND batch_id IS NOT NULL AND node_id IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_file_records_anchor ON file_records(anchor_level, project_id);
CREATE INDEX IF NOT EXISTS idx_file_records_batch ON file_records(batch_id);

-- 3.11 提交日志
CREATE TABLE IF NOT EXISTS submission_logs (
    log_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id         INTEGER NOT NULL REFERENCES file_records(file_id),
    doc_def_id      INTEGER NOT NULL,
    project_id      TEXT NOT NULL,
    batch_id        TEXT,
    route_id        INTEGER,
    leg_id          INTEGER,
    node_id         INTEGER,
    anchor_level    TEXT NOT NULL,
    action          TEXT NOT NULL
                    CHECK (action IN ('submit','update','approve','reject','supersede')),
    operator        TEXT,
    remark          TEXT,
    created_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_submission_logs_file ON submission_logs(file_id);

-- 8.1 费用（可按段）
CREATE TABLE IF NOT EXISTS charges (
    charge_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL,
    batch_id        TEXT,
    route_id        INTEGER,
    leg_id          INTEGER,
    node_id         INTEGER,
    direction       TEXT CHECK (direction IN ('receivable','payable')),
    charge_type     TEXT,
    amount          REAL,
    currency        TEXT,
    status          TEXT,
    created_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_charges_project ON charges(project_id);

-- 8.2 分包合同（可按段）
CREATE TABLE IF NOT EXISTS subcontracts (
    subcontract_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL,
    batch_id        TEXT,
    leg_id          INTEGER,
    supplier_id     TEXT,
    contract_no     TEXT,
    amount          REAL,
    currency        TEXT,
    status          TEXT,
    created_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_subcontracts_project ON subcontracts(project_id);

-- 8.3 保险（可按段）
CREATE TABLE IF NOT EXISTS insurances (
    insurance_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL,
    batch_id        TEXT,
    route_id        INTEGER,
    leg_id          INTEGER,
    policy_no       TEXT,
    insurer         TEXT,
    amount          REAL,
    currency        TEXT,
    status          TEXT,
    created_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_insurances_project ON insurances(project_id);

-- 5.2 兼容视图：旧接口继续按 batch_id 查节点 / 文件
CREATE VIEW IF NOT EXISTS v_nodes_legacy AS
SELECT node_id, batch_id, node_key, node_name, planned_date, actual_date, status FROM nodes;
CREATE VIEW IF NOT EXISTS v_files_legacy AS
SELECT file_id, doc_def_id, project_id, batch_id, file_name, file_path, file_hash,
       version, status, submitted_at, submitted_by
FROM file_records WHERE anchor_level IN ('project','batch');
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
    _ensure_anchor_indexes(conn)
    # 项目级单证一次性迁移（老库里的 9 行重复项 → project_files 3 行）；幂等，见函数内守卫
    _migrate_project_files(conn)
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
        ("route_id", "INTEGER"),     # 多式联运统一模型锚点：所属线路 instance
        ("leg_id", "INTEGER"),       # 所属运输段（纯海运 → 默认 sea leg）
        ("node_def_id", "INTEGER"),  # 关联 node_defs 模板
        ("planned_date", "TEXT"),    # 统一模型的计划节点日期（与 plan_start 同源）
        ("actual_date", "TEXT"),     # 统一模型的实际节点日期
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


def _ensure_anchor_indexes(conn):
    """§6 多式联运统一模型的锚点唯一索引（锚点列加好后建，幂等）。

    段级节点唯一：同一运输段内 node_key 稳定标识不可重复；
    线路级节点唯一：同一线路内 node_key 不可重复。
    未锚点（route_id/leg_id 为 NULL）的旧节点行不受部分唯一索引约束。
    """
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_route_key "
                 "ON nodes(route_id, node_key) WHERE route_id IS NOT NULL")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_leg_key "
                 "ON nodes(leg_id, node_key) WHERE leg_id IS NOT NULL")


def _rebuild_clean(conn):
    """旧库（无 batches 表）→ 彻底清空业务表重建，避免残余旧主键/列冲突。"""
    owned = ["batch_schedule_changes", "batch_route_changes", "batch_parties",
             "party_roles", "parties", "doc_dependencies", "reminder_rules",
             "doc_types", "insurance_policies",
             "container_batch_link", "containers", "vessel_positions", "vessel",
             "cargo_items", "files", "project_files", "nodes", "shift_history", "op_log", "batches",
             "batch_routes", "projects", "settings", "migration_status",
             # 多式联运统一模型
             "submission_logs", "file_records", "doc_definitions", "node_defs",
             "route_legs", "routes", "route_template_legs", "route_templates",
             "locations", "charges", "subcontracts", "insurances"]
    for t in owned:
        try:
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        except sqlite3.OperationalError:
            pass
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.executescript(SCHEMA_SQL)


# 项目级单证迁移的一次性守卫键（settings）：迁移过就不再扫库
_PROJECT_FILES_MIGRATION_KEY = "project_files_migrated_v1"


def _migrate_project_files(conn):
    """一次性迁移：把 files 里「项目级单证」搬进 project_files（每项目一份）。

    为什么需要迁移：这些单证在 files 里无节点锚点（node_key/due_node_key/due_type/due_rule
    全为 NULL，与 services/file_checklist 的 files_project 展开口径一致），却被旧口径按批次
    各复制一份 —— 3 个批次就是 9 行，用户要提交 3 次、报告里出现 9 行。新表按项目只留一份。

    取哪一条：按 (project_id, doc_name) 分组保留最早一条（file_id 最小）的字段；各批次行
    字段同源，取最早即最接近模板。status 取组内「最强态」——任一批次已提交则该单证在整个
    项目上视为已提交；submitted_date 取组内最早的已提交日期。

    幂等与安全：settings 键 project_files_migrated_v1 守卫（已迁直接返回）；迁移异常一律
    吞掉并打印一行中文提示 —— 迁移失败绝不能让 init_db() 崩（否则用户连库都打不开）；
    project_files 上的唯一索引 (project_id, doc_name) 保证重跑只 IGNORE 不重复。
    """
    try:
        if conn.execute("SELECT 1 FROM settings WHERE key=?",
                        (_PROJECT_FILES_MIGRATION_KEY,)).fetchone():
            return
        rows = conn.execute(
            "SELECT * FROM files WHERE node_key IS NULL AND due_node_key IS NULL "
            "AND due_type IS NULL AND due_rule IS NULL "
            "ORDER BY project_id, doc_name, file_id").fetchall()
        if rows:
            groups = {}
            for r in rows:
                groups.setdefault((r["project_id"], r["doc_name"]), []).append(r)
            for (project_id, doc_name), grp in groups.items():
                head = grp[0]                       # 最早一条（file_id 最小）
                done = [g["submitted_date"] for g in grp if g["status"] == "submitted"]
                dates = sorted([d for d in done if d])
                conn.execute(
                    "INSERT OR IGNORE INTO project_files (project_id, doc_name, doc_type, "
                    "owner_dept, copies, remind_before_days, status, submitted_date, owner, "
                    "note, is_default, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (project_id, doc_name, head["doc_type"], head["owner_dept"], head["copies"],
                     head["remind_before_days"],
                     "submitted" if done else "pending", dates[0] if dates else None,
                     head["owner"], head["note"], 1 if head["is_default"] else 0, _now()))
            # 搬完即从 files 删除这些行：否则批次单证列表/统计仍会把它们算进去（重复计数）
            ids = [r["file_id"] for r in rows]
            conn.execute(f"DELETE FROM files WHERE file_id IN ({','.join('?' * len(ids))})", ids)
            conn.commit()
            # 只读校验：迁移不得破坏既有不变量（files 全部挂批次）
            left = conn.execute("SELECT COUNT(*) AS c FROM files WHERE batch_id IS NULL").fetchone()["c"]
            print(f"[迁移] 项目级单证：搬运 {len(groups)} 行到 project_files（涉及 files 原行 "
                  f"{len(ids)} 行，已删除）" + (f"；警告：files.batch_id IS NULL 仍有 {left} 行" if left else ""))
            # 只在**确实有东西可搬**时才落守卫：全新库/已收敛库保持"可重扫"，
            # 否则先 init_db() 再播种的旧写入路径产出的行永远不会被搬（守卫会把它挡掉）。
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)",
                         (_PROJECT_FILES_MIGRATION_KEY, _now()))
            conn.commit()
        else:
            return
    except Exception as e:            # noqa: BLE001 —— 启动路径绝不因迁移失败而中断
        print(f"[迁移] 项目级单证迁移未完成（已跳过，不影响启动，下次启动会重试）：{e}")


def migrate_project_files_now():
    """手动重跑「项目级单证单库一份」迁移：清守卫后立即执行。

    用途：升级后想立刻把存量库里"按批次复制的项目级单证"收敛到 project_files
    （正常路径下 `init_db()` 只要发现候选行就会自动搬，本函数用于强制再扫一次）。
    返回 {"added": 新增行数, "total": 现有行数}。
    """
    conn = get_conn()
    conn.execute("DELETE FROM settings WHERE key=?", (_PROJECT_FILES_MIGRATION_KEY,))
    conn.commit()
    before = conn.execute("SELECT COUNT(*) AS c FROM project_files").fetchone()["c"]
    _migrate_project_files(conn)
    after = conn.execute("SELECT COUNT(*) AS c FROM project_files").fetchone()["c"]
    return {"added": after - before, "total": after}


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


def list_projects():
    """列出全部项目（不区分状态），供线路管理等跨项目入口选择。"""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM projects ORDER BY create_date, project_id").fetchall()
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
    out = [dict(r) for r in rows]
    # scope 只是新增字段（键与排序不动）：UI 把批次单证与 project_files（scope='project'）
    # 拼在同一列表渲染时靠它区分归属，也便于回写时选对表。
    for r in out:
        r["scope"] = "batch"
    return out


def get_files_by_batch(batch_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM files WHERE batch_id=? ORDER BY node_id, file_id", (batch_id,)).fetchall()
    out = [dict(r) for r in rows]
    for r in out:
        r["scope"] = "batch"
    return out


def update_file(file_id, **kw):
    row = get_conn().execute("SELECT batch_id FROM files WHERE file_id=?", (file_id,)).fetchone()
    if row:
        assert_batch_writable(row["batch_id"])
    conn = get_conn()
    if kw:
        sets = ", ".join(f"{k}=?" for k in kw)
        conn.execute(f"UPDATE files SET {sets} WHERE file_id=?", (*kw.values(), file_id))
        conn.commit()


# ── Project files（项目级单证，单库一份；与 files 行结构兼容） ──

def _project_file_row(row):
    """把 project_files 行整形成与 files 行同构的 dict（UI 两者拼同一列表渲染）。

    file_id 为什么带 "pf-" 前缀：files.file_id 是数字主键，两张表自增序列各自独立，
    裸数字会与批次单证撞车（勾选/提交回写会打错表）；无锚点字段一律补 None。
    """
    r = dict(row)
    return {
        "file_id": f"pf-{r['project_file_id']}",
        "project_file_id": r["project_file_id"],
        "scope": "project",
        "batch_id": None,                 # 项目级单证不挂批次（files.batch_id 的对应位）
        "project_id": r["project_id"],
        "node_id": None, "node_key": None,
        "due_node_id": None, "due_node_key": None,
        "due_type": None, "due_rule": None, "due_hours": None,
        "baseline_source": None, "due_date": None,
        "doc_name": r["doc_name"], "doc_type": r["doc_type"],
        "owner_dept": r["owner_dept"], "copies": r["copies"],
        "remind_before_days": r["remind_before_days"],
        "status": r["status"], "submitted_date": r["submitted_date"],
        "owner": r["owner"], "note": r["note"], "is_default": r["is_default"],
        "created_at": r["created_at"],
    }


def _project_file_id(file_id):
    """接受 "pf-123" 或裸 123（UI 混排两类行，回写时拿到的可能是字符串）。"""
    if isinstance(file_id, str) and file_id.startswith("pf-"):
        return int(file_id[3:])
    return int(file_id)


def insert_project_files(project_id, files: list):
    """写入项目级单证；同项目同 doc_name 已存在则跳过（INSERT OR IGNORE 语义）。

    为什么幂等：老库迁移、重复播种、批次复制都会再次送来同名条目，而项目级只该有一份；
    靠唯一索引 (project_id, doc_name) 兜底，调用方无需先查后写（避免竞态与噪音）。
    """
    conn = get_conn()
    for f in files:
        conn.execute(
            "INSERT OR IGNORE INTO project_files (project_id, doc_name, doc_type, owner_dept, "
            "copies, remind_before_days, status, submitted_date, owner, note, is_default, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, f["doc_name"], f["doc_type"], f.get("owner_dept"), f.get("copies"),
             f.get("remind_before_days", 3), f.get("status") or "pending",
             f.get("submitted_date"), f.get("owner"), f.get("note", ""),
             1 if f.get("is_default", 1) else 0, _now()))
    conn.commit()


def get_project_files(project_id):
    """项目级单证（项目内一份，不分批次）；行结构与 files 行兼容，见 _project_file_row。"""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM project_files WHERE project_id=? "
                        "ORDER BY project_file_id", (project_id,)).fetchall()
    return [_project_file_row(r) for r in rows]


def get_project_file(file_id):
    """单个项目级单证（支持 "pf-123" 或 123）；不存在返回 None。"""
    conn = get_conn()
    row = conn.execute("SELECT * FROM project_files WHERE project_file_id=?",
                       (_project_file_id(file_id),)).fetchone()
    return _project_file_row(row) if row else None


def update_project_file(file_id, **kw):
    """更新项目级单证（支持 "pf-123" 或 123）。

    status 与 submitted_date 的联动由调用方决定（与 update_file 同口径：后端不猜业务），
    submitted_date 随 status 一起传进来即可。
    """
    conn = get_conn()
    if kw:
        sets = ", ".join(f"{k}=?" for k in kw)
        conn.execute(f"UPDATE project_files SET {sets} WHERE project_file_id=?",
                     (*kw.values(), _project_file_id(file_id)))
        conn.commit()


def count_project_files(project_id):
    """项目级单证统计；口径与 services/file_checklist.count_files 一致（pending 只算 required）。"""
    conn = get_conn()
    rows = conn.execute("SELECT doc_type, status FROM project_files WHERE project_id=?",
                        (project_id,)).fetchall()
    return {
        "total": len(rows),
        "required": len([r for r in rows if r["doc_type"] == "required"]),
        "pending": len([r for r in rows if r["doc_type"] == "required"
                        and r["status"] == "pending"]),
        "submitted": len([r for r in rows if r["status"] == "submitted"]),
    }


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
        if v is None:
            # None 必须走 IS NULL：`batch_id = NULL` 在 SQL 里永远不成立，
            # 项目级单证/旧日志行（batch_id 为空）的收敛删除就靠这一支。
            sql += f" AND {k} IS NULL"
            continue
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


def last_file_actions_by_batch(project_id):
    """按 (批次, 单证名) 取最后一次提交/撤交动作：{(batch_id_or_None, doc_name): row}。

    为什么要它：报告按项目汇总时，同名单证在多个批次各有一条动作；last_file_actions 按
    doc_name 去重（历史调用方依赖其行为，不改），会把 A 批次的动作盖到 B 批次上 —— 报告因此
    串批次。本函数保留批次维度；项目级动作的 batch_id 为 None，键即 (None, doc_name)。
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM op_log WHERE project_id=? AND kind IN ('file_submit','file_withdraw') "
        f"ORDER BY {_TS_EXPR} DESC, id DESC", (project_id,)).fetchall()
    out = {}
    for r in rows:
        key = (r["batch_id"], r["subject"] or "")
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


# ═══════════════════════════════════════════════════════════════════════════
# 多式联运统一模型访问层（数据模型升级：Route → Leg → Node + 统一地点/模板）
# 新增能力全部独立命名，不干扰既有 V2 接口；纯海运批次的默认线路/段由迁移工具回填。
# ═══════════════════════════════════════════════════════════════════════════

_MM_NOW = _now


# ── 3.3 地点库 ──

def upsert_location(name, unlocode=None, loc_type="port", country=None,
                    timezone=None, return_existing=False):
    """按 unlocode/name 幂等写入地点；存在则不重复。返回 loc_id。"""
    conn = get_conn()
    row = None
    if unlocode:
        row = conn.execute("SELECT loc_id FROM locations WHERE unlocode=?", (unlocode,)).fetchone()
    if row is None:
        row = conn.execute("SELECT loc_id FROM locations WHERE name=?", (name,)).fetchone()
    if row:
        return row["loc_id"]
    cur = conn.execute(
        "INSERT INTO locations (unlocode, name, loc_type, country, timezone, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (unlocode, name, loc_type, country, timezone, _now()))
    conn.commit()
    return cur.lastrowid


def get_location(loc_id):
    row = get_conn().execute("SELECT * FROM locations WHERE loc_id=?", (loc_id,)).fetchone()
    return dict(row) if row else None


def list_locations(loc_type=None):
    if loc_type:
        rows = get_conn().execute(
            "SELECT * FROM locations WHERE loc_type=? ORDER BY name", (loc_type,)).fetchall()
    else:
        rows = get_conn().execute("SELECT * FROM locations ORDER BY loc_type, name").fetchall()
    return [dict(r) for r in rows]


# ── 3.4 线路模板 ──

def create_route_template(template_name, description=None, legs=None):
    """创建线路模板及其模板段。legs: [{seq, mode, origin_name, dest_name,
    default_carrier_role}] → 返回 template_id。"""
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO route_templates (template_name, description, status, created_at) "
        "VALUES (?,?,'active',?)", (template_name, description, _now()))
    tid = cur.lastrowid
    for leg in legs or []:
        conn.execute(
            "INSERT INTO route_template_legs (template_id, seq, mode, origin_name, dest_name, "
            "default_carrier_role) VALUES (?,?,?,?,?,?)",
            (tid, leg["seq"], leg["mode"], leg.get("origin_name"), leg.get("dest_name"),
             leg.get("default_carrier_role")))
    conn.commit()
    return tid


def get_route_template(template_id):
    conn = get_conn()
    t = conn.execute("SELECT * FROM route_templates WHERE template_id=?",
                     (template_id,)).fetchone()
    if not t:
        return None
    legs = conn.execute("SELECT * FROM route_template_legs WHERE template_id=? ORDER BY seq",
                        (template_id,)).fetchall()
    return {**dict(t), "legs": [dict(r) for r in legs]}


def list_route_templates(status="active"):
    rows = get_conn().execute(
        "SELECT * FROM route_templates WHERE status=? ORDER BY template_id", (status,)).fetchall()
    return [dict(r) for r in rows]


# ── 3.5 运输线路实例 ──

def create_route(batch_id, route_name, mode_chain=None, template_id=None,
                 status="candidate", is_active=0):
    """在批次下创建线路。mode_chain: 逗号分隔的段模式，如 'sea,road'，用于展开默认段。"""
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO routes (batch_id, template_id, route_name, status, is_active, created_at, "
        "updated_at) VALUES (?,?,?,?,?,?,?)",
        (batch_id, template_id, route_name, status, int(is_active), _now(), _now()))
    rid = cur.lastrowid
    if mode_chain:
        seq = 0
        for m in [x.strip() for x in mode_chain.split(",") if x.strip()]:
            seq += 1
            conn.execute(
                "INSERT INTO route_legs (route_id, seq, mode, status, created_at, updated_at) "
                "VALUES (?,?,?,'pending',?,?)", (rid, seq, m, _now(), _now()))
    conn.commit()
    return rid


def get_routes(batch_id, include_cancelled=False):
    sql = "SELECT * FROM routes WHERE batch_id=?"
    if not include_cancelled:
        sql += " AND status != 'cancelled'"
    rows = get_conn().execute(sql + " ORDER BY is_active DESC, route_id", (batch_id,)).fetchall()
    return [dict(r) for r in rows]


def get_active_route(batch_id):
    row = get_conn().execute(
        "SELECT * FROM routes WHERE batch_id=? AND is_active=1 AND status NOT IN "
        "('cancelled','completed') ORDER BY route_id LIMIT 1", (batch_id,)).fetchone()
    return dict(row) if row else None


def create_route_from_template(batch_id, template_id, route_name=None):
    """按线路模板在批次下新建一条候选线路，并复制模板各段。返回 route_id。"""
    tpl = get_route_template(template_id)
    if not tpl:
        raise ValueError("线路模板不存在")
    name = route_name or tpl["template_name"] or f"{batch_id} 线路"
    rid = create_route(batch_id, name, template_id=template_id, status="candidate", is_active=0)
    for leg in tpl["legs"]:
        add_leg(rid, leg["mode"], seq=leg["seq"], origin_name=leg.get("origin_name"),
                dest_name=leg.get("dest_name"))
    return rid


def set_active_route(batch_id, route_id):
    """设为执行线路：同批次其它线路解除 active，本线路置 active/active 状态。

    部分唯一索引 idx_routes_active 保证同一批次同时最多一条 active，
    故必须先整体清零再置一（每条 UPDATE 语句后全程至多一行 is_active=1）。
    """
    conn = get_conn()
    conn.execute("UPDATE routes SET is_active=0 WHERE batch_id=?", (batch_id,))
    conn.execute(
        "UPDATE routes SET is_active=1, status='active', updated_at=? WHERE route_id=?",
        (_now(), route_id))
    conn.commit()


def update_route_status(route_id, status, is_active=None):
    conn = get_conn()
    kw = {"status": status, "updated_at": _now()}
    if is_active is not None:
        kw["is_active"] = int(is_active)
    sets = ", ".join(f"{k}=?" for k in kw)
    conn.execute(f"UPDATE routes SET {sets} WHERE route_id=?", (*kw.values(), route_id))
    conn.commit()


# ── 3.6 运输段 ──

def add_leg(route_id, mode, seq=None, origin_name=None, dest_name=None, planned_etd=None,
            planned_eta=None, carrier_id=None, voyage_flight_train=None):
    conn = get_conn()
    if seq is None:
        r = conn.execute("SELECT COALESCE(MAX(seq),0)+1 AS s FROM route_legs WHERE route_id=?",
                         (route_id,)).fetchone()
        seq = r["s"]
    cur = conn.execute(
        "INSERT INTO route_legs (route_id, seq, mode, origin_name, dest_name, planned_etd, "
        "planned_eta, carrier_id, voyage_flight_train, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?)",
        (route_id, seq, mode, origin_name, dest_name, planned_etd, planned_eta, carrier_id,
         voyage_flight_train, _now(), _now()))
    conn.commit()
    return cur.lastrowid


def get_leg(leg_id):
    row = get_conn().execute("SELECT * FROM route_legs WHERE leg_id=?", (leg_id,)).fetchone()
    return dict(row) if row else None


def get_legs(route_id):
    rows = get_conn().execute(
        "SELECT * FROM route_legs WHERE route_id=? ORDER BY seq", (route_id,)).fetchall()
    return [dict(r) for r in rows]


def update_leg(leg_id, **kw):
    conn = get_conn()
    if kw:
        kw["updated_at"] = _now()
        sets = ", ".join(f"{k}=?" for k in kw)
        conn.execute(f"UPDATE route_legs SET {sets} WHERE leg_id=?", (*kw.values(), leg_id))
        conn.commit()


# ── 3.7 节点定义模板 ──

def upsert_node_def(mode, node_key, node_name, phase=None, work_item=None,
                    default_anchor_level="node", required_docs=None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO node_defs (mode, node_key, node_name, phase, work_item, "
        "default_anchor_level, required_docs) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(mode, node_key) DO UPDATE SET node_name=excluded.node_name, "
        "phase=excluded.phase, work_item=excluded.work_item, "
        "default_anchor_level=excluded.default_anchor_level, required_docs=excluded.required_docs",
        (mode, node_key, node_name, phase, work_item, default_anchor_level, required_docs))
    conn.commit()
    row = conn.execute("SELECT node_def_id FROM node_defs WHERE mode=? AND node_key=?",
                       (mode, node_key)).fetchone()
    return row["node_def_id"]


def list_node_defs(mode=None):
    if mode:
        rows = get_conn().execute(
            "SELECT * FROM node_defs WHERE mode=? ORDER BY node_def_id", (mode,)).fetchall()
    else:
        rows = get_conn().execute("SELECT * FROM node_defs ORDER BY mode, node_def_id").fetchall()
    return [dict(r) for r in rows]


def get_node_def(node_def_id):
    row = get_conn().execute("SELECT * FROM node_defs WHERE node_def_id=?",
                             (node_def_id,)).fetchone()
    return dict(row) if row else None


# ── 3.9 单证定义 ──

def upsert_doc_definition(doc_code, doc_name, default_anchor_level="batch", required=0,
                          phase=None, work_item=None, due_node_key=None, due_type=None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO doc_definitions (doc_code, doc_name, default_anchor_level, required, phase, "
        "work_item, due_node_key, due_type, created_at) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(doc_code) DO UPDATE SET doc_name=excluded.doc_name, "
        "default_anchor_level=excluded.default_anchor_level, required=excluded.required, "
        "phase=excluded.phase, work_item=excluded.work_item, "
        "due_node_key=excluded.due_node_key, due_type=excluded.due_type",
        (doc_code, doc_name, default_anchor_level, int(required), phase, work_item,
         due_node_key, due_type, _now()))
    conn.commit()
    row = conn.execute("SELECT doc_def_id FROM doc_definitions WHERE doc_code=?",
                       (doc_code,)).fetchone()
    return row["doc_def_id"]


def list_doc_definitions(anchor_level=None):
    if anchor_level:
        rows = get_conn().execute(
            "SELECT * FROM doc_definitions WHERE default_anchor_level=? ORDER BY phase, doc_code",
            (anchor_level,)).fetchall()
    else:
        rows = get_conn().execute(
            "SELECT * FROM doc_definitions ORDER BY phase, default_anchor_level, doc_code").fetchall()
    return [dict(r) for r in rows]


def get_doc_definition(doc_code):
    row = get_conn().execute("SELECT * FROM doc_definitions WHERE doc_code=?",
                             (doc_code,)).fetchone()
    return dict(row) if row else None


# ── 3.10 文件记录（统一锚点） ──

def insert_file_record(doc_def_id, anchor_level, project_id, file_name, file_path,
                       batch_id=None, route_id=None, leg_id=None, node_id=None,
                       file_hash=None, status="pending", submitted_by=None):
    """写入一条统一文件记录（multi-modal 锚点）。校验锚点完整性与唯一性。"""
    conn = get_conn()
    _assert_anchor(anchor_level, project_id, batch_id, route_id, leg_id, node_id)
    _assert_file_unique(conn, anchor_level, project_id, batch_id, route_id, leg_id, node_id,
                        doc_def_id)
    cur = conn.execute(
        "INSERT INTO file_records (doc_def_id, anchor_level, project_id, batch_id, route_id, "
        "leg_id, node_id, file_name, file_path, file_hash, version, status, submitted_at, "
        "submitted_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?)",
        (doc_def_id, anchor_level, project_id, batch_id, route_id, leg_id, node_id,
         file_name, file_path, file_hash, status,
         _now() if status in ("submitted", "approved") else None, submitted_by, _now(), _now()))
    fid = cur.lastrowid
    conn.commit()
    return fid


def _assert_anchor(anchor_level, project_id, batch_id, route_id, leg_id, node_id):
    required = {"project": dict(project_id=True),
                "batch": dict(project_id=True, batch_id=True),
                "route": dict(project_id=True, batch_id=True, route_id=True),
                "leg": dict(project_id=True, batch_id=True, route_id=True, leg_id=True),
                "node": dict(project_id=True, batch_id=True, node_id=True)}[anchor_level]
    for k, need in required.items():
        if need and not locals().get(k):
            raise ValueError(f"锚点级别 {anchor_level} 缺少 {k}")


def _assert_file_unique(conn, anchor_level, project_id, batch_id, route_id, leg_id, node_id,
                        doc_def_id):
    """同锚点同一文档只允许一份有效文件（pending/submitted/approved）。"""
    cond, vals = {
        "project": ("project_id=? AND anchor_level='project'", [project_id]),
        "batch": ("project_id=? AND batch_id=? AND anchor_level='batch'", [project_id, batch_id]),
        "route": ("project_id=? AND batch_id=? AND route_id=? AND anchor_level='route'",
                  [project_id, batch_id, route_id]),
        "leg": ("project_id=? AND batch_id=? AND route_id=? AND leg_id=? AND anchor_level='leg'",
                [project_id, batch_id, route_id, leg_id]),
        "node": ("project_id=? AND batch_id=? AND node_id=? AND anchor_level='node'",
                 [project_id, batch_id, node_id]),
    }[anchor_level]
    row = conn.execute(
        f"SELECT 1 FROM file_records WHERE doc_def_id=? AND {cond} "
        "AND status NOT IN ('rejected','superseded','deleted') LIMIT 1",
        (doc_def_id, *vals)).fetchone()
    if row:
        raise ValueError(f"该锚点下文档 {doc_def_id} 已存在有效文件，请先更新或作废")


def get_file_records(anchor_level=None, project_id=None, batch_id=None, route_id=None,
                     leg_id=None, node_id=None):
    sql = "SELECT * FROM file_records WHERE 1=1"
    args = []
    if anchor_level:
        sql += " AND anchor_level=?"
        args.append(anchor_level)
    if project_id:
        sql += " AND project_id=?"
        args.append(project_id)
    if batch_id:
        sql += " AND batch_id=?"
        args.append(batch_id)
    if route_id:
        sql += " AND route_id=?"
        args.append(route_id)
    if leg_id:
        sql += " AND leg_id=?"
        args.append(leg_id)
    if node_id:
        sql += " AND node_id=?"
        args.append(node_id)
    rows = get_conn().execute(sql + " ORDER BY anchor_level, created_at", args).fetchall()
    return [dict(r) for r in rows]


def get_file_record(file_id):
    row = get_conn().execute("SELECT * FROM file_records WHERE file_id=?",
                             (file_id,)).fetchone()
    return dict(row) if row else None


def update_file_record(file_id, **kw):
    conn = get_conn()
    if kw.get("status") in ("submitted", "approved"):
        kw.setdefault("submitted_at", _now())
    if kw:
        kw["updated_at"] = _now()
        sets = ", ".join(f"{k}=?" for k in kw)
        conn.execute(f"UPDATE file_records SET {sets} WHERE file_id=?", (*kw.values(), file_id))
        conn.commit()


# ── 3.11 提交日志 ──

def log_submission(file_id, doc_def_id, project_id, anchor_level, action, operator=None,
                   remark=None, batch_id=None, route_id=None, leg_id=None, node_id=None):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO submission_logs (file_id, doc_def_id, project_id, batch_id, route_id, "
        "leg_id, node_id, anchor_level, action, operator, remark, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (file_id, doc_def_id, project_id, batch_id, route_id, leg_id, node_id, anchor_level,
         action, operator, remark, _now()))
    conn.commit()
    return cur.lastrowid


def get_submission_logs(file_id=None, project_id=None, limit=200):
    sql = "SELECT * FROM submission_logs WHERE 1=1"
    args = []
    if file_id:
        sql += " AND file_id=?"
        args.append(file_id)
    if project_id:
        sql += " AND project_id=?"
        args.append(project_id)
    rows = get_conn().execute(sql + " ORDER BY log_id DESC LIMIT ?", (*args, limit)).fetchall()
    return [dict(r) for r in rows]


# ── 8.1 费用 / 8.2 分包 / 8.3 保险（可按段） ──

def insert_charge(project_id, direction, charge_type, amount, currency="USD",
                  batch_id=None, route_id=None, leg_id=None, node_id=None, status="open"):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO charges (project_id, batch_id, route_id, leg_id, node_id, direction, "
        "charge_type, amount, currency, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (project_id, batch_id, route_id, leg_id, node_id, direction, charge_type, amount,
         currency, status, _now()))
    conn.commit()
    return cur.lastrowid


def list_charges(project_id, leg_id=None):
    if leg_id:
        rows = get_conn().execute(
            "SELECT * FROM charges WHERE project_id=? AND leg_id=? ORDER BY charge_id",
            (project_id, leg_id)).fetchall()
    else:
        rows = get_conn().execute(
            "SELECT * FROM charges WHERE project_id=? ORDER BY charge_id", (project_id,)).fetchall()
    return [dict(r) for r in rows]


def insert_subcontract(project_id, supplier_id, contract_no, amount, currency="USD",
                       batch_id=None, leg_id=None, status="open"):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO subcontracts (project_id, batch_id, leg_id, supplier_id, contract_no, "
        "amount, currency, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (project_id, batch_id, leg_id, supplier_id, contract_no, amount, currency, status, _now()))
    conn.commit()
    return cur.lastrowid


def list_subcontracts(project_id, leg_id=None):
    if leg_id:
        rows = get_conn().execute(
            "SELECT * FROM subcontracts WHERE project_id=? AND leg_id=? ORDER BY subcontract_id",
            (project_id, leg_id)).fetchall()
    else:
        rows = get_conn().execute(
            "SELECT * FROM subcontracts WHERE project_id=? ORDER BY subcontract_id",
            (project_id,)).fetchall()
    return [dict(r) for r in rows]


def insert_insurance(project_id, policy_no, insurer, amount=None, currency="USD",
                     batch_id=None, route_id=None, leg_id=None, status="active"):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO insurances (project_id, batch_id, route_id, leg_id, policy_no, insurer, "
        "amount, currency, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (project_id, batch_id, route_id, leg_id, policy_no, insurer, amount, currency, status,
         _now()))
    conn.commit()
    return cur.lastrowid


def list_insurances(project_id, leg_id=None):
    if leg_id:
        rows = get_conn().execute(
            "SELECT * FROM insurances WHERE project_id=? AND leg_id=? ORDER BY insurance_id",
            (project_id, leg_id)).fetchall()
    else:
        rows = get_conn().execute(
            "SELECT * FROM insurances WHERE project_id=? ORDER BY insurance_id",
            (project_id,)).fetchall()
    return [dict(r) for r in rows]


# ── §6 关键查询（多式联运组合查询） ──

def get_batch_route_detail(batch_id):
    """§6.1 查询批次完整线路：线路 + 排序段。无线路时返回 None。"""
    conn = get_conn()
    route = get_active_route(batch_id)
    if not route:
        return None
    legs = get_legs(route["route_id"])
    return {**route, "legs": legs}


def get_batch_nodes_by_leg(batch_id):
    """§6.2 查询批次全部节点（按段分组）：[{leg_seq, mode, nodes:[...]}]。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT l.seq AS leg_seq, l.mode, l.leg_id, n.node_id, n.node_key, n.node_name, "
        "n.plan_start, n.plan_end, n.status AS node_status "
        "FROM routes r "
        "JOIN route_legs l ON l.route_id = r.route_id "
        "JOIN nodes n ON n.batch_id = r.batch_id "
        "WHERE r.batch_id=? AND r.is_active=1 AND n.leg_id = l.leg_id "
        "ORDER BY l.seq, n.seq", (batch_id,)).fetchall()
    groups = {}
    for r in rows:
        g = groups.setdefault((r["leg_seq"], r["mode"], r["leg_id"]),
                              {"leg_seq": r["leg_seq"], "mode": r["mode"],
                               "leg_id": r["leg_id"], "nodes": []})
        g["nodes"].append({"node_id": r["node_id"], "node_key": r["node_key"],
                           "node_name": r["node_name"], "plan_start": r["plan_start"],
                           "plan_end": r["plan_end"], "status": r["node_status"]})
    return list(groups.values())


def get_leg_required_files(leg_id):
    """§6.3 查询某段需要提交的段级文件（缺省锚点=leg 的单证 + 已提交记录）。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT d.doc_code, d.doc_name, d.required, "
        "f.status AS file_status, f.file_id, f.version "
        "FROM doc_definitions d "
        "LEFT JOIN file_records f ON f.doc_def_id = d.doc_def_id "
        "AND f.leg_id = ? AND f.anchor_level = 'leg' "
        "AND f.status NOT IN ('rejected','superseded','deleted') "
        "WHERE d.default_anchor_level = 'leg' "
        "ORDER BY d.doc_code", (leg_id,)).fetchall()
    return [dict(r) for r in rows]


def count_project_level_missing(project_id):
    """缺单证统计：项目级必填仅算一次（同项目一份）。"""
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM doc_definitions d "
        "LEFT JOIN file_records f ON f.doc_def_id = d.doc_def_id "
        "AND f.project_id = ? AND f.anchor_level = 'project' "
        "WHERE d.default_anchor_level = 'project' AND d.required = 1 "
        "AND (f.file_id IS NULL OR f.status = 'pending')", (project_id,)).fetchone()
    return row["c"]


def count_anchor_missing(project_id, anchor_level, anchor_id):
    """通用缺单证统计：batch/route/leg/node 各自按锚点 ID 统计必填未交数。"""
    col = {"batch": "batch_id", "route": "route_id", "leg": "leg_id", "node": "node_id"}.get(
        anchor_level)
    if col is None:
        raise ValueError(f"非法锚点级别: {anchor_level!r}")
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM doc_definitions d "
        "LEFT JOIN file_records f ON f.doc_def_id = d.doc_def_id "
        f"AND f.{col} = ? AND f.anchor_level = ? "
        f"WHERE d.default_anchor_level = ? AND d.required = 1 "
        "AND (f.file_id IS NULL OR f.status = 'pending')",
        (anchor_id, anchor_level, anchor_level)).fetchone()
    return row["c"]


def aggregate_seg_status(batch_id):
    """节点→段→线路状态聚合（§7）：段完成=该段全部节点 Done；线路完成=全部段完成。"""
    route = get_active_route(batch_id)
    if not route:
        return {"route_status": "none", "legs": []}
    legs = get_legs(route["route_id"])
    out = []
    for leg in legs:
        nodes = get_conn().execute(
            "SELECT status FROM nodes WHERE batch_id=? AND leg_id=?", (batch_id, leg["leg_id"])
        ).fetchall()
        done = all(n["status"] == "Done" for n in nodes)
        out.append({**leg, "all_nodes_done": done})
    all_done = bool(out) and all(o["all_nodes_done"] for o in out)
    return {"route_status": "completed" if all_done else "pending", "legs": out}