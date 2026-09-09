"""
港口地图定位组件（PortMapView）
================================
纯 QPainter 自绘：真实中国轮廓（GeoJSON → pyproj Lambert 等角圆锥投影）+
港口点经同一投影精确定位；港口群着色、选中高亮、悬浮 Tooltip、群筛选灰显。

坐标流：WGS84 经纬度 → Lambert 平面米 → 画布仿射（ui/map_geo.py）。
轮廓数据：data/china_outline.json（DataV GeoAtlas 全国面，含南海诸岛）。
港口坐标读自 services/ports_cn.py（唯一权威），组件内不重复维护。

依赖/数据缺失时自动降级：矩形陆地示意 + 线性投影，界面不中断。
"""

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ui import map_geo

# 港口群配色（与设计文档一致）
GROUP_COLORS = {
    "环渤海港口群": QColor("#2196F3"),
    "长三角港口群": QColor("#FFC107"),
    "东南沿海港口群": QColor("#4CAF50"),
    "粤港澳(大陆)": QColor("#F44336"),
    "环北部湾港口群": QColor("#9C27B0"),
}
SELECT_COLOR = QColor("#D32F2F")

SEA_COLOR = QColor("#E6EEF6")
LAND_COLOR = QColor("#F3EADA")
COAST_COLOR = QColor("#CFD8E0")

# 聚焦海岸线时的最小视口跨度（米，约 16° 纬度），防止少港口时过度放大
MIN_SPAN_M = 1_800_000

# 降级用的线性投影范围（东经 / 北纬，仅依赖缺失时启用）
LON_MIN, LON_MAX = 105.0, 126.0
LAT_MIN, LAT_MAX = 17.0, 42.0


