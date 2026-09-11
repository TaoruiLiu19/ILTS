"""时区换算辅助（《多式联运.md》§3.11 / §6.6 D21）。

全系统统一**北京时间（UTC+8）**：
  · 时间戳存 `2026-09-10T14:30:00+08:00`（ISO 8601 + 北京时间偏移）
  · 业务日期 `YYYY-MM-DD`，语义 = 北京时间当日
  · 界面统一标注「北京时间」

船期表常为**当地时间**，误当北京时间会错一整段。本模块提供：
  · `port_timezone_offset(port_key)` — 港口时区偏移（读国内海港字典，全部 +8）
  · `country_timezone_offset(code)` — 目的国/地区偏移（船期表常见来源地）
  · `local_to_beijing(dt)` / `beijing_to_local(dt, offset)` — 双向换算
  · `hint_for_port(port_key)` — 录入辅助提示文案
  · `check_plausible(date_str, offset, today)` — 与合理区间偏差过大给出提示（允许保存）

**仅提示，不参与存储与计算**（D21）：任何换算结果都不得写回 ETD/ETA。
"""

from datetime import datetime, timedelta

# 国内海港全部位于 UTC+8；目的国按船期表常见来源地登记（仅供录入提示）
PORT_OFFSET_DEFAULT = 8

COUNTRY_OFFSETS = {
    "BR": -3,    # 巴西（巴西利亚时间，Sepetiba/Santos）
    "US": -5,    # 美国东岸（纽约）；西岸 -8
    "MX": -6,
    "AR": -3,
    "CL": -4,
    "PE": -5,
    "ZA": 2,     # 南非
    "AE": 4,     # 阿联酋
    "SA": 3,
    "EG": 2,
    "IN": 5.5,
    "SG": 8,
    "MY": 8,
    "TH": 7,
    "VN": 7,
    "ID": 7,
    "JP": 9,
    "KR": 9,
    "AU": 10,
    "NZ": 12,
    "NL": 1,
    "DE": 1,
    "BE": 1,
    "FR": 1,
    "GB": 0,
    "IT": 1,
    "ES": 1,
    "GR": 2,
    "PL": 1,
    "RU": 3,
    "TR": 3,
    "CA": -5,
    "PA": -5,
    "CN": 8,
}

COUNTRY_NAMES = {
    "BR": "巴西", "US": "美国", "MX": "墨西哥", "AR": "阿根廷", "CL": "智利",
    "PE": "秘鲁", "ZA": "南非", "AE": "阿联酋", "SA": "沙特", "EG": "埃及",
    "IN": "印度", "SG": "新加坡", "MY": "马来西亚", "TH": "泰国", "VN": "越南",
    "ID": "印尼", "JP": "日本", "KR": "韩国", "AU": "澳大利亚", "NZ": "新西兰",
    "NL": "荷兰", "DE": "德国", "BE": "比利时", "FR": "法国", "GB": "英国",
    "IT": "意大利", "ES": "西班牙", "GR": "希腊", "PL": "波兰", "RU": "俄罗斯",
    "TR": "土耳其", "CA": "加拿大", "PA": "巴拿马", "CN": "中国",
}


def port_timezone_offset(port_key):
    """国内海港时区偏移（§6.6：读港口字典 port_timezone_offset）。"""
    if not port_key:
        return PORT_OFFSET_DEFAULT
    try:
        from services import ports as _ports
        p = _ports.get_port(port_key)
        if p and p.get("port_timezone_offset") is not None:
            return float(p["port_timezone_offset"])
    except Exception:
        pass
    return PORT_OFFSET_DEFAULT


def country_timezone_offset(country_code):
    return COUNTRY_OFFSETS.get((country_code or "").upper(), PORT_OFFSET_DEFAULT)


def _fmt_offset(h):
    sign = "+" if h >= 0 else "-"
    h = abs(float(h))
    hh = int(h)
    mm = int(round((h - hh) * 60))
    return f"UTC{sign}{hh:02d}:{mm:02d}" if mm else f"UTC{sign}{hh:02d}"


def offset_label(h):
    return _fmt_offset(h)


def _parse(s):
    if isinstance(s, datetime):
        return s
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def local_to_beijing(local_dt, offset):
    """当地时间 → 北京时间（仅提示，不参与存储）。"""
    d = _parse(local_dt)
    if d is None:
        return None
    return d + timedelta(hours=(PORT_OFFSET_DEFAULT - float(offset)))


def beijing_to_local(beijing_dt, offset):
    """北京时间 → 当地时间。"""
    d = _parse(beijing_dt)
    if d is None:
        return None
    return d - timedelta(hours=(PORT_OFFSET_DEFAULT - float(offset)))


def convert_line(date_text, offset, direction="local2bj", country_code=None):
    """生成一条换算提示文案（供录入框旁展示）。"""
    off_label = offset_label(offset)
    where = COUNTRY_NAMES.get((country_code or "").upper(), "") or "当地"
    if direction == "local2bj":
        out = local_to_beijing(date_text, offset)
        if out is None:
            return ""
        return (f"{where}时间 {date_text}（{off_label}）≈ 北京时间 "
                f"{out.strftime('%Y-%m-%d %H:%M')}（UTC+08:00）")
    out = beijing_to_local(date_text, offset)
    if out is None:
        return ""
    return (f"北京时间 {date_text}（UTC+08:00）≈ {where}时间 "
            f"{out.strftime('%Y-%m-%d %H:%M')}（{off_label}）")


def hint_for_port(port_key, country_code=None):
    """录入辅助提示（ETD/ETA 输入框旁）。"""
    po = port_timezone_offset(port_key)
    bits = [f"出发港 {offset_label(po)}"]
    if country_code:
        co = country_timezone_offset(country_code)
        bits.append(f"目的国 {offset_label(co)}")
    bits.append("系统统一按北京时间存储，当地时间请换算后再录入")
    return " · ".join(bits)


def plausibility_warning(etd, eta, today=None, max_ahead_days=730):
    """与合理区间偏差过大给出提示（**允许保存**，§6.6）。"""
    from services.clock import get_today
    t = today or get_today()
    d_etd, d_eta = _parse(etd), _parse(eta)
    msgs = []
    if d_etd and d_eta and d_eta < d_etd:
        msgs.append("ETA 早于 ETD")
    if d_etd:
        delta = (d_etd.date() - t).days
        if delta < -365:
            msgs.append(f"ETD 早于今日 {-delta} 天，疑似把当地时间当成了北京时间或年份录错")
        elif delta > max_ahead_days:
            msgs.append(f"ETD 晚于今日 {delta} 天，超出常见船期范围")
    return msgs
