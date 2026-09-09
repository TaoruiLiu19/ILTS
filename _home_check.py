"""启动页构建检查（布局警告回归）"""
import os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)

from ui.pages.home_page import HomePage
p = HomePage()
p.resize(1400, 900)
p.show()
p.refresh()
app.processEvents()

cards = [p.card_active, p.card_completed, p.card_new, p.card_report]
print("四卡对象:", [type(c).__name__ for c in cards])
print("全部可见:", all(c.isVisible() for c in cards))
print("进行中文案:", p.card_active.findChildren(type(p.card_active))[:1] or "n/a")
# 卡片父级应是 grid_wrap（QWidget），而不是 HomePage
parents = {c.parent().__class__.__name__ for c in cards}
print("父级:", parents)
ok = len(set(id(c) for c in cards)) == 4 and all(c.isVisible() for c in cards)
print("✅ 启动页正常" if ok else "❌ 启动页异常")
sys.exit(0 if ok else 1)
