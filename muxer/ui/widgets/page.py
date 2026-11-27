"""页面骨架：统一页头排版，页面内容由子类填充。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget


class Page(QWidget):
    """标题 + 副标题 + 右侧操作区 + 内容区。"""

    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 26, 32, 24)
        outer.setSpacing(0)

        header = QHBoxLayout()
        header.setSpacing(12)
        title_column = QVBoxLayout()
        title_column.setSpacing(4)

        self._title = QLabel(title, self)
        self._title.setObjectName("pageTitle")
        title_column.addWidget(self._title)

        self._subtitle = QLabel(subtitle, self)
        self._subtitle.setObjectName("pageSubtitle")
        self._subtitle.setVisible(bool(subtitle))
        title_column.addWidget(self._subtitle)

        header.addLayout(title_column)
        header.addStretch(1)

        self._actions = QHBoxLayout()
        self._actions.setSpacing(8)
        self._actions.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        header.addLayout(self._actions)

        outer.addLayout(header)
        outer.addSpacing(18)

        self.content = QVBoxLayout()
        self.content.setSpacing(14)
        outer.addLayout(self.content, 1)

    def add_action(self, widget: QWidget) -> None:
        """把按钮加进页头右侧操作区。"""
        self._actions.addWidget(widget)

    def set_subtitle(self, text: str) -> None:
        self._subtitle.setText(text)
        self._subtitle.setVisible(bool(text))