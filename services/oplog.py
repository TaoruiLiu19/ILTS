"""
操作日志统一入口（报告时间线 + 审计留痕）

设计要点：
 · 白名单：只记「导致业务状态净变化」的动作，浏览/预览/切页一律不记。
 · 审计覆盖：文件类（file_submit / file_withdraw）绝不全删 —— 回收旧有效行置 valid=0，
   报告默认只展示 valid=1 的最新提交；撤交与被覆盖行作为底稿保留。
 · 位移当日净收敛：node_shift 同项目·同节点·同日折叠为「当日净值」，净 0 不记；
   node_unshift（撤销）单独成条、永不与主动位移对消灭失。
 · 所有日志带 project_id + created_at（双条件检索，杜绝跨项目串数据）。
"""

import re

import db

# 白名单：仅这些 kind 允许落库
WHITELIST = {
    "project_create", "project_close",
    "node_shift", "node_unshift", "node_done",
    "file_submit", "file_withdraw",
    "vessel_position", "cargo_edit",
}

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
        return created_at
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _day(created_at):
    return created_at[:10]


def _nets_of_day(project_id, kind, subject, day):
    """取同项目·同主体·同日的有效行净天数累计（用于位移当日收敛）。"""
    net = 0
    for row in db.get_op_log_range(project_id, start=day + " 00:00",
                                   end=day + " 23:59", valid_only=True):
        if row["kind"] == kind and (row["subject"] or "") == (subject or ""):
            _, d = _strip_delta(row["detail"] or "")
            net += d
    return net


def record(kind, project_id, node_id=None, subject="", detail="",
           delta=None, created_at=None):
    """
    统一入口。返回新行 id；未到白名单 / 当日净 0 收敛时不落库返回 None。
    delta 仅供 node_shift 收敛计算（正=推迟，负=提前）。
    """
    if kind not in WHITELIST:
        return None
    created_at = _now(created_at)
    day = _day(created_at)

    # ── 文件提交/撤交：审计覆盖（绝不全删）──
    if kind in ("file_submit", "file_withdraw"):
        # 提交或撤交都把该表单当日仍有效的提交行置 0（审计底稿保留）
        db.invalidate_op_log({"project_id": project_id, "kind": "file_submit",
                              "subject": subject, "created_day": day})
        return db.insert_op_log(project_id, kind, subject=subject,
                                detail=detail or "", node_id=node_id,
                                valid=1, created_at=created_at)

    # ── 主动位移：当日净收敛（净 0 不记）──
    if kind == "node_shift":
        prev = _nets_of_day(project_id, "node_shift", subject, day)
        new_net = prev + (delta if delta is not None else 0)
        # 覆盖该主体当日旧有效位移行，再写最新净态
        db.invalidate_op_log({"project_id": project_id, "kind": "node_shift",
                              "subject": subject, "created_day": day})
        if new_net == 0:
            return None  # 当日净 0：不记
        detail = f"{detail or ''} [D{new_net:+d}]"
        return db.insert_op_log(project_id, "node_shift", subject=subject,
                                detail=detail.strip(), node_id=node_id,
                                valid=1, created_at=created_at)

    # ── 其余动作：直接追加 ──
    return db.insert_op_log(project_id, kind, subject=subject,
                            detail=detail or "", node_id=node_id,
                            valid=1, created_at=created_at)