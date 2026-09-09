"""
几何矢量图标工厂 · Apple 极简风
用 QPainter 绘制由圆形/圆角矩形/三角形/线段构成的几何图标，
不依赖 emoji 或外部图片，颜色随语境切换（灰 / 强调蓝）。
"""

from PySide6.QtGui import (
    QIcon, QPixmap, QPainter, QPen, QBrush, QColor, QPainterPath, QPolygonF
)
from PySide6.QtCore import QRectF, QPointF, Qt

from ui.theme import ACCENT, TEXT_SECONDARY


def _rt(x, y, w, h, r):
    """圆角矩形路径"""
    path = QPainterPath()
    path.addRoundedRect(QRectF(x, y, w, h), r, r)
    return path


def _new_painter(size):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    return p, pm


def _pen(color, width, cap=Qt.RoundCap, join=Qt.RoundJoin):
    return QPen(QColor(color), width, Qt.SolidLine, cap, join)


def pixmap(name, color=ACCENT, size=22):
    p, pm = _new_painter(size)
    s = float(size)
    c = color
    p.setBrush(QBrush(QColor(c)))
    p.setPen(Qt.NoPen)

    if name == "home":
        # 几何房屋：三角屋顶 + 圆角矩形底座
        roof = QPolygonF([QPointF(s * 0.50, s * 0.08),
                          QPointF(s * 0.08, s * 0.50),
                          QPointF(s * 0.92, s * 0.50)])
        p.drawPolygon(roof)
        p.drawPath(_rt(s * 0.24, s * 0.50, s * 0.52, s * 0.42, s * 0.06))

    elif name == "dashboard":
        # 2×2 网格：左上实心、其余描边
        cell = s * 0.335
        gap = s * 0.10
        x0, y0 = s * 0.12, s * 0.12
        for r in range(2):
            for col in range(2):
                x = x0 + col * (cell + gap)
                y = y0 + r * (cell + gap)
                path = _rt(x, y, cell, cell, s * 0.075)
                if r == 0 and col == 0:
                    p.setBrush(QBrush(QColor(c)))
                    p.setPen(Qt.NoPen)
                    p.drawPath(path)
                else:
                    p.setBrush(Qt.NoBrush)
                    p.setPen(_pen(c, s * 0.09))
                    p.drawPath(path)

    elif name == "new":
        # 十字：两条圆角矩形
        p.drawPath(_rt(s * 0.42, s * 0.10, s * 0.16, s * 0.80, s * 0.05))
        p.drawPath(_rt(s * 0.10, s * 0.42, s * 0.80, s * 0.16, s * 0.05))

    elif name == "completed":
        # 对勾：圆角折线
        path = QPainterPath()
        path.moveTo(s * 0.16, s * 0.52)
        path.lineTo(s * 0.40, s * 0.76)
        path.lineTo(s * 0.84, s * 0.26)
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(c, s * 0.14))
        p.drawPath(path)

    elif name == "box":
        # 箱体：圆角矩形 + 顶部横隔线
        p.drawPath(_rt(s * 0.08, s * 0.30, s * 0.84, s * 0.62, s * 0.10))
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen("#FFFFFF", s * 0.075))
        p.drawLine(QPointF(s * 0.08, s * 0.46), QPointF(s * 0.92, s * 0.46))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(c)))

    elif name == "folder":
        # 文件夹：圆角底座 + 顶tab
        p.drawPath(_rt(s * 0.08, s * 0.34, s * 0.84, s * 0.58, s * 0.08))
        p.drawPath(_rt(s * 0.08, s * 0.24, s * 0.34, s * 0.22, s * 0.06))

    elif name == "chevron_down":
        path = QPainterPath()
        path.moveTo(s * 0.22, s * 0.36)
        path.lineTo(s * 0.50, s * 0.64)
        path.lineTo(s * 0.78, s * 0.36)
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(c, s * 0.13))
        p.drawPath(path)

    elif name == "chevron_up":
        path = QPainterPath()
        path.moveTo(s * 0.22, s * 0.64)
        path.lineTo(s * 0.50, s * 0.36)
        path.lineTo(s * 0.78, s * 0.64)
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(c, s * 0.13))
        p.drawPath(path)

    elif name == "search":
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(c, s * 0.12))
        p.drawEllipse(QRectF(s * 0.12, s * 0.12, s * 0.58, s * 0.58))
        p.drawLine(QPointF(s * 0.62, s * 0.62), QPointF(s * 0.88, s * 0.88))

    elif name == "bell":
        p.drawPath(_rt(s * 0.28, s * 0.14, s * 0.44, s * 0.12, s * 0.05))
        path = QPainterPath()
        path.moveTo(s * 0.20, s * 0.40)
        path.lineTo(s * 0.20, s * 0.64)
        path.lineTo(s * 0.15, s * 0.74)
        path.lineTo(s * 0.85, s * 0.74)
        path.lineTo(s * 0.80, s * 0.64)
        path.lineTo(s * 0.80, s * 0.40)
        path.arcTo(QRectF(s * 0.20, s * 0.14, s * 0.60, s * 0.52), -90, 180)
        path.lineTo(s * 0.20, s * 0.40)
        p.drawPath(path)

    elif name == "calendar":
        p.drawPath(_rt(s * 0.10, s * 0.20, s * 0.80, s * 0.70, s * 0.08))
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen("#FFFFFF", s * 0.08))
        p.drawLine(QPointF(s * 0.10, s * 0.42), QPointF(s * 0.90, s * 0.42))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(c)))
        p.drawPath(_rt(s * 0.30, s * 0.06, s * 0.10, s * 0.22, s * 0.03))
        p.drawPath(_rt(s * 0.60, s * 0.06, s * 0.10, s * 0.22, s * 0.03))

    elif name == "doc":
        p.drawPath(_rt(s * 0.24, s * 0.08, s * 0.52, s * 0.84, s * 0.07))
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen("#FFFFFF", s * 0.075))
        p.drawLine(QPointF(s * 0.32, s * 0.30), QPointF(s * 0.68, s * 0.30))
        p.drawLine(QPointF(s * 0.32, s * 0.46), QPointF(s * 0.68, s * 0.46))
        p.drawLine(QPointF(s * 0.32, s * 0.62), QPointF(s * 0.58, s * 0.62))

    elif name == "route":
        # 跟踪路径：三点 + 首尾箭头
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(c, s * 0.10))
        p.drawLine(QPointF(s * 0.12, s * 0.50), QPointF(s * 0.88, s * 0.50))
        p.setBrush(QBrush(QColor(c)))
        p.setPen(Qt.NoPen)
        for fx in (0.12, 0.50):
            p.drawEllipse(QPointF(s * fx, s * 0.50), s * 0.10, s * 0.10)
        arrow = QPolygonF([QPointF(s * 0.78, s * 0.30),
                           QPointF(s * 0.96, s * 0.50),
                           QPointF(s * 0.78, s * 0.70)])
        p.drawPolygon(arrow)

    elif name == "undo":
        # 撤销：顶部开口的环形箭头（逆时针回退）
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(c, s * 0.11))
        p.drawArc(QRectF(s * 0.20, s * 0.22, s * 0.62, s * 0.56), 130 * 16, 300 * 16)
        # 环末箭头（约 70° 处），指向左上（回退方向）
        p.setBrush(QBrush(QColor(c)))
        p.setPen(Qt.NoPen)
        head = QPolygonF([QPointF(s * 0.70, s * 0.26),
                          QPointF(s * 0.46, s * 0.34),
                          QPointF(s * 0.68, s * 0.48)])
        p.drawPolygon(head)

    elif name == "clock_hist":
        # 时钟 + 回拨小箭头（位移历史）
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(c, s * 0.11))
        p.drawEllipse(QRectF(s * 0.12, s * 0.12, s * 0.76, s * 0.76))
        p.setPen(_pen(c, s * 0.11))
        p.drawLine(QPointF(s * 0.50, s * 0.50), QPointF(s * 0.50, s * 0.30))
        p.drawLine(QPointF(s * 0.50, s * 0.50), QPointF(s * 0.70, s * 0.58))

    elif name == "anchor":
        # 锚：顶部圆环 + 中轴 + 横杆 + 底部弯钩
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(c, s * 0.10))
        p.drawEllipse(QRectF(s * 0.38, s * 0.10, s * 0.24, s * 0.24))
        p.drawLine(QPointF(s * 0.50, s * 0.34), QPointF(s * 0.50, s * 0.86))
        p.drawLine(QPointF(s * 0.24, s * 0.54), QPointF(s * 0.76, s * 0.54))
        path = QPainterPath()
        path.arcTo(QRectF(s * 0.22, s * 0.56, s * 0.56, s * 0.56), 180, 180)
        p.drawPath(path)

    elif name == "note":
        # 备注文档：圆角矩形 + 两条横线 + 右下圆点标记
        p.drawPath(_rt(s * 0.16, s * 0.10, s * 0.68, s * 0.80, s * 0.08))
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen("#FFFFFF", s * 0.075))
        p.drawLine(QPointF(s * 0.26, s * 0.32), QPointF(s * 0.74, s * 0.32))
        p.drawLine(QPointF(s * 0.26, s * 0.50), QPointF(s * 0.60, s * 0.50))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(c)))
        p.drawEllipse(QPointF(s * 0.70, s * 0.72), s * 0.10, s * 0.10)

    elif name == "pin":
        # 定位针：实心圆头 + 三角尾
        p.drawEllipse(QRectF(s * 0.26, s * 0.10, s * 0.48, s * 0.48))
        tri = QPolygonF([QPointF(s * 0.20, s * 0.54),
                         QPointF(s * 0.80, s * 0.54),
                         QPointF(s * 0.50, s * 0.92)])
        p.drawPolygon(tri)

    elif name == "crane":
        # 浮吊：A 型门架 + 吊索 + 吊钩
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(c, s * 0.09))
        p.drawLine(QPointF(s * 0.26, s * 0.88), QPointF(s * 0.50, s * 0.18))
        p.drawLine(QPointF(s * 0.74, s * 0.88), QPointF(s * 0.50, s * 0.18))
        p.drawLine(QPointF(s * 0.34, s * 0.36), QPointF(s * 0.66, s * 0.36))
        p.drawLine(QPointF(s * 0.50, s * 0.18), QPointF(s * 0.50, s * 0.58))
        p.drawArc(QRectF(s * 0.38, s * 0.56, s * 0.24, s * 0.26), 0, 180)

    elif name == "wave":
        # 波浪：两道起伏曲线（航道吃水）
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(c, s * 0.10))
        path = QPainterPath()
        path.moveTo(s * 0.10, s * 0.36)
        path.cubicTo(s * 0.20, s * 0.26, s * 0.30, s * 0.26, s * 0.40, s * 0.36)
        path.cubicTo(s * 0.50, s * 0.46, s * 0.60, s * 0.46, s * 0.70, s * 0.36)
        path.cubicTo(s * 0.78, s * 0.28, s * 0.86, s * 0.28, s * 0.90, s * 0.36)
        p.drawPath(path)
        path = QPainterPath()
        path.moveTo(s * 0.10, s * 0.66)
        path.cubicTo(s * 0.20, s * 0.56, s * 0.30, s * 0.56, s * 0.40, s * 0.66)
        path.cubicTo(s * 0.50, s * 0.76, s * 0.60, s * 0.76, s * 0.70, s * 0.66)
        path.cubicTo(s * 0.78, s * 0.58, s * 0.86, s * 0.58, s * 0.90, s * 0.66)
        p.drawPath(path)

    elif name == "warehouse":
        # 库场：三角屋顶 + 矩形仓体 + 门洞
        roof = QPolygonF([QPointF(s * 0.14, s * 0.42),
                          QPointF(s * 0.50, s * 0.12),
                          QPointF(s * 0.86, s * 0.42)])
        p.drawPolygon(roof)
        p.drawPath(_rt(s * 0.14, s * 0.42, s * 0.72, s * 0.46, s * 0.05))
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen("#FFFFFF", s * 0.07))
        p.drawRect(QRectF(s * 0.42, s * 0.60, s * 0.16, s * 0.28))

    elif name == "compass":
        # 罗盘：外圆 + 上实下空的菱形指针
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(c, s * 0.10))
        p.drawEllipse(QRectF(s * 0.10, s * 0.10, s * 0.80, s * 0.80))
        p.setBrush(QBrush(QColor(c)))
        p.setPen(Qt.NoPen)
        top = QPolygonF([QPointF(s * 0.50, s * 0.20),
                         QPointF(s * 0.72, s * 0.50),
                         QPointF(s * 0.50, s * 0.50)])
        p.drawPolygon(top)
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(c, s * 0.08))
        bot = QPolygonF([QPointF(s * 0.50, s * 0.80),
                         QPointF(s * 0.28, s * 0.50),
                         QPointF(s * 0.50, s * 0.50)])
        p.drawPolygon(bot)

    elif name == "coord":
        # 经纬度：圆 + 十字准星
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(c, s * 0.09))
        p.drawEllipse(QRectF(s * 0.10, s * 0.10, s * 0.80, s * 0.80))
        p.drawLine(QPointF(s * 0.10, s * 0.50), QPointF(s * 0.90, s * 0.50))
        p.drawLine(QPointF(s * 0.50, s * 0.10), QPointF(s * 0.50, s * 0.90))

    elif name == "tide":
        # 潮汐：上弦月 + 波浪线
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(c, s * 0.09))
        path = QPainterPath()
        path.arcTo(QRectF(s * 0.28, s * 0.10, s * 0.44, s * 0.44), 135, 270)
        p.drawPath(path)
        path = QPainterPath()
        path.moveTo(s * 0.12, s * 0.72)
        path.cubicTo(s * 0.22, s * 0.64, s * 0.32, s * 0.64, s * 0.42, s * 0.72)
        path.cubicTo(s * 0.52, s * 0.80, s * 0.62, s * 0.80, s * 0.72, s * 0.72)
        path.cubicTo(s * 0.80, s * 0.66, s * 0.86, s * 0.66, s * 0.90, s * 0.72)
        p.drawPath(path)

    elif name == "info":
        # 信息：圆 + 点 + 竖（"i"）
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(c, s * 0.10))
        p.drawEllipse(QRectF(s * 0.10, s * 0.10, s * 0.80, s * 0.80))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(c)))
        p.drawEllipse(QPointF(s * 0.50, s * 0.36), s * 0.07, s * 0.07)
        p.drawPath(_rt(s * 0.46, s * 0.52, s * 0.08, s * 0.24, s * 0.04))

    elif name == "warn":
        # 警告：三角 + 白色感叹号
        tri = QPolygonF([QPointF(s * 0.50, s * 0.10),
                         QPointF(s * 0.92, s * 0.86),
                         QPointF(s * 0.08, s * 0.86)])
        p.drawPolygon(tri)
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen("#FFFFFF", s * 0.10))
        p.drawLine(QPointF(s * 0.50, s * 0.32), QPointF(s * 0.50, s * 0.60))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush("#FFFFFF"))
        p.drawEllipse(QPointF(s * 0.50, s * 0.74), s * 0.06, s * 0.06)

    elif name == "close":
        # 关闭：两条交叉圆头线
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(c, s * 0.12))
        p.drawLine(QPointF(s * 0.22, s * 0.22), QPointF(s * 0.78, s * 0.78))
        p.drawLine(QPointF(s * 0.78, s * 0.22), QPointF(s * 0.22, s * 0.78))

    elif name == "chevron_right":
        path = QPainterPath()
        path.moveTo(s * 0.36, s * 0.22)
        path.lineTo(s * 0.64, s * 0.50)
        path.lineTo(s * 0.36, s * 0.78)
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(c, s * 0.13))
        p.drawPath(path)

    else:  # 默认圆点
        p.drawEllipse(QRectF(s * 0.2, s * 0.2, s * 0.6, s * 0.6))

    p.end()
    return pm


def icon(name, color=ACCENT, size=22):
    return QIcon(pixmap(name, color, size))


def tile_pixmap(kind, color=ACCENT, bg="#EAF3FF", size=44):
    """圆角浅底 + 几何图形，用作卡片/入口图标"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setBrush(QBrush(QColor(bg)))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.30, size * 0.30)
    glyph = pixmap(kind, color, int(size * 0.60))
    off = int((size - glyph.width()) / 2)
    p.drawPixmap(off, off, glyph)
    p.end()
    return pm


def tile_icon(kind, color=ACCENT, bg="#EAF3FF", size=44):
    return QIcon(tile_pixmap(kind, color, bg, size))


def glyph_pixmap(kind, color=TEXT_SECONDARY, size=18):
    """别名，供外部统一调用"""
    return pixmap(kind, color, size)