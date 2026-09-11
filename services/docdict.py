"""单证字典与提醒规则主数据（《多式联运.md》§10.1 / §10.2 / §10.3）。

单一来源：本文件定义默认值并幂等播种进库（doc_types / reminder_rules /
doc_dependencies），UI 与提醒引擎一律读库，不在业务代码里硬编码字典。

§10.1 单证字典（含新增项）：
  订舱委托、SO、VGM、报关单、核销单、箱单、发票、合同、HBL、MBL、SWB、电放保函、
  D/O、产地证、保险单、AMS、ISF、ENS、大副收据、签收单、设备交接单（提/还箱）、
  到货通知配套单据。
"""

# 角色 key → 中文
ROLE_CN = {"CUSTOMER": "客户", "SHIPPER": "发货人", "CONSIGNEE": "收货人",
           "NOTIFY": "通知方", "IMPORTER": "进口商"}

# ── §10.1 单证类型主数据 ──
# key: (显示名, 分类, 默认提前量天数, 基准来源备注, 必填角色)
DOC_TYPES = [
    ("BOOKING",        "订舱委托",            "订舱",   3, "船公司截单规则", ("SHIPPER",)),
    ("SO",             "SO 订舱确认",         "订舱",   3, "船公司放舱规则", None),
    ("VGM",            "VGM 重量验证",        "出口",   1, "SOLAS 装船前提交要求", ("SHIPPER",)),
    ("EXPORT_DECL",    "出口报关单",          "出口",   3, "中国海关申报时限（装船前）", ("SHIPPER", "IMPORTER")),
    ("VERIFICATION",   "核销单",              "出口",   3, "外汇核销管理要求", ("SHIPPER",)),
    ("PACKING_LIST",   "装箱单",              "出口",   3, "随报关单提交", ("SHIPPER",)),
    ("INVOICE",        "商业发票",            "出口",   3, "随报关单提交", ("SHIPPER",)),
    ("CONTRACT",       "出口合同",            "出口",   3, "随报关单提交", ("SHIPPER",)),
    ("HBL",            "HBL 分提单",          "海运",   2, "货代签发惯例（通常晚于 MBL）", ("SHIPPER", "CONSIGNEE")),
    ("MBL",            "MBL 主提单",          "海运",   3, "船公司签发惯例", ("SHIPPER", "CONSIGNEE")),
    ("SWB",            "SWB 海运单",          "海运",   2, "不可转让海运单放货规则", ("SHIPPER", "CONSIGNEE")),
    ("TELEX_RELEASE",  "电放保函",            "海运",   2, "电放放货要求", ("SHIPPER",)),
    ("DO",             "D/O 提货单",          "进口",   2, "船代换单放货规则", ("SHIPPER", "CONSIGNEE")),
    ("CO",             "原产地证",            "进口",   3, "进口国优惠关税要求", ("SHIPPER",)),
    ("INSURANCE",      "保险单",              "海运",   5, "CIF 条款投保要求（发运前）", ("SHIPPER",)),
    ("AMS",            "AMS 美国舱单预申报",   "申报",   1, "美线 24 小时舱单规则", ("SHIPPER", "CONSIGNEE")),
    ("ISF",            "ISF 美国进口安全申报", "申报",   2, "美线 10+2 规则（装船前 48h）", ("SHIPPER", "CONSIGNEE", "IMPORTER")),
    ("ENS",            "ENS 欧盟入境摘要报关", "申报",   1, "欧盟装船前 24 小时规则", ("SHIPPER", "CONSIGNEE")),
    ("MATE_RECEIPT",   "大副收据(理货单)",     "海运",   2, "装船理货惯例", None),
    ("ARRIVAL_NOTICE", "到货通知 AN",          "进口",   2, "到港后即推送收货人", ("SHIPPER", "CONSIGNEE")),
    ("SIGNED_RECEIPT", "签收单",               "交付",   2, "工地交付签收要求", ("CONSIGNEE",)),
    ("EIR_PICKUP",     "设备交接单（提箱）",   "空箱",   2, "堆场提箱交接要求", None),
    ("EIR_RETURN",     "设备交接单（还箱）",   "空箱",   2, "堆场还箱交接要求", None),
]

