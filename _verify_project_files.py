"""
项目级单证「单库一份」验证：新表 project_files + CRUD + 一次性迁移 + 批次感知日志查询。

覆盖：
  A 建表：老库 init_db() 自动补建 project_files（CREATE TABLE IF NOT EXISTS）
  B insert_project_files 幂等（同项目同 doc_name 插两次仍 1 行）
  C 行结构：project_files 行与 files 行兼容（file_id="pf-<n>" / scope / batch_id=None），
    且 files 行新增 scope="batch"（既有键与排序不变）
  D update_project_file / get_project_file 支持 "pf-<n>" 与裸 int 两种写法
  E 迁移：3 批次 × 3 行无锚点单证（9 行）→ project_files 3 行、files 无锚点行 0 行、
    任一批次已提交 → 迁移后 submitted（submitted_date 取组内最早）
  F 迁移幂等：重跑行数不变（守卫 + 清守卫各验一次）
  G 既有不变量：files.batch_id IS NULL 仍为 0（tools/migrate_batches.py 口径）
  H last_file_actions_by_batch 按 (批次, 单证名) 分开返回；旧 last_file_actions 行为不变

隔离临时库，绝不触碰 data/logistics.db。
"""

import io
import os
import shutil
import sys
import tempfile
import contextlib

os.environ["QT_QPA_PLATFORM"] = "offscreen"          # 与本仓库其它验收脚本保持一致
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:                                                 # Windows 控制台默认 GBK，emoji/中文会炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import db                                            # noqa: E402

# ★ 隔离临时库：务必在 import services / 建连接之前设置
_TMP = tempfile.mkdtemp(prefix="pf_")
db.DB_PATH = os.path.join(_TMP, "t.db")
db._conn = None
db.init_db()

from config import get_country                        # noqa: E402

FAILED = []


def check(msg, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}{('  · ' + str(detail)) if detail else ''}")
    if not cond:
        FAILED.append(msg)
    return bool(cond)


def count(sql, args=()):
    return db.get_conn().execute(sql, args).fetchone()[0]


PID_A = "pf-verify-crud"          # A 组：CRUD / 行结构
PID_M = "pf-verify-migrate"       # B 组：迁移场景

