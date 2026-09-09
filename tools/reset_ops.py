"""
清空操作数据（op_log）—— 仅清操作日志，其他表一律不动。

用途：反复测试「操作动态 / 日报周报」时把日志清干净重新演示。

清空内容：
  · op_log 全部行（提交/撤销单证、位移、自动完成、船位、货物变更等埋点）
  · 顺带重置 settings.oplog_demo_seeded —— 否则 app 启动不会再灌演示日志

保持不动：
  · projects / nodes / files（单证勾选状态保留）/ cargo_items / vessel
  · vessel_positions / shift_history（位移历史与「撤销上一步位移」按钮仍可用）
  · settings 其余键

用法（项目根目录下执行）：
    py -3.12 tools\\reset_ops.py            # 直接清空
    py -3.12 tools\\reset_ops.py --list     # 只看当前有多少条，不删
    py -3.12 tools\\reset_ops.py --keep-seed  # 清日志但保留演示标记
"""

import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

import db  # noqa: E402

SEED_KEY = "oplog_demo_seeded"


def _counts():
    conn = db.get_conn()
    total = conn.execute("SELECT COUNT(*) FROM op_log").fetchone()[0]
    by_kind = conn.execute(
        "SELECT kind, COUNT(*) n FROM op_log GROUP BY kind ORDER BY n DESC").fetchall()
    return total, [(r["kind"], r["n"]) for r in by_kind]


def main(argv):
    list_only = "--list" in argv
    keep_seed = "--keep-seed" in argv

    if not os.path.exists(db.DB_PATH):
        print(f"[X] 数据库不存在：{db.DB_PATH}")
        return 1

    total, by_kind = _counts()
    print(f"数据库：{db.DB_PATH}")
    print(f"op_log 现有 {total} 条")
    for kind, n in by_kind:
        print(f"  {kind:<18} {n}")

    if list_only:
        print("（--list 模式，未做任何修改）")
        return 0

    if total == 0 and (keep_seed or db.get_setting(SEED_KEY) is None):
        print("op_log 已经是空的，无需清理。")
        return 0

    conn = db.get_conn()
    conn.execute("DELETE FROM op_log")
    if not keep_seed:
        conn.execute("DELETE FROM settings WHERE key=?", (SEED_KEY,))
    conn.commit()

    after, _ = _counts()
    print(f"\n[OK] 已清空 op_log：{total} → {after} 条")
    print("     其他表（项目/节点/单证/货物/班轮/船位/位移历史）均未改动。")
    if not keep_seed:
        print("     已重置演示日志标记，下次启动 app 会重新灌入演示日志。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except sqlite3.Error as e:
        print(f"[X] 数据库操作失败：{e}")
        sys.exit(1)