# ── §10.2 提前量多维度规则（单证类型 × 目的国 × 承运人） ──
# 空 = 任意。匹配顺序：精确 → 目的国默认 → 单证类型默认 → 全局默认。
# before_days / before_hours 二者取其一（小时级用于"装船前 N 小时"）。
REMINDER_RULES = [
    # MBL 美线早 3 天；其余目的国走单证类型默认
    {"doc_type": "MBL", "country": "US", "carrier": None, "before_days": 3,
     "baseline_source": "美线提单签发惯例", "note": "美线 MBL 提前 3 天"},
    {"doc_type": "MBL", "country": None, "carrier": "中远海运", "before_days": 4,
     "baseline_source": "承运人截单要求", "note": "中远海运 MBL 提前 4 天"},
    # 提单分层：MBL / HBL 类型级默认（分别配置）
    {"doc_type": "MBL", "country": None, "carrier": None, "before_days": 3,
     "baseline_source": "船公司签发惯例", "note": "MBL 类型默认 3 天"},
    {"doc_type": "HBL", "country": None, "carrier": None, "before_days": 2,
     "baseline_source": "货代分提单签发惯例", "note": "HBL 与 MBL 分别配置"},
    # 申报类：装船前 N 小时（小时级截止）
    {"doc_type": "AMS", "country": None, "carrier": None, "before_hours": 24,
     "baseline_source": "美线 24 小时舱单规则（19 CFR 4.7）", "note": "装船前 24 小时"},
    {"doc_type": "ISF", "country": None, "carrier": None, "before_hours": 48,
     "baseline_source": "美线 10+2 规则（装船前 48 小时）", "note": "装船前 48 小时"},
    {"doc_type": "ENS", "country": None, "carrier": None, "before_hours": 24,
     "baseline_source": "欧盟 ENS 装船前 24 小时规则", "note": "装船前 24 小时"},
    # 保险 5 天；报关 3 天
    {"doc_type": "INSURANCE", "country": None, "carrier": None, "before_days": 5,
     "baseline_source": "CIF 条款投保要求", "note": "发运前 5 天"},
    {"doc_type": "EXPORT_DECL", "country": None, "carrier": None, "before_days": 3,
     "baseline_source": "中国海关申报时限", "note": "报关前 3 天"},
    # 全局默认兜底
    {"doc_type": None, "country": None, "carrier": None, "before_days": 3,
     "baseline_source": "系统全局默认", "note": "未命中任何规则时的兜底"},
]

GLOBAL_DEFAULT_DAYS = 3

# ── §10.3 单证依赖链 ──
DOC_DEPS = [
    ("MBL", "HBL"),                 # 先有主提单才能签发分提单
    ("EXPORT_DECL", "LOADING"),     # 报关单 → 装船
    ("INSURANCE", "LOADING"),       # 保险单 → 装船
    ("ARRIVAL_NOTICE", "DO"),       # 到货通知 → D/O
    ("DO", "PICKUP"),               # D/O → 提货
    ("SO", "VGM"),                  # 订舱确认 → 重量验证
    ("VGM", "LOADING"),             # 重量验证 → 装船
]

# 非单证类型的“动作/节点”依赖目标（用于 D/O → 提货 这类链路）
VIRTUAL_TARGETS = {"LOADING": "装船", "PICKUP": "提货"}


def doc_name_of(key):
    if key in VIRTUAL_TARGETS:
        return VIRTUAL_TARGETS[key]
    for k, name, *_ in DOC_TYPES:
        if k == key:
            return name
    return key


def seed(force=False):
    """幂等播种单证字典 / 提醒规则 / 依赖链。force=True 时覆盖已有规则。"""
    import db
    conn = db.get_conn()
    have = conn.execute("SELECT COUNT(*) AS c FROM doc_types").fetchone()["c"]
    if force or not have:
        for i, (key, name, cat, days, src, roles) in enumerate(DOC_TYPES, start=1):
            import json
            db.upsert_doc_type(key, type_name=name, category=cat,
                               baseline_source=src, default_before_days=days,
                               required_roles=json.dumps(list(roles), ensure_ascii=False)
                               if roles else None, seq=i)
    have_r = conn.execute("SELECT COUNT(*) AS c FROM reminder_rules").fetchone()["c"]
    if force or not have_r:
        db.set_reminder_rules(REMINDER_RULES)
    have_d = conn.execute("SELECT COUNT(*) AS c FROM doc_dependencies").fetchone()["c"]
    if force or not have_d:
        db.set_doc_dependencies(DOC_DEPS)
    return {"doc_types": len(DOC_TYPES), "reminder_rules": len(REMINDER_RULES),
            "doc_dependencies": len(DOC_DEPS)}


