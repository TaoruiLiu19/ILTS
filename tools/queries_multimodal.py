#!/usr/bin/env python3
"""《数据模型升级.md》 §6 关键查询 与 唯一性约束 —— 可执行 SQL 演示。

在隔离临时库建新 schema → 播演示数据 → 依次执行 §6.1/6.2/6.3 查询，
并重放 §3.8/§4.2 的锚点唯一性约束（部分唯一索引），打印真实结果。

用法: python tools/queries_multimodal.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import db

_TMP = tempfile.mkdtemp(prefix="mmq_")
db.DB_PATH = os.path.join(_TMP, "t.db")
db._conn = None
db.init_db()

import mock_data
import migrate_multimodal as mm


def main():
    conn = db.get_conn()
    # —— 建项目批次 → 跑统一迁移（播种主数据 + 建默认海运线路 + 回填节点锚点）——
    bld = mock_data.build_project(mock_data.DEMO_PROJECT)
    bid = bld["batch_id"]
    mm.do_migrate(conn, dry=False)
    rid = db.get_active_route(bid)["route_id"]
    legs = db.get_legs(rid)
    leg_id = legs[0]["leg_id"]

    print(f"演示批次 {bid} / 线路 {rid} / {len(legs)} 段 / 节点 "
          f"{len(db.get_nodes_by_batch(bid))}\n")

    # —— §6.1 某批次完整线路 ——
    print("== §6.1 批次完整线路 ==")
    for row in conn.execute(
            "SELECT r.route_id, r.route_name, r.status AS route_status, "
            "       l.seq, l.mode, l.origin_name, l.dest_name, "
            "       l.planned_etd, l.planned_eta, l.status AS leg_status "
            "FROM routes r JOIN route_legs l ON l.route_id = r.route_id "
            "WHERE r.batch_id=? AND r.is_active=1 ORDER BY l.seq", (bid,)):
        print("  ", " | ".join(str(row[k]) for k in row.keys()))

    # —— §6.2 全部节点按段分组 ——
    print("\n== §6.2 节点按段分组（前 5 行） ==")
    rows = conn.execute(
        "SELECT l.seq AS leg_seq, l.mode, n.node_key, n.node_name, "
        "       n.planned_date, n.actual_date, n.status "
        "FROM nodes n JOIN route_legs l ON l.leg_id = n.leg_id "
        "WHERE n.batch_id=? ORDER BY l.seq, n.seq LIMIT 5", (bid,)).fetchall()
    for row in rows:
        print("  ", " | ".join(str(row[k]) for k in row.keys()))

    # —— §6.3 某段需提交的段级文件 ——
    print(f"\n== §6.3 段 {leg_id} 段级文件清单 ==")
    rows = conn.execute(
        "SELECT dd.doc_code, dd.doc_name, dd.required, f.status, f.file_name "
        "FROM doc_definitions dd "
        "LEFT JOIN file_records f ON f.doc_def_id = dd.doc_def_id "
        "   AND f.leg_id=? AND f.anchor_level='leg' "
        "WHERE dd.default_anchor_level='leg' ORDER BY dd.doc_code", (leg_id,)).fetchall()
    for row in rows:
        print("  ", " | ".join(str(row[k]) for k in row.keys()))

    # —— §4.2 锚点唯一性（部分唯一索引）实际约束验证 ——
    print("\n== §4.2 文件锚点唯一性（同锚点同文档重复 → 拒绝） ==")
    proj_id = conn.execute("SELECT project_id FROM batches WHERE batch_id=?",
                           (bid,)).fetchone()["project_id"]
    dd = db.upsert_doc_definition("Q_LEG_DOC", "段级演示单证", "leg", required=1)
    f1 = db.insert_file_record(dd, "leg", proj_id, "a.pdf", "/x/a.pdf", batch_id=bid,
                               route_id=rid, leg_id=leg_id)
    print(f"  首份段文件写入 file_id={f1}")
    try:
        db.insert_file_record(dd, "leg", proj_id, "a2.pdf", "/x/a2.pdf", batch_id=bid,
                              route_id=rid, leg_id=leg_id)
        print("  未拦截（异常）")
    except ValueError as e:
        print(f"  重复写入被拒绝 → {e}")

    # —— §3.8 段级节点唯一（部分唯一索引） ——
    print("\n== §3.8 段级节点唯一 (leg_id, node_key) ==")
    try:
        conn.execute(
            "INSERT INTO nodes (batch_id, node_id, node_key, node_name, role_label, seq, "
            "area, calendar_mode, default_duration, duration, status, route_id, leg_id) "
            "VALUES (?,999,'LOADING','重复装船','x',99,'SEA','NATURAL',1,1,'Pending',?,?)",
            (bid, rid, leg_id))
        conn.commit()
        print("  未被唯一索引拦截（异常）")
    except Exception as e:
        print(f"  重复 node_key 被拒 → {str(e)[:70]}")


if __name__ == "__main__":
    main()
    print("\n§6 关键查询 + 唯一性约束执行完毕。")