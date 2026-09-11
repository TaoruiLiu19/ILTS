"""
多批次演示数据播种：以项目现有第一个批次为样板，复制出「整体推迟 N 天」的批次。

用途：演示甘特工作台的**多批次合并视图**与**资源冲突提示**。
特点：
  · 幂等：批次号已存在则跳过（可重复执行）；
  · 「基本一样」：走 services.batches.copy_batch（线路 + 15 节点 + 单证 + 客户角色全带），
    只把 ETD/ETA 整体后移，再按 §5.3 规则重算全部计划日期；
  · 每个批次只清「订舱号/MBL/HBL」并重编（提单号是每票货自己的，不该与样板相同）；
  · 为了冲突提示有可演示内容，给项目内**全部启用批次**补齐同一报关行与同一船舶航次：
      - 同出口港 + 境内窗口重叠 → PORT_WINDOW
      - 同报关行 + 出口报关窗口重叠 → CUSTOMS_BROKER
      - 同船名航次 + 海运窗口重叠 → VESSEL_VOYAGE

运行：python -X utf8 _seed_multi_batch_demo.py [--project demo-qd-br-001]
"""
import os, sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import db
from services import batches as bsvc
from services import schedule2

SHIFTS = (("B02", 5), ("B03", 10))          # 新批次编号后缀 → 相对样板推迟的天数
BROKER = "中外运报关行"                      # 同一项目同一报关行（冲突演示）
VOYAGE = "V0123"                            # 同一船舶航次（冲突演示）


def _d(s):
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))


def main():
    args = sys.argv[1:]
    pid = args[args.index("--project") + 1] if "--project" in args else "demo-qd-br-001"
    proj = db.get_project(pid)
    if not proj:
        print(f"[FAIL] 项目不存在：{pid}")
        return 1
    src = db.get_batches(pid)
    if not src:
        print(f"[FAIL] 项目 {pid} 没有启用中批次")
        return 1
    src = src[0]
    src_route = db.get_route(src["batch_id"]) or {}
    etd0, eta0 = src_route.get("etd"), src_route.get("eta")
    if not etd0 or not eta0:
        print(f"[FAIL] 样板批次 {src['batch_no']} 缺 ETD/ETA，无法排期")
        return 1

    print(f"库：{db.DB_PATH}")
    print(f"样板批次：{src['batch_no']}  ETD {etd0} → ETA {eta0}"
          f"  （{len(db.get_nodes_by_batch(src['batch_id']))} 节点 / "
          f"{len(db.get_files_by_batch(src['batch_id']))} 单证）\n")

    created = []
    for suffix, days in SHIFTS:
        batch_no = f"{(db.get_project(pid) or {}).get('project_no') or 'P'}-{suffix}"
        exist = next((b for b in db.get_batches(pid, include_cancelled=True)
                      if b["batch_no"] == batch_no), None)
        if exist:
            print(f"[SKIP] {batch_no} 已存在（幂等）")
            created.append(exist["batch_id"])
            continue
        nb = bsvc.copy_batch(pid, src["batch_id"], new_batch_no=batch_no,
                             batch_name=f"{suffix}·推迟{days}天")
        bid = nb["batch_id"]
        # 提单号是每票货自己的，复制后重编
        db.update_batch(bid, booking_no=f"BOOK-DEMO-{suffix[1:]}",
                        mbl_no=f"MBL-COS-{suffix[1:]}", hbl_nos="[]")
        # 线路：整体推迟 days 天 + 同报关行
        etd = (_d(etd0) + timedelta(days=days)).isoformat()
        eta = (_d(eta0) + timedelta(days=days)).isoformat()
        db.upsert_route(bid, etd=etd, eta=eta, customs_broker=BROKER)
        schedule2.recompute_batch_schedule(pid, batch_id=bid,
                                          reason=f"多批次演示：整体推迟 {days} 天")
        bsvc.sync_batch_status(bid)
        created.append(bid)
        print(f"[OK  ] {batch_no} 已建立：ETD {etd} → ETA {eta}（推迟 {days} 天）")

    # 全部启用批次统一报关行 + 船舶航次（冲突演示所需）
    for b in db.get_batches(pid):
        route = db.get_route(b["batch_id"]) or {}
        if route.get("customs_broker") != BROKER:
            db.upsert_route(b["batch_id"], customs_broker=BROKER)
        v = db.get_vessel(pid, b["batch_id"]) or {}
        if v.get("voyage") != VOYAGE or not v.get("vessel_name"):
            db.upsert_vessel(pid, vessel_name=v.get("vessel_name") or "COSCO INTEGRITY",
                             voyage=VOYAGE, batch_id=b["batch_id"])
    bsvc.update_project_status(pid)

    print("\n== 项目批次总览 ==")
    for b in db.get_batches(pid, include_cancelled=True):
        ns = db.get_nodes_by_batch(b["batch_id"])
        r = db.get_route(b["batch_id"]) or {}
        v = db.get_vessel(pid, b["batch_id"]) or {}
        print(f"  {b['batch_no']:<18} {bsvc.state_label(b['status']):<6} "
              f"{r.get('etd')} → {r.get('eta')}  {len(ns)} 节点  "
              f"港口 {r.get('export_port')}  报关行 {r.get('customs_broker')}  "
              f"船 {v.get('vessel_name')} {v.get('voyage')}")

    print("\n== 冲突扫描（合并视图会标出来的东西）==")
    from services.resource_conflict import scan_batch_conflicts
    confs = scan_batch_conflicts(pid)
    if not confs:
        print("  （无）")
    for c in confs:
        print(f"  [{c['level']:<6}] {c['kind']:<15} {c['message']}")

    print(f"\n完成：共 {len(db.get_batches(pid))} 个启用中批次。"
          f"打开「甘特工作台 → 全批次」即可对比（行=批次 · 三段色块 · 提交状态 · 冲突标注）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