def match_doc_type(doc_name):
    """按单证名称反查字典 key（支持中英文/关键词包含）。"""
    if not doc_name:
        return None
    n = str(doc_name)
    for key, name, *_ in DOC_TYPES:
        if name == n or key.upper() == n.upper():
            return key
    # 关键词回退
    kw = [("MBL", ("MBL", "主提单")), ("HBL", ("HBL", "分提单")),
          ("SWB", ("SWB", "海运单")), ("DO", ("D/O", "提货单", "换单")),
          ("AMS", ("AMS",)), ("ISF", ("ISF",)), ("ENS", ("ENS",)),
          ("INSURANCE", ("保险",)), ("EXPORT_DECL", ("出口报关",)),
          ("ARRIVAL_NOTICE", ("到货通知",)), ("CO", ("原产地", "产地证")),
          ("EIR_PICKUP", ("提箱单",)), ("EIR_RETURN", ("还箱单",)),
          ("SIGNED_RECEIPT", ("签收单",)), ("MATE_RECEIPT", ("大副收据",)),
          ("PACKING_LIST", ("装箱单",)), ("INVOICE", ("发票",)),
          ("CONTRACT", ("合同",)), ("VERIFICATION", ("核销单",)),
          ("BOOKING", ("订舱",)), ("TELEX_RELEASE", ("电放",)),
          ("SO", ("SO",)), ("VGM", ("VGM",))]
    for key, keys in kw:
        if any(k in n for k in keys):
            return key
    return None


def lead_time(doc_key, country=None, carrier=None):
    """§10.2 提前量回落链（按维度特异性由高到低）：

        ① (类型, 目的国, 承运人) 精确
        ② (类型, 目的国, 任意)    ← 「目的国默认」
        ③ (类型, 任意, 承运人)    ← 「承运人默认」
        ④ (类型, 任意, 任意)      ← 「单证类型默认」
        ⑤ (任意, 目的国, 任意)
        ⑥ (任意, 任意, 任意)      ← 「全局默认」

    规则键为 (单证类型, 目的国, 承运人)（D23）；②③④ 即规格所述
    「未命中 → 目的国默认 → 单证类型默认」逐级放宽，承运人维度按
    规则键语义在目的国之后、类型默认之前生效。

    返回 dict：{before_days, before_hours, baseline_source, source}
    """
    import db
    rules = db.list_reminder_rules()

    def eq(a, b):
        return (a or None) == (b or None)

    def pick(match):
        for r in rules:
            if match(r):
                return r
        return None

    chain = [
        ("规则（类型+目的国+承运人）",
         lambda r: eq(r["doc_type"], doc_key) and eq(r["country"], country)
         and eq(r["carrier"], carrier) and bool(r["carrier"])),
        ("目的国默认",
         lambda r: eq(r["doc_type"], doc_key) and eq(r["country"], country)
         and not r["carrier"] and bool(r["country"])),
        ("承运人默认",
         lambda r: eq(r["doc_type"], doc_key) and not r["country"]
         and eq(r["carrier"], carrier) and bool(r["carrier"])),
        ("单证类型默认",
         lambda r: eq(r["doc_type"], doc_key) and not r["country"] and not r["carrier"]),
        ("目的国默认",
         lambda r: not r["doc_type"] and eq(r["country"], country) and bool(r["country"])),
        ("全局默认",
         lambda r: not r["doc_type"] and not r["country"] and not r["carrier"]),
    ]
    for src, match in chain:
        hit = pick(match)
        if hit:
            return {"before_days": hit.get("before_days"),
                    "before_hours": hit.get("before_hours"),
                    "baseline_source": hit.get("baseline_source"), "source": src}

    return {"before_days": GLOBAL_DEFAULT_DAYS, "before_hours": None,
            "baseline_source": "系统全局默认", "source": "全局默认（内建）"}
