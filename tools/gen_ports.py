#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国内海港数据工具（可选 · 日常改港不需要跑它）
=========================================
功能：
  1) python tools/gen_ports.py --check
     完整性自检 services/ports_cn.py —— 校验 40 港齐、key 唯一、必填非空、
     坐标可上图、港口群命名与代码一致、检索字段完整。软错只告警，硬错（重复
     key / 缺港 / key/name/group/province 为空）严格模式非零退出。
  2) python tools/gen_ports.py --md 国内海港.md [-o services/ports_cn.py]
     一次性把 md 导入生成 ports_cn.py（可选；产物为可编辑 Python 数据文件）。
     容错：单港某字段缺失/为 `—`/`未查到` → 落空串 + 告警；条目损坏 → 占位条目，
     保证总数=40、不整体中断。

设计原则（§8）：默认“容忍缺失”，数据文件任何情况下整体可用、绝不抛错中断。
"""

import os
import re
import sys
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

OUT_DATA = os.path.join("services", "ports_cn.py")

_STANDARD_GROUPS = [
    "环渤海港口群",
    "长三角港口群",
    "东南沿海港口群",
    "粤港澳(大陆)",
    "环北部湾港口群",
]
_EXPECT_COUNT = 40
_LAT_RANGE = (15.0, 45.0)
_LON_RANGE = (100.0, 130.0)
_EMPTY_VALUES = {"", "—", "无", "未查到", "未查到公开数据", "未查到公开资料"}

# 名字 → 拼音（导入降级用；全拼仅用于检索，缺少数时可容忍为空）
_STATIC_PINYIN = {
    "青岛": "qingdao", "天津": "tianjin", "大连": "dalian", "营口": "yingkou",
    "丹东": "dandong", "锦州": "jinzhou", "秦皇岛": "qinhuangdao", "唐山": "tangshan",
    "黄骅": "huanghua", "烟台": "yantai", "日照": "rizhao", "威海": "weihai",
    "龙口": "longkou", "上海": "shanghai", "连云港": "lianyungang", "太仓": "taicang",
    "大丰": "dafeng", "宁波舟山": "ningbozhoushan", "温州": "wenzhou", "台州": "taizhou",
    "嘉兴": "jiaxing", "福州": "fuzhou", "厦门": "xiamen", "泉州": "quanzhou",
    "漳州": "zhangzhou", "宁德": "ningde", "广州": "guangzhou", "深圳": "shenzhen",
    "湛江": "zhanjiang", "珠海": "zhuhai", "汕头": "shantou", "惠州": "huizhou",
    "茂名": "maoming", "潮州": "chaozhou", "防城": "fangcheng", "钦州": "qinzhou",
    "北海": "beihai", "海口": "haikou", "洋浦": "yangpu", "三亚": "sanya",
}
_STATIC_INITIALS = {
    "青岛": "qd", "天津": "tj", "大连": "dl", "营口": "yk", "丹东": "dd", "锦州": "jz",
    "秦皇岛": "qhd", "唐山": "ts", "黄骅": "hh", "烟台": "yt", "日照": "rz", "威海": "wh",
    "龙口": "lk", "上海": "sh", "连云港": "lyg", "太仓": "tc", "大丰": "df",
    "宁波舟山": "nbzs", "温州": "wz", "台州": "tz", "嘉兴": "jx", "福州": "fz",
    "厦门": "xm", "泉州": "qz", "漳州": "zz", "宁德": "nd", "广州": "gz", "深圳": "sz",
    "湛江": "zj", "珠海": "zh", "汕头": "st", "惠州": "hz", "茂名": "mm", "潮州": "cz",
    "防城": "fc", "钦州": "qz", "北海": "bh", "海口": "hk", "洋浦": "yp", "三亚": "sy",
}


def _warn(msg):
    print(f"⚠️  {msg}")


def _err(msg):
    print(f"❌  {msg}")


def _ok(msg):
    print(f"✅  {msg}")


def _is_empty(v):
    return (v or "").strip() in _EMPTY_VALUES


def load_port_data():
    from services import ports_cn
    return list(ports_cn.all_ports())


def check_data(strict=False):
    """校验 ports_cn.py。返回 (硬错数, 软警告数)。"""
    hard = soft = 0
    ports = load_port_data()

    keys = [str(p.get("key", "")).upper() for p in ports]
    dup = {k for k in keys if keys.count(k) > 1}
    if dup:
        hard += 1
        _err(f"存在重复 key：{sorted(dup)}")

    if len(ports) != _EXPECT_COUNT:
        hard += 1
        _err(f"港口数={len(ports)}，期望 {_EXPECT_COUNT}（不满足则硬伤）")
    else:
        _ok(f"港口总数 {_EXPECT_COUNT} ✓")

    seen_groups = set()
    for p in ports:
        key = p.get("key", "")
        name = p.get("name", "")
        # 必填
        req_ok = True
        for f in ("key", "name", "group", "province"):
            if not p.get(f):
                _err(f"{name or key} 缺少必填字段 '{f}'")
                req_ok = False
        if not req_ok:
            hard += 1
            continue

        if key in ("",):
            _err(f"端口 {name} key 为空")
            hard += 1
            continue

        grp = p.get("group")
        seen_groups.add(grp)
        if grp not in _STANDARD_GROUPS:
            _warn(f"{name} 港口群命名'{grp}'不在标准集 {_STANDARD_GROUPS}，将不被 map/list_tree 正确归组")
            soft += 1

        # 坐标
        lat, lon = p.get("lat"), p.get("lon")
        has_pos = (lat is not None and lon is not None
                   and _LAT_RANGE[0] <= float(lat) <= _LAT_RANGE[1]
                   and _LON_RANGE[0] <= float(lon) <= _LON_RANGE[1])
        if not has_pos:
            ap = p.get("approx_pos")
            has_pos = bool(ap and ap.get("lat") and ap.get("lon"))
            if not has_pos:
                _warn(f"{name} 缺乏可上图坐标（lat/lon 或 approx_pos）")
                soft += 1

        # 检索字段
        if not p.get("initials"):
            _warn(f"{name} 缺 initials（首字母，影响检索）")
            soft += 1
        if not p.get("pinyin"):
            _warn(f"{name} 缺 pinyin（全拼，影响检索）")
            soft += 1

        # 7 项资料空值提示（容忍，不阻断）
        fields = p.get("fields", {}) or {}
        for fld in ("港口代码", "经纬度", "潮汐", "航道吃水", "引水服务", "浮吊情况", "库场情况"):
            v = fields.get(fld)
            if _is_empty(v):
                _warn(f"{name} 字段'{fld}'为空（显示层将转『暂无资料』）")
                soft += 1

        # 未知字段名提示（防止笔误：改名/丢逗号引出的幽灵字段）
        known = {"港口代码", "经纬度", "潮汐", "航道吃水", "引水服务", "浮吊情况", "库场情况"}
        for fld in fields:
            if fld not in known:
                _warn(f"{name} 存在未知字段名'{fld}'（可能笔误，仅识别 7 个标准字段）")
                soft += 1

    for g in _STANDARD_GROUPS:
        if g not in seen_groups:
            _warn(f"港口群 '{g}' 无任何港口（拖空）")
            soft += 1

    print(f"\n—— 自检完成：{_EXPECT_COUNT} 港基准 | 硬错 {hard} 条 | 软警告 {soft} 条 ——")
    if hard:
        _err("存在硬伤，请修正；可用严格模式校验 : exit 1")
    return hard


# ─────────────── md 导入（可选） ───────────────

def _parse_coord(latlon):
    """把 md 经纬度文本解析为十进制 (lat, lon)；无法解析返回 (None, None)。"""
    # 取第一个近似形如 "dexdx′N/S, dexdx′E/W" 的片段
    if not latlon:
        return None, None
    m = re.search(r"(\d{1,3})[°\s](\d{1,2}(?:\.\d+)?)['′]?([NS]),?\s*(\d{1,3})[°\s](\d{1,2}(?:\.\d+)?)['′]?([EW])", latlon)
    if not m:
        # 退回匹配 "40.68" 十进制度对
        digs = re.findall(r"(-?\d{1,3}\.\d+)", latlon)
        if len(digs) >= 2:
            try:
                return float(digs[1]), float(digs[0])  # (lat, lon) 假定后 lat 前 lon
            except ValueError:
                return None, None
        return None, None
    d1 = int(m.group(1)) + float(m.group(2)) / 60.0
    d2 = int(m.group(4)) + float(m.group(5)) / 60.0
    if m.group(3) == "S":
        d1 = -d1
    if m.group(6) == "W":
        d2 = -d2
    return round(d1, 3), round(d2, 3)


def _parse_md(path):
    """解析《国内海港.md》→ [{group, province, name_i18n, fields}]。"""
    if not os.path.exists(path):
        _err(f"找不到 {path}")
        sys.exit(2)
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()

    group = province = None
    ports = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("## "):
            group = line[3:].strip()
        elif line.startswith("### "):
            province = line[4:].strip()
        elif line.startswith("#### "):
            title = line[5:].strip()  # "大连港 (Dalian)"
            m = re.match(r"(.+?)\s*\(([^)]*)\)\s*$", title)
            if m:
                name, name_en = m.group(1).strip(), m.group(2).strip()
            else:
                name, name_en = title, ""
            # 读取该港接下来的 markdown 表（直到下一个 #### 或空行前的连续表格）
            fields = {}
            j = i + 1
            while j < len(lines):
                l2 = lines[j].strip()
                if l2.startswith("#### ") or l2.startswith("### ") or l2.startswith("## "):
                    break
                fm = re.match(r"\|?\s*\*\*(.+?)\*\*\s*\|(.+)\|?\s*$", l2)
                if fm:
                    fields[fm.group(1).strip()] = fm.group(2).strip()
                j += 1
            ports.append({"group": group, "province": province, "name": name,
                          "name_en": name_en, "fields": fields})
            i = j
            continue
        i += 1
    return ports


def _gen_key(name, name_en, used):
    """导出港口唯一 key（青岛固定 QD）。"""
    if "青岛" in name:
        return "QD"
    base = _STATIC_INITIALS.get(name.rstrip("港"), "")
    base = (base or "").upper()
    if base in ("QZ",):  # 泉州用 QZ，钦州用 QZH；由 used 冲突决定加后缀
        pass
    key = base
    suffix = 1
    while key in used and suffix < 10:
        key = f"{base}{suffix}" if base not in ("",) else f"P{suffix}"
        suffix += 1
    return key or f"P{len(used) + 1}"


def _gen_pinyin(name):
    n = name.rstrip("港")
    return _STATIC_PINYIN.get(n, "") + "gang" if _STATIC_PINYIN.get(n) else ""


def _gen_initials(name):
    n = name.rstrip("港")
    return _STATIC_INITIALS.get(n, "") + "g" if _STATIC_INITIALS.get(n) else ""


def _fmt_py(v):
    """把 md 文本转 Python 字符串字面量（中文引号无需转义）。"""
    return repr(v)


def _render_data_file(ports):
    """ports: 导出的条目列表 → Python 数据文件文本。"""
    out = []
    out.append('"""')
    out.append('国内海港权威数据文件（唯一数据源 · 人工可直接编辑）')
    out.append('""".%s' % "")
    out.append('')
    out.append('')
    out.append('PORT_DATA = [')
    for p in ports:
        out.append('    {')
        out.append(f'        "key": {_fmt_py(p["key"])},')
        out.append(f'        "name": {_fmt_py(p["name"])},')
        out.append(f'        "name_en": {_fmt_py(p.get("name_en", ""))},')
        out.append(f'        "province": {_fmt_py(p["province"])},')
        out.append(f'        "group": {_fmt_py(p["group"])},')
        out.append(f'        "codes": {_fmt_py(p.get("codes", ""))},')
        out.append(f'        "initials": {_fmt_py(p.get("initials", ""))},')
        out.append(f'        "pinyin": {_fmt_py(p.get("pinyin", ""))},')
        out.append(f'        "aliases": {repr(p.get("aliases", []))},')
        out.append(f'        "lat": {p.get("lat")}, "lon": {p.get("lon")},')
        out.append('        "fields": {')
        fields = p.get("fields", {})
        for k in ("港口代码", "经纬度", "潮汐", "航道吃水", "引水服务", "浮吊情况", "库场情况"):
            out.append(f'            {_fmt_py(k)}: {_fmt_py(fields.get(k, ""))},')
        out.append('        },')
        out.append('    },')
        out.append('')
    out.append(']')
    out.append('')
    return "\n".join(out)


