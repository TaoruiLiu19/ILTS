"""
UI 视觉规范 · Apple 白色极简风
设计原则：
  · 浅灰底白卡片、大量留白、统一大圆角
  · 单一蓝色强调色，状态色克制
  · 几何矢量图标（见 ui/icons.py）
  · 降低信息密度，弱化边框、弱化噪声
"""

import os

from PySide6.QtGui import QColor, QPixmap, QPainter, QPen, QPainterPath
from PySide6.QtCore import Qt as _Qt, QPointF
from PySide6.QtWidgets import QGraphicsDropShadowEffect

# ── 生成复选框对勾资源（风格化后排除了原生勾，需自绘；延迟到 QApplication 就绪） ──
_ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
os.makedirs(_ASSET_DIR, exist_ok=True)
_CHECK_PATH = os.path.join(_ASSET_DIR, "check.png").replace("\\", "/")


def ensure_check_asset():
    """QPainter 需要 GUI 环境，须在 QApplication 创建后调用"""
    pm = QPixmap(20, 20)
    pm.fill(_Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(QPen(QColor("#FFFFFF"), 2.6, _Qt.SolidLine, _Qt.RoundCap, _Qt.RoundJoin))
    p.setBrush(_Qt.NoBrush)
    check = QPainterPath()
    check.moveTo(5.5, 10.5)
    check.lineTo(9.0, 14.0)
    check.lineTo(15.0, 6.5)
    p.drawPath(check)
    p.end()
    pm.save(os.path.join(_ASSET_DIR, "check.png"))

# ── 基础色板 ──
BG             = "#F5F5F7"   # 窗口背景 · 苹果浅灰
CARD           = "#FFFFFF"
BORDER         = "#E5E5EA"   # 细分隔线 / 卡片描边
HAIRLINE       = "#F0F0F2"

TEXT_PRIMARY   = "#1D1D1F"
TEXT_SECONDARY = "#6E6E73"
TEXT_TERTIARY  = "#AEAEB2"

ACCENT         = "#007AFF"   # 主强调 · Apple 蓝
ACCENT_SOFT    = "#EAF3FF"   # 蓝色浅底
ACCENT_DEEP    = "#0A84FF"   # hover 蓝

GREEN          = "#34C759"
RED            = "#FF3B30"
ORANGE         = "#FF9500"
YELLOW         = "#FFCC00"
GRAY           = "#C7C7CC"
GRAY_SOFT      = "#F2F2F7"

# ── 圆角 ──
RADIUS_SM   = 8
RADIUS_MD   = 12
RADIUS_LG   = 16
RADIUS_PILL = 20

# ── 三区背景色（柔和淡彩，用于甘特/迷你条） ──
AREA_COLORS = {
    "DOME":    "#EAF0FF",   # 境内 · 淡蓝
    "SEA":     "#DFF0FF",   # 海运 · 淡蓝
    "OVERSEA": "#FFF3E2",   # 境外 · 淡杏
}
AREA_TEXT = {
    "DOME": "境内",
    "SEA": "海运",
    "OVERSEA": "境外",
}
AREA_FG = {
    "DOME":    "#5A78D8",
    "SEA":     "#3E8FD8",
    "OVERSEA": "#C9822B",
}

# ── 节点状态颜色 ──
NODE_STATUS_COLORS = {
    "Pending": GRAY,      # 待开始
    "Active":  ACCENT,    # 进行中
    "Overdue": RED,       # 逾期
    "Done":    GREEN,     # 已完成
}
NODE_STATUS_TEXT = {
    "Pending": "待开始",
    "Active": "进行中",
    "Overdue": "逾期",
    "Done": "已完成",
}

# ── 泳道角色配色（几何圆点，柔和克制的中间调） ──
ROLE_COLORS = {
    "报关行/货代":       "#7AA5D9",
    "货代/港口":        "#6FB98C",
    "码头/理货":        "#6FB98C",
    "船公司/码头":       "#5FB8CE",
    "船公司(中远海运)":   "#5FB8CE",
    "海关/境外代理":      "#E0A75C",
    "境外代理/船代":      "#A98FC9",
    "境外代理/进口商":    "#A98FC9",
    "境外代理/报关行":    "#A98FC9",
    "境外代理/码头":      "#A98FC9",
    "境外代理/运输公司":   "#A98FC9",
    "收货人/项目组":      "#E08FA0",
}


def get_role_color(role):
    return ROLE_COLORS.get(role, "#B8B8BE")


# ── 文件状态色 ──
FILE_STATUS_COLORS = {
    "pending_idle":  GRAY,    # 待准备
    "pending_near":  ORANGE,  # 临近
    "pending_urgent": RED,    # 节点进行中仍缺
    "pending_late":  RED,     # 超建议日
    "submitted":     GREEN,   # 已提交
}


def card_shadow(widget, blur=20, dy=5, alpha=24):
    """给卡片加柔和的投影，营造浮起质感"""
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, dy)
    effect.setColor(QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(effect)
    return effect


# ── 全局 QSS ──
GLOBAL_QSS = f"""
QMainWindow {{
    background: {BG};
}}

* {{
    outline: none;
}}

/* ── 侧栏：纯白，胶囊高亮 ── */
#sidebar {{
    background: {CARD};
    border-right: 1px solid {BORDER};
}}
#logoText {{
    color: {TEXT_PRIMARY};
    font-size: 17px;
    font-weight: 600;
}}
#logoSub {{
    color: {TEXT_TERTIARY};
    font-size: 11px;
}}
#sidebar QPushButton {{
    color: {TEXT_SECONDARY};
    text-align: left;
    padding: 11px 16px;
    border: none;
    border-radius: 12px;
    font-size: 14px;
    font-weight: 400;
}}
#sidebar QPushButton:hover {{
    background: {GRAY_SOFT};
    color: {TEXT_PRIMARY};
}}
#sidebar QPushButton:checked {{
    background: {ACCENT_SOFT};
    color: {ACCENT};
    font-weight: 600;
}}

/* ── 顶栏 ── */
#topbar {{
    background: #FBFBFD;
    border-bottom: 1px solid {BORDER};
}}
#topbarTitle {{
    font-size: 17px;
    font-weight: 600;
    color: {TEXT_PRIMARY};
}}

QStackedWidget {{ background: {BG}; }}
QScrollArea {{ border: none; background: transparent; }}

/* ── 卡片 ── */
QFrame#card {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_LG}px;
}}
QFrame#card:hover {{ border: 1px solid #D4E4F9; }}

/* ── 文本 ── */
QLabel {{ font-size: 14px; color: {TEXT_PRIMARY}; background: transparent; }}
QLabel#title {{
    font-size: 26px;
    font-weight: 600;
    color: {TEXT_PRIMARY};
}}
QLabel#subtitle {{ font-size: 14px; color: {TEXT_SECONDARY}; }}
QLabel#section {{
    font-size: 15px;
    font-weight: 600;
    color: {TEXT_PRIMARY};
    padding-top: 6px;
}}
QLabel#empty {{ font-size: 15px; color: {TEXT_TERTIARY}; padding: 40px; }}

/* ── 输入控件 ── */
QLineEdit, QDateEdit, QComboBox, QSpinBox {{
    padding: 9px 12px;
    border: 1px solid {BORDER};
    border-radius: 10px;
    background: {CARD};
    font-size: 14px;
    color: {TEXT_PRIMARY};
    selection-background-color: {ACCENT};
    selection-color: #FFFFFF;
}}
QLineEdit:hover, QDateEdit:hover, QComboBox:hover, QSpinBox:hover {{
    border: 1px solid #C7C7CC;
}}
QLineEdit:focus, QDateEdit:focus, QComboBox:focus, QSpinBox:focus {{
    border: 2px solid {ACCENT};
    padding: 8px 11px;
}}
QComboBox::drop-down {{
    border: none;
    width: 26px;
}}
QComboBox QAbstractItemView {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 10px;
    selection-background-color: {ACCENT_SOFT};
    selection-color: {TEXT_PRIMARY};
    padding: 4px;
}}
QDateEdit::drop-down {{
    border: none;
    width: 26px;
    border-top-right-radius: 10px;
    border-bottom-right-radius: 10px;
}}
QSpinBox::up-button, QSpinBox::down-button {{
    border: none;
    width: 18px;
}}

/* ── 按钮 ── */
QPushButton#primary {{
    background: {ACCENT};
    color: #FFFFFF;
    border: none;
    padding: 10px 24px;
    border-radius: 12px;
    font-size: 14px;
    font-weight: 600;
}}
QPushButton#primary:hover {{ background: {ACCENT_DEEP}; }}
QPushButton#primary:pressed {{ background: #0066CC; }}

QPushButton#secondary {{
    background: {GRAY_SOFT};
    color: {TEXT_PRIMARY};
    border: none;
    padding: 9px 20px;
    border-radius: 12px;
    font-size: 14px;
}}
QPushButton#secondary:hover {{ background: #E5E5EA; }}

QPushButton#danger {{
    background: {RED};
    color: #FFFFFF;
    border: none;
    padding: 9px 20px;
    border-radius: 12px;
    font-size: 14px;
    font-weight: 600;
}}
QPushButton#danger:hover {{ background: #FF564C; }}

QPushButton#ghost {{
    background: transparent;
    color: {ACCENT};
    border: none;
    padding: 6px 12px;
    border-radius: 8px;
    font-size: 13px;
}}
QPushButton#ghost:hover {{ background: {ACCENT_SOFT}; }}

/* ── 表格 ── */
QTableWidget {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 12px;
    gridline-color: {HAIRLINE};
    alternate-background-color: #FAFAFC;
    selection-background-color: {ACCENT_SOFT};
    selection-color: {TEXT_PRIMARY};
    font-size: 13px;
}}
QTableWidget::item {{ padding: 10px 10px; border: none; }}
QHeaderView::section {{
    background: #FAFAFC;
    padding: 10px;
    border: none;
    border-bottom: 1px solid {BORDER};
    font-weight: 600;
    color: {TEXT_SECONDARY};
    font-size: 12px;
}}
QTableCornerButton::section {{ background: #FAFAFC; border: none; }}

/* ── 分组框 ── */
QGroupBox {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 12px;
    margin-top: 10px;
    padding-top: 10px;
    font-size: 14px;
    font-weight: 600;
    color: {TEXT_PRIMARY};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 16px;
    padding: 0 4px;
    color: {TEXT_PRIMARY};
}}

/* ── 复选框 ── */
QCheckBox {{
    spacing: 8px;
    font-size: 13px;
    color: {TEXT_PRIMARY};
}}
QCheckBox::indicator {{
    width: 20px;
    height: 20px;
    border-radius: 6px;
    border: 2px solid #C7C7CC;
    background: {CARD};
}}
QCheckBox::indicator:hover {{ border: 2px solid {ACCENT}; }}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border: 2px solid {ACCENT};
    image: url({_CHECK_PATH});
}}

QToolTip {{
    background: #1D1D1F;
    color: #FFFFFF;
    border: none;
    border-radius: 6px;
    padding: 6px 8px;
    font-size: 12px;
}}
"""

APP_FONT = "Microsoft YaHei UI"
APP_FONT_SIZE = 10