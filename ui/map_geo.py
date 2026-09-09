"""
中国地图几何数据：GeoJSON 轮廓 + pyproj Lambert 投影 + QPainterPath 缓存。
==========================================================================
坐标流：WGS84 经纬度 (EPSG:4326) → Lambert 等角圆锥（中国定制参数）
        平面米 → 画布像素（仿射变换，保持纵横比并居中）。
轮廓数据：data/china_outline.json（DataV GeoAtlas 全国面，含南海诸岛）。
pyproj 或数据文件缺失时自动降级：load_outline/to_plane 返回 None，
由调用方回退到线性投影，保证界面不中断。
"""

import json
import os

from pyproj import CRS
from PySide6.QtCore import QPointF
from PySide6.QtGui import QPainterPath, QPolygonF, QTransform

try:
    import pyproj
    _HAS_PYPROJ = True
except ImportError:  # pragma: no cover
    pyproj = None
    CRS = None
    _HAS_PYPROJ = False

DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "china_outline.json")

SRC_CRS = "EPSG:4326"  # WGS84 经纬度
# Lambert 等角圆锥（中国定制）：标准纬线 25N/47N，中央经线 105E，原点 25N。
# 中国全境单调：东 x 大、北 y 大，便于画布直映。
DST_CRS = CRS.from_proj4(
    "+proj=lcc +lat_0=25 +lon_0=105 +lat_1=25 +lat_2=47 "
    "+datum=WGS84 +units=m +no_defs")

_transformer = None
_cache = None


def _tr():
    global _transformer
    if _transformer is None:
        _transformer = pyproj.Transformer.from_crs(
            SRC_CRS, DST_CRS, always_xy=True)
    return _transformer


def load_outline(path=None):
    """加载并投影中国轮廓。

    返回 {"path": QPainterPath(Albers 平面坐标), "bbox": (x0,y0,x1,y1)}，
    或 None（依赖/数据缺失）。结果全局缓存，绘制时配 QTransform 直接使用。
    """
    global _cache
    if _cache is not None:
        return _cache
    if not _HAS_PYPROJ:
        return None
    try:
        with open(path or DATA_PATH, encoding="utf-8") as f:
            gj = json.load(f)
    except (OSError, ValueError):
        return None
    try:
        tr = _tr()
    except Exception:
        return None

    rings = []
    for feat in gj.get("features", []):
        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        if gtype == "Polygon":
            polys = [geom.get("coordinates") or []]
        elif gtype == "MultiPolygon":
            polys = geom.get("coordinates") or []
        else:
            continue
        for poly in polys:
            for ring in poly:
                rings.append([tr.transform(lon, lat) for lon, lat in ring])
    if not rings:
        return None

    path = QPainterPath()
    for ring in rings:
        path.addPolygon(QPolygonF([QPointF(x, y) for x, y in ring]))
    xs = [p[0] for ring in rings for p in ring]
    ys = [p[1] for ring in rings for p in ring]
    _cache = {"path": path, "bbox": (min(xs), min(ys), max(xs), max(ys))}
    return _cache


def fit_transform(bbox, width, height, margin):
    """平面坐标范围 → 画布像素的仿射变换。

    保持纵横比、居中；平面 y 向北，画布 y 向下（负缩放 + 偏移实现翻转）。
    """
    x0, y0, x1, y1 = bbox
    bw, bh = x1 - x0, y1 - y0
    if bw <= 0 or bh <= 0:
        return QTransform()
    scale = min((width - 2 * margin) / bw, (height - 2 * margin) / bh)
    if scale <= 0:
        return QTransform()
    tx = (width - bw * scale) / 2.0 - x0 * scale
    ty = (height - bh * scale) / 2.0 + y1 * scale
    return QTransform(scale, 0, 0, -scale, tx, ty)


def to_plane(lon, lat):
    """单个经纬度 → Albers 平面坐标 (x, y)；依赖/转换失败返回 None。"""
    if not _HAS_PYPROJ:
        return None
    try:
        return _tr().transform(lon, lat)
    except Exception:
        return None
