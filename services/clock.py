"""
统一时钟（测试用）

默认返回真实系统日期；测试时可通过 set_simulated_today() 切换到模拟日期，
使整个应用的"今日"都基于该日期计算（便于提前演示节点与单证预警）。

交付成产品前：删除本模块，并将所有 clock.get_today() 改回 date.today() 即可。
"""

from datetime import date, datetime

_simulated: date | None = None


def get_today() -> date:
    """返回当前"今日"（模拟日期优先，否则真实日期）"""
    if _simulated is not None:
        return _simulated
    return date.today()


def get_today_str() -> str:
    """返回 'YYYY-MM-DD' 格式的今日"""
    return get_today().strftime("%Y-%m-%d")


def set_simulated_today(d: date | str | None) -> None:
    """设置模拟日期；传 None 恢复为真实系统日期。"""
    global _simulated
    if d is None:
        _simulated = None
    elif isinstance(d, str):
        y, m, day = d.split("-")
        _simulated = date(int(y), int(m), int(day))
    else:
        _simulated = d


def is_simulated() -> bool:
    return _simulated is not None


def reset() -> None:
    """恢复真实系统日期"""
    global _simulated
    _simulated = None