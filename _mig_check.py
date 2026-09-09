"""迁移验证：op_log → 「每单证每天只留 1 行最终态」

两种源库都能跑：
  · 旧口径库（含 valid 列、可能有被覆盖底稿/同日重复行）→ 验证迁移清理与去重；
  · 已迁移库（无 valid 列）→ 验证幂等与不变量仍成立。
"""
import os, sys, shutil, tempfile, sqlite3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_HERE = os.path.dirname(os.path.abspath(__file__))

SRC = os.path.join(_HERE, "data", "logistics.db")
if not os.path.exists(SRC):
    print("SKIP 无可用数据库")
    sys.exit(0)
print(f"源库: {os.path.basename(SRC)}")

_TMP = tempfile.mkdtemp(prefix="mig_")
DST = os.path.join(_TMP, "logistics.db")
shutil.copy2(SRC, DST)
# WAL 模式下最新数据在 -wal 里，必须一起复制
for suffix in ("-wal", "-shm"):
    if os.path.exists(SRC + suffix):
        shutil.copy2(SRC + suffix, DST + suffix)

import db
db.DB_PATH = DST
db._conn = None

conn = sqlite3.connect(DST)
conn.row_factory = sqlite3.Row
before_cols = [r["name"] for r in conn.execute("PRAGMA table_info(op_log)")]
before_n = conn.execute("SELECT COUNT(*) FROM op_log").fetchone()[0]
if "valid" in before_cols:
    before_kind = {r["kind"]: (r["n"], r["v"]) for r in conn.execute(
        "SELECT kind, COUNT(*) n, SUM(valid) v FROM op_log GROUP BY kind")}
else:
    before_kind = {r["kind"]: (r["n"], None) for r in conn.execute(
        "SELECT kind, COUNT(*) n FROM op_log GROUP BY kind")}
conn.close()
print(f"迁移前: 列={before_cols} 行数={before_n}")
print(f"        按 kind={before_kind}")

db.init_db()

conn = sqlite3.connect(DST)
conn.row_factory = sqlite3.Row
after_cols = [r["name"] for r in conn.execute("PRAGMA table_info(op_log)")]
after_n = conn.execute("SELECT COUNT(*) FROM op_log").fetchone()[0]
print(f"迁移后: 列={after_cols} 行数={after_n}")
groups = {}
for r in conn.execute("SELECT * FROM op_log"):
    if r["kind"] in ("file_submit", "file_withdraw"):
        groups.setdefault((r["project_id"], r["subject"], r["created_at"][:10]), []).append(r["kind"])
conn.close()

FAILED = []
def check(c, m):
    print(("PASS  " if c else "FAIL  ") + m)
    if not c:
        FAILED.append(m)

check("valid" not in after_cols, "valid 列已移除")
check(after_n <= before_n, f"行数未增加（{before_n} → {after_n}）")
check(all(len(v) == 1 for v in groups.values()),
      f"每（项目, 单证, 日期）仅 1 行（共 {len(groups)} 组）")
# 幂等：再跑一次不报错、行数不变
db.init_db()
conn = sqlite3.connect(DST)
again = conn.execute("SELECT COUNT(*) FROM op_log").fetchone()[0]
conn.close()
check(again == after_n, f"重复迁移幂等（{after_n} → {again}）")

print("\n" + ("✅ 迁移通过" if not FAILED else f"❌ {len(FAILED)} 项失败"))
shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(0 if not FAILED else 1)
