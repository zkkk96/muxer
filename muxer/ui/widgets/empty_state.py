"""空状态占位：图标 + 标题 + 说明 + 可选主操作。"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout, QWidget

from muxer.ui.theme import manager as theme_manager
from muxer.ui.widgets import icons


class EmptyState(QWidget):
    """居中排版，用于列表为空或功能未就绪时。"""

    actionClicked = Signal()

    def __init__(
        self,
        icon_name: str,
        title: str,
        subtitle: str,
        action_text: str = "",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._icon_name = icon_name

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addStretch(1)

        icon_frame = QFrame(self)
        icon_frame.setObjectName("emptyIcon")
        icon_frame.setFixedSize(72, 72)
        icon_layout = QVBoxLayout(icon_frame)
        icon_layout.setContentsMargins(0, 0, 0, 0)
        self._icon_label = QLabel(icon_frame)
        self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_layout.addWidget(self._icon_label)

        self._title = QLabel(title, self)
        self._title.setObjectName("emptyTitle")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._subtitle = QLabel(subtitle, self)
        self._subtitle.setObjectName("emptySubtitle")
        self._subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._subtitle.setWordWrap(True)
        self._subtitle.setFixedWidth(460)
        # QLabel 在带对齐标志的布局里不会自动按 heightForWidth 撑高，
        # 这里预先按字体度量算好换行后的高度，避免文字被裁掉。
        body_font = QFont(theme_manager.current().font_sans)
        body_font.setPixelSize(theme_manager.current().fs_body)
        self._subtitle.setFont(body_font)
        metrics = QFontMetrics(body_font)
        text_rect = metrics.boundingRect(
            QRect(0, 0, 460, 1000), int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignCenter), subtitle
        )
        self._subtitle.setFixedHeight(text_rect.height() + 2)

        layout.addWidget(icon_frame, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self._title, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self._subtitle, 0, Qt.AlignmentFlag.AlignHCenter)

        self._action: QPushButton | None = None
        if action_text:
            self._action = QPushButton(action_text, self)
            self._action.setObjectName("primaryButton")
            self._action.setCursor(Qt.CursorShape.PointingHandCursor)
            self._action.clicked.connect(self.actionClicked)
            layout.addWidget(self._action, 0, Qt.AlignmentFlag.AlignHCenter)

        layout.addStretch(1)
        theme_manager.subscribe(self._refresh)
        self.destroyed.connect(lambda: theme_manager.unsubscribe(self._refresh))
        self._refresh()

    def set_texts(self, title: str, subtitle: str) -> None:
        self._title.setText(title)
        self._subtitle.setText(subtitle)

    def _refresh(self) -> None:
        theme = theme_manager.current()
        self._icon_label.setPixmap(
            icons.pixmap(self._icon_name, 30, theme.accent)
        )