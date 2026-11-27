"""嗅探结果行：勾选、协议、状态与下载入口。"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from muxer.core.models import ProbeResult
from muxer.ui.format import format_bytes, format_duration, protocol_label
from muxer.ui.widgets.pill import Pill


def _elide(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _title_hint(url: str) -> str:
    parsed = urlparse(url)
    name = Path(unquote(parsed.path)).stem
    return name or parsed.netloc or "媒体地址"


class SniffRow(QFrame):
    """一条拦截到的媒体请求。"""

    downloadRequested = Signal(str)
    selectionChanged = Signal()

    def __init__(
        self,
        url: str,
        referer: str,
        cookie: str,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("historyRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._url = url
        self._referer = referer
        self._cookie = cookie
        self._title = _title_hint(url)
        self._supported = False
        self._queued = False
        self._result: ProbeResult | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)

        self._checkbox = QCheckBox(self)
        self._checkbox.setEnabled(False)
        self._checkbox.stateChanged.connect(lambda _state: self.selectionChanged.emit())

        self._pill = Pill("…", "neutral", self)

        text_column = QVBoxLayout()
        text_column.setSpacing(2)
        self._title_label = QLabel(_elide(self._title, 52), self)
        self._title_label.setObjectName("cardTitle")
        self._title_label.setToolTip(url)
        self._url_label = QLabel(_elide(url, 78), self)
        self._url_label.setObjectName("caption")
        self._url_label.setToolTip(url)
        text_column.addWidget(self._title_label)
        text_column.addWidget(self._url_label)

        self._status_label = QLabel("探测中…", self)
        self._status_label.setObjectName("caption")
        self._status_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._status_label.setMaximumWidth(300)

        self._download_button = QPushButton("下载", self)
        self._download_button.setObjectName("ghostButton")
        self._download_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._download_button.setEnabled(False)
        self._download_button.clicked.connect(lambda: self.downloadRequested.emit(self._url))

        layout.addWidget(self._checkbox)
        layout.addWidget(self._pill)
        layout.addLayout(text_column, 1)
        layout.addWidget(self._status_label)
        layout.addWidget(self._download_button)

    # ---- 状态 ----

    @property
    def url(self) -> str:
        return self._url

    @property
    def title(self) -> str:
        return self._title

    def headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._referer:
            headers["Referer"] = self._referer
        if self._cookie:
            headers["Cookie"] = self._cookie
        return headers

    def is_selected(self) -> bool:
        return self._checkbox.isChecked()

    def is_supported(self) -> bool:
        return self._supported and not self._queued

    def set_checked(self, checked: bool) -> None:
        self._checkbox.setChecked(checked)

    def set_probing(self) -> None:
        self._pill.setText("…")
        self._pill.set_tone("neutral")
        self._status_label.setText("探测中…")

    def set_submitting(self) -> None:
        self._download_button.setEnabled(False)
        self._checkbox.setEnabled(False)
        self._status_label.setText("正在加入队列…")

    def set_queued(self) -> None:
        self._queued = True
        self._supported = False
        self._pill.setText("已加入")
        self._pill.set_tone("success")
        self._status_label.setText("已加入下载队列")
        self._download_button.setEnabled(False)
        self._checkbox.setEnabled(False)

    def set_failed(self, message: str) -> None:
        self._supported = False
        self._pill.setText("无效")
        self._pill.set_tone("danger")
        self._status_label.setText(_elide(message, 48))
        self._status_label.setToolTip(message)
        self._download_button.setEnabled(False)
        self._checkbox.setEnabled(False)

    def set_result(self, result: ProbeResult) -> None:
        self._result = result
        self._title = result.title or self._title
        self._title_label.setText(_elide(self._title, 52))
        self._pill.setText(protocol_label(result.protocol))
        self._pill.set_tone("accent" if result.supported else "warning")

        if not result.supported:
            message = result.message or "当前版本暂不支持"
            self._status_label.setText(_elide(message, 48))
            self._status_label.setToolTip(message)
            self._supported = False
            self._download_button.setEnabled(False)
            self._checkbox.setEnabled(False)
            return

        bits: list[str] = []
        if result.live:
            bits.append("直播")
        if result.duration_seconds:
            bits.append(format_duration(result.duration_seconds))
        if result.size_bytes:
            bits.append(format_bytes(result.size_bytes))
        if len(result.variants) > 1:
            bits.append(f"{len(result.variants)} 个清晰度")
        status = " · ".join(bits) or "可下载"
        self._status_label.setText(_elide(status, 48))
        self._status_label.setToolTip(result.message or status)
        self._supported = True
        self._download_button.setEnabled(True)
        self._checkbox.setEnabled(True)