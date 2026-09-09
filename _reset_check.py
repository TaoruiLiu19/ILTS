"""验证 reset_ops.py：只清 op_log，其他表不动"""
import os, sys, subprocess, sqlite3

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import db

WATCH = ("projects", "nodes", "files", "cargo_items", "vessel",
         "vessel_positions", "shift_history", "settings")


def snapshot():
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    out = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in WATCH}
    out["op_log"] = conn.execute("SELECT COUNT(*) FROM op_log").fetchone()[0]
    out["files_submitted"] = conn.execute(
        "SELECT COUNT(*) FROM files WHERE status='submitted'").fetchone()[0]
    out["nodes_done"] = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE status='Done'").fetchone()[0]
    out["_settings"] = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM settings")}
    conn.close()
    return out


before = snapshot()
print("清空前:", {k: v for k, v in before.items() if k != "_settings"})

r = subprocess.run([sys.executable, "-X", "utf8",
                    os.path.join(_HERE, "tools", "reset_ops.py")],
                   capture_output=True, text=True, encoding="utf-8", cwd=_HERE)
print("--- 脚本输出 ---")
print(r.stdout.strip())
if r.stderr.strip():
    print("stderr:", r.stderr.strip()[:500])

after = snapshot()
print("清空后:", {k: v for k, v in after.items() if k != "_settings"})

FAILED = []
def check(c, m):
    print(("PASS  " if c else "FAIL  ") + m)
    if not c:
        FAILED.append(m)

check(r.returncode == 0, f"脚本退出码 0（实际 {r.returncode}）")
check(after["op_log"] == 0, f"op_log 已清空（{before['op_log']} → {after['op_log']}）")
for t in WATCH:
    if t == "settings":
        continue
    check(after[t] == before[t], f"{t} 行数未变（{before[t]}）")
check(after["files_submitted"] == before["files_submitted"],
      f"单证勾选状态保留（已提交 {after['files_submitted']}）")
check(after["nodes_done"] == before["nodes_done"],
      f"节点完成状态保留（Done {after['nodes_done']}）")
check(after["_settings"].get("oplog_demo_seeded") is None,
      "演示日志标记已重置（下次启动会重新灌演示日志）")
for k, v in before["_settings"].items():
    if k == "oplog_demo_seeded":
        continue
    check(after["_settings"].get(k) == v, f"settings.{k} 保留")

print("\n" + ("✅ 全部通过" if not FAILED else f"❌ {len(FAILED)} 项失败"))
sys.exit(0 if not FAILED else 1)
