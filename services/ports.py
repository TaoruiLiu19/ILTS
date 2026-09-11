"""
国内海港查询 / 检索服务
=======================
读取唯一权威数据文件 services/ports_cn.py，对外提供：
  list_tree()           港口群→省→港 层级
  search_ports(kw)      多方式检索（首字母/全拼/别名/代码/中文）
  get_port(key)         按 key 取港口条目
  platform_note(key, node_id)  取某港对某节点的平台备注
  merge_node_notes(base_remark, port_key)  合并模板备注+平台备注（同源，WYSIWYG）
  port_ability(key)     解析能力字段（浮吊/吃水/库场）
  nop_records()         40 港规模信息（供标题展示）

检索规则（kw 先小写去空格）：
  1. 命中 key / codes（子串）
  2. 命中 name / province / group（中文子串）
  3. 命中 initials（名称首字母，如 tj / tjg → 天津港）
  4. 命中 pinyin（前缀）
  5. 命中 aliases（人工别名）
"""

from services import ports_cn

_GROUP_ORDER = ["环渤海港口群", "长三角港口群", "东南沿海港口群",
                "粤港澳(大陆)", "环北部湾港口群"]

# ── 检索 ──

def _norm(kw):
    return (kw or "").strip().lower().replace(" ", "").replace("　", "")


def search_ports(kw, limit=50):
    """返回命中港口条目列表，按顺序（key/codes > 中文 > initials > pinyin > aliases）。"""
    q = _norm(kw)
    if not q:
        return list(ports_cn.all_ports())

    exact_priority = []
    chinese_priority = []
    initial_priority = []
    pinyin_priority = []
    alias_priority = []

    for p in ports_cn.all_ports():
        if not p["key"]:
            continue
        # 1. key / codes
        if q == p["key"].lower() or q in p.get("codes", "").lower():
            exact_priority.append((0, _GROUP_ORDER.index(p["group"]) if p["group"] in _GROUP_ORDER else 99, p))
        # 2. 中文子串
        elif q in p.get("name", "") or q in p.get("province", "") or q in p.get("group", ""):
            chinese_priority.append((1, _GROUP_ORDER.index(p["group"]) if p["group"] in _GROUP_ORDER else 99, p))
        # 3. 名称首字母
        elif p.get("initials") and q in p.get("initials", ""):
            initial_priority.append((2, _GROUP_ORDER.index(p["group"]) if p["group"] in _GROUP_ORDER else 99, p))
        # 4. 全拼前缀
        elif p.get("pinyin") and p.get("pinyin", "").startswith(q):
            pinyin_priority.append((3, _GROUP_ORDER.index(p["group"]) if p["group"] in _GROUP_ORDER else 99, p))
        # 5. 别名
        elif q in [a for a in p.get("aliases", [])]:
            alias_priority.append((4, _GROUP_ORDER.index(p["group"]) if p["group"] in _GROUP_ORDER else 99, p))

    def _sort(items):
        return [p for _, _, p in sorted(items, key=lambda x: (x[0], x[1]))]

    return (_sort(exact_priority) + _sort(chinese_priority)
            + _sort(initial_priority) + _sort(pinyin_priority)
            + _sort(alias_priority))[:limit]


# ── 层级树 ──

def list_tree():
    """返回 [{group, provinces: [{province, ports: [entry...]}]}]，按标准港口群顺序。"""
    result = []
    for g in _GROUP_ORDER:
        if g not in {p.get("group") for p in ports_cn.all_ports()}:
            continue
        group_ports = [p for p in ports_cn.all_ports() if p.get("group") == g]
        provinces = {}
        for p in group_ports:
            provinces.setdefault(p.get("province", "未知"), []).append(p)
        result.append({
            "group": g,
            "provinces": [{"province": prov, "ports": ports}
                          for prov, ports in provinces.items()],
        })
    return result


def list_ports():
    """返回全部港口条目（平铺，含注入的 port_timezone_offset）。"""
    return [_with_tz(p) for p in ports_cn.all_ports()]


# ── 查询 ──

def get_port(key):
    """兼容 config.get_port：返回港口条目 dict 或 None（含 port_timezone_offset）。"""
    return _with_tz(ports_cn.get_entry(key))


def _with_tz(p):
    """§6.6 D21：为港口条目补 port_timezone_offset（仅提示，不参与计算）。
    国内海港全部 UTC+8；生成文件 ports_cn.py 不再改动。"""
    if not p:
        return p
    if "port_timezone_offset" not in p:
        p = dict(p)
        p["port_timezone_offset"] = 8
    return p


def platform_note(key, node_id):
    p = ports_cn.get_entry(key)
    if not p:
        return None
    notes = p.get("platform_notes", {}) or {}
    return notes.get(node_id)


# ── 备注合并（WYSIWYG 唯一函数） ──

def merge_node_notes(base_remark, port_key, node_id=None):
    """
    合并"模板原始备注 + 平台注入备注"，返回最终备注字符串。
    - base_remark：国家模板节点的原始 remark（可空）
    - port_key：港口 key（None/空 → 不注入）
    - node_id：节点序号，仅当该港对该节点有平台备注时才注入
    """
    txt = base_remark or ""
    if not port_key:
        return txt
    note = platform_note(port_key, node_id)
    if not note:
        return txt
    return f"{txt} | {note}" if txt else note


# ── 能力解析（供详情/预警复用） ──

_EMPTY = {"present": False, "text": ""}


def _field(p, key):
    return (p.get("fields", {}) or {}).get(key) or ""


def port_ability(key):
    """
    返回 {crane, draft, storage}，每项 {present:bool, text:str}。
    present=False 表示该字段缺失或为 `—` / `未查到` 等空值（显示层转"暂无资料"）。
    """
    p = ports_cn.get_entry(key)
    empty = lambda: {"present": False, "text": ""}
    if not p:
        return {"crane": empty(), "draft": empty(), "storage": empty()}

    def judge(v):
        if not v:
            return False
        v2 = v.strip()
        if v2 in ("", "—", "无", "未查到"):
            return False
        return True

    crane = _field(p, "浮吊情况")
    draft = _field(p, "航道吃水")
    storage = _field(p, "库场情况")

    def _mk(v, j):
        return {"present": j, "text": v}

    return {
        "crane": _mk(crane, judge(crane)),
        "draft": _mk(draft, judge(draft)),
        "storage": _mk(storage, judge(storage)),
    }


def summary():
    """返回 {groups: [名称], count} 用于标题展示。"""
    ports = list_ports()
    return {
        "count": len(ports),
        "groups": [p["group"] for p in ports],
    }