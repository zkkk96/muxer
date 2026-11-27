"""新建下载页：粘贴链接、探测远端信息、创建任务。

M2 支持直链与 FLV；HLS / DASH 会探测协议并明确提示
解析能力分别在 M3、M4 接入。
"""

from __future__ import annotations

import uuid

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from muxer.core.models import ProbeResult
from muxer.ui.format import format_bytes, format_duration, protocol_label
from muxer.ui.widgets.page import Page
from muxer.ui.widgets.pill import Pill
from muxer.ui.widgets.url_input import UrlInput


class NewDownloadPage(Page):
    """新建下载。"""

    probeRequested = Signal(dict)
    taskRequested = Signal(dict)
    messageRequested = Signal(str, str)

    def __init__(self, download_dir: str, parent: QWidget | None = None):
        super().__init__("新建下载", "粘贴链接，探测远端信息后加入下载队列", parent)
        self._download_dir = download_dir
        self._request_id = ""
        self._probe: ProbeResult | None = None
        self._pending_headers: dict[str, str] = {}
        self._pending_headers_url = ""

        self.url_input = UrlInput(self)
        self.url_input.parseRequested.connect(self._on_parse)
        self.content.addWidget(self.url_input)

        self._steps_card = self._build_steps_card()
        self.content.addWidget(self._steps_card)

        self._media_card = self._build_media_card()
        self._media_card.setVisible(False)
        self.content.addWidget(self._media_card)

        tip = QLabel(
            "仅用于下载你有权获取的内容，请遵守目标站点的服务条款。"
            "当前版本支持直链、HTTP-FLV、HLS 与 DASH 点播；DRM 与直播流会明确提示暂不支持。",
            self,
        )
        tip.setObjectName("caption")
        tip.setWordWrap(True)
        self.content.addWidget(tip)
        self.content.addStretch(1)

    # ---- 构建 ----

    def _build_steps_card(self) -> QFrame:
        card = QFrame(self)
        card.setObjectName("card")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
        title = QLabel("使用提示", card)
        title.setObjectName("cardTitle")
        layout.addWidget(title)
        layout.addWidget(
            self._step_row(card, "1", "粘贴链接", "支持 m3u8 / mpd / flv / 直链，可多行批量粘贴")
        )
        layout.addWidget(
            self._step_row(card, "2", "确认信息", "选择清晰度与保存位置；AES-128 加密会自动解密")
        )
        layout.addWidget(
            self._step_row(card, "3", "开始下载", "支持暂停续传、直播录制，完成后自动合并")
        )
        return card

    def _step_row(self, parent: QWidget, number: str, title: str, caption: str) -> QWidget:
        row = QWidget(parent)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        badge = Pill(number, "accent", row)
        badge.setFixedSize(24, 22)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column = QVBoxLayout()
        column.setSpacing(2)
        title_label = QLabel(title, row)
        title_label.setObjectName("secondary")
        caption_label = QLabel(caption, row)
        caption_label.setObjectName("caption")
        column.addWidget(title_label)
        column.addWidget(caption_label)
        layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(column, 1)
        return row

    def _build_media_card(self) -> QFrame:
        card = QFrame(self)
        card.setObjectName("card")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        self._media_title = QLabel("—", card)
        self._media_title.setObjectName("cardTitle")
        self._media_meta = QLabel("—", card)
        self._media_meta.setObjectName("caption")
        layout.addWidget(self._media_title)
        layout.addWidget(self._media_meta)

        self._note = QLabel("", card)
        self._note.setObjectName("caption")
        self._note.setWordWrap(True)
        self._note.setVisible(False)
        layout.addWidget(self._note)

        self._variant_row = QWidget(card)
        variant_layout = QHBoxLayout(self._variant_row)
        variant_layout.setContentsMargins(0, 0, 0, 0)
        variant_layout.setSpacing(10)
        variant_label = QLabel("清晰度", self._variant_row)
        variant_label.setObjectName("caption")
        self._variant_combo = QComboBox(self._variant_row)
        self._variant_combo.setMinimumWidth(280)
        variant_layout.addWidget(variant_label)
        variant_layout.addWidget(self._variant_combo)
        variant_layout.addStretch(1)
        self._variant_row.setVisible(False)
        layout.addWidget(self._variant_row)

        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        self._path_edit = QLineEdit(self._download_dir, card)
        browse = QPushButton("选择目录", card)
        browse.setCursor(Qt.CursorShape.PointingHandCursor)
        browse.clicked.connect(self._choose_directory)
        path_row.addWidget(self._labeled("保存到", self._path_edit, card), 1)
        path_row.addWidget(browse, 0, Qt.AlignmentFlag.AlignBottom)
        layout.addLayout(path_row)

        action_row = QHBoxLayout()
        action_row.addStretch(1)
        self._start_button = QPushButton("开始下载", card)
        self._start_button.setObjectName("primaryButton")
        self._start_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._start_button.setEnabled(False)
        self._start_button.clicked.connect(self._start_download)
        action_row.addWidget(self._start_button)
        layout.addLayout(action_row)

        return card

    def _labeled(self, text: str, widget: QWidget, parent: QWidget) -> QWidget:
        container = QWidget(parent)
        column = QVBoxLayout(container)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        label = QLabel(text, container)
        label.setObjectName("caption")
        column.addWidget(label)
        column.addWidget(widget)
        return container

    # ---- 嗅探联动 ----

    def set_download_dir(self, directory: str) -> None:
        """首次引导或设置变更后刷新默认保存目录。"""
        self._download_dir = directory
        self._path_edit.setText(directory)

    def prepare(self, url: str, headers: dict[str, str] | None = None) -> None:
        """由嗅探页填入链接与请求头，并自动开始探测。"""
        self._pending_headers = dict(headers or {})
        self._pending_headers_url = url
        self.url_input.set_text(url)
        self._on_parse([url])

    # ---- 解析 ----

    def _on_parse(self, urls: list[str]) -> None:
        if not urls:
            return
        if len(urls) > 1:
            self.messageRequested.emit("当前版本一次解析一个链接，已取第一个", "info")
        url = urls[0]
        if url != self._pending_headers_url:
            self._pending_headers = {}
            self._pending_headers_url = ""
        self._request_id = uuid.uuid4().hex
        self._probe = None
        self._start_button.setEnabled(False)

        self._media_title.setText("正在探测远端信息…")
        self._media_meta.setText(url)
        self._note.setVisible(False)
        self._variant_row.setVisible(False)
        self._media_card.setVisible(True)
        self._steps_card.setVisible(False)
        self.url_input.set_busy(True)
        self.probeRequested.emit(
            {
                "request_id": self._request_id,
                "url": url,
                "headers": dict(self._pending_headers),
            }
        )

    def on_probe_finished(self, result: ProbeResult) -> None:
        """引擎探测成功（由主窗口转发）。"""
        if result.request_id != self._request_id:
            return
        self._probe = result
        self.url_input.set_busy(False)

        self._media_title.setText(result.title)
        bits = [protocol_label(result.protocol)]
        if result.duration_seconds:
            bits.append(f"时长 {format_duration(result.duration_seconds)}")
        if result.size_bytes:
            bits.append(f"约 {format_bytes(result.size_bytes)}")
        if result.content_type:
            bits.append(result.content_type)
        self._media_meta.setText(" · ".join(bits))

        self._populate_variants(result)

        if result.supported:
            if result.live:
                self._note.setText(
                    result.message or "直播流：持续录制，点击任务卡的停止按钮完成并合并。"
                )
            elif result.protocol == "hls":
                self._note.setText(result.message or "HLS 点播，下载后会自动合并分片。")
            elif result.protocol == "dash":
                self._note.setText(result.message or "DASH 点播，将分别下载音视频轨后合并。")
            else:
                self._note.setText("直链下载，支持断点续传。")
        else:
            self._note.setText(result.message or "当前版本暂不支持该协议")
        self._note.setVisible(True)
        self._media_card.setVisible(True)
        self._steps_card.setVisible(False)
        self._start_button.setEnabled(result.supported)

    def _populate_variants(self, result: ProbeResult) -> None:
        """HLS 主列表存在多个码率时，提供清晰度选择。"""
        self._variant_combo.clear()
        if not result.variants:
            self._variant_row.setVisible(False)
            return
        self._variant_combo.addItem("自动（最高码率）", "")
        ordered = sorted(result.variants, key=lambda item: item.bandwidth, reverse=True)
        for option in ordered:
            self._variant_combo.addItem(option.label, option.uri)
        self._variant_combo.setCurrentIndex(0)
        single_playlist = len(result.variants) == 1 and not result.variants[0].bandwidth
        self._variant_row.setVisible(not single_playlist)

    def on_probe_failed(self, request_id: str, message: str) -> None:
        """引擎探测失败（由主窗口转发）。"""
        if request_id != self._request_id:
            return
        self._request_id = ""
        self._probe = None
        self.url_input.set_busy(False)
        self._media_card.setVisible(False)
        self._variant_row.setVisible(False)
        self.messageRequested.emit(f"解析失败：{message}", "danger")

    # ---- 下载 ----

    def _choose_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择保存目录", self._path_edit.text())
        if directory:
            self._path_edit.setText(directory)

    def _start_download(self) -> None:
        if self._probe is None or not self._probe.supported:
            self.messageRequested.emit("请先解析一个受支持的链接", "warning")
            return
        variant_uri = self._variant_combo.currentData() or ""
        quality = ""
        if variant_uri:
            quality = self._variant_combo.currentText().split(" · ")[0]
        self.taskRequested.emit(
            {
                "url": self._probe.url,
                "title": self._probe.title,
                "protocol": self._probe.protocol,
                "output_dir": self._path_edit.text().strip() or self._download_dir,
                "size": self._probe.size_bytes or 0,
                "quality": quality,
                "container": "mp4",
                "variant": variant_uri,
                "live": self._probe.live,
                "headers": dict(self._pending_headers),
            }
        )
        self.url_input.set_text("")
        self.url_input.set_busy(False)
        self._media_card.setVisible(False)
        self._steps_card.setVisible(True)
        self._probe = None
        self._request_id = ""
        self._pending_headers = {}
        self._pending_headers_url = ""