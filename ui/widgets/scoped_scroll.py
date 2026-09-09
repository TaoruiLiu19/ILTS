"""
滚动区域控件：滚轮在区域内滚动；到达边界时吞掉事件，
不把滚动上抛给外层大滚动区，避免「列表在滚、整页也跟着滚」的级联问题。
"""

from PySide6.QtWidgets import QScrollArea


class ScopedScrollArea(QScrollArea):
    """滚轮只作用于本滚动区自身的滚动区子类。"""

    def wheelEvent(self, event):
        if self.widget() is None:
            event.ignore()
            return

        vbar = self.verticalScrollBar()
        dy = event.angleDelta().y()

        if dy != 0 and vbar.maximum() > vbar.minimum():
            at_end = ((dy > 0 and vbar.value() <= vbar.minimum()) or
                      (dy < 0 and vbar.value() >= vbar.maximum()))
            if not at_end:
                super().wheelEvent(event)
            else:
                event.accept()          # 到边界：吞掉，不级联外层
            return

        hbar = self.horizontalScrollBar()
        if hbar and hbar.maximum() > hbar.minimum():
            super().wheelEvent(event)
        else:
            event.accept()              # 无可滚动方向：吞掉