print("A. 建表（老库 init_db() 自动补建）")
tables = {r[0] for r in db.get_conn().execute(
    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
check("init_db() 后 project_files 表存在", "project_files" in tables)
check("唯一索引 idx_project_files_doc 存在",
      "idx_project_files_doc" in {r[0] for r in db.get_conn().execute(
          "SELECT name FROM sqlite_master WHERE type='index'").fetchall()})
check("无候选行时不落守卫（保持可重扫，避免旧写入路径产出的行永远搬不动）",
      db.get_setting("project_files_migrated_v1") is None,
      db.get_setting("project_files_migrated_v1"))

print("\nB/C/D. CRUD 与行结构")
db.insert_project({"project_id": PID_A, "project_no": "T-A",
                   "project_name": "项目级单证验证（CRUD）", "country": "BR"})
bid_a = db.create_default_batch(PID_A, "T-A-B01")["batch_id"]

docs = [f for f in get_country("BR")["files_project"] if not f.get("due_node_key")][:3]
check("BR 模板有 3 条无锚点的项目级单证", len(docs) == 3, [d["doc_name"] for d in docs])
db.insert_project_files(PID_A, docs)
db.insert_project_files(PID_A, docs)                  # 再插一次：应被唯一索引忽略
pfs = db.get_project_files(PID_A)
check("insert_project_files 幂等（插两次仍 3 行）", len(pfs) == 3, f"rows={len(pfs)}")
check("count_project_files 口径 total/required/pending/submitted",
      db.count_project_files(PID_A) == {"total": 3, "required": 3, "pending": 3, "submitted": 0},
      db.count_project_files(PID_A))
check("get_project_file 不存在的 id 返回 None", db.get_project_file("pf-999999") is None)

row0 = pfs[0]
check("file_id 为字符串 'pf-<project_file_id>'",
      row0["file_id"] == f"pf-{row0['project_file_id']}", row0["file_id"])
check("project_file_id 为 int", isinstance(row0["project_file_id"], int))
check("scope='project' 且 batch_id=None",
      row0["scope"] == "project" and row0["batch_id"] is None)
missing = [k for k in ("node_id", "node_key", "due_node_id", "due_node_key", "due_type",
                       "due_rule", "due_hours", "baseline_source", "due_date")
           if row0.get(k, "x") is not None]
check("无锚点键位全部补齐为 None", not missing, missing)
check("doc_name/doc_type/remind_before_days 取真实值",
      bool(row0["doc_name"]) and row0["doc_type"] in ("required", "optional")
      and row0["remind_before_days"] is not None, row0["doc_name"])

db.insert_files(PID_A, [{"doc_name": "出口报关单", "doc_type": "required",
                         "node_id": 6, "node_key": "EXPORT_CUSTOMS"}], batch_id=bid_a)
bf = db.get_files(PID_A)[0]
check("files 行新增 scope='batch'", bf.get("scope") == "batch", bf.get("scope"))
check("files 行既有键未变（file_id int / batch_id 有值 / 排序键 node_id 在）",
      isinstance(bf["file_id"], int) and bf["batch_id"] == bid_a and bf["node_id"] == 6)

# update_project_file：两种写法
db.update_project_file(pfs[1]["file_id"], status="submitted", submitted_date="2026-03-07")
check("update_project_file 支持 'pf-<n>' 写法",
      db.get_project_file(pfs[1]["file_id"])["status"] == "submitted")
db.update_project_file(pfs[1]["project_file_id"], note="裸 int 写入")
check("update_project_file 支持裸 int 写法",
      db.get_project_file(pfs[1]["project_file_id"])["note"] == "裸 int 写入")
check("update_project_file 未自动改 submitted_date（由调用方传）",
      db.get_project_file(pfs[1]["file_id"])["submitted_date"] == "2026-03-07")
check("count_project_files 随状态更新", db.count_project_files(PID_A)["submitted"] == 1)
# 复位，避免干扰后续断言
db.update_project_file(pfs[1]["file_id"], status="pending", submitted_date=None)
check("复位后仍按 doc_name 唯一（同项目再插不新增行）",
      db.insert_project_files(PID_A, docs) is None and len(db.get_project_files(PID_A)) == 3)

print("\nE. 一次性迁移（3 批次 × 3 行无锚点单证 → 项目级 3 行）")
db.insert_project({"project_id": PID_M, "project_no": "T-M",
                   "project_name": "项目级单证验证（迁移）", "country": "BR"})
bids = [db.create_default_batch(PID_M, f"T-M-B{i:02d}")["batch_id"] for i in (1, 2, 3)]
tpl = get_country("BR")["files_project"]              # 5 条：3 条无锚点 + 2 条挂 EXPORT_CUSTOMS
for b in bids:
    db.insert_files(PID_M, tpl, batch_id=b)           # 模拟旧口径：每批次各复制一份
n_all = count("SELECT COUNT(*) FROM files WHERE project_id=?", (PID_M,))
n_orphan = count("SELECT COUNT(*) FROM files WHERE project_id=? AND node_key IS NULL "
                 "AND due_node_key IS NULL AND due_type IS NULL AND due_rule IS NULL", (PID_M,))
check("造数：3 批次共 15 行 files，其中无锚点 9 行", n_all == 15 and n_orphan == 9,
      f"all={n_all} orphan={n_orphan}")

# 一个批次已提交（且不同批次日期不同 → 验证「取最早已提交日期」）
r1 = db.get_files_by_batch(bids[0])
f1 = [f for f in r1 if f["doc_name"] == "项目日报"][0]
db.update_file(f1["file_id"], status="submitted", submitted_date="2026-03-10")
r3 = db.get_files_by_batch(bids[2])
f3 = [f for f in r3 if f["doc_name"] == "项目日报"][0]
db.update_file(f3["file_id"], status="submitted", submitted_date="2026-03-05")

conn = db.get_conn()
conn.execute("DELETE FROM settings WHERE key='project_files_migrated_v1'")   # 清守卫 → 重跑迁移
conn.commit()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    db.init_db()
log = buf.getvalue()
check("迁移打印中文一行（搬了/删了几行）",
      "[迁移]" in log and "搬运 3 行" in log and "9 行" in log, log.strip().splitlines()[-1:])

pfs_m = db.get_project_files(PID_M)
check("迁移后 project_files 3 行", len(pfs_m) == 3, f"rows={len(pfs_m)}")
check("迁移后 files 里无锚点行为 0",
      count("SELECT COUNT(*) FROM files WHERE node_key IS NULL AND due_node_key IS NULL "
            "AND due_type IS NULL AND due_rule IS NULL") == 0)
check("有锚点的行仍留在 files（2 条 × 3 批次 = 6 行）",
      count("SELECT COUNT(*) FROM files WHERE project_id=?", (PID_M,)) == 6,
      count("SELECT COUNT(*) FROM files WHERE project_id=?", (PID_M,)))
d = {p["doc_name"]: p for p in pfs_m}
check("任一批次已提交 → 迁移后为 submitted",
      d["项目日报"]["status"] == "submitted", d["项目日报"]["status"])
check("submitted_date 取组内最早（2026-03-05 而非 2026-03-10）",
      d["项目日报"]["submitted_date"] == "2026-03-05", d["项目日报"]["submitted_date"])
check("无提交动作的单证迁移后仍 pending",
      {d[n]["status"] for n in ("物流动态跟踪表", "项目进度报告")} == {"pending"})
check("迁移保留模板字段（owner_dept / remind_before_days）",
      d["项目日报"]["owner_dept"] == "操作组" and d["项目日报"]["remind_before_days"] == 1,
      f"{d['项目日报']['owner_dept']} / {d['项目日报']['remind_before_days']}")
check("迁移保留 project_id 且不挂批次",
      d["项目日报"]["project_id"] == PID_M and d["项目日报"]["batch_id"] is None)

print("\nF/G. 迁移幂等 + 既有不变量")
before = len(db.get_project_files(PID_M))
db.init_db()                                          # 有守卫：直接返回
check("带守卫重跑迁移：行数不变", len(db.get_project_files(PID_M)) == before)
check("搬过东西 → 守卫已落 settings", db.get_setting("project_files_migrated_v1") is not None,
      db.get_setting("project_files_migrated_v1"))
conn.execute("DELETE FROM settings WHERE key='project_files_migrated_v1'")
conn.commit()
with contextlib.redirect_stdout(io.StringIO()):
    db._migrate_project_files(conn)                   # 无守卫再跑一次：已无候选行，不应重复
check("清守卫重跑迁移：无候选行 → 行数不变（唯一索引兜底）",
      len(db.get_project_files(PID_M)) == before == 3 and
      count("SELECT COUNT(*) FROM project_files") == len(db.get_project_files(PID_A)) + 3,
      f"migrate={len(db.get_project_files(PID_M))}")
check("不变量：files.batch_id IS NULL 为 0",
      count("SELECT COUNT(*) FROM files WHERE batch_id IS NULL") == 0)
check("不变量：迁移后 files 无 NULL batch_id 且 project_files 未挂批次",
      count("SELECT COUNT(*) FROM project_files") > 0)

print("\nH. 批次感知的日志查询（修报告串批次）")
db.insert_op_log(PID_M, "file_submit", subject="项目日报", batch_id=bids[0],
                 created_at="2026-03-01T10:00:00+08:00")
db.insert_op_log(PID_M, "file_submit", subject="项目日报", batch_id=bids[2],
                 created_at="2026-03-08T10:00:00+08:00")
db.insert_op_log(PID_M, "file_submit", subject="项目日报", batch_id=None,
                 created_at="2026-03-12T10:00:00+08:00")   # 项目级动作（batch_id 为 None）
by_batch = db.last_file_actions_by_batch(PID_M)
check("last_file_actions_by_batch 键为 (批次, 单证名) 且三态分开",
      set(by_batch.keys()) == {(bids[0], "项目日报"), (bids[2], "项目日报"),
                               (None, "项目日报")}, sorted(map(str, by_batch.keys())))
check("各批次各取自己的最后一次动作（不串批次）",
      by_batch[(bids[0], "项目日报")]["batch_id"] == bids[0]
      and by_batch[(bids[2], "项目日报")]["created_at"].startswith("2026-03-08")
      and by_batch[(None, "项目日报")]["batch_id"] is None)
old = db.last_file_actions(PID_M)
check("旧 last_file_actions 行为不变（按 doc_name 只返回一条）",
      list(old.keys()) == ["项目日报"] and old["项目日报"]["batch_id"] is None,
      {k: v["batch_id"] for k, v in old.items()})

# ══════════════════ 收尾 ══════════════════

try:
    if db._conn is not None:
        db._conn.close()
        db._conn = None
except Exception:
    pass
shutil.rmtree(_TMP, ignore_errors=True)

print("\n" + ("PASS ALL" if not FAILED else f"FAIL {len(FAILED)}"))
if FAILED:
    for f in FAILED:
        print("  - " + f)
sys.exit(0 if not FAILED else 1)
