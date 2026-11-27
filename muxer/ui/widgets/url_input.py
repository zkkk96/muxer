"""链接输入卡：多行粘贴、协议识别、解析入口。"""

from __future__ import annotations

import re

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from muxer.ui.widgets.pill import Pill

PROTOCOL_HINTS = (
    (re.compile(r"\.m3u8(\?|$)", re.IGNORECASE), "HLS", "accent"),
    (re.compile(r"\.mpd(\?|$)", re.IGNORECASE), "DASH", "accent"),
    (re.compile(r"\.flv(\?|$)", re.IGNORECASE), "FLV", "accent"),
    (
        re.compile(r"\.(mp4|webm|mkv|mov|m4a|mp3|aac|zip|bin)(\?|$)", re.IGNORECASE),
        "直链",
        "accent",
    ),
)


class UrlInput(QFrame):
    """粘贴区 + 解析按钮。

    回车解析，Ctrl+Enter 也解析；解析结果通过 ``parseRequested`` 发出。
    """

    parseRequested = Signal(list)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("inputCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(8)

        self.editor = QTextEdit(self)
        self.editor.setObjectName("urlInput")
        self.editor.setPlaceholderText("粘贴 m3u8 / mpd / flv 链接，支持多行批量粘贴")
        self.editor.setFixedHeight(76)
        self.editor.setAcceptRichText(False)
        self.editor.setTabChangesFocus(True)
        layout.addWidget(self.editor)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self._protocol_pill = Pill("待识别", "neutral", self)
        hint = QLabel("支持 HLS · DASH · HTTP-FLV", self)
        hint.setObjectName("inputHint")
        self._parse_button = QPushButton("解析", self)
        self._parse_button.setObjectName("primaryButton")
        self._parse_button.setCursor(Qt.CursorShape.PointingHandCursor)

        bottom.addWidget(self._protocol_pill)
        bottom.addWidget(hint)
        bottom.addStretch(1)
        bottom.addWidget(self._parse_button)
        layout.addLayout(bottom)

        self.editor.textChanged.connect(self._update_protocol)
        self.editor.installEventFilter(self)
        self._parse_button.clicked.connect(self._emit_urls)

    def urls(self) -> list[str]:
        """按行拆分、去重、过滤空行，只保留 http(s) 链接。"""
        seen: set[str] = set()
        result: list[str] = []
        for raw_line in self.editor.toPlainText().splitlines():
            line = raw_line.strip()
            if not line or line in seen:
                continue
            if not line.lower().startswith(("http://", "https://")):
                continue
            seen.add(line)
            result.append(line)
        return result

    def set_text(self, text: str) -> None:
        self.editor.setPlainText(text)

    def set_busy(self, busy: bool) -> None:
        """解析进行中：禁用按钮并显示进度文案。"""
        self._parse_button.setEnabled(not busy)
        self._parse_button.setText("解析中…" if busy else "解析")

    def _emit_urls(self) -> None:
        urls = self.urls()
        if urls:
            self.parseRequested.emit(urls)

    def _update_protocol(self) -> None:
        text = self.editor.toPlainText().strip()
        if not text:
            self._protocol_pill.setText("待识别")
            self._protocol_pill.set_tone("neutral")
            return
        for pattern, label, tone in PROTOCOL_HINTS:
            if pattern.search(text):
                self._protocol_pill.setText(label)
                self._protocol_pill.set_tone(tone)
                return
        self._protocol_pill.setText("未知")
        self._protocol_pill.set_tone("warning")

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt 命名
        if watched is self.editor and event.type() == QEvent.Type.KeyPress:
            key_event = event
            if key_event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                modifiers = key_event.modifiers()
                if modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
                    self._emit_urls()
                    return True
        return super().eventFilter(watched, event)