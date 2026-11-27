"""首次启动引导：确认下载目录并给出环境提示。"""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from muxer.config import AppConfig, save_config
from muxer.core.ffmpeg import locate
from muxer.ui.widgets import icons


class OnboardingDialog(QDialog):
    """首次运行时弹出的欢迎与基础设置。"""

    def __init__(self, config: AppConfig, parent: QWidget | None = None):
        super().__init__(parent)
        self._config = config
        self.setWindowTitle("欢迎使用 Muxer")
        self.setObjectName("onboardingDialog")
        self.setModal(True)
        self.setFixedWidth(540)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 22)
        layout.setSpacing(12)

        logo = QLabel(self)
        logo.setPixmap(icons.app_icon().pixmap(56, 56))
        title = QLabel("欢迎使用 Muxer", self)
        title.setObjectName("pageTitle")
        subtitle = QLabel("HLS / DASH / FLV 流媒体下载器", self)
        subtitle.setObjectName("caption")

        intro = QLabel(
            "先确认默认下载目录，之后可以在「设置」里随时修改。"
            "粘贴直链或使用内置浏览器嗅探，即可开始下载。",
            self,
        )
        intro.setWordWrap(True)
        intro.setObjectName("secondary")

        ffmpeg_status = self._ffmpeg_status()
        ffmpeg_label = QLabel(ffmpeg_status, self)
        ffmpeg_label.setObjectName("caption")
        ffmpeg_label.setWordWrap(True)

        directory_row = QHBoxLayout()
        directory_row.setSpacing(8)
        self._directory_edit = QLineEdit(config.download_dir, self)
        browse = QPushButton("选择", self)
        browse.setCursor(Qt.CursorShape.PointingHandCursor)
        browse.clicked.connect(self._choose_directory)
        directory_row.addWidget(self._directory_edit, 1)
        directory_row.addWidget(browse)

        self._error_label = QLabel("", self)
        self._error_label.setObjectName("caption")
        self._error_label.setStyleSheet("color: #B4453B;")
        self._error_label.setVisible(False)

        notice = QLabel(
            "仅用于下载你有权获取的内容。遇到 DRM 加密流会明确提示，不做解密绕过。",
            self,
        )
        notice.setObjectName("caption")
        notice.setWordWrap(True)

        actions = QHBoxLayout()
        actions.addStretch(1)
        start_button = QPushButton("开始使用", self)
        start_button.setObjectName("primaryButton")
        start_button.setCursor(Qt.CursorShape.PointingHandCursor)
        start_button.clicked.connect(self._accept)
        actions.addWidget(start_button)

        layout.addWidget(logo)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(6)
        layout.addWidget(intro)
        layout.addWidget(ffmpeg_label)
        layout.addSpacing(4)
        download_label = QLabel("默认下载目录", self)
        download_label.setObjectName("caption")
        layout.addWidget(download_label)
        layout.addLayout(directory_row)
        layout.addWidget(self._error_label)
        layout.addSpacing(4)
        layout.addWidget(notice)
        layout.addSpacing(6)
        layout.addLayout(actions)

        self.adjustSize()

    def _ffmpeg_status(self) -> str:
        found = locate(self._config.ffmpeg_path)
        if found is not None:
            return f"已检测到 ffmpeg：{found.ffmpeg}"
        if shutil.which("ffmpeg"):
            return "已从 PATH 检测到 ffmpeg。"
        return "未检测到 ffmpeg：合并与转封装功能不可用，稍后可在设置中指定路径。"

    def _choose_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择默认下载目录", self._directory_edit.text()
        )
        if directory:
            self._directory_edit.setText(directory)

    def _accept(self) -> None:
        directory = Path(self._directory_edit.text().strip())
        if not str(directory):
            self._show_error("请选择一个下载目录")
            return
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._show_error(f"无法创建目录：{exc}")
            return

        self._config.download_dir = str(directory)
        self._config.first_run_completed = True
        save_config(self._config)
        self.accept()

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.setVisible(True)