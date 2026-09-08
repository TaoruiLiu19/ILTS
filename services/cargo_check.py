"""
货物超限判定（优化方案 D1 §2.1）—— 呼应巴西模板节点3「捆扎固定」吊装约束

规则（可调阈值，默认与 config 模板备注一致）：
  · 单件毛重 > 100 t            → 超重（吊装能力预警，对应 吊机≥100t）
  · 单件任一外型尺寸 > 36 m     → 超长/超宽/超高（对应 臂长≥36m）
  · 手动勾选 od_type            → 视为超限（超宽/超高/超长/超重 明细）
"""

OVER_WEIGHT_KG = 100_000.0     # 100t
OVER_ARM_M = 36.0              # 36m


def item_over_types(item):
    """返回该货项命中的超限类型列表（空 = 未超限）"""
    types = []
    weight = _f(item.get("weight_kg"))
    if weight and weight > OVER_WEIGHT_KG:
        types.append("超重")
    dims = [d for d in (_f(item.get("dim_l")), _f(item.get("dim_w")),
                        _f(item.get("dim_h"))) if d]
    if any(d > OVER_ARM_M for d in dims):
        long_dim = max(dims)
        if long_dim > OVER_ARM_M:
            which = ("超长" if item.get("dim_l") and _f(item.get("dim_l")) == long_dim
                     else "超高" if item.get("dim_h") and _f(item.get("dim_h")) == long_dim
                     else "超宽")
            if which not in types:
                types.append(which)
    od = (item.get("od_type") or "").strip()
    if od:
        for t in od.replace("/", ",").replace("、", ",").split(","):
            t = t.strip()
            if t and t not in types:
                types.append(t)
    return types


def is_over(item):
    return bool(item_over_types(item))


def normalize_item(item):
    """保存前规整：自动判定 over_flag / od_type / gross_m3 缺省"""
    item = dict(item)
    item["over_flag"] = 1 if is_over(item) else 0
    if not item.get("od_type"):
        types = item_over_types(item)
        item["od_type"] = "、".join(types) if types else None
    if not item.get("gross_m3"):
        dims = [_f(item.get(k)) for k in ("dim_l", "dim_w", "dim_h")]
        weight = _f(item.get("weight_kg"))
        if all(dims) and weight:
            item["gross_m3"] = round(dims[0] * dims[1] * dims[2] * item.get("qty", 1), 3)
    return item


def summary(items):
    """返回台账统计 {count, over, over_list, total_weight_kg}"""
    over_list = []
    total_w = 0.0
    for it in items:
        w = _f(it.get("weight_kg")) or 0
        total_w += w * (it.get("qty") or 1)
        if is_over(it):
            over_list.append(it)
    return {"count": len(items), "over": len(over_list),
            "over_list": over_list, "total_weight_kg": round(total_w, 1)}


def _f(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
