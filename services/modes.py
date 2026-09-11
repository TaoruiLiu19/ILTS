"""运输方式字典与占位红线（《多式联运.md》§9）。

红线（§9 / D3 / §2.2）：
  · 一期仅 `SEA` 启用；`AIR/ROAD/RAIL/SEA_AIR/SEA_RAIL/ROAD_SEA` 为占位。
  · `mode_primary` 一期恒 `SEA`，`mode_chain` 恒 `["SEA"]`。
  · 占位只能展示，不能进入业务流程：后端**双重校验拒绝**（UI 置灰 + 本模块校验），
    非启用方案不生成节点/单证/提醒、不进统计与报告范围。

任何写入 batch_routes 的路径都必须过 `validate_route_modes()`。
"""


class ModeNotEnabledError(ValueError):
    """使用了本期未启用的运输方式（占位红线）。"""


# key → (显示名, 是否启用)
MODES = {
    "SEA":      ("纯海运", True),
    "AIR":      ("空运", False),
    "ROAD":     ("公路", False),
    "RAIL":     ("铁路/中欧班列", False),
    "SEA_AIR":  ("海空联运", False),
    "SEA_RAIL": ("海铁联运", False),
    "ROAD_SEA": ("公海联运", False),
}

# 一期恒定的线路组合
DEFAULT_PRIMARY = "SEA"
DEFAULT_CHAIN = '["SEA"]'

ENABLED = {k for k, (_, on) in MODES.items() if on}
PLACEHOLDERS = {k for k, (_, on) in MODES.items() if not on}


def label(key):
    return MODES.get(key, (key, False))[0]


def is_enabled(key):
    return key in ENABLED


def placeholder_label(key):
    """占位项展示文案：置灰 +「后续开放」。"""
    return f"{label(key)}（后续开放）"


def validate_route_modes(mode_primary=None, mode_chain=None):
    """校验线路方式；非法即抛 ModeNotEnabledError（后端硬拒绝）。

    仅校验传入的字段（None = 不改动，跳过）。
    """
    if mode_primary is not None:
        if mode_primary not in MODES:
            raise ModeNotEnabledError(
                f"未知运输方式「{mode_primary}」；可用：{', '.join(sorted(MODES))}")
        if not is_enabled(mode_primary):
            raise ModeNotEnabledError(
                f"运输方式「{label(mode_primary)}」为后续阶段能力，本期不可选"
                f"（占位红线 §9）。本期仅支持：{label(DEFAULT_PRIMARY)}")
    if mode_chain is not None:
        chain = _parse_chain(mode_chain)
        if chain is None:
            raise ModeNotEnabledError(f"线路组合格式非法：{mode_chain!r}（应为 JSON 数组）")
        for k in chain:
            if not is_enabled(k):
                raise ModeNotEnabledError(
                    f"线路组合含未启用方式「{label(k)}」；本期 mode_chain 恒为 "
                    f'{DEFAULT_CHAIN}（占位红线 §9）')
        if len(chain) != 1 or chain[0] != DEFAULT_PRIMARY:
            raise ModeNotEnabledError(
                f'本期 mode_chain 恒为 {DEFAULT_CHAIN}，收到 {mode_chain!r}（占位红线 §9）')


def _parse_chain(mode_chain):
    if isinstance(mode_chain, (list, tuple)):
        return list(mode_chain)
    import json
    try:
        v = json.loads(mode_chain)
    except (TypeError, ValueError):
        return None
    return list(v) if isinstance(v, list) else None


def combo_choices():
    """新建/编辑线路用的下拉项：[(显示文本, key, 是否可选)]，占位项置灰不可选。"""
    out = []
    for key, (name, on) in MODES.items():
        out.append((name if on else placeholder_label(key), key, on))
    return out
