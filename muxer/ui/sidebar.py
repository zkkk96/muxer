"""左侧导航栏：应用标识、页面入口、主题切换与版本信息。"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from muxer import APP_VERSION
from muxer.ui.theme import manager as theme_manager
from muxer.ui.widgets import icons

NAV_ITEMS = (
    ("plus", "新建下载"),
    ("list", "任务"),
    ("clock", "历史"),
    ("radar", "嗅探"),
    ("sliders", "设置"),
)


class NavButton(QWidget):
    """自绘导航项：悬停底色、选中态、左侧强调条与角标。"""

    clicked = Signal()

    def __init__(self, icon_name: str, text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._icon_name = icon_name
        self._text = text
        self._badge = 0
        self._active = False
        self._hover = False
        self.setFixedHeight(38)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def set_active(self, active: bool) -> None:
        if self._active != active:
            self._active = active
            self.update()

    def set_badge(self, count: int) -> None:
        count = max(0, int(count))
        if self._badge != count:
            self._badge = count
            self.update()

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        theme = theme_manager.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        body = QRectF(self.rect()).adjusted(0.0, 0.0, -1.0, -1.0)
        path = QPainterPath()
        path.addRoundedRect(body, 10.0, 10.0)
        if self._active:
            painter.fillPath(path, QColor(theme.accent_soft))
        elif self._hover:
            painter.fillPath(path, QColor(theme.surface_muted))

        icon_color = theme.accent if self._active else theme.text_secondary
        icon_pixmap = icons.pixmap(self._icon_name, 18, icon_color)
        icon_x = 12.0
        icon_y = (self.height() - 18) / 2.0
        painter.drawPixmap(int(icon_x), int(icon_y), icon_pixmap)

        if self._active:
            bar = QRectF(0.0, (self.height() - 16) / 2.0, 3.0, 16.0)
            bar_path = QPainterPath()
            bar_path.addRoundedRect(bar, 1.5, 1.5)
            painter.fillPath(bar_path, QColor(theme.accent))

        text_font = QFont(theme.font_sans)
        text_font.setPixelSize(theme.fs_body)
        text_font.setWeight(QFont.Weight.DemiBold if self._active else QFont.Weight.Medium)
        painter.setFont(text_font)
        painter.setPen(QColor(theme.text if self._active else theme.text_secondary))

        badge_width = 0.0
        if self._badge > 0:
            badge_width = self._badge_text_width(str(self._badge)) + 16.0

        text_left = 40.0
        text_right = body.right() - 12.0 - badge_width
        text_rect = QRectF(text_left, 0.0, max(0.0, text_right - text_left), float(self.height()))
        elided = painter.fontMetrics().elidedText(
            self._text, Qt.TextElideMode.ElideRight, int(text_rect.width())
        )
        painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), elided)

        if self._badge > 0:
            self._paint_badge(painter, theme, str(self._badge), body)

        painter.end()

    def _badge_text_width(self, text: str) -> float:
        font = QFont(theme_manager.current().font_sans)
        font.setPixelSize(11)
        font.setWeight(QFont.Weight.DemiBold)
        return QFontMetricsF(font).horizontalAdvance(text)

    def _paint_badge(self, painter: QPainter, theme, text: str, body: QRectF) -> None:
        font = QFont(theme.font_sans)
        font.setPixelSize(11)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        width = max(18.0, QFontMetricsF(font).horizontalAdvance(text) + 12.0)
        height = 18.0
        badge = QRectF(body.right() - 10.0 - width, (self.height() - height) / 2.0, width, height)
        badge_path = QPainterPath()
        badge_path.addRoundedRect(badge, height / 2.0, height / 2.0)
        background = QColor(theme.accent if self._active else theme.border_strong)
        painter.fillPath(badge_path, background)
        painter.setPen(QColor("#FFFFFF") if self._active else QColor(theme.text_secondary))
        painter.drawText(badge, int(Qt.AlignmentFlag.AlignCenter), text)


class Sidebar(QFrame):
    """导航容器。"""

    pageChanged = Signal(int)
    themeToggleRequested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(224)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 18, 14, 14)
        layout.setSpacing(3)

        layout.addWidget(self._build_brand())
        layout.addSpacing(16)

        self._buttons: list[NavButton] = []
        for index, (icon_name, text) in enumerate(NAV_ITEMS):
            button = NavButton(icon_name, text, self)
            button.clicked.connect(lambda idx=index: self._select(idx))
            self._buttons.append(button)
            layout.addWidget(button)

        layout.addStretch(1)
        layout.addWidget(self._build_footer())
        self._select(0, notify=False)

        theme_manager.subscribe(self._refresh_theme)
        self.destroyed.connect(lambda: theme_manager.unsubscribe(self._refresh_theme))

    def _build_brand(self) -> QWidget:
        container = QWidget(self)
        row = QHBoxLayout(container)
        row.setContentsMargins(6, 0, 0, 0)
        row.setSpacing(10)

        logo = QLabel(container)
        logo.setPixmap(icons.app_icon().pixmap(30, 30))
        logo.setFixedSize(30, 30)

        column = QVBoxLayout()
        column.setSpacing(0)
        name = QLabel("Muxer", container)
        name.setObjectName("appName")
        tagline = QLabel("流媒体下载器", container)
        tagline.setObjectName("appTagline")
        column.addWidget(name)
        column.addWidget(tagline)

        row.addWidget(logo)
        row.addLayout(column)
        row.addStretch(1)
        return container

    def _build_footer(self) -> QWidget:
        footer = QFrame(self)
        footer.setObjectName("sidebarFooter")
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(6)

        self._theme_button = QPushButton("切换主题", footer)
        self._theme_button.setObjectName("footerButton")
        self._theme_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._theme_button.setIconSize(QSize(14, 14))
        self._theme_button.clicked.connect(self.themeToggleRequested)

        version = QLabel(f"v{APP_VERSION}", footer)
        version.setObjectName("caption")
        version.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        layout.addWidget(self._theme_button, 1)
        layout.addWidget(version)
        return footer

    def _select(self, index: int, notify: bool = True) -> None:
        for position, button in enumerate(self._buttons):
            button.set_active(position == index)
        if notify:
            self.pageChanged.emit(index)

    def set_current(self, index: int) -> None:
        """同步选中态但不发信号，供程序化切页使用。"""
        self._select(index, notify=False)

    def set_badge(self, index: int, count: int) -> None:
        """更新某个导航项的角标数字。"""
        if 0 <= index < len(self._buttons):
            self._buttons[index].set_badge(count)

    def _refresh_theme(self) -> None:
        theme = theme_manager.current()
        icon_name = "sun" if theme.is_dark else "moon"
        self._theme_button.setIcon(icons.icon(icon_name, theme.text_secondary, 14))
        self._theme_button.setIconSize(QSize(14, 14))
        for button in self._buttons:
            button.update()