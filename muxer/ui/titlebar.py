"""自绘标题栏：左侧彩色圆点、标题、右侧窗口控制按钮。"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QToolButton, QWidget

from muxer.ui.theme import manager as theme_manager
from muxer.ui.widgets import icons

TITLEBAR_HEIGHT = 46
DOT_STYLE = (
    "QToolButton {{ background: {color}; border: none; border-radius: 6px; }}"
    "QToolButton:hover {{ background: {hover}; }}"
)


class TitleBar(QWidget):
    """无边框窗口的标题栏，承载拖动与窗口按钮。"""

    def __init__(self, window: QWidget, parent: QWidget | None = None):
        super().__init__(parent)
        self._window = window
        self.setFixedHeight(TITLEBAR_HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 10, 0)
        layout.setSpacing(8)

        layout.addWidget(self._build_dot("#E0806B", "#C96A55", "关闭", lambda: self._window.close()))
        layout.addWidget(self._build_dot("#DFB56A", "#C79E55", "最小化", lambda: self._window.showMinimized()))
        layout.addWidget(self._build_dot("#8FAE8B", "#7A9976", "最大化", self._toggle_maximize))

        layout.addSpacing(10)
        title = QLabel("Muxer", self)
        title.setStyleSheet(
            f"font-size: 13px; font-weight: 600; color: {theme_manager.current().text};"
        )
        self._title_label = title
        layout.addWidget(title)
        layout.addStretch(1)

        self._min_button = self._build_control("minimize", "最小化", lambda: self._window.showMinimized())
        self._max_button = self._build_control("maximize", "最大化", self._toggle_maximize)
        self._close_button = self._build_control("close", "关闭", lambda: self._window.close(), close=True)

        for button in (self._min_button, self._max_button, self._close_button):
            layout.addWidget(button)

        theme_manager.subscribe(self._refresh_theme)
        self.destroyed.connect(lambda: theme_manager.unsubscribe(self._refresh_theme))
        self._refresh_theme()

    # ---- 构建 ----

    def _build_dot(self, color: str, hover: str, tooltip: str, slot) -> QToolButton:
        button = QToolButton(self)
        button.setToolTip(tooltip)
        button.setFixedSize(12, 12)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet(DOT_STYLE.format(color=color, hover=hover))
        button.clicked.connect(slot)
        return button

    def _build_control(self, icon_name: str, tooltip: str, slot, close: bool = False) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName("closeButton" if close else "titleButton")
        button.setToolTip(tooltip)
        button.setFixedSize(30, 28)
        button.setIconSize(QSize(14, 14))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(slot)
        button.setProperty("iconName", icon_name)
        return button

    # ---- 交互 ----

    def _toggle_maximize(self) -> None:
        if self._window.isMaximized():
            self._window.showNormal()
        else:
            self._window.showMaximized()
        self.sync_state()

    def sync_state(self) -> None:
        """窗口最大化状态变化后同步按钮图标。"""
        is_max = self._window.isMaximized()
        self._max_button.setProperty("iconName", "restore" if is_max else "maximize")
        self._max_button.setToolTip("还原" if is_max else "最大化")
        self._refresh_theme()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self._window.windowHandle()
            if handle is not None:
                handle.startSystemMove()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            self._toggle_maximize()
        super().mouseDoubleClickEvent(event)

    # ---- 主题 ----

    def _refresh_theme(self) -> None:
        theme = theme_manager.current()
        self._title_label.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {theme.text};")
        for button in (self._min_button, self._max_button, self._close_button):
            name = button.property("iconName")
            color = theme.text_secondary
            button.setIcon(icons.icon(name, color, 14))