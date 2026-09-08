"""
Mock 数据：青岛→巴西Sepetiba 光伏组件运输
数据来源：用户提供的青岛→巴西甘特图（7节点简化版 + 海运压缩），已映射到12节点模板。
开发时直接 import 使用，或调用 seed_demo_project() 写入 SQLite。
"""

from datetime import date, timedelta


DEMO_PROJECT = {
    "project_id": "demo-qd-br-001",
    "project_name": "青岛→巴西Sepetiba 光伏组件运输",
    "country": "BR",
    "export_port": "QD",
    "status": "Active",
    "etd": "2026-09-10",
    "eta": "2026-10-26",
    "buffer_days": 4,
    "create_date": "2026-09-04",
    "durations": {1: 3, 2: 1, 3: 1, 4: 1, 5: 46, 6: 1, 7: 1, 8: 3, 9: 1, 10: 1, 11: 2, 12: 1},
}


DEMO_NODES = [
    {"node_id": 1,  "node_name": "出口报关",       "role_label": "报关行/货代",      "area": "DOME",    "seq": 1,  "duration": 3,  "remark": "青岛港·单一窗口/电子口岸，需签三方协议"},
    {"node_id": 2,  "node_name": "集港",           "role_label": "货代/港口",        "area": "DOME",    "seq": 2,  "duration": 1,  "remark": "青岛港·云港通办理装卸合同/流向填报"},
    {"node_id": 3,  "node_name": "捆扎固定",        "role_label": "码头/理货",        "area": "DOME",    "seq": 3,  "duration": 1,  "remark": "件杂货船自吊机，吊装能力≥100t，臂长≥36m"},
    {"node_id": 4,  "node_name": "装船",           "role_label": "船公司/码头",      "area": "DOME",    "seq": 4,  "duration": 1,  "remark": ""},
    {"node_id": 5,  "node_name": "海上运输",        "role_label": "船公司(中远海运)",  "area": "SEA",     "seq": 5,  "duration": 46, "remark": "航线青岛→上海→香港→马六甲→Colombo→CapeTown→巴西Sepetiba，直航36-40天"},
    {"node_id": 6,  "node_name": "到港换单",        "role_label": "境外代理/船代",    "area": "OVERSEA", "seq": 6,  "duration": 1,  "remark": "提前7个工作日核对正本提单副本"},
    {"node_id": 7,  "node_name": "进口许可LI",      "role_label": "境外代理/进口商",  "area": "OVERSEA", "seq": 7,  "duration": 1,  "remark": "Siscomex系统申请，须装运前完成否则罚款30%CIF"},
    {"node_id": 8,  "node_name": "进口清关申报",     "role_label": "境外代理/报关行",  "area": "OVERSEA", "seq": 8,  "duration": 3,  "remark": "需3-7工作日；清关前付清II/IPI/PIS/COFINS/ICMS"},
    {"node_id": 9,  "node_name": "海关查验/放行",   "role_label": "海关/境外代理",    "area": "OVERSEA", "seq": 9,  "duration": 1,  "remark": "绿/黄/红/灰四通道；不可预清关，须货到后清关"},
    {"node_id": 10, "node_name": "临时堆存费",      "role_label": "境外代理/码头",    "area": "OVERSEA", "seq": 10, "duration": 1,  "remark": "堆存费收据、放行通知单"},
    {"node_id": 11, "node_name": "境外内陆运输",     "role_label": "境外代理/运输公司", "area": "OVERSEA", "seq": 11, "duration": 2,  "remark": "5轴半挂车，200hp以上MACK/MAN重型牵引车"},
    {"node_id": 12, "node_name": "工地交付",        "role_label": "收货人/项目组",    "area": "OVERSEA", "seq": 12, "duration": 1,  "remark": "签收单、随车单据"},
]


def _parse(s):
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))


def _fmt(dt):
    return dt.strftime("%Y-%m-%d")


def generate_schedule(etd_str, eta_str, durations):
    """排程算法（境内串行→海运→境外串行），返回 {node_id: (start_str, end_str)}"""
    etd = _parse(etd_str)
    eta = _parse(eta_str)
    if eta <= etd:
        raise ValueError("ETA 必须晚于 ETD 至少 1 天")

    plan = {}

    dome = [n for n in DEMO_NODES if n["area"] == "DOME"]
    cursor = etd
    for n in reversed(dome):
        end = cursor
        start = cursor - timedelta(days=n["duration"])
        plan[n["node_id"]] = (_fmt(start), _fmt(end))
        cursor = start

    sea = [n for n in DEMO_NODES if n["area"] == "SEA"][0]
    plan[sea["node_id"]] = (_fmt(etd), _fmt(eta))

    oversea = [n for n in DEMO_NODES if n["area"] == "OVERSEA"]
    cursor = eta
    for n in oversea:
        start = cursor
        end = cursor + timedelta(days=n["duration"])
        plan[n["node_id"]] = (_fmt(start), _fmt(end))
        cursor = end

    return plan


def get_demo_schedule():
    """返回 demo 项目的完整排程结果，用于验证和 UI 渲染"""
    return generate_schedule(DEMO_PROJECT["etd"], DEMO_PROJECT["eta"], DEMO_PROJECT["durations"])


def get_demo_project_full():
    """返回完整的 demo 项目数据（project + nodes + schedule），可直接灌入 UI 或 DB"""
    schedule = get_demo_schedule()
    nodes = []
    for n in DEMO_NODES:
        s, e = schedule[n["node_id"]]
        nodes.append({
            **n,
            "plan_start": s,
            "plan_end": e,
            "default_duration": n["duration"],
            "is_delayed": 0,
            "delay_days": 0,
            "status": "Pending",
            "actual_completion_date": None,
        })
    return {**DEMO_PROJECT, "nodes": nodes}


if __name__ == "__main__":
    # 验证排程结果
    plan = get_demo_schedule()
    print(f"项目: {DEMO_PROJECT['project_name']}")
    print(f"出口港: 青岛港 (QD) | ETD: {DEMO_PROJECT['etd']} | ETA: {DEMO_PROJECT['eta']}")
    print("-" * 70)
    for n in DEMO_NODES:
        s, e = plan[n["node_id"]]
        dur = n["duration"]
        print(f"  节点{n['node_id']:2d}  {n['node_name']:<12s}  {s} → {e}  ({dur}天)  [{n['role_label']}]")
    print("-" * 70)
    print(f"总工期: {DEMO_NODES[0]['node_name']} ~ {DEMO_NODES[-1]['node_name']}")
    first = _parse(plan[1][0])
    last = _parse(plan[12][1])
    print(f"起止: {plan[1][0]} ~ {plan[12][1]}  共 {(last - first).days} 天")
