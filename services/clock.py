"""
统一时钟（测试用）

默认返回真实系统日期/时间；测试时可通过 set_simulated_today() 切换到模拟日期，
使整个应用的"今日"以及**所有入库时间戳**都基于该模拟时间（便于提前演示节点/单证
预警与操作日志），避免出现「节点按模拟日期判定、日志却记真实时间」的错位。

交付成产品前：删除本模块，并将所有 clock.get_today()/get_now() 改回 date.today()/datetime.now() 即可。
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


def get_now() -> datetime:
    """返回当前"现在"（模拟日期 + 真实时分秒；未模拟则为真实时间）。

    所有需要落库/展示的时间戳都应走这里，保证与「今日」同一时间基准。
    """
    if _simulated is not None:
        real = datetime.now()
        return datetime(_simulated.year, _simulated.month, _simulated.day,
                        real.hour, real.minute, real.second, real.microsecond)
    return datetime.now()


def get_now_str(fmt: str = "%Y-%m-%d %H:%M") -> str:
    """返回当前"现在"的格式化字符串（默认分钟级）"""
    return get_now().strftime(fmt)


def set_simulated_today(d: date | datetime | str | None) -> None:
    """设置模拟日期；传 None 恢复为真实系统日期。"""
    global _simulated
    if d is None:
        _simulated = None
    elif isinstance(d, datetime):
        _simulated = d.date()
    elif isinstance(d, str):
        y, m, day = d.split("-")[:3]
        _simulated = date(int(y), int(m), int(day))
    else:
        _simulated = d


def is_simulated() -> bool:
    return _simulated is not None


def reset() -> None:
    """恢复真实系统日期"""
    global _simulated
    _simulated = None