"""设置页：外观、下载参数、ffmpeg 与关于信息。"""

from __future__ import annotations

import shutil

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from muxer import APP_DESCRIPTION, APP_NAME, APP_VERSION
from muxer.config import APP_DATA_DIR, AppConfig, save_config
from muxer.ui.theme import manager as theme_manager
from muxer.ui.widgets import icons
from muxer.ui.widgets.page import Page

THEME_CHOICES = (("light", "浅色"), ("dark", "深色"), ("system", "跟随系统"))


class SettingsPage(Page):
    """应用设置。"""

    messageRequested = Signal(str, str)
    rateLimitChanged = Signal(int)
    downloadDirChanged = Signal(str)
    showLogsRequested = Signal()

    def __init__(self, config: AppConfig, parent: QWidget | None = None):
        super().__init__("设置", "外观、下载参数与运行环境", parent)
        self._config = config
        self._theme_buttons: dict[str, QPushButton] = {}

        container = QWidget(self)
        sections = QVBoxLayout(container)
        sections.setContentsMargins(0, 0, 6, 0)
        sections.setSpacing(14)
        sections.addWidget(self._build_general())
        sections.addWidget(self._build_appearance())
        sections.addWidget(self._build_download())
        sections.addWidget(self._build_environment())
        sections.addWidget(self._build_about())
        sections.addStretch(1)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(container)
        self.content.addWidget(scroll, 1)

        theme_manager.subscribe(self._sync_theme_buttons)
        self.destroyed.connect(lambda: theme_manager.unsubscribe(self._sync_theme_buttons))
        self._sync_theme_buttons()

    # ---- 分区构建 ----

    def _section(self, title: str) -> QFrame:
        frame = QFrame(self)
        frame.setObjectName("settingsSection")
        frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
        label = QLabel(title, frame)
        label.setObjectName("sectionTitle")
        layout.addWidget(label)
        return frame

    def _row(self, text: str, control: QWidget, stretch: bool = False) -> QWidget:
        """一行设置项；固定尺寸控件左对齐，可拉伸控件填满剩余宽度。"""
        container = QWidget(control.parentWidget())
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        label = QLabel(text, container)
        label.setObjectName("secondary")
        label.setFixedWidth(140)
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(label)
        row.addWidget(control, 1 if stretch else 0)
        if not stretch:
            row.addStretch(1)
        return container

    def _build_general(self) -> QFrame:
        frame = self._section("常规")

        self._tray_check = QCheckBox("关闭窗口时最小化到托盘", frame)
        self._tray_check.setChecked(self._config.minimize_to_tray)
        self._tray_check.toggled.connect(self._save_tray_preference)

        buttons = QWidget(frame)
        button_row = QHBoxLayout(buttons)
        button_row.setContentsMargins(0, 0, 0, 0)
        button_row.setSpacing(8)
        log_button = QPushButton("查看日志", buttons)
        log_button.setCursor(Qt.CursorShape.PointingHandCursor)
        log_button.clicked.connect(self.showLogsRequested)

        data_button = QPushButton("打开数据目录", buttons)
        data_button.setCursor(Qt.CursorShape.PointingHandCursor)
        data_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(APP_DATA_DIR)))
        )
        folder_button = QPushButton("打开日志目录", buttons)
        folder_button.setCursor(Qt.CursorShape.PointingHandCursor)
        folder_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(APP_DATA_DIR / "logs")))
        )
        button_row.addWidget(log_button)
        button_row.addWidget(data_button)
        button_row.addWidget(folder_button)
        button_row.addStretch(1)

        layout = frame.layout()
        layout.addWidget(self._row("托盘", self._tray_check))
        layout.addWidget(self._row("本地数据", buttons, stretch=True))
        return frame

    def _build_appearance(self) -> QFrame:
        frame = self._section("外观")
        segmented = QFrame(frame)
        segmented.setObjectName("segmented")
        segmented.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QHBoxLayout(segmented)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)

        group = QButtonGroup(segmented)
        group.setExclusive(True)
        for key, text in THEME_CHOICES:
            button = QPushButton(text, segmented)
            button.setObjectName("segmentButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked, mode=key: self._apply_theme(mode))
            group.addButton(button)
            layout.addWidget(button)
            self._theme_buttons[key] = button

        wrapper = QWidget(frame)
        wrapper_layout = QHBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.addWidget(segmented)
        wrapper_layout.addStretch(1)

        frame.layout().addWidget(self._row("主题", wrapper, stretch=True))
        return frame

    def _build_download(self) -> QFrame:
        frame = self._section("下载")

        directory = QWidget(frame)
        directory_row = QHBoxLayout(directory)
        directory_row.setContentsMargins(0, 0, 0, 0)
        directory_row.setSpacing(8)
        self._dir_edit = QLineEdit(self._config.download_dir, directory)
        browse = QPushButton("选择", directory)
        browse.setCursor(Qt.CursorShape.PointingHandCursor)
        browse.clicked.connect(self._choose_directory)
        directory_row.addWidget(self._dir_edit, 1)
        directory_row.addWidget(browse)
        self._dir_edit.editingFinished.connect(self._save_directory)

        self._task_spin = self._spin(1, 5, self._config.task_concurrency, frame)
        self._task_spin.setToolTip("同时下载的任务数，修改后下次启动生效")
        self._task_spin.valueChanged.connect(self._save_task_concurrency)

        self._segment_spin = self._spin(1, 32, self._config.segment_concurrency, frame)
        self._segment_spin.setToolTip("单任务同时下载的分片数，修改后下次启动生效")
        self._segment_spin.valueChanged.connect(self._save_segment_concurrency)

        self._rate_spin = self._spin(0, 102400, self._config.rate_limit_kbps, frame, suffix=" KB/s")
        self._rate_spin.setSpecialValueText("不限速")
        self._rate_spin.valueChanged.connect(self._save_rate_limit)

        self._live_spin = self._spin(0, 720, self._config.live_max_minutes, frame, suffix=" 分钟")
        self._live_spin.setSpecialValueText("不限")
        self._live_spin.setToolTip("直播录制到达该时长后自动完成并合并；修改后下次启动生效")
        self._live_spin.valueChanged.connect(self._save_live_limit)

        layout = frame.layout()
        layout.addWidget(self._row("默认保存目录", directory, stretch=True))
        layout.addWidget(self._row("同时下载任务数", self._task_spin))
        layout.addWidget(self._row("单任务分片并发", self._segment_spin))
        layout.addWidget(self._row("全局限速", self._rate_spin))
        layout.addWidget(self._row("最长录制时长", self._live_spin))
        return frame

    def _build_environment(self) -> QFrame:
        frame = self._section("运行环境")
        self._ffmpeg_label = QLabel(frame)
        self._ffmpeg_label.setObjectName("caption")
        self._ffmpeg_label.setWordWrap(True)
        self._ffmpeg_label.setText(self._ffmpeg_status())

        choose = QPushButton("指定 ffmpeg 路径", frame)
        choose.setToolTip("修改后下次启动生效")
        choose.setCursor(Qt.CursorShape.PointingHandCursor)
        choose.clicked.connect(self._choose_ffmpeg)

        wrapper = QWidget(frame)
        row = QHBoxLayout(wrapper)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(self._ffmpeg_label, 1)
        row.addWidget(choose)

        frame.layout().addWidget(self._row("ffmpeg", wrapper, stretch=True))
        return frame

    def _build_about(self) -> QFrame:
        frame = self._section("关于")
        block = QWidget(frame)
        row = QHBoxLayout(block)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(14)

        logo = QLabel(block)
        logo.setPixmap(icons.app_icon().pixmap(48, 48))
        logo.setFixedSize(48, 48)

        column = QVBoxLayout()
        column.setSpacing(2)
        name = QLabel(f"{APP_NAME} {APP_VERSION}", block)
        name.setObjectName("cardTitle")
        description = QLabel(APP_DESCRIPTION, block)
        description.setObjectName("caption")
        column.addWidget(name)
        column.addWidget(description)

        row.addWidget(logo)
        row.addLayout(column, 1)

        notice = QLabel(
            "仅用于下载你有权获取的内容。遇到 DRM 加密流会明确提示并不做解密绕过。",
            frame,
        )
        notice.setObjectName("caption")
        notice.setWordWrap(True)

        frame.layout().addWidget(block)
        frame.layout().addWidget(notice)
        return frame

    # ---- 控件工厂 ----

    def _spin(self, minimum: int, maximum: int, value: int, parent: QWidget, suffix: str = "") -> QSpinBox:
        spin = QSpinBox(parent)
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        spin.setFixedWidth(140)
        if suffix:
            spin.setSuffix(suffix)
        return spin

    # ---- 行为 ----

    def _apply_theme(self, mode: str) -> None:
        theme_manager.apply(mode)
        self._config.theme = mode
        save_config(self._config)

    def _sync_theme_buttons(self) -> None:
        active = theme_manager.mode()
        for key, button in self._theme_buttons.items():
            button.blockSignals(True)
            button.setChecked(key == active)
            button.blockSignals(False)

    def _choose_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择默认保存目录", self._dir_edit.text()
        )
        if directory:
            self._dir_edit.setText(directory)
            self._save_directory()

    def _save_directory(self) -> None:
        self._config.download_dir = self._dir_edit.text().strip() or self._config.download_dir
        save_config(self._config)
        self.downloadDirChanged.emit(self._config.download_dir)

    def _save_task_concurrency(self, value: int) -> None:
        self._config.task_concurrency = value
        save_config(self._config)

    def _save_segment_concurrency(self, value: int) -> None:
        self._config.segment_concurrency = value
        save_config(self._config)

    def _save_tray_preference(self, enabled: bool) -> None:
        self._config.minimize_to_tray = enabled
        save_config(self._config)

    def refresh_download_dir(self, directory: str) -> None:
        """首次引导修改目录后同步输入框。"""
        self._dir_edit.setText(directory)

    def _save_live_limit(self, value: int) -> None:
        self._config.live_max_minutes = value
        save_config(self._config)

    def _save_rate_limit(self, value: int) -> None:
        self._config.rate_limit_kbps = value
        save_config(self._config)
        self.rateLimitChanged.emit(value)

    def _choose_ffmpeg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 ffmpeg 可执行文件", "", "可执行文件 (*.exe);;所有文件 (*)"
        )
        if not path:
            return
        self._config.ffmpeg_path = path
        save_config(self._config)
        self._ffmpeg_label.setText(self._ffmpeg_status())
        self.messageRequested.emit("ffmpeg 路径已保存", "success")

    def _ffmpeg_status(self) -> str:
        if self._config.ffmpeg_path:
            return f"已指定：{self._config.ffmpeg_path}"
        found = shutil.which("ffmpeg")
        if found:
            return f"已从 PATH 检测到：{found}"
        return "未检测到 ffmpeg，合并与转封装功能不可用"