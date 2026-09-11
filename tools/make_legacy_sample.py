#!/usr/bin/env python3
"""
生成「遗留（pre-batch」样本库，用于验证 tools/migrate_batches.py（T11）。
模拟升级前 schema：12 节点、无 batch 四件套、无 node_key/batch_id 列。
用法: python tools/make_legacy_sample.py [path]
"""
import os
import sys
import sqlite3
import shutil
from datetime import date, timedelta

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
LEGACY = os.path.join(BASE, "logistics.legacy.db")

OLD_12 = [
    (1, "出口报关", "DOME", 2), (2, "集港", "DOME", 1), (3, "捆扎", "DOME", 1),
    (4, "装船", "DOME", 1), (5, "海运", "SEA", 30), (6, "换单", "OVERSEA", 2),
    (7, "LI", "OVERSEA", 1), (8, "清关", "OVERSEA", 2), (9, "查验", "OVERSEA", 2),
    (10, "堆存", "OVERSEA", 3), (11, "内陆", "OVERSEA", 2), (12, "交付", "OVERSEA", 1),
]


def build(path):
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE projects (
        project_id TEXT PRIMARY KEY, project_name TEXT, country TEXT,
        export_port TEXT, status TEXT, etd TEXT, eta TEXT,
        vessel_name TEXT, actual_completion_date TEXT, buffer_days INTEGER DEFAULT 0
    );
    CREATE TABLE nodes (
        node_id INTEGER, project_id TEXT, node_name TEXT, status TEXT,
        seq INTEGER, area TEXT, duration INTEGER, plan_start TEXT,
        plan_end TEXT, actual_completion_date TEXT, remark TEXT,
        PRIMARY KEY (project_id, node_id)
    );
    CREATE TABLE files (
        id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, node_id INTEGER,
        doc_name TEXT, doc_type TEXT, status TEXT, due_date TEXT, submitted_date TEXT
    );
    CREATE TABLE cargo_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, container_no TEXT,
        seal_no TEXT
    );
    CREATE TABLE vessel (
        project_id TEXT, vessel_name TEXT
    );
    CREATE TABLE op_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, kind TEXT, subject TEXT,
        detail TEXT, created_at TEXT
    );
    CREATE TABLE shift_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, node_id INTEGER,
        delta INTEGER, reason TEXT, at TEXT
    );
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
    """)

    def plan12(etd_s, eta_s):
        etd = date(*map(int, etd_s.split("-")))
        eta = date(*map(int, eta_s.split("-")))
        plan = {}
        cursor = etd
        for nid, name, area, dur in OLD_12:
            if area == "DOME":
                end = cursor
                start = cursor - timedelta(days=dur)
                plan[nid] = (start.isoformat(), end.isoformat())
                cursor = start
            elif area == "SEA":
                plan[nid] = (etd.isoformat(), eta.isoformat())
        cursor = eta
        for nid, name, area, dur in OLD_12:
            if area == "OVERSEA":
                start = cursor
                end = cursor + timedelta(days=dur)
                plan[nid] = (start.isoformat(), end.isoformat())
                cursor = end
        return plan

    # 两个项目：一完成一运行
    projects = [
        ("lg-demo-001", "移民项目-运行中", "BR", "QD", "Active", "2026-08-01", "2026-09-01", "COSCO TEST", None),
        ("lg-demo-002", "移民项目-已完成", "BR", "QD", "Completed", "2026-04-01", "2026-05-01", "COSCO DONE", "2026-05-05"),
    ]
    for pid, name, ctry, port, st, etd, eta, vessel, actual in projects:
        cur.execute("INSERT INTO projects(project_id,project_name,country,export_port,status,etd,eta,vessel_name,actual_completion_date)"
                    " VALUES(?,?,?,?,?,?,?,?,?)", (pid, name, ctry, port, st, etd, eta, vessel, actual))
        plan = plan12(etd, eta)
        for nid, nd, area, dur in OLD_12:
            ps, pe = plan[nid]
            cur.execute("INSERT INTO nodes(node_id,project_id,node_name,status,seq,area,duration,plan_start,plan_end)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (nid, pid, nd, "Done" if (st == "Completed") else "Pending", nid, area, dur, ps, pe))
        # 单证
        for i, (nid, doc) in enumerate([(1, "出口报关单"), (3, "吊装方案"), (6, "到货通知"), (6, "空白提单")]):
            cur.execute("INSERT INTO files(project_id,node_id,doc_name,doc_type,status,due_date)"
                        " VALUES(?,?,?,'required','pending',?)", (pid, nid, doc, plan[nid][0]))
        # 集装箱
        for cno in ("COSU1234567", "COSU7654321"):
            cur.execute("INSERT INTO cargo_items(project_id,container_no,seal_no)"
                        " VALUES(?,?,'SEAL001')", (pid, cno))
        # op_log
        cur.execute("INSERT INTO op_log(project_id,kind,subject,detail,created_at)"
                    " VALUES(?,'create','创建项目','legacy','2026-08-01T09:00:00+08:00')", (pid,))
        # shift_history
        cur.execute("INSERT INTO shift_history(project_id,node_id,delta,reason,at)"
                    " VALUES(?,6,3,'人工调整','2026-08-10T10:00:00+08:00')", (pid,))
    # 无 scope 的系统级日志
    cur.execute("INSERT INTO op_log(project_id,kind,subject,detail,created_at)"
                " VALUES(NULL,'system','启动','legacy','2026-08-01T08:00:00+08:00')")
    conn.commit()
    conn.close()
    print(f"[OK] 遗留样本库已生成: {path}")
    return path


if __name__ == "__main__":
    if len(sys.argv) > 1:
        build(os.path.abspath(sys.argv[1]))
    else:
        build(LEGACY)