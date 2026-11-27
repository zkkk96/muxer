"""自绘圆角进度条。

QProgressBar 的 chunk 无法画出两端圆角，也难以叠加流光，
所以这里用 QPainter 自行绘制：圆角轨道 + 渐变填充 + 移动高光。
"""

from __future__ import annotations

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath
from PySide6.QtWidgets import QWidget

from muxer.ui.theme import manager as theme_manager
from muxer.ui.theme.tokens import Theme

SHIMMER_INTERVAL_MS = 33
SHIMMER_STEP = 0.012


class ProgressBar(QWidget):
    """0.0 - 1.0 的圆角进度条，值变化带补间动画。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._value = 0.0
        self._display = 0.0
        self._tone = "accent"
        self._phase = 0.0
        self._indeterminate = False
        self.setFixedHeight(8)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._animation = QPropertyAnimation(self, b"displayValue", self)
        self._animation.setDuration(420)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._shimmer = QTimer(self)
        self._shimmer.setInterval(SHIMMER_INTERVAL_MS)
        self._shimmer.timeout.connect(self._tick)

        theme_manager.subscribe(self.update)
        self.destroyed.connect(lambda: theme_manager.unsubscribe(self.update))

    # ---- 属性 ----

    def _get_display(self) -> float:
        return self._display

    def _set_display(self, value: float) -> None:
        self._display = max(0.0, min(1.0, float(value)))
        self.update()

    displayValue = Property(float, _get_display, _set_display)

    # ---- 对外接口 ----

    def value(self) -> float:
        return self._value

    def set_value(self, value: float, animate: bool = True) -> None:
        """设置进度，默认用 420ms 补间；0 与 1 时停止流光。"""
        self._value = max(0.0, min(1.0, float(value)))
        self._animation.stop()
        if animate:
            self._animation.setStartValue(self._display)
            self._animation.setEndValue(self._value)
            self._animation.start()
        else:
            self._set_display(self._value)
        self._sync_shimmer()

    def set_tone(self, tone: str) -> None:
        """填充色语义：accent（进行中）/ success（完成）/ danger（失败）。"""
        self._tone = tone if tone in ("accent", "success", "danger") else "accent"
        self.update()

    def set_indeterminate(self, enabled: bool) -> None:
        """直播录制：总量未知时展示循环移动的光带。"""
        if self._indeterminate == enabled:
            return
        self._indeterminate = enabled
        self._sync_shimmer()
        self.update()

    def _sync_shimmer(self) -> None:
        should_run = (self._indeterminate or 0.0 < self._value < 1.0) and self.isVisible()
        if should_run and not self._shimmer.isActive():
            self._shimmer.start()
        elif not should_run and self._shimmer.isActive():
            self._shimmer.stop()
            self._phase = 0.0

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        self._sync_shimmer()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().hideEvent(event)
        self._shimmer.stop()

    def _tick(self) -> None:
        self._phase = (self._phase + SHIMMER_STEP) % 1.0
        self.update()

    # ---- 绘制 ----

    def _fill_colors(self, theme: Theme) -> tuple[QColor, QColor]:
        if self._tone == "success":
            base = QColor(theme.success)
            return base, base.lighter(112)
        if self._tone == "danger":
            base = QColor(theme.danger)
            return base, base.lighter(112)
        return QColor(theme.accent), QColor(theme.accent_hover)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        theme = theme_manager.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(self.rect()).adjusted(0.0, 0.0, 0.0, -1.0)
        radius = rect.height() / 2.0

        track = QPainterPath()
        track.addRoundedRect(rect, radius, radius)
        track_color = QColor(theme.border)
        track_color.setAlpha(150 if not theme.is_dark else 200)
        painter.fillPath(track, track_color)

        if self._indeterminate:
            self._paint_indeterminate(painter, track, rect)
            painter.end()
            return

        fill_width = rect.width() * self._display
        if fill_width <= 0.5:
            painter.end()
            return

        fill_rect = QRectF(rect.left(), rect.top(), fill_width, rect.height())
        fill_radius = min(radius, fill_width / 2.0)
        fill_path = QPainterPath()
        fill_path.addRoundedRect(fill_rect, fill_radius, fill_radius)

        base, lighter = self._fill_colors(theme)
        gradient = QLinearGradient(fill_rect.topLeft(), fill_rect.topRight())
        gradient.setColorAt(0.0, base)
        gradient.setColorAt(1.0, lighter)
        painter.fillPath(fill_path, gradient)

        self._paint_shimmer(painter, fill_path, fill_width)
        painter.end()

    def _paint_indeterminate(
        self, painter: QPainter, track: QPainterPath, rect: QRectF
    ) -> None:
        """一条来回移动的珊瑚色光带，表示"持续录制中"。"""
        band_width = max(48.0, rect.width() * 0.32)
        travel = rect.width() + band_width
        left = rect.left() + self._phase * travel - band_width
        band = QRectF(left, rect.top(), band_width, rect.height())

        painter.save()
        painter.setClipPath(track)
        gradient = QLinearGradient(band.left(), 0.0, band.right(), 0.0)
        base = QColor(theme_manager.current().accent)
        start = QColor(base)
        start.setAlpha(30)
        middle = QColor(base)
        middle.setAlpha(220)
        gradient.setColorAt(0.0, start)
        gradient.setColorAt(0.5, middle)
        gradient.setColorAt(1.0, start)
        painter.fillRect(band, gradient)
        painter.restore()

    def _paint_shimmer(self, painter: QPainter, fill_path: QPainterPath, fill_width: float) -> None:
        """在填充区域内画一条缓慢右移的高光，让下载中的进度条更有生命感。"""
        if not self._shimmer.isActive() or fill_width < 24:
            return

        painter.save()
        painter.setClipPath(fill_path)
        band_width = 90.0
        travel = fill_width + band_width * 2
        center = self._phase * travel - band_width
        highlight = QLinearGradient(center - band_width, 0.0, center + band_width, 0.0)
        highlight.setColorAt(0.0, QColor(255, 255, 255, 0))
        highlight.setColorAt(0.5, QColor(255, 255, 255, 70))
        highlight.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillPath(fill_path, highlight)
        painter.restore()