"""历史页：来自引擎持久化的完成 / 失败记录。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QMessageBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from muxer.core.models import TaskSnapshot, TaskStatus
from muxer.ui.format import (
    format_bytes,
    protocol_label,
    time_clock,
    time_group,
)
from muxer.ui.theme import manager as theme_manager
from muxer.ui.widgets import icons
from muxer.ui.widgets.empty_state import EmptyState
from muxer.ui.widgets.page import Page
from muxer.ui.widgets.pill import Pill

HISTORY_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED})


class HistoryPage(Page):
    """下载历史。"""

    messageRequested = Signal(str, str)
    openRequested = Signal(str)
    redownloadRequested = Signal(dict)
    clearRequested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__("历史", "已完成与失败的下载记录", parent)
        self._entries: dict[str, TaskSnapshot] = {}
        self._keyword = ""

        self._search = QLineEdit(self)
        self._search.setPlaceholderText("搜索标题或协议")
        self._search.setFixedWidth(220)
        self._search.textChanged.connect(self._on_search)
        self.add_action(self._search)

        clear_button = QPushButton("清空记录", self)
        clear_button.setObjectName("ghostButton")
        clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_button.clicked.connect(self._confirm_clear)
        self.add_action(clear_button)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QWidget(scroll)
        self._list_layout = QVBoxLayout(container)
        self._list_layout.setContentsMargins(0, 0, 6, 0)
        self._list_layout.setSpacing(2)
        scroll.setWidget(container)
        self.content.addWidget(scroll, 1)

        self._rebuild()
        theme_manager.subscribe(self._rebuild)
        self.destroyed.connect(lambda: theme_manager.unsubscribe(self._rebuild))

    # ---- 引擎事件接口 ----

    def load_tasks(self, snapshots) -> None:
        """启动时接收全部历史记录。"""
        self._entries = {
            snapshot.id: snapshot
            for snapshot in snapshots
            if snapshot.status in HISTORY_STATUSES
        }
        self._rebuild()

    def clear_entries(self) -> None:
        """引擎确认清空后同步界面。"""
        self._entries.clear()
        self._rebuild()

    def _confirm_clear(self) -> None:
        if not self._entries:
            self.messageRequested.emit("没有可清空的历史记录", "info")
            return
        answer = QMessageBox.question(
            self,
            "清空历史",
            "确定删除全部完成 / 失败记录？此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.clearRequested.emit()

    def upsert(self, snapshot: TaskSnapshot) -> None:
        """任务完成或失败时追加 / 更新记录。"""
        if snapshot.status not in HISTORY_STATUSES:
            return
        self._entries[snapshot.id] = snapshot
        self._rebuild()

    # ---- 构建 ----

    def _rebuild(self) -> None:
        """重建整个列表；条目数量有限，重建比增量维护更简单可靠。"""
        self._clear_layout()
        theme = theme_manager.current()

        visible = [
            snapshot
            for snapshot in sorted(
                self._entries.values(), key=lambda item: item.updated_at, reverse=True
            )
            if self._matches(snapshot)
        ]

        if not visible:
            keyword_hit = bool(self._keyword) and bool(self._entries)
            state = EmptyState(
                "search" if keyword_hit else "clock",
                "没有匹配的记录" if keyword_hit else "暂无下载记录",
                "换个关键词试试。" if keyword_hit else "完成或失败的任务会显示在这里。",
                parent=self,
            )
            self._list_layout.addWidget(state, 1)
            self._list_layout.addStretch(0)
            return

        current_group = ""
        for snapshot in visible:
            group = time_group(snapshot.updated_at)
            if group != current_group:
                current_group = group
                label = QLabel(group, self)
                label.setObjectName("caption")
                label.setContentsMargins(4, 12, 0, 4)
                self._list_layout.addWidget(label)
            self._list_layout.addWidget(self._build_row(snapshot, theme))
        self._list_layout.addStretch(1)

    def _build_row(self, snapshot: TaskSnapshot, theme) -> QFrame:
        row = QFrame(self)
        row.setObjectName("historyRow")
        row.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        layout = QHBoxLayout(row)
        layout.setContentsMargins(8, 10, 8, 10)
        layout.setSpacing(12)

        icon_label = QLabel(row)
        icon_label.setFixedSize(28, 28)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setPixmap(icons.pixmap("play", 14, theme.accent))

        column = QVBoxLayout()
        column.setSpacing(2)
        title = QLabel(snapshot.title, row)
        title.setObjectName("cardTitle")

        size = snapshot.bytes_total or snapshot.bytes_done
        meta_text = f"{time_clock(snapshot.updated_at)} · {format_bytes(size)} · {protocol_label(snapshot.protocol)}"
        meta = QLabel(meta_text, row)
        meta.setObjectName("caption")
        if snapshot.status is TaskStatus.FAILED and snapshot.error:
            meta.setToolTip(snapshot.error)
        column.addWidget(title)
        column.addWidget(meta)

        completed = snapshot.status is TaskStatus.COMPLETED
        pill = Pill("已完成" if completed else "失败", "success" if completed else "danger", row)

        redownload_button = QPushButton("重新下载", row)
        redownload_button.setObjectName("ghostButton")
        redownload_button.setCursor(Qt.CursorShape.PointingHandCursor)
        redownload_button.clicked.connect(
            lambda _checked=False, item=snapshot: self.redownloadRequested.emit(
                {
                    "url": item.url,
                    "title": item.title,
                    "protocol": item.protocol,
                    "output_dir": item.output_dir,
                    "size": 0,
                    "quality": item.quality,
                    "container": item.container,
                    "live": item.live,
                    "headers": dict(item.headers),
                }
            )
        )

        open_button = QPushButton("打开位置", row)
        open_button.setObjectName("ghostButton")
        open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        open_button.clicked.connect(
            lambda _checked=False, path=snapshot.output_path or snapshot.output_dir: self.openRequested.emit(path)
        )

        layout.addWidget(icon_label)
        layout.addLayout(column, 1)
        layout.addWidget(pill)
        layout.addWidget(redownload_button)
        layout.addWidget(open_button)
        return row

    def _clear_layout(self) -> None:
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    # ---- 搜索 ----

    def _on_search(self, text: str) -> None:
        self._keyword = text.strip().lower()
        self._rebuild()

    def _matches(self, snapshot: TaskSnapshot) -> bool:
        if not self._keyword:
            return True
        haystack = " ".join(
            (snapshot.title, protocol_label(snapshot.protocol), snapshot.error)
        ).lower()
        return self._keyword in haystack