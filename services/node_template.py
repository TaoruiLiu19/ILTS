"""
海运节点模板 —— 对照《多式联运.md》§5.2 一期 15 节点（node_key 稳定标识）

设计要点：
  · seq（即展示序）从 1 起，node_id 与 seq 保持一致，仅作展示排序。
  · 所有业务规则（吊装预警/缓冲提示/海运时长重算/actual_* 推导）一律按 node_key 定位，
    禁止再比较 node_id 或 seq。
  · node_id 用「序」表示，模板顺序由本文件决定。
"""

# node_key 稳定常量（唯一业务标识，改名模板只会动 name，不坏规则）
EMPTY_PICKUP = "EMPTY_PICKUP"      # 1  提空箱
LASHING = "LASHING"                # 2  装箱/加固（捆扎固定）—— 吊装预警节点
GATE_IN = "GATE_IN"                # 3  返场/集港（还重箱）
EXPORT_CUSTOMS = "EXPORT_CUSTOMS"  # 4  办理出口报关（WORKDAY）
LOADING = "LOADING"                # 5  实施货物装船（关键节点）
SEA_TRANSIT = "SEA_TRANSIT"        # 6  进行海上运输（关键节点）
ARRIVAL_NOTICE = "ARRIVAL_NOTICE"  # 7  收到到货通知
D_O_COLLECT = "D_O_COLLECT"        # 8  到港换单/领取 D/O（关键节点）
IMPORT_LICENSE = "IMPORT_LICENSE"  # 9  申请进口许可 LI
IMPORT_CUSTOMS = "IMPORT_CUSTOMS"  # 10 进行进口清关申报（WORKDAY）
CUSTOMS_INSPECT = "CUSTOMS_INSPECT"  # 11 执行海关查验/放行（WORKDAY，缓冲消耗提示）
STORAGE_FEE = "STORAGE_FEE"        # 12 支付临时堆存费（缓冲消耗提示）
INLAND_TRANSPORT = "INLAND_TRANSPORT"  # 13 安排境外内陆运输
SITE_DELIVERY = "SITE_DELIVERY"    # 14 完成工地交付（关键节点）
EMPTY_RETURN = "EMPTY_RETURN"      # 15 还空箱（同批次末节点）

# (key, 节点名, area, 默认时长, 责任人, 日历模式, 是否关键节点)
_RAW = [
    (EMPTY_PICKUP,     "提空箱",              "DOME",    1, "货代/堆场",       "NATURAL", False),
    (LASHING,          "装箱/加固（捆扎固定）", "DOME",    1, "码头/理货",       "NATURAL", False),
    (GATE_IN,          "返场/集港（还重箱）",   "DOME",    1, "货代/港口",       "NATURAL", False),
    (EXPORT_CUSTOMS,   "办理出口报关",        "DOME",    3, "报关行/货代",      "WORKDAY", False),
    (LOADING,          "实施货物装船",        "DOME",    1, "船公司/码头",      "NATURAL", True),
    (SEA_TRANSIT,      "进行海上运输",        "SEA",    46, "船公司(中远海运)", "NATURAL", True),
    (ARRIVAL_NOTICE,   "收到到货通知",        "OVERSEA", 1, "境外代理/船代",    "NATURAL", False),
    (D_O_COLLECT,      "到港换单/领取 D/O",   "OVERSEA", 1, "境外代理/船代",    "NATURAL", True),
    (IMPORT_LICENSE,   "申请进口许可 LI",     "OVERSEA", 1, "境外代理/进口商",  "NATURAL", False),
    (IMPORT_CUSTOMS,   "进行进口清关申报",    "OVERSEA", 3, "境外代理/报关行",  "WORKDAY", False),
    (CUSTOMS_INSPECT,  "执行海关查验/放行",   "OVERSEA", 1, "海关/境外代理",    "WORKDAY", False),
    (STORAGE_FEE,      "支付临时堆存费",      "OVERSEA", 1, "境外代理/码头",    "NATURAL", False),
    (INLAND_TRANSPORT, "安排境外内陆运输",    "OVERSEA", 2, "境外代理/运输公司", "NATURAL", False),
    (SITE_DELIVERY,    "完成工地交付",        "OVERSEA", 1, "收货人/项目组",    "NATURAL", True),
    (EMPTY_RETURN,     "还空箱",              "OVERSEA", 1, "收货人/堆场",      "NATURAL", False),
]

# 模板节点 list（每项含 node_id(序), node_key, seq, node_name, role_label, area,
# default_duration, duration, calendar_mode, key_node）
NODES = []
for i, (key, name, area, dur, role, calendar, key_node) in enumerate(_RAW, start=1):
    NODES.append({
        "node_id": i,
        "node_key": key,
        "seq": i,
        "node_name": name,
        "role_label": role,
        "area": area,
        "default_duration": dur,
        "duration": dur,
        "calendar_mode": calendar,
        "key_node": key_node,
    })

KEY_NODES = {n["node_key"] for n in NODES if n["key_node"]}

# 海/境内/境外分区
AREA_ORDER = ("DOME", "SEA", "OVERSEA")

# 索引
_BY_KEY = {n["node_key"]: n for n in NODES}
_BY_ID = {n["node_id"]: n for n in NODES}


def template():
    return list(NODES)


def by_key(key):
    return _BY_KEY.get(key)


def by_id(node_id):
    return _BY_ID.get(node_id)


def seq_of_key(key):
    n = _BY_KEY.get(key)
    return n["seq"] if n else None


def key_of_seq(seq):
    n = _BY_ID.get(seq)
    return n["node_key"] if n else None


def is_key_node(key):
    return key in KEY_NODES


# 旧 12 节点 -> 新 node_key（迁移/播种映射，对照 §5.2）
OLD_TO_KEY = {
    1: EXPORT_CUSTOMS,
    2: GATE_IN,
    3: LASHING,
    4: LOADING,
    5: SEA_TRANSIT,
    6: D_O_COLLECT,
    7: IMPORT_LICENSE,
    8: IMPORT_CUSTOMS,
    9: CUSTOMS_INSPECT,
    10: STORAGE_FEE,
    11: INLAND_TRANSPORT,
    12: SITE_DELIVERY,
}

# 各 node_key 默认时长（覆盖模板 duration，供按 ETD/ETA 重算时使用）
def default_duration(key):
    n = _BY_KEY.get(key)
    return n["default_duration"] if n else 1


# 固定规则锚点（业务告警 / actual_* 推导）
LASH_ALERT = LASHING                      # 吊装预警节点
SEA_ANCHOR = SEA_TRANSIT                  # 海运时长重算锚点
BUFFER_HINT = {CUSTOMS_INSPECT, STORAGE_FEE}  # 缓冲消耗提示节点
ACTUAL_SOURCE = {
    "actual_etd": LOADING,            # 实际离港
    "actual_eta": ARRIVAL_NOTICE,     # 实际到港（缺则取 D_O_COLLECT）
    "actual_delivery": SITE_DELIVERY, # 实际交付
    "empty_returned_at": EMPTY_RETURN,  # 还空箱
}