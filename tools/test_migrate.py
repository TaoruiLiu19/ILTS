#!/usr/bin/env python3
"""T11 迁移工具端到端测试：造遗留库 → check → apply → 幂等重跑 → rollback → 还原校验。"""
import os, sys, subprocess, sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DB = os.path.join(ROOT, "data", "logistics.legacy.db")
TOOL = os.path.join(ROOT, "tools", "migrate_batches.py")
MAKER = os.path.join(ROOT, "tools", "make_legacy_sample.py")
env = dict(os.environ, ILTS_DB_PATH=DB)

def run(args, inp=None):
    r = subprocess.run([sys.executable, *args], cwd=ROOT, env=env,
                       input=inp, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr

def conn():
    return sqlite3.connect(DB)


def shutil_copy(s, d):
    import shutil
    shutil.copy2(s, d)


def main():
    for f in os.listdir(os.path.join(ROOT, "data")):
        if f.startswith("logistics.legacy.db"):
            os.remove(os.path.join(ROOT, "data", f))

    # 1. 造遗留库
    rc, out = run([MAKER]); assert rc == 0, out
    print("[1] 遗留库生成 OK")

    # 2. check
    rc, out = run([TOOL, "--check"])
    assert rc == 0 and "预检查通过" in out, out
    print("[2] --check OK")

    # 3. apply
    rc, out = run([TOOL, "--apply"])
    assert rc == 0 and "迁移成功" in out, out
    print("[3] --apply OK")

    # 3a. 校验关键不变量
    c = conn(); cur = c.cursor()
    assert cur.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0] == "2"
    for t in ["nodes","files","cargo_items","shift_history"]:
        assert cur.execute(f"SELECT COUNT(*) FROM {t} WHERE batch_id IS NULL").fetchone()[0] == 0, t
    assert cur.execute("SELECT COUNT(*) FROM nodes WHERE node_key IS NULL").fetchone()[0] == 0
    nb = cur.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
    np_ = cur.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    assert nb == np_, (nb, np_)
    # node_key 全覆盖正确映射
    assert cur.execute("SELECT COUNT(*) FROM nodes WHERE node_key='SEA_TRANSIT'").fetchone()[0] == 2
    # 完成项目 closed
    assert {'closed'} <= {r[0] for r in cur.execute("SELECT status FROM batches").fetchall()}
    c.close()
    print("[3a] 迁移不变量校验 OK (schema_version=2, 无空batch_id, node_key全映射, 批次=项目)")

    # 4. 幂等重跑 —— 不应新增批次
    rc, out = run([TOOL, "--apply"])
    assert rc == 0 and "迁移成功" in out, out
    c = conn(); cur = c.cursor()
    assert cur.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == nb, "幂等失败：批次数变化"
    assert cur.execute("SELECT COUNT(*) FROM container_batch_link").fetchone()[0] == 4, "幂等失败：柜关联重复"
    assert cur.execute("SELECT COUNT(*) FROM migration_status WHERE status='completed'").fetchone()[0] == 1
    c.close()
    print("[4] 幂等重跑 OK")

    # 5. rollback（复用 do_rollback 的备份还原逻辑：覆盖 + 清 WAL sidecar）
    c.close()  # close before overwriting to free all file handles
    baks = sorted(f for f in os.listdir(os.path.join(ROOT, "data"))
                  if f.startswith("logistics.legacy.db.bak."))
    assert baks, "无备份"
    shutil_copy(os.path.join(ROOT, "data", baks[0]), DB)
    for ext in ("-wal", "-shm", "-journal"):
        side = DB + ext
        if os.path.exists(side):
            os.remove(side)
    c = conn(); cur = c.cursor()
    tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "batches" not in tables, "回滚后不应有批次表"
    assert "node_key" not in [r[1] for r in cur.execute("PRAGMA table_info(nodes)").fetchall()], "回滚后 nodes 不应有 node_key"
    assert cur.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 24, "回滚后应为旧 12 节点×2"
    c.close()
    print("[5] rollback 还原 OK (批次表/列已去除, nodes 复原为 12×2)")

    # 6. 文件锁机制：锁存在时 --apply 应拒绝
    lock = DB + ".migrate.lock"
    with open(lock, "w") as f:
        f.write("99999")
    rc, out = run([TOOL, "--apply"])
    assert rc != 0 and "迁移已被锁定" in out, out
    os.remove(lock)
    print("[6] 文件锁 OK")

    print("\n=== T11 全部通过 ===")


if __name__ == "__main__":
    main()