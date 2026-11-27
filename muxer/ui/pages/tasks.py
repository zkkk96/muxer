"""任务页：真实任务列表，由引擎事件驱动。

页面只负责显示与收集用户操作，状态全部来自引擎推送的快照。
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from muxer.core.models import TaskSnapshot, TaskStatus
from muxer.ui.format import (
    format_bytes,
    format_eta,
    format_size_pair,
    format_speed,
    protocol_label,
)
from muxer.ui.widgets.empty_state import EmptyState
from muxer.ui.widgets.page import Page
from muxer.ui.widgets.task_card import TaskCard, TaskData

FILTERS = ("全部", "进行中", "已完成")
ACTIVE_FOR_FILTER = frozenset(
    {TaskStatus.QUEUED, TaskStatus.DOWNLOADING, TaskStatus.PAUSED, TaskStatus.FAILED}
)


class TasksPage(Page):
    """下载任务列表。"""

    messageRequested = Signal(str, str)
    countChanged = Signal(int)
    pauseRequested = Signal(str)
    resumeRequested = Signal(str)
    cancelRequested = Signal(str)
    stopRequested = Signal(str)
    openRequested = Signal(str)
    archiveRequested = Signal()
    newDownloadRequested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__("任务", "下载队列与实时进度", parent)
        self._snapshots: dict[str, TaskSnapshot] = {}
        self._cards: dict[str, TaskCard] = {}
        self._filter_index = 0
        self._bulk = False

        self.add_action(self._build_filter())

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        pause_all = QPushButton("全部暂停", self)
        pause_all.setObjectName("ghostButton")
        pause_all.setCursor(Qt.CursorShape.PointingHandCursor)
        pause_all.clicked.connect(self.pause_all)

        clear_done = QPushButton("清除已完成", self)
        clear_done.setObjectName("ghostButton")
        clear_done.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_done.clicked.connect(self._archive_completed)

        toolbar.addStretch(1)
        toolbar.addWidget(pause_all)
        toolbar.addWidget(clear_done)
        self.content.addLayout(toolbar)
        self.content.addWidget(self._build_scroll_area(), 1)

        self._refresh_counts()

    # ---- 构建 ----

    def _build_filter(self) -> QFrame:
        frame = QFrame(self)
        frame.setObjectName("segmented")
        frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)

        group = QButtonGroup(frame)
        group.setExclusive(True)
        for index, text in enumerate(FILTERS):
            button = QPushButton(text, frame)
            button.setObjectName("segmentButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setChecked(index == 0)
            button.clicked.connect(lambda _checked, idx=index: self._set_filter(idx))
            group.addButton(button, index)
            layout.addWidget(button)
        return frame

    def _build_scroll_area(self) -> QScrollArea:
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        container = QWidget(scroll)
        self._list_layout = QVBoxLayout(container)
        self._list_layout.setContentsMargins(0, 0, 6, 0)
        self._list_layout.setSpacing(10)

        self._empty = EmptyState(
            "inbox",
            "还没有任务",
            "在「新建下载」粘贴链接，确认保存位置后开始下载。",
            "去新建下载",
            container,
        )
        self._empty.actionClicked.connect(self.newDownloadRequested)
        self._list_layout.addWidget(self._empty, 1)
        self._list_layout.addStretch(0)
        scroll.setWidget(container)
        return scroll

    # ---- 引擎事件接口 ----

    def load_tasks(self, snapshots: tuple[TaskSnapshot, ...] | list[TaskSnapshot]) -> None:
        """启动时整体重建列表。"""
        self._bulk = True
        try:
            for card in self._cards.values():
                card.setParent(None)
                card.deleteLater()
            self._cards.clear()
            self._snapshots.clear()

            # 按时间升序处理，每次插入到顶部，最终得到最新在前的顺序
            ordered = sorted(snapshots, key=lambda item: item.updated_at)
            for snapshot in ordered:
                self._apply_snapshot(snapshot, silent=True)
        finally:
            self._bulk = False
        self._refresh_counts()
        self._apply_filter()

    def upsert(self, snapshot: TaskSnapshot) -> None:
        """任务创建 / 进度更新 / 状态变化。"""
        self._apply_snapshot(snapshot, silent=False)

    def remove(self, task_id: str) -> None:
        """任务被取消或归档。"""
        self._snapshots.pop(task_id, None)
        card = self._cards.pop(task_id, None)
        if card is not None:
            card.setParent(None)
            card.deleteLater()
        self._refresh_counts()
        self._apply_filter()

    def active_count(self) -> int:
        """进行中（排队 + 下载）的任务数。"""
        return sum(
            1
            for snapshot in self._snapshots.values()
            if snapshot.status in (TaskStatus.QUEUED, TaskStatus.DOWNLOADING)
        )

    # ---- 内部：快照 -> 卡片 ----

    def _apply_snapshot(self, snapshot: TaskSnapshot, silent: bool) -> None:
        if snapshot.archived or snapshot.status is TaskStatus.CANCELED:
            self.remove(snapshot.id)
            return

        previous = self._snapshots.get(snapshot.id)
        self._snapshots[snapshot.id] = snapshot

        card = self._cards.get(snapshot.id)
        if card is None:
            card = TaskCard(self._view(snapshot), self)
            card.pauseRequested.connect(self.pauseRequested)
            card.resumeRequested.connect(self.resumeRequested)
            card.cancelRequested.connect(self._on_card_cancel)
            card.stopRequested.connect(self.stopRequested)
            card.openRequested.connect(self._open_task)
            self._cards[snapshot.id] = card
            self._list_layout.insertWidget(0, card)
            if not self._bulk:
                self._animate_card_in(card)
        else:
            card.update_task(self._view(snapshot))

        if not silent and previous is not None and previous.status is not snapshot.status:
            self._notify_transition(previous, snapshot)

        if not self._bulk:
            self._refresh_counts()
            self._apply_filter()

    @staticmethod
    def _animate_card_in(card: TaskCard) -> None:
        """新任务卡片淡入，避免列表突然出现。"""
        effect = QGraphicsOpacityEffect(card)
        card.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"opacity", card)
        animation.setDuration(220)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.finished.connect(lambda: card.setGraphicsEffect(None))
        card._fade_animation = animation  # noqa: SLF001 - 保持动画对象存活
        animation.start()

    def _view(self, snapshot: TaskSnapshot) -> TaskData:
        """把内核快照转成卡片视图模型。"""
        downloading = snapshot.status is TaskStatus.DOWNLOADING
        status_value = "recording" if snapshot.live and downloading else snapshot.status.value
        speed = format_speed(snapshot.speed_bps) if downloading else "--"
        eta = format_eta(snapshot.eta_seconds) if downloading else "--"
        if snapshot.status is TaskStatus.COMPLETED:
            size_text = format_bytes(snapshot.bytes_total or snapshot.bytes_done)
        else:
            size_text = format_size_pair(snapshot.bytes_done, snapshot.bytes_total)
        return TaskData(
            id=snapshot.id,
            title=snapshot.title,
            protocol=protocol_label(snapshot.protocol),
            quality=snapshot.quality,
            container=snapshot.container,
            status=status_value,
            progress=snapshot.progress,
            speed=speed,
            eta=eta,
            size=size_text,
            error=snapshot.error,
            segments_done=snapshot.segments_done,
            segments_total=snapshot.segments_total,
            live=snapshot.live,
            live_seconds=snapshot.live_seconds,
        )

    def _notify_transition(self, previous: TaskSnapshot, current: TaskSnapshot) -> None:
        if current.status is TaskStatus.COMPLETED:
            self.messageRequested.emit(f"下载完成：{current.title}", "success")
        elif current.status is TaskStatus.FAILED:
            self.messageRequested.emit(f"下载失败：{current.error or current.title}", "danger")
        elif current.status is TaskStatus.PAUSED and previous.status is TaskStatus.DOWNLOADING:
            self.messageRequested.emit("任务已暂停", "info")

    # ---- 交互 ----

    def _on_card_cancel(self, task_id: str) -> None:
        self.cancelRequested.emit(task_id)

    def _open_task(self, task_id: str) -> None:
        snapshot = self._snapshots.get(task_id)
        if snapshot is None:
            return
        self.openRequested.emit(snapshot.output_path or snapshot.output_dir)

    def pause_all(self) -> None:
        targets = [
            snapshot.id
            for snapshot in self._snapshots.values()
            if snapshot.status in (TaskStatus.QUEUED, TaskStatus.DOWNLOADING)
        ]
        for task_id in targets:
            self.pauseRequested.emit(task_id)
        self.messageRequested.emit(f"已请求暂停 {len(targets)} 个任务", "info")

    def _archive_completed(self) -> None:
        if any(snapshot.status is TaskStatus.COMPLETED for snapshot in self._snapshots.values()):
            self.archiveRequested.emit()
            self.messageRequested.emit("正在清除已完成任务", "info")
        else:
            self.messageRequested.emit("没有已完成的任务可清除", "info")

    # ---- 视图状态 ----

    def _set_filter(self, index: int) -> None:
        self._filter_index = index
        self._apply_filter()

    def _apply_filter(self) -> None:
        for task_id, card in self._cards.items():
            snapshot = self._snapshots.get(task_id)
            if snapshot is None:
                card.setVisible(False)
                continue
            if self._filter_index == 0:
                visible = True
            elif self._filter_index == 1:
                visible = snapshot.status in ACTIVE_FOR_FILTER
            else:
                visible = snapshot.status is TaskStatus.COMPLETED
            card.setVisible(visible)

        if not self._cards:
            self._empty.set_texts("还没有任务", "在「新建下载」粘贴链接，确认保存位置后开始下载。")
            self._empty.setVisible(True)
            return
        any_visible = any(card.isVisible() for card in self._cards.values())
        if any_visible:
            self._empty.setVisible(False)
        else:
            self._empty.set_texts("当前筛选没有任务", "切换到「全部」查看其他任务。")
            self._empty.setVisible(True)

    def _refresh_counts(self) -> None:
        active = sum(
            1
            for snapshot in self._snapshots.values()
            if snapshot.status in (TaskStatus.QUEUED, TaskStatus.DOWNLOADING)
        )
        total = len(self._snapshots)
        self.countChanged.emit(active)
        self.set_subtitle(f"{active} 个进行中 · 共 {total} 个任务")