class PortMapView(QWidget):
    """沿海港口概览地图。API：set_ports / select / set_group / clear_lines。"""

    port_selected = Signal(str)   # 携带 key
    group_selected = Signal(str)  # 携带群名

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries = []        # [{key,name,group,lat,lon}]
        self._selected = None     # key
        self._group_filter = None # 群名 or None
        self._hover = None        # key
        self._outline = None      # map_geo 结果（懒加载）
        self._transform = None    # 当前视口仿射 QTransform
        self.setFixedHeight(220)
        self.setMouseTracking(True)
        self.setMinimumWidth(260)

    # ── 数据入口 ──

    def set_ports(self, entries):
        """
        entries: [{key, name, group, name_en, lat, lon}]
        组件仅读取 key/name/group/lat/lon。
        """
        self._entries = []
        for e in entries:
            if e.get("lat") is None or e.get("lon") is None:
                continue
            self._entries.append({
                "key": e["key"], "name": e["name"], "group": e.get("group", ""),
                "lat": float(e["lat"]), "lon": float(e["lon"]),
            })
        self.update()

    def select(self, key):
        self._selected = key
        self.update()

    def set_group(self, group):
        self._group_filter = group
        self.update()

    def clear_group(self):
        self._group_filter = None
        self.update()

    def clear_selection(self):
        self._selected = None
        self.update()

    # ── 投影 ──

    def _margin(self):
        return max(12, min(self.width(), self.height()) // 18)

    def _coast_bbox(self):
        """聚焦海岸线：港口点平面坐标包围盒 + 15% 边距，并设最小视口。

        无港口或投影失败返回 None（回退全中国视角）。
        """
        if not self._entries:
            return None
        xs, ys = [], []
        for e in self._entries:
            pt = map_geo.to_plane(e["lon"], e["lat"])
            if pt is None:
                return None
            xs.append(pt[0])
            ys.append(pt[1])
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        sx = max(x1 - x0, MIN_SPAN_M)
        sy = max(y1 - y0, MIN_SPAN_M)
        pad = 1.15
        return (cx - sx * pad / 2, cy - sy * pad / 2,
                cx + sx * pad / 2, cy + sy * pad / 2)

    def _rebuild_transform(self):
        """按当前视口尺寸重算仿射；轮廓缺失时置 None 走降级路径。"""
        if self._outline is None:
            self._outline = map_geo.load_outline()
        if self._outline is None:
            self._transform = None
            return
        bbox = self._coast_bbox() or self._outline["bbox"]
        self._transform = map_geo.fit_transform(
            bbox, self.width(), self.height(), self._margin())

    def _to_pt(self, lon, lat):
        if self._transform is None:
            # 降级：线性投影
            m = self._margin()
            w = self.width() - 2 * m
            h = self.height() - 2 * m
            x = m + (lon - LON_MIN) / (LON_MAX - LON_MIN) * w
            y = m + (LAT_MAX - lat) / (LAT_MAX - LAT_MIN) * h
            return QPointF(x, y)
        pt = map_geo.to_plane(lon, lat)
        if pt is None:
            return QPointF(-1e9, -1e9)
        return self._transform.map(QPointF(*pt))

    def _entry_by_key(self, key):
        for e in self._entries:
            if e["key"] == str(key).upper():
                return e
        for e in self._entries:
            if e["key"] == key:
                return e
        return None

    # ── 交互 ──

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        found = None
        for e in self._entries:
            p = self._to_pt(e["lon"], e["lat"])
            if (p.x() - pos.x()) ** 2 + (p.y() - pos.y()) ** 2 <= 12 ** 2:
                found = e
                break
        if found != (self._hover and self._entry_by_key(self._hover)):
            self._hover = found["key"] if found else None
            self.setToolTip(f"{found['name']} · {found['group']}" if found else "")
            self.update()

    def mouseReleaseEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        pos = ev.position()
        for e in self._entries:
            p = self._to_pt(e["lon"], e["lat"])
            if (p.x() - pos.x()) ** 2 + (p.y() - pos.y()) ** 2 <= 12 ** 2:
                if self._group_filter and e["group"] != self._group_filter:
                    continue
                self._selected = e["key"]
                self.port_selected.emit(e["key"])
                self.update()
                return

    # ── 绘制 ──

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        # 海洋背景
        p.fillRect(self.rect(), SEA_COLOR)

        self._rebuild_transform()
        if self._transform is not None and not self._transform.isIdentity():
            # 真实轮廓：平面坐标 path + 仿射变换（零逐点计算）
            p.setTransform(self._transform)
            p.setPen(QPen(COAST_COLOR, 1.0))
            p.setBrush(QBrush(LAND_COLOR))
            p.drawPath(self._outline["path"])
            p.resetTransform()
        else:
            # 降级：矩形陆地示意
            m = self._margin()
            p.fillRect(QRectF(m * 0.4, m * 0.4,
                              self.width() * 0.32, self.height() - m * 0.8),
                       LAND_COLOR)

        for e in self._entries:
            self._draw_point(p, e)

    def _draw_point(self, p, e):
        pos = self._to_pt(e["lon"], e["lat"])
        group_color = GROUP_COLORS.get(e["group"], QColor("#90A4AE"))
        selected = (self._selected == e["key"])
        filtered_out = (self._group_filter is not None and e["group"] != self._group_filter)
        hover = (self._hover == e["key"])

        if selected:
            radius = 6
            color = SELECT_COLOR
            # 呼吸灯光晕
            halo = QColor(SELECT_COLOR)
            halo.setAlpha(60)
            p.setBrush(QBrush(halo))
            p.setPen(Qt.NoPen)
            p.drawEllipse(pos, radius + 5, radius + 5)
            alpha = 255
        else:
            radius = (5 if hover else 3)
            alpha = 40 if filtered_out else (150 if self._selected or self._group_filter else 255)
            color = group_color if not filtered_out else group_color
            if filtered_out:
                color = group_color.lighter(160)
        c = QColor(color)
        c.setAlpha(alpha)
        p.setBrush(QBrush(c))
        p.setPen(QPen(QColor("#FFFFFF"), 1))
        p.drawEllipse(pos, radius, radius)

        # 选中点名称标签
        if selected:
            p.setPen(QPen(QColor("#333333"), 1))
            p.drawText(QPointF(pos.x() - 4, pos.y() - 8), e["name"])
