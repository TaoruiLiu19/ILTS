#!/usr/bin/env python3
"""T1–T40 总验收（《多式联运.md》§14）。

运行：python tools/test_acceptance_all.py
退出码非 0 表示有未通过用例。

覆盖方式：
  · T1–T8 / T11–T16 / T19 / T22–T28 / T31–T34：本文件内联验证（1A/§8 主体）
  · T9–T10 / T17–T18 / T20–T21 / T29–T30：由 1B/1C 专项套件覆盖，
    本文件通过子进程运行它们并把结论并入总表（缺失则标记 SKIP 并计入失败）

数据安全（**注意不是全隔离**）：
  · 本文件内联用例与 1A/1B/1C 专项套件均在隔离临时库上运行（db.DB_PATH → tempdir）；
  · 但 T1（tools/test_migrate.py）会在 data/ 下重建 logistics.legacy.db* 迁移样本库；
  · T12（_audit/run_tests.py）会串跑若干直接读写 data/logistics.db 的脚本
    （其中 _reset_check.py 会清空真实库 op_log）。
  · 因此本脚本启动时会自动把 data/logistics.db 备份为
    data/logistics.db.auto-<时间戳>（保留最近 3 份），需要时直接 copy 回去即可还原。
"""
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _guard_real_db():
    """真实库护栏：跑验收前留一份备份（T12 串跑的脚本会读写真实库）。"""
    real = os.path.join(ROOT, "data", "logistics.db")
    data_dir = os.path.join(ROOT, "data")
    if not os.path.exists(real):
        return None
    dst = os.path.join(data_dir,
                       f"logistics.db.auto-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    try:
        shutil.copy2(real, dst)
    except Exception as e:
        print(f"[GUARD] 真实库备份失败：{e}")
        return None
    autos = sorted(f for f in os.listdir(data_dir) if f.startswith("logistics.db.auto-"))
    for old in autos[:-3]:
        try:
            os.remove(os.path.join(data_dir, old))
        except Exception:
            pass
    print(f"[GUARD] 真实库已备份 → data/{os.path.basename(dst)}")
    return dst


_guard_real_db()

import db

_TMP = tempfile.mkdtemp(prefix="accept_all_")
db.DB_PATH = os.path.join(_TMP, "t.db")
db._conn = None
db.init_db()

from services import batches as bs
from services import modes
from services import node_template as nt
from services import schedule2
from services import schedule_change as sch
from services import validation as vd
from services.reminder import compute_reminders
from mock_data import DEMO_PROJECT, build_project

RESULTS = {}          # case -> (status, note)
FAILED = []


def rec(case, ok, note=""):
    RESULTS[case] = ("PASS" if ok else "FAIL", note)
    if not ok:
        FAILED.append(f"{case}: {note}")
    print(f"  [{'PASS' if ok else 'FAIL'}] {case} {note}")


def d(s):
    return date(*map(int, str(s)[:10].split("-")))


def new_proj(tag, complete=False):
    pid = f"{DEMO_PROJECT['project_id']}-{tag}"
    r = build_project({**DEMO_PROJECT, "project_id": pid,
                       "actual_completion_date": "2026-11-10"}, complete=complete)
    return pid, r["batch_id"]


# ══════════════════ T1–T4 ══════════════════
print("== T1 迁移无感 ==")
r = subprocess.run([sys.executable, "tools/test_migrate.py"], cwd=ROOT,
                   capture_output=True, text=True, encoding="utf-8", errors="replace",
                   env=dict(os.environ, PYTHONUTF8="1", ILTS_DB_PATH=""))
rec("T1", r.returncode == 0 and "T11 全部通过" in (r.stdout or ""),
    "tools/test_migrate.py 六步全通过（project_no/B01/计数/node_key 全覆盖）")

print("== T2 多批次 ==")
pid2, b1 = new_proj("t2")
b2 = db.create_batch(pid2)["batch_id"]
b3 = db.create_batch(pid2)["batch_id"]
db.update_route(b2, export_port="SH", etd="2026-10-01", eta="2026-11-10")
db.update_route(b3, export_port="NB", etd="2026-11-01", eta="2026-12-10")
ok = (len(db.get_batches(pid2)) == 3
      and db.get_route(b1)["export_port"] == "QD"
      and db.get_route(b2)["export_port"] == "SH"
      and db.get_route(b3)["export_port"] == "NB"
      and db.get_route(b1)["etd"] != db.get_route(b3)["etd"])
rec("T2", ok, "同项目 3 批次，船期/港口独立（line 与 UI 均支持新增批次）")

print("== T3 批次隔离 ==")
n1 = {n["node_key"]: n["plan_start"] for n in db.get_nodes_by_batch(b1)}
from services.scheduler import apply_shift
apply_shift(pid2, db.get_nodes_by_batch(b1)[0]["node_id"], 3, batch_id=b1)
n1b = {n["node_key"]: n["plan_start"] for n in db.get_nodes_by_batch(b1)}
n2 = {n["node_key"]: n["plan_start"] for n in db.get_nodes_by_batch(b2)}
rec("T3", n1 != n1b and all(n2[k] == v for k, v in n2.items()),
    "对批次A位移不影响批次B的节点计划")

print("== T4 占位红线 ==")
rejected = 0
for bad in ("AIR", "ROAD", "RAIL", "SEA_AIR", "SEA_RAIL", "ROAD_SEA"):
    try:
        db.update_route(b1, mode_primary=bad)
    except modes.ModeNotEnabledError:
        rejected += 1
greyed = [c for c in modes.combo_choices() if not c[2]]
rec("T4", rejected == 6 and len(greyed) == 6 and modes.DEFAULT_PRIMARY == "SEA",
    f"6 个占位方式后端全部拒绝；下拉 {len(greyed)} 项置灰；mode_primary 恒 SEA")

# ══════════════════ T5–T8（§8 状态机） ══════════════════
print("== T5 状态判定 ==")
pid5, b5 = new_proj("t5")
st0 = db.get_batch(b5)["status"]
db.update_node(pid5, db.get_nodes_by_batch(b5)[0]["node_id"], batch_id=b5, status="Active")
r5 = bs.sync_batch_status(b5)
# 造可完成候选
pid5b, b5b = new_proj("t5b")
for n in db.get_nodes_by_batch(b5b):
    if n["is_key_node"]:
        db.update_node(pid5b, n["node_id"], batch_id=b5b, status="Done",
                       actual_completion_date=n["plan_end"])
er = db.node_by_key(b5b, nt.EMPTY_RETURN)
db.update_node(pid5b, er["node_id"], batch_id=b5b, status="Done",
               actual_completion_date=er["plan_end"])
for f in db.get_files_by_batch(b5b):
    if f["doc_type"] == "required":
        db.update_file(f["file_id"], status="submitted")
sync5 = bs.sync_batch_status(b5b)
suggest = sync5["suggest_complete"]
rec("T5", st0 == "ready" and r5["status"] == "running" and suggest
    and db.get_batch(b5b)["status"] != "completed",
    "节点Active→running；关键节点+必填齐+箱已还→「建议完成」，须人工确认")

print("== T6 取消与恢复 ==")
pid6, b6 = new_proj("t6")
db.update_batch(b6, status="running")
bs.cancel_batch(b6, reason="验收")
hidden = not any(x["batch_id"] == b6 for x in db.get_batches(pid6))
audit = any(x["batch_id"] == b6 for x in db.get_batches(pid6, include_cancelled=True))
try:
    db.update_node(pid6, db.get_nodes_by_batch(b6)[0]["node_id"], batch_id=b6, status="Active")
    blocked = False
except db.BatchCancelledError:
    blocked = True
res6 = bs.restore_batch(b6)
rec("T6", hidden and audit and blocked and res6["status"] == "running",
    "取消后隐藏+写入被拒；列表可查（审计）并恢复为 running")

print("== T7 换线影响 ==")
pid7, b7 = new_proj("t7")
db.update_batch(b7, status="running")
locked = bs.route_locked(b7)
imp = bs.change_route(b7, {"etd": "2026-09-25", "eta": "2026-11-05"}, reason="改配")
rec("T7", locked and len(db.get_route_changes(b7)) == 1
    and imp["nodes_need_recheck"] > 0,
    f"running 后线路只读；换线留痕并输出影响清单（需复核 {imp['nodes_need_recheck']} 节点）")

print("== T8 复制批次 ==")
pid8, b8 = new_proj("t8")
for f in db.get_files_by_batch(b8)[:4]:
    db.update_file(f["file_id"], status="submitted")
nb8 = bs.copy_batch(pid8, b8)
nbid = nb8["batch_id"]
ok8 = (all(n["status"] == "Pending" for n in db.get_nodes_by_batch(nbid))
       and all(f["status"] == "pending" for f in db.get_files_by_batch(nbid))
       and len(db.get_shift_history(nbid)) == 0
       and bool(db.get_route(nbid)["template_snapshot"]))
rec("T8", ok8, "副本无状态/日志/完成日期，线路含冻结模板快照")

# ══════════════════ T11–T16 ══════════════════
print("== T11 迁移回滚 ==")
rec("T11", r.returncode == 0 and "rollback 还原 OK" in (r.stdout or ""),
    "事务回滚 + .bak 还原 + 文件锁（test_migrate [5][6]）")

print("== T12 回归 ==")
rr = subprocess.run([sys.executable, os.path.join("_audit", "run_tests.py")], cwd=ROOT,
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    env=dict(os.environ, PYTHONUTF8="1"))
_out = rr.stdout or ""
tail = [l for l in _out.splitlines() if "通过 ==" in l or "未通过" in l]
# 用例数会随回归集增删变化，故按 "N/N 通过" 解析（全部通过 + 无失败清单）而非写死 18
import re as _re
_m = _re.search(r"===\s*(\d+)/(\d+)\s*通过\s*===", _out)
_passed, _total = (int(_m.group(1)), int(_m.group(2))) if _m else (0, 0)
rec("T12", rr.returncode == 0 and _total > 0 and _passed == _total
    and "未通过" not in _out,
    f"回归套件：{tail[0] if tail else 'n/a'}")

print("== T13 批次号/重命名 ==")
pid13, b13 = new_proj("t13")
no = db.get_batch(b13)["batch_no"]
nxt = db.create_batch(pid13)["batch_no"]
db.update_batch(b13, batch_name="首批发运")
dup_ok = True
try:
    db.get_conn().execute("INSERT INTO batches (batch_id,project_id,batch_no,status,created_at)"
                          " VALUES ('x',?,?,'draft','2026-01-01T00:00:00+08:00')", (pid13, no))
    db.get_conn().commit()
    dup_ok = False
except Exception:
    dup_ok = True
rec("T13", no.endswith("-B01") and nxt.endswith("-B02") and dup_ok
    and db.get_batch(b13)["batch_name"] == "首批发运",
    f"自动 {no} → 下一个 {nxt}；项目内唯一；可重命名")

print("== T14 项目号 ==")
pid14, b14 = new_proj("t14")
pno14 = db.get_project(pid14)["project_no"]
before_no = db.get_batch(b14)["batch_no"]
db.set_project_no(pid14, "P-CUSTOM01")
after_pno = db.get_project(pid14)["project_no"]
after_no = db.get_batch(b14)["batch_no"]
rec("T14", bool(pno14) and pno14.startswith("P-") and after_pno == "P-CUSTOM01"
    and before_no.startswith(pno14) and after_no.startswith("P-CUSTOM01"),
    f"旧项目自动生成 {pno14}，可改 → {after_pno}，批次号 {before_no} → {after_no}")

print("== T15 项目状态推导 ==")
pid15, b15 = new_proj("t15")
act = db.get_project(pid15)["status"]
db.update_batch(b15, status="closed")
bs.update_project_status(pid15)
comp = db.get_project(pid15)["status"]
rec("T15", act == "Active" and comp == "Completed",
    f"有启用批次→{act}；全部 closed→{comp}")

print("== T16 时区统一 ==")
from services import timezone_helper as tzh
from services.oplog import record as _rec
_rec("project_create", pid2, batch_id=b1, subject="TZ", detail="检查")
row = db.get_conn().execute(
    "SELECT created_at FROM op_log WHERE batch_id=? ORDER BY id DESC LIMIT 1",
    (b1,)).fetchone()
ts_ok = "T" in row["created_at"] and "+08:00" in row["created_at"]
conv = tzh.convert_line("2026-10-26 08:00", -3, "local2bj", country_code="BR")
port_off = db.get_conn()  # noqa
from config import get_port
po = (get_port("QD") or {}).get("port_timezone_offset")
rec("T16", ts_ok and "北京时间" in conv and po == 8,
    f"时间戳 {row['created_at']}；港口偏移 {po}；换算辅助可用")

# ══════════════════ T19 ══════════════════
print("== T19 数据校验 ==")
pid19, b19 = new_proj("t19")
db.update_route(b19, etd="2026-10-30", eta="2026-10-26")
try:
    vd.validate_batch_or_raise(b19)
    rejected = False
except vd.ValidationError as e:
    rejected = any("ETA" in p for p in e.problems)
db.update_route(b19, etd="2026-09-15", eta="2026-10-26")
rec("T19", rejected, "ETD>ETA 保存被拒并给出明确错误")

# ══════════════════ T22–T28 ══════════════════
print("== T22 空箱节点 ==")
tmpl = nt.template()
ok22 = (tmpl[0]["node_key"] == "EMPTY_PICKUP" and tmpl[-1]["node_key"] == "EMPTY_RETURN"
        and tmpl[0]["seq"] == 1 and tmpl[-1]["seq"] == 15)
rec("T22", ok22, "模板序1=提空箱、序15=还空箱")

print("== T23 迁移时区 ==")
rec("T23", r.returncode == 0, "迁移写入 +08:00 时间戳与 batch_routes（test_migrate 断言）")

print("== T24 状态恢复 ==")
pid24, b24 = new_proj("t24")
db.update_batch(b24, status="running")
bs.cancel_batch(b24)
try:
    db.update_route(b24, etd="2027-01-01")
    b24_blocked = False
except db.BatchCancelledError:
    b24_blocked = True
r24 = bs.restore_batch(b24)
rec("T24", b24_blocked and r24["status"] == "running",
    "取消期间写入被拒；恢复后仍为 running")

print("== T25 node_key 迁移 ==")
src = open(os.path.join(ROOT, "services", "scheduler.py"), encoding="utf-8").read()
import re as _re
hard = _re.findall(r"node_id\s*==\s*\d", src)
ok25 = (not hard and r.returncode == 0
        and nt.LASH_ALERT == "LASHING" and nt.SEA_ANCHOR == "SEA_TRANSIT"
        and nt.BUFFER_HINT == {"CUSTOMS_INSPECT", "STORAGE_FEE"})
rec("T25", ok25, "旧12节点映射完成；吊装/缓冲/海运规则均按 node_key 常量定位")

print("== T26 containers 表 ==")
pid26, b26 = new_proj("t26")
cid = db.insert_container(b26, "CSNU1234567", seal_no="SEAL01",
                          container_type="40HQ", pickup_at="2026-09-08",
                          return_due="2026-11-12")
db.link_container(b26, cid)
db.update_container(cid, returned_at="2026-11-10")
clist = db.get_containers(b26)
dup26 = False
try:
    db.insert_container(b26, "CSNU1234567")
except Exception:
    dup26 = True
rec("T26", len(clist) == 1 and clist[0]["container_type"] == "40HQ"
    and clist[0]["returned_at"] == "2026-11-10" and dup26,
    "柜型/提箱/还箱/免箱期可维护；柜号批次内唯一")

print("== T27 订舱号与提单 ==")
pid27, b27 = new_proj("t27")
db.update_batch(b27, booking_no="BK-2026-001", mbl_no="MBL-COS-9", hbl_nos='["HBL-1","HBL-2"]')
bb = db.get_batch(b27)
from services import docdict as dd
hbl_lead = dd.lead_time("HBL", country="BR")
mbl_lead = dd.lead_time("MBL", country="BR")
docs27 = {f["doc_name"] for f in db.get_files_by_batch(b27)}
rec("T27", bb["booking_no"] == "BK-2026-001" and "HBL-1" in bb["hbl_nos"]
    and "HBL 分提单" in docs27 and "正本海运提单 MBL" in docs27
    and hbl_lead["before_days"] != mbl_lead["before_days"],
    f"批次可录订舱号/MBL/HBL；HBL={hbl_lead['before_days']}天 vs MBL={mbl_lead['before_days']}天分别生效")

print("== T28 ready 状态 ==")
pid28, b28 = new_proj("t28")
db.update_batch(b28, status="draft")
r28 = bs.sync_batch_status(b28)
rec("T28", r28["status"] == "ready" and db.get_batch(b28)["status"] == "ready",
    "选完海运线路且资料就绪 → ready，未开始执行不误判 running")

# ══════════════════ T31–T34 ══════════════════
print("== T31 共柜预留 ==")
link = db.get_conn().execute(
    "SELECT role FROM container_batch_link WHERE batch_id=?", (b26,)).fetchone()
tbl = db.get_conn().execute(
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='container_batch_link'").fetchone()
rec("T31", bool(tbl) and link and link["role"] == "PRIMARY",
    "container_batch_link 存在，一期写入 PRIMARY")

print("== T32 全取消项目 ==")
pid32, b32 = new_proj("t32")
bs.cancel_batch(b32)
st32 = db.get_project(pid32)["status"]
from services.batches import state_label
lab = state_label("cancelled")
rec("T32", st32 == "Cancelled" and lab == "已取消",
    f"全部批次取消 ⇒ 项目状态 {st32}（已取消），已完成页只取 Completed 故不出现")

print("== T33 actual_* 自动推导 ==")
pid33, b33 = new_proj("t33")
nk = {n["node_key"]: n for n in db.get_nodes_by_batch(b33)}
db.update_node(pid33, nk[nt.LOADING]["node_id"], batch_id=b33, status="Done",
               actual_completion_date="2026-09-16")
auto = db.get_batch(b33)["actual_etd"]
bs.override_actual(b33, "actual_etd", "2026-09-17", reason="以码头日报为准")
manual = db.get_batch(b33)["actual_etd"]
db.update_node(pid33, nk[nt.LOADING]["node_id"], batch_id=b33, status="Done",
               actual_completion_date="2026-09-16")
kept = db.get_batch(b33)["actual_etd"]
logs33 = db.get_op_log_range(pid33, batch_id=b33) or []
rec("T33", auto == "2026-09-16" and manual == "2026-09-17" and kept == "2026-09-17"
    and any(l["kind"] == "batch_actual_override" for l in logs33),
    "装船完成自动回填；手工覆盖优先且留痕")

print("== T34 子阶段验收 ==")
phases = []
for f in ("tools/test_acceptance_1a.py", "tools/test_acceptance_1b.py",
          "tools/test_acceptance_1c.py"):
    p = os.path.join(ROOT, f)
    if not os.path.exists(p):
        phases.append((f, None))
        continue
    pr = subprocess.run([sys.executable, os.path.relpath(p, ROOT)], cwd=ROOT,
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        env=dict(os.environ, PYTHONUTF8="1"))
    phases.append((f, pr.returncode == 0))
missing = [f for f, okk in phases if okk is None]
rec("T34", not missing and all(okk for _, okk in phases),
    "1A/1B/1C 各自独立验收：" + "；".join(
        f"{os.path.basename(f)}={'通过' if okk else ('未通过' if okk is False else '缺失')}"
        for f, okk in phases))

# ══════════════════ T35–T40（复用 1A 专项套件结论） ══════════════════
print("== T35–T40 专项套件 ==")
p1a = subprocess.run([sys.executable, "tools/test_acceptance_1a.py"], cwd=ROOT,
                     capture_output=True, text=True, encoding="utf-8", errors="replace",
                     env=dict(os.environ, PYTHONUTF8="1"))
out1a = (p1a.stdout or "") + (p1a.stderr or "")
ok1a = p1a.returncode == 0 and "T35–T40 全部通过" in out1a
for c in ("T35", "T36", "T37", "T38", "T39", "T40"):
    rec(c, ok1a, "tools/test_acceptance_1a.py 通过")

# ══════════════════ T9/T10/T17/T18/T20/T21/T29/T30 ══════════════════
print("== 1B/1C 专项套件并入 ==")
PHASE_MAP = {
    "tools/test_acceptance_1b.py": ["T10", "T17", "T18", "T30"],
    "tools/test_acceptance_1c.py": ["T9", "T20", "T21", "T29"],
}
for f, cases in PHASE_MAP.items():
    p = os.path.join(ROOT, f)
    if not os.path.exists(p):
        for c in cases:
            RESULTS.setdefault(c, ("SKIP", f"{f} 不存在"))
            FAILED.append(f"{c}: 缺少 {f}")
        continue
    pr = subprocess.run([sys.executable, f], cwd=ROOT, capture_output=True, text=True,
                        encoding="utf-8", errors="replace",
                        env=dict(os.environ, PYTHONUTF8="1"))
    okk = pr.returncode == 0
    for c in cases:
        rec(c, okk, f"{f} {'通过' if okk else '未通过'}")

# ══════════════════ 汇总 ══════════════════
print("\n" + "=" * 62)
print("T1–T40 验收总表")
print("=" * 62)
order = sorted(RESULTS, key=lambda k: int(k[1:]))
line = []
for c in order:
    st, note = RESULTS[c]
    line.append(f"{c}:{st}")
    print(f"  {c:<4} {st:<5} {note}")
n_pass = sum(1 for c in RESULTS if RESULTS[c][0] == "PASS")
n_fail = sum(1 for c in RESULTS if RESULTS[c][0] == "FAIL")
n_skip = sum(1 for c in RESULTS if RESULTS[c][0] == "SKIP")
print("-" * 62)
print(f"通过 {n_pass} / 失败 {n_fail} / 缺失 {n_skip} / 共 {len(RESULTS)}")
miss = [f"T{i}" for i in range(1, 41) if f"T{i}" not in RESULTS]
if miss:
    print("未覆盖用例：", "、".join(miss))
if FAILED:
    print("\n未通过明细：")
    for x in FAILED:
        print("  ✗", x)
print()
print("结论：" + ("T1–T40 全部通过 ✅" if n_pass == 40 else f"仍有 {n_fail + n_skip} 项未通过/缺失"))
sys.exit(0 if n_pass == 40 else 1)
