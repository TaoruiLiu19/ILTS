"""
操作日志统一入口（报告时间线 + 留痕）

设计要点：
  · 白名单：只记「导致业务状态净变化」的动作，浏览/预览/切页一律不记。
  · 单证类收敛（file_submit / file_withdraw）：**同一项目·同一单证·同一天只保留一行最终态**。
    同一张单证当天反复勾选/取消，库里永远只有 1 行：kind 即最终动作（提交/撤销），
    created_at 即该次动作时间；跨天各留一行（报告可按天回溯）。
    勾选 → 覆盖为「提交」；取消 → 覆盖为「撤销」，不会同时存在两条。
  · 位移当日净收敛：node_shift 同项目·同节点·同日折叠为「当日净值」，净 0 不记；
    node_unshift（撤销）单独成条、永不与主动位移对消灭失。
  · 所有日志带 project_id + created_at（双条件检索，杜绝跨项目串数据）。
"""

import re

import db

# 白名单：仅这些 kind 允许落库
WHITELIST = {
    "project_create", "project_close", "batch_create", "batch_cancel", "batch_restore",
    "batch_complete_confirm", "batch_close",
    "node_shift", "node_unshift", "node_done",
    "file_submit", "file_withdraw",
    "vessel_position", "cargo_edit",
    "schedule_recompute", "batch_schedule_change", "batch_actual_override",
    "route_change", "party_edit",
}

# 单证类（同项目·同单证·同日只留一行最终态）
FILE_KINDS = ("file_submit", "file_withdraw")

_DELTA_RE = re.compile(r"\[D([+-]\d+)\]$")


def _strip_delta(detail):
    """剥离位移行末尾的 [D+3] 收敛标记，返回 (展示文本, 净天数)"""
    if not detail:
        return "", 0
    m = _DELTA_RE.search(detail)
    if m:
        return detail[: m.start()].rstrip(), int(m.group(1))
    return detail, 0


def display_detail(detail):
    """报告展示用：去掉内部收敛标记。"""
    if not detail:
        return ""
    return _DELTA_RE.sub("", detail).strip()


def _now(created_at):
    if created_at:
        # §6.6：归一为 ISO 8601 +08:00（调用方可能传 'YYYY-MM-DD HH:MM'）
        return db.normalize_ts(created_at)
    # 走统一时钟；时间戳格式统一为 ISO 8601 +08:00（§6.6）
    return db._now()


def _day(created_at):
    return created_at[:10]


def _nets_of_day(project_id, kind, subject, day):
    """取同项目·同主体·同日的位移行净天数累计（用于位移当日收敛）。"""
    net = 0
    for row in db.get_op_log_range(project_id, start=day + " 00:00",
                                   end=day + " 23:59"):
        if row["kind"] == kind and (row["subject"] or "") == (subject or ""):
            _, d = _strip_delta(row["detail"] or "")
            net += d
    return net


def record(kind, project_id, node_id=None, subject="", detail="",
           delta=None, created_at=None, batch_id=None, node_key=None, scope="batch"):
    """
    统一入口。返回新行 id；未到白名单 / 当日净 0 收敛时不落库返回 None。
    delta 仅供 node_shift 收敛计算（正=推迟，负=提前）。
    """
    if kind not in WHITELIST:
        return None
    created_at = _now(created_at)
    day = _day(created_at)

    # ── 单证提交/撤交：同项目·同单证·同日收敛为一行最终态 ──
    if kind in FILE_KINDS:
        # 先删掉该单证当天已有的提交/撤销行（含被反复切换的），再写最终态
        db.delete_op_log({"project_id": project_id, "kind": FILE_KINDS,
                          "subject": subject, "created_day": day})
        return db.insert_op_log(project_id, kind, subject=subject,
                                detail=detail or ("提交" if kind == "file_submit" else "撤交"),
                                node_id=node_id, batch_id=batch_id, node_key=node_key,
                                scope=scope, created_at=created_at)

    # ── 主动位移：当日净收敛（净 0 不记）──
    if kind == "node_shift":
        prev = _nets_of_day(project_id, "node_shift", subject, day)
        new_net = prev + (delta if delta is not None else 0)
        # 覆盖该主体当日旧位移行，再写最新净态
        db.delete_op_log({"project_id": project_id, "kind": "node_shift",
                          "subject": subject, "created_day": day})
        if new_net == 0:
            return None  # 当日净 0：不记
        detail = f"{detail or ''} [D{new_net:+d}]"
        return db.insert_op_log(project_id, "node_shift", subject=subject,
                                detail=detail.strip(), node_id=node_id, batch_id=batch_id,
                                node_key=node_key, scope=scope, created_at=created_at)

    # ── 其余动作：直接追加 ──
    return db.insert_op_log(project_id, kind, subject=subject,
                            detail=detail or "", node_id=node_id, batch_id=batch_id,
                            node_key=node_key, scope=scope, created_at=created_at)
