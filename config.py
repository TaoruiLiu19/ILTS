"""
国家模板（节点/单证清单）+ 出口港子配置 —— 单证与节点统一按 node_key 定位
单一来源：新增国家/港口只改本文件，DB 与 UI 无需改结构。

节点模板数据来自 services/node_template.py（15 节点，node_key 稳定标识）。
"""

from services import ports as _ports
from services.node_template import NODES as _TEMPLATE_NODES
from services.node_template import (
    EMPTY_PICKUP, LASHING, GATE_IN, EXPORT_CUSTOMS, LOADING, SEA_TRANSIT,
    ARRIVAL_NOTICE, D_O_COLLECT, IMPORT_LICENSE, IMPORT_CUSTOMS,
    CUSTOMS_INSPECT, STORAGE_FEE, INLAND_TRANSPORT, SITE_DELIVERY, EMPTY_RETURN,
)

# 节点模板（含 node_id 序 / node_key / 时长 / 日历模式 / 关键节点标记）
NODES = _TEMPLATE_NODES
# 兼容旧代码里 node["id"][key]，保证读模板的调用方不破
for _n in NODES:
    _n["id"] = _n["node_id"]

COUNTRIES = {
    "BR": {
        "name": "巴西",
        "name_en": "Brazil",
        "flag": "BR",
        "buffer_days": 4,
        "nodes": NODES,
        # 目的国模板：进口清关是否需要税号（巴西 = CNPJ/CPF）
        "importer_tax_id_required": True,
        "files_project": [
            {"doc_name": "项目日报", "doc_type": "required", "owner_dept": "操作组", "copies": None,
             "due_node_key": None, "due_type": None, "remind_before_days": 1, "note": "全程每日"},
            {"doc_name": "物流动态跟踪表", "doc_type": "required", "owner_dept": "操作组", "copies": None,
             "due_node_key": None, "due_type": None, "remind_before_days": 7, "note": "全程每周"},
            {"doc_name": "项目进度报告", "doc_type": "required", "owner_dept": "项目部", "copies": None,
             "due_node_key": None, "due_type": None, "remind_before_days": 7, "note": "全程每周"},
            {"doc_name": "批次实施计划", "doc_type": "required", "owner_dept": "项目部", "copies": None,
             "due_node_key": EXPORT_CUSTOMS, "due_type": "node_start", "remind_before_days": 3,
             "note": "出口报关前，提前 3 天提醒"},
            {"doc_name": "人员安排计划", "doc_type": "required", "owner_dept": "项目部", "copies": None,
             "due_node_key": EXPORT_CUSTOMS, "due_type": "node_start", "remind_before_days": 3,
             "note": "出口报关前，提前 3 天提醒"},
        ],
        "files_nodes": [
            # 提空箱（新增）＋ 订舱类（§10.1：订舱委托 / SO）
            {"node_key": EMPTY_PICKUP, "doc_name": "订舱委托", "doc_type": "required", "owner_dept": "货代", "copies": None,
             "due_type": "node_start", "remind_before_days": 3, "doc_type_key": "BOOKING",
             "note": "订舱申请，船公司截单前提交"},
            {"node_key": EMPTY_PICKUP, "doc_name": "SO 订舱确认", "doc_type": "required", "owner_dept": "船公司", "copies": None,
             "due_type": "node_start", "remind_before_days": 3, "doc_type_key": "SO",
             "note": "船公司放舱确认书"},
            {"node_key": EMPTY_PICKUP, "doc_name": "提箱单/设备交接单", "doc_type": "required", "owner_dept": "货代/堆场", "copies": None,
             "due_type": "node_end", "remind_before_days": 2, "doc_type_key": "EIR_PICKUP",
             "note": "订舱后提空柜"},
            # 装箱/加固（吊装预警）
            {"node_key": LASHING, "doc_name": "绑扎加固方案", "doc_type": "required", "owner_dept": "码头/理货", "copies": None,
             "due_type": "node_start", "remind_before_days": 2, "note": "吊机能力≥100t/臂长≥36m"},
            {"node_key": LASHING, "doc_name": "装载计划", "doc_type": "required", "owner_dept": "码头", "copies": None,
             "due_type": "node_start", "remind_before_days": 2, "note": ""},
            # 返场/集港（VGM 属 SOLAS 装船前提交）
            {"node_key": GATE_IN, "doc_name": "VGM 重量验证", "doc_type": "required", "owner_dept": "发货人", "copies": None,
             "due_type": "node_end", "remind_before_days": 1, "doc_type_key": "VGM",
             "note": "SOLAS 要求，装船前提交"},
            {"node_key": GATE_IN, "doc_name": "集港通知", "doc_type": "required", "owner_dept": "货代", "copies": None,
             "due_type": "node_start", "remind_before_days": 2, "note": ""},
            {"node_key": GATE_IN, "doc_name": "港杂费单据", "doc_type": "required", "owner_dept": "港口", "copies": None,
             "due_type": "node_start", "remind_before_days": 2, "note": ""},
            # 出口报关（含核销单）
            {"node_key": EXPORT_CUSTOMS, "doc_name": "出口报关单", "doc_type": "required", "owner_dept": "报关行", "copies": 1,
             "due_type": "node_end", "remind_before_days": 3, "doc_type_key": "EXPORT_DECL",
             "note": "中国电子口岸三方协议"},
            {"node_key": EXPORT_CUSTOMS, "doc_name": "核销单", "doc_type": "required", "owner_dept": "报关行", "copies": 1,
             "due_type": "node_end", "remind_before_days": 3, "doc_type_key": "VERIFICATION",
             "note": "外汇核销用，随报关单提交"},
            {"node_key": EXPORT_CUSTOMS, "doc_name": "商业发票", "doc_type": "required", "owner_dept": "发货人", "copies": 3,
             "due_type": "node_end", "remind_before_days": 3, "doc_type_key": "INVOICE",
             "note": "需公证件，英/葡双语，注明 HS/原产地/贸易术语"},
            {"node_key": EXPORT_CUSTOMS, "doc_name": "装箱单", "doc_type": "required", "owner_dept": "发货人", "copies": 3,
             "due_type": "node_end", "remind_before_days": 3, "doc_type_key": "PACKING_LIST",
             "note": "详细列明每箱货物"},
            {"node_key": EXPORT_CUSTOMS, "doc_name": "出口合同", "doc_type": "required", "owner_dept": "收货人", "copies": 1,
             "due_type": "node_end", "remind_before_days": 3, "doc_type_key": "CONTRACT",
             "note": ""},
            {"node_key": EXPORT_CUSTOMS, "doc_name": "出口许可证(如需)", "doc_type": "optional", "owner_dept": "相关部门", "copies": None,
             "due_type": "node_end", "remind_before_days": 3, "note": "涉证商品需备"},
            # 装船（含申报类：AMS/ISF/ENS 截止 = 装船前 24/48 小时）
            {"node_key": LOADING, "doc_name": "装船单", "doc_type": "required", "owner_dept": "码头", "copies": None,
             "due_type": "node_end", "remind_before_days": 2, "note": ""},
            {"node_key": LOADING, "doc_name": "大副收据(理货单)", "doc_type": "required", "owner_dept": "大副/理货", "copies": 1,
             "due_type": "node_end", "remind_before_days": 2, "doc_type_key": "MATE_RECEIPT",
             "note": ""},
            {"node_key": LOADING, "doc_name": "AMS 美国舱单预申报", "doc_type": "required", "owner_dept": "货代/船代", "copies": None,
             "due_type": "node_start", "due_rule": "loading_before_hours", "due_hours": 24,
             "remind_before_days": 1, "doc_type_key": "AMS",
             "baseline_source": "美线 24 小时舱单规则（19 CFR 4.7）", "note": "装船前 24 小时"},
            {"node_key": LOADING, "doc_name": "ISF 美国进口安全申报", "doc_type": "required", "owner_dept": "货代/进口商", "copies": None,
             "due_type": "node_start", "due_rule": "loading_before_hours", "due_hours": 48,
             "remind_before_days": 2, "doc_type_key": "ISF",
             "baseline_source": "美线 10+2 规则（装船前 48 小时）", "note": "装船前 48 小时"},
            {"node_key": LOADING, "doc_name": "ENS 欧盟入境摘要报关", "doc_type": "optional", "owner_dept": "货代/船代", "copies": None,
             "due_type": "node_start", "due_rule": "loading_before_hours", "due_hours": 24,
             "remind_before_days": 1, "doc_type_key": "ENS",
             "baseline_source": "欧盟 ENS 装船前 24 小时规则", "note": "欧盟航线适用"},
            # 海上运输（提单分层：MBL / HBL / SWB 分别配置提前量）
            {"node_key": SEA_TRANSIT, "doc_name": "正本海运提单 MBL", "doc_type": "required", "owner_dept": "船公司", "copies": 3,
             "due_type": "node_start", "remind_before_days": 3, "doc_type_key": "MBL",
             "note": "航线见批次备注"},
            {"node_key": SEA_TRANSIT, "doc_name": "HBL 分提单", "doc_type": "required", "owner_dept": "货代", "copies": 3,
             "due_type": "node_start", "remind_before_days": 2, "doc_type_key": "HBL",
             "note": "货代签发，通常晚于 MBL"},
            {"node_key": SEA_TRANSIT, "doc_name": "SWB 海运单", "doc_type": "optional", "owner_dept": "船公司", "copies": None,
             "due_type": "node_start", "remind_before_days": 2, "doc_type_key": "SWB",
             "note": "不可转让海运单，与正本提单择一"},
            {"node_key": SEA_TRANSIT, "doc_name": "随船箱单/发票", "doc_type": "required", "owner_dept": "发货人", "copies": None,
             "due_type": "node_start", "remind_before_days": 3, "note": ""},
            {"node_key": SEA_TRANSIT, "doc_name": "保险单", "doc_type": "required", "owner_dept": "商务部", "copies": None,
             "due_type": "node_start", "remind_before_days": 5, "doc_type_key": "INSURANCE",
             "baseline_source": "CIF 条款投保要求（发运前）",
             "note": "发运前投保，CIF 条款，保险到期提醒"},
            # 到货通知（新增）
            {"node_key": ARRIVAL_NOTICE, "doc_name": "到货通知 AN", "doc_type": "required", "owner_dept": "境外代理/船代", "copies": None,
             "due_type": "node_end", "remind_before_days": 2, "doc_type_key": "ARRIVAL_NOTICE",
             "note": "到港后即推送收货人"},
            # 到港换单/领 D/O
            {"node_key": D_O_COLLECT, "doc_name": "正本提单(换单)", "doc_type": "required", "owner_dept": "船代", "copies": 1,
             "due_type": "node_start", "remind_before_days": 7, "doc_type_key": "MBL",
             "note": "提前 7 个工作日核对副本"},
            {"node_key": D_O_COLLECT, "doc_name": "电放保函", "doc_type": "optional", "owner_dept": "发货人", "copies": 1,
             "due_type": "node_start", "remind_before_days": 2, "doc_type_key": "TELEX_RELEASE",
             "note": "电放放货时替代正本提单"},
            {"node_key": D_O_COLLECT, "doc_name": "换单费收据", "doc_type": "required", "owner_dept": "船代", "copies": None,
             "due_type": "node_end", "remind_before_days": 2, "note": ""},
            {"node_key": D_O_COLLECT, "doc_name": "D/O 提货单", "doc_type": "required", "owner_dept": "船代", "copies": 1,
             "due_type": "node_end", "remind_before_days": 2, "doc_type_key": "DO",
             "note": "需核对清关单证一致"},
            # 进口许可 LI
            {"node_key": IMPORT_LICENSE, "doc_name": "非自动进口许可证申请书", "doc_type": "required", "owner_dept": "境外代理/进口商", "copies": None,
             "due_type": "node_start", "remind_before_days": 3, "note": "Siscomex 申请"},
            {"node_key": IMPORT_LICENSE, "doc_name": "原产地证(如需)", "doc_type": "optional", "owner_dept": "发货人", "copies": 1,
             "due_type": "node_start", "remind_before_days": 3, "note": "优惠关税 FORM A/CO"},
            {"node_key": IMPORT_LICENSE, "doc_name": "ANVISA/INMETRO 认证", "doc_type": "required", "owner_dept": "相关部门", "copies": None,
             "due_type": "node_start", "remind_before_days": 3, "note": "巴西强制认证（涉及产品时）"},
            # 进口清关申报
            {"node_key": IMPORT_CUSTOMS, "doc_name": "进口证", "doc_type": "required", "owner_dept": "收货人", "copies": 2,
             "due_type": "node_end", "remind_before_days": 3, "note": "外贸部签发；1 正 1 复；有效期 60 天"},
            {"node_key": IMPORT_CUSTOMS, "doc_name": "进口税费缴纳凭证", "doc_type": "required", "owner_dept": "报关行", "copies": None,
             "due_type": "node_end", "remind_before_days": 3, "note": "II/IPI/PIS/COFINS/ICMS"},
            # 海关查验
            {"node_key": CUSTOMS_INSPECT, "doc_name": "查验通知", "doc_type": "required", "owner_dept": "海关", "copies": None,
             "due_type": "node_start", "remind_before_days": 2, "note": "四通道：绿2/黄7-10/红15/灰20天-6月"},
            {"node_key": CUSTOMS_INSPECT, "doc_name": "熏蒸证书(木包装)", "doc_type": "required", "owner_dept": "发货人", "copies": 1,
             "due_type": "node_start", "remind_before_days": 2, "note": "木质包装必检；含非木质包装证明"},
            {"node_key": CUSTOMS_INSPECT, "doc_name": "危险品证明(如有)", "doc_type": "optional", "owner_dept": "发货人", "copies": 1,
             "due_type": "node_start", "remind_before_days": 2, "note": ""},
            {"node_key": CUSTOMS_INSPECT, "doc_name": "商品检验证明(二手/机器)", "doc_type": "optional", "owner_dept": "发货人", "copies": None,
             "due_type": "node_start", "remind_before_days": 2, "note": "二手/机器装置需出具"},
            # 临时堆存
            {"node_key": STORAGE_FEE, "doc_name": "堆存费收据", "doc_type": "required", "owner_dept": "码头", "copies": None,
             "due_type": "node_end", "remind_before_days": 2, "note": "超期仓储风险控制"},
            {"node_key": STORAGE_FEE, "doc_name": "放行通知单", "doc_type": "required", "owner_dept": "海关/码头", "copies": None,
             "due_type": "node_end", "remind_before_days": 2, "note": ""},
            # 境外内陆运输
            {"node_key": INLAND_TRANSPORT, "doc_name": "送货通知", "doc_type": "required", "owner_dept": "境外代理", "copies": None,
             "due_type": "node_start", "remind_before_days": 2, "note": ""},
            # 工地交付
            {"node_key": SITE_DELIVERY, "doc_name": "签收单", "doc_type": "required", "owner_dept": "收货人", "copies": None,
             "due_type": "node_end", "remind_before_days": 2, "note": ""},
            {"node_key": SITE_DELIVERY, "doc_name": "随车单据", "doc_type": "required", "owner_dept": "运输公司", "copies": None,
             "due_type": "node_end", "remind_before_days": 2, "note": ""},
            # 还空箱（新增）
            {"node_key": EMPTY_RETURN, "doc_name": "还箱单/设备交接单", "doc_type": "required", "owner_dept": "收货人/堆场", "copies": None,
             "due_type": "node_end", "remind_before_days": 2, "doc_type_key": "EIR_RETURN",
             "note": "空箱归还堆场"},
        ],
    }
}

# ── 出口港（国内海港）薄壳 ──
PORTS = {str(p["key"]): p for p in _ports.list_ports()}


def get_country(country_code):
    return COUNTRIES.get(country_code, {})


def get_port(port_code):
    """返回港口条目 dict 或 None。"""
    if not port_code:
        return None
    return _ports.get_port(port_code)