def build_from_md(md_path):
    parsed = _parse_md(md_path)
    used = set()
    ports = []
    for pr in parsed:
        try:
            fields = {k: "" if _is_empty(v) else v for k, v in pr["fields"].items()}
            lat, lon = _parse_coord(fields.get("经纬度", ""))
            key = _gen_key(pr["name"], pr.get("name_en", ""), used)
            used.add(key)
            if not key:
                _warn(f"{pr['name']}：无法生成 key，占位")
            ports.append({
                "key": key,
                "name": pr["name"],
                "name_en": pr.get("name_en", ""),
                "province": pr.get("province") or "未知",
                "group": pr.get("group") or "未知港口群",
                "codes": fields.get("港口代码", ""),
                "initials": _gen_initials(pr["name"]),
                "pinyin": _gen_pinyin(pr["name"]),
                "aliases": [key.lower()] if key else [],
                "lat": lat, "lon": lon,
                "fields": fields,
            })
        except Exception as e:  # noqa: BLE001  条目级容错
            _warn(f"{pr['name']}：条目异常 {e!r}，生成为占位条目")
            ports.append({"key": f"P{len(ports) + 1}", "name": pr["name"],
                          "name_en": "", "province": pr.get("province") or "未知",
                          "group": pr.get("group") or "未知港口群", "codes": "",
                          "initials": "", "pinyin": "", "aliases": [],
                          "lat": None, "lon": None, "fields": pr.get("fields", {})})
    return ports


def main():
    ap = argparse.ArgumentParser(prog="gen_ports", description="国内海港数据工具")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="完整性自检 ports_cn.py（软错只告警，硬错 exit 1）")
    g.add_argument("--md", metavar="国内海港.md", help="一次性从 md 导入生成 ports_cn.py")
    ap.add_argument("-o", "--out", default=OUT_DATA, help="输出文件（--md 用）")
    ap.add_argument("--strict", action="store_true", help="--check 严格模式：有硬错即非零退出")
    args = ap.parse_args()

    if args.check:
        hard = check_data(strict=args.strict)
        sys.exit(1 if (args.strict and hard) else 0)

    if args.md:
        ports = build_from_md(args.md)
        text = _render_data_file(ports) + "\n"
        out = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        _ok(f"已生成 {len(ports)} 港 → {out}")
        print("注意：md 导入仅为一次性脚手架；日常改港请直接编辑数据文件。"
              "platform_notes/banner 需人工补充。")
        sys.exit(0)

    ap.print_help()
    sys.exit(0)


if __name__ == "__main__":
    main()