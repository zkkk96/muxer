"""底部浮动提示条。

单条显示、自动消失，带淡入淡出与轻微上移动画。
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QTimer,
    Qt,
)
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QWidget,
)

from muxer.ui.theme import manager as theme_manager
from muxer.ui.widgets import icons

TONE_STYLE = {
    "info": ("check", "accent"),
    "success": ("check", "success"),
    "warning": ("alert", "warning"),
    "danger": ("alert", "danger"),
}


class Toast(QFrame):
    """一条提示：左侧语义色图标，右侧文字。"""

    def __init__(self, message: str, tone: str, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("toast")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        theme = theme_manager.current()
        icon_name, color_field = TONE_STYLE.get(tone, TONE_STYLE["info"])
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 18, 10)
        layout.setSpacing(10)

        icon_label = QLabel(self)
        icon_label.setPixmap(icons.pixmap(icon_name, 16, getattr(theme, color_field)))
        icon_label.setFixedSize(16, 16)

        text_label = QLabel(message, self)
        text_label.setObjectName("toastText")

        layout.addWidget(icon_label)
        layout.addWidget(text_label)
        self.adjustSize()


class ToastManager:
    """管理当前可见的 Toast，负责定位与生命周期。"""

    def __init__(self, window: QWidget):
        self._window = window
        self._toast: Toast | None = None
        self._effect: QGraphicsOpacityEffect | None = None
        self._animations: list[QPropertyAnimation] = []

    def show(self, message: str, tone: str = "info", duration_ms: int = 2600) -> None:
        """展示一条提示，替换掉正在显示的旧提示。"""
        self._dismiss_current()
        toast = Toast(message, tone, self._window)
        toast.move(self._target_position(toast))
        toast.show()
        toast.raise_()

        effect = QGraphicsOpacityEffect(toast)
        effect.setOpacity(0.0)
        toast.setGraphicsEffect(effect)

        fade_in = QPropertyAnimation(effect, b"opacity", toast)
        fade_in.setDuration(160)
        fade_in.setStartValue(0.0)
        fade_in.setEndValue(1.0)
        fade_in.setEasingCurve(QEasingCurve.Type.OutCubic)

        slide = QPropertyAnimation(toast, b"pos", toast)
        start_pos = self._target_position(toast) + QPoint(0, 10)
        slide.setDuration(180)
        slide.setStartValue(start_pos)
        slide.setEndValue(self._target_position(toast))
        slide.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._toast = toast
        self._effect = effect
        self._animations = [fade_in, slide]
        fade_in.start()
        slide.start()

        QTimer.singleShot(duration_ms, lambda: self._fade_out(toast, effect))

    def _target_position(self, toast: Toast) -> QPoint:
        """底部居中，留出 32px 边距。"""
        rect = self._window.rect()
        x = (rect.width() - toast.width()) // 2
        y = rect.height() - toast.height() - 32
        return QPoint(max(0, x), max(0, y))

    def _fade_out(self, toast: Toast, effect: QGraphicsOpacityEffect) -> None:
        if toast is not self._toast:
            return
        fade_out = QPropertyAnimation(effect, b"opacity", toast)
        fade_out.setDuration(220)
        fade_out.setStartValue(effect.opacity())
        fade_out.setEndValue(0.0)
        fade_out.finished.connect(toast.deleteLater)
        fade_out.finished.connect(self._clear_refs)
        self._animations = [fade_out]
        fade_out.start()

    def _clear_refs(self) -> None:
        self._toast = None
        self._effect = None

    def _dismiss_current(self) -> None:
        if self._toast is not None:
            toast = self._toast
            self._toast = None
            toast.deleteLater()