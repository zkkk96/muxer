"""任务卡片：标题、状态、进度、操作按钮。

M1 使用内存里的 ``TaskData``；M2 接入内核后由真实任务快照驱动。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from muxer.ui.theme import manager as theme_manager
from muxer.ui.format import format_duration
from muxer.ui.theme.tokens import Theme
from muxer.ui.widgets import icons
from muxer.ui.widgets.pill import Pill
from muxer.ui.widgets.progress_bar import ProgressBar

# 状态 -> (中文标签, 胶囊语义色)
STATUS_META: dict[str, tuple[str, str]] = {
    "queued": ("排队中", "neutral"),
    "probing": ("解析中", "accent"),
    "downloading": ("下载中", "accent"),
    "postprocessing": ("合并中", "accent"),
    "paused": ("已暂停", "warning"),
    "recording": ("录制中", "accent"),
    "completed": ("已完成", "success"),
    "failed": ("失败", "danger"),
    "canceled": ("已取消", "neutral"),
}


@dataclass
class TaskData:
    """UI 层的任务快照，字段与内核 Task 对齐但更精简。"""

    id: str
    title: str
    protocol: str = "HLS"
    quality: str = "1080P"
    container: str = "MP4"
    status: str = "downloading"
    progress: float = 0.0
    speed: str = "--"
    eta: str = "--"
    size: str = "--"
    error: str = ""
    segments_done: int = 0
    segments_total: int = 0
    live: bool = False
    live_seconds: float = 0.0


class TaskCard(QFrame):
    """一行任务卡片，操作通过信号交回页面处理。"""

    pauseRequested = Signal(str)
    resumeRequested = Signal(str)
    cancelRequested = Signal(str)
    stopRequested = Signal(str)
    openRequested = Signal(str)

    def __init__(self, task: TaskData, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._task = task
        self._icon_specs: dict[QToolButton, tuple[str, str]] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(12)

        self._thumbnail = QFrame(self)
        self._thumbnail.setObjectName("thumbnail")
        self._thumbnail.setFixedSize(44, 44)
        thumbnail_layout = QVBoxLayout(self._thumbnail)
        thumbnail_layout.setContentsMargins(0, 0, 0, 0)
        self._thumb_icon = QLabel(self._thumbnail)
        self._thumb_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumbnail_layout.addWidget(self._thumb_icon)

        text_column = QVBoxLayout()
        text_column.setSpacing(3)
        self._title_label = QLabel(self)
        self._title_label.setObjectName("cardTitle")
        self._meta_label = QLabel(self)
        self._meta_label.setObjectName("caption")
        text_column.addWidget(self._title_label)
        text_column.addWidget(self._meta_label)

        status_column = QVBoxLayout()
        status_column.setSpacing(4)
        status_column.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        self._pill = Pill("", "neutral", self)
        self._percent_label = QLabel(self)
        self._percent_label.setObjectName("caption")
        self._percent_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        status_column.addWidget(self._pill, 0, Qt.AlignmentFlag.AlignRight)
        status_column.addWidget(self._percent_label, 0, Qt.AlignmentFlag.AlignRight)

        top.addWidget(self._thumbnail)
        top.addLayout(text_column, 1)
        top.addLayout(status_column)
        layout.addLayout(top)

        self._progress = ProgressBar(self)
        layout.addWidget(self._progress)

        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        self._stats_label = QLabel(self)
        self._stats_label.setObjectName("caption")
        bottom.addWidget(self._stats_label, 1)

        self._stop_button = self._make_button("stop", "完成录制并合并", bottom, accent=True)
        self._toggle_button = self._make_button("pause", "暂停", bottom)
        self._open_button = self._make_button("folder", "打开所在文件夹", bottom)
        self._cancel_button = self._make_button("close", "取消任务", bottom, danger=True)

        self._toggle_button.clicked.connect(self._on_toggle)
        self._stop_button.clicked.connect(lambda: self.stopRequested.emit(self._task.id))
        self._open_button.clicked.connect(lambda: self.openRequested.emit(self._task.id))
        self._cancel_button.clicked.connect(lambda: self.cancelRequested.emit(self._task.id))
        layout.addLayout(bottom)

        theme_manager.subscribe(self._refresh_theme)
        self.destroyed.connect(lambda: theme_manager.unsubscribe(self._refresh_theme))

        self.update_task(task)
        self._refresh_theme()

    # ---- 构建 ----

    def _make_button(
        self,
        icon_name: str,
        tooltip: str,
        layout: QHBoxLayout,
        danger: bool = False,
        accent: bool = False,
    ) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName("iconButton")
        button.setToolTip(tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFixedSize(28, 28)
        button.setIconSize(QSize(16, 16))
        color_field = "danger" if danger else ("accent" if accent else "text_secondary")
        self._icon_specs[button] = (icon_name, color_field)
        layout.addWidget(button)
        return button

    # ---- 更新 ----

    @property
    def task(self) -> TaskData:
        """当前任务快照（只读用途）。"""
        return self._task

    def apply_changes(self, **changes) -> TaskData:
        """就地修改任务字段并刷新界面，供 M1 的演示交互使用。"""
        for key, value in changes.items():
            setattr(self._task, key, value)
        self.update_task(self._task)
        return self._task

    def update_task(self, task: TaskData) -> None:
        """用新的任务快照刷新界面。"""
        self._task = task
        self._title_label.setText(task.title)
        parts = [part for part in (task.protocol, task.quality, task.container) if part]
        self._meta_label.setText(" · ".join(parts))

        label, tone = STATUS_META.get(task.status, ("未知", "neutral"))
        self._pill.setText(label)
        self._pill.set_tone(tone)

        recording = task.status == "recording"
        percent = int(round(max(0.0, min(1.0, task.progress)) * 100))
        if recording:
            self._percent_label.setText(
                format_duration(task.live_seconds) if task.live_seconds else "录制中"
            )
        else:
            self._percent_label.setText(f"{percent}%")

        self._progress.set_indeterminate(recording)
        self._progress.set_value(task.progress, animate=False)
        if task.status == "completed":
            progress_tone = "success"
        elif task.status == "failed":
            progress_tone = "danger"
        else:
            progress_tone = "accent"
        self._progress.set_tone(progress_tone)

        self._stats_label.setText(self._stats_text(task))
        self._percent_label.setStyleSheet(f"color: {self._percent_color(task.status)};")

        is_paused = task.status in ("paused", "failed")
        finished = task.status in ("completed", "canceled")
        self._toggle_button.setToolTip("继续" if is_paused else "暂停")
        self._toggle_button.setVisible(not finished)
        self._toggle_button.setEnabled(
            task.status in ("downloading", "recording", "queued", "paused", "failed")
        )
        self._icon_specs[self._toggle_button] = ("play" if is_paused else "pause", "text_secondary")
        self._refresh_icon(self._toggle_button)
        self._cancel_button.setVisible(not finished)
        self._stop_button.setVisible(recording)
        if recording:
            self._refresh_icon(self._stop_button)

    def _stats_text(self, task: TaskData) -> str:
        if task.status == "recording":
            parts: list[str] = []
            if task.speed not in ("--", ""):
                parts.append(task.speed)
            if task.live_seconds > 0:
                parts.append(f"已录 {format_duration(task.live_seconds)}")
            if task.segments_done:
                parts.append(f"分片 {task.segments_done}")
            if task.size not in ("--", ""):
                parts.append(task.size)
            return " · ".join(parts) or "正在连接…"
        if task.status == "downloading":
            parts = []
            if task.speed not in ("--", ""):
                parts.append(task.speed)
            if task.eta not in ("--", ""):
                parts.append(f"剩余 {task.eta}")
            if task.segments_total > 0:
                parts.append(f"分片 {task.segments_done}/{task.segments_total}")
            parts.append(task.size)
            return " · ".join(parts)
        if task.status == "paused":
            if task.live and task.live_seconds > 0:
                return f"已暂停 · 已录 {format_duration(task.live_seconds)} · {task.size}"
            return f"已暂停 · {task.size}"
        if task.status == "queued":
            return "等待中 · 即将开始"
        if task.status == "completed":
            if task.segments_total > 0:
                return f"已完成 · {task.segments_total} 个分片 · {task.size}"
            if task.live and task.live_seconds > 0:
                return f"已完成 · 已录 {format_duration(task.live_seconds)} · {task.size}"
            return f"已完成 · {task.size}"
        if task.status == "failed":
            return f"失败 · {task.error or '未知错误'}"
        return task.size

    def _percent_color(self, status: str) -> str:
        theme: Theme = theme_manager.current()
        if status == "completed":
            return theme.success
        if status == "failed":
            return theme.danger
        if status == "paused":
            return theme.text_secondary
        return theme.accent

    # ---- 主题 ----

    def _refresh_theme(self) -> None:
        theme = theme_manager.current()
        self._thumb_icon.setPixmap(icons.pixmap("play", 18, theme.accent))
        for button in self._icon_specs:
            self._refresh_icon(button)
        self._percent_label.setStyleSheet(f"color: {self._percent_color(self._task.status)};")
        self._progress.update()

    def _refresh_icon(self, button: QToolButton) -> None:
        theme = theme_manager.current()
        icon_name, color_field = self._icon_specs.get(button, ("play", "text_secondary"))
        button.setIcon(icons.icon(icon_name, getattr(theme, color_field), 16))

    # ---- 交互 ----

    def _on_toggle(self) -> None:
        if self._task.status in ("paused", "failed"):
            self.resumeRequested.emit(self._task.id)
        else:
            self.pauseRequested.emit(self._task.id)