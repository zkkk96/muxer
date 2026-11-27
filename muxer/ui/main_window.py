"""主窗口：无边框外壳、页面路由、引擎事件分发。"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QEvent, QPoint, QPropertyAnimation, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from muxer.config import AppConfig, save_config
from muxer.core.models import TaskStatus
from muxer.core.engine import EngineThread
from muxer.core.events import (
    HistoryCleared,
    ProbeFailed,
    ProbeFinished,
    TaskRemoved,
    TasksLoaded,
    TaskUpserted,
)
from muxer.ui.pages.history import HistoryPage
from muxer.ui.pages.new_download import NewDownloadPage
from muxer.ui.pages.settings import SettingsPage
from muxer.ui.pages.sniffer import SnifferPage
from muxer.ui.pages.tasks import TasksPage
from muxer.ui.sidebar import Sidebar
from muxer.ui.log_dialog import LogDialog
from muxer.ui.logging_bridge import LogStore
from muxer.ui.theme import manager as theme_manager
from muxer.ui.titlebar import TitleBar
from muxer.ui.widgets import icons
from muxer.ui.widgets.toast import ToastManager

LOGGER = logging.getLogger(__name__)

PAGE_NEW, PAGE_TASKS, PAGE_HISTORY, PAGE_SNIFFER, PAGE_SETTINGS = range(5)


class MainWindow(QWidget):
    """应用主窗口。"""

    EDGE_MARGIN = 12
    RESIZE_BORDER = 10

    def __init__(
        self,
        config: AppConfig,
        start_engine: bool = True,
        engine_data_dir: Path | None = None,
        enable_tray: bool = True,
    ):
        super().__init__()
        self._config = config
        self._engine_data_dir = engine_data_dir
        self._enable_tray = enable_tray
        self._tray = None
        self._quitting = False
        self._tray_hint_shown = False
        self._watchdog_started = False
        self._log_store = LogStore(self)
        self.setWindowTitle("Muxer")
        self.setWindowIcon(icons.app_icon())
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setMinimumSize(980, 640)
        self.resize(1180, 800)

        self._engine: EngineThread | None = None
        self._build_shell()
        self._build_pages()
        self._wire_signals()
        self._toasts = ToastManager(self)
        self._setup_tray(enable_tray)
        self._start_engine(start_engine)

        theme_manager.subscribe(self._refresh_theme)
        self._refresh_theme()

    # ---- 结构 ----

    def _build_shell(self) -> None:
        self._root = QVBoxLayout(self)
        margin = self.EDGE_MARGIN
        self._root.setContentsMargins(margin, margin, margin, margin)
        self._root.setSpacing(0)

        self._body = QFrame(self)
        self._body.setObjectName("windowBody")
        self._body.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._shadow = QGraphicsDropShadowEffect(self._body)
        self._shadow.setBlurRadius(38)
        self._shadow.setOffset(0, 8)
        self._shadow.setColor(QColor(0, 0, 0, 60))
        self._body.setGraphicsEffect(self._shadow)

        self._root.addWidget(self._body)

        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        self._titlebar = TitleBar(self, self._body)
        body_layout.addWidget(self._titlebar)

        content = QHBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)

        self._sidebar = Sidebar(self._body)
        self._stack = QStackedWidget(self._body)
        self._stack.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        content.addWidget(self._sidebar)
        content.addWidget(self._stack, 1)
        body_layout.addLayout(content, 1)

    def _build_pages(self) -> None:
        self._new_page = NewDownloadPage(self._config.download_dir, self._stack)
        self._tasks_page = TasksPage(self._stack)
        self._history_page = HistoryPage(self._stack)
        self._sniffer_page = SnifferPage(self._stack)
        self._settings_page = SettingsPage(self._config, self._stack)

        for page in (
            self._new_page,
            self._tasks_page,
            self._history_page,
            self._sniffer_page,
            self._settings_page,
        ):
            self._stack.addWidget(page)

    def _wire_signals(self) -> None:
        self._sidebar.pageChanged.connect(self.go_to)
        self._sidebar.themeToggleRequested.connect(self._toggle_theme)

        self._new_page.messageRequested.connect(self._show_toast)
        self._new_page.probeRequested.connect(self._on_probe_requested)
        self._new_page.taskRequested.connect(self._on_task_requested)

        self._tasks_page.messageRequested.connect(self._show_toast)
        self._tasks_page.countChanged.connect(lambda count: self._sidebar.set_badge(PAGE_TASKS, count))
        self._tasks_page.pauseRequested.connect(self._on_pause_requested)
        self._tasks_page.resumeRequested.connect(self._on_resume_requested)
        self._tasks_page.cancelRequested.connect(self._on_cancel_requested)
        self._tasks_page.stopRequested.connect(self._on_stop_requested)
        self._tasks_page.archiveRequested.connect(self._on_archive_requested)
        self._tasks_page.openRequested.connect(self._open_path)
        self._tasks_page.newDownloadRequested.connect(lambda: self.go_to(PAGE_NEW))

        self._sniffer_page.messageRequested.connect(self._show_toast)
        self._sniffer_page.probeRequested.connect(self._on_probe_requested)
        self._sniffer_page.downloadRequested.connect(self._on_sniff_download)
        self._sniffer_page.batchRequested.connect(self._on_sniff_batch)

        self._history_page.messageRequested.connect(self._show_toast)
        self._history_page.openRequested.connect(self._open_path)
        self._history_page.redownloadRequested.connect(self._on_redownload_requested)
        self._history_page.clearRequested.connect(self._on_clear_history)

        self._settings_page.messageRequested.connect(self._show_toast)
        self._settings_page.rateLimitChanged.connect(self._on_rate_limit_changed)
        self._settings_page.showLogsRequested.connect(self._show_logs)
        self._settings_page.downloadDirChanged.connect(self.apply_download_dir)

        self._sidebar.set_badge(PAGE_TASKS, 0)

    def _start_engine(self, enabled: bool) -> None:
        if not enabled:
            return
        self._engine = EngineThread(self._config, self, data_dir=self._engine_data_dir)
        self._engine.event.connect(self._on_engine_event)
        self._engine.start()

    # ---- 页面与提示 ----

    def go_to(self, index: int) -> None:
        """切换页面并做一次淡入。"""
        if not 0 <= index < self._stack.count():
            return
        self._stack.setCurrentIndex(index)
        self._sidebar.set_current(index)
        page = self._stack.currentWidget()
        effect = QGraphicsOpacityEffect(page)
        page.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"opacity", self)
        animation.setDuration(200)
        animation.setStartValue(0.35)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.finished.connect(lambda: page.setGraphicsEffect(None))
        self._page_animation = animation
        animation.start()

    def _show_toast(self, message: str, tone: str = "info") -> None:
        self._toasts.show(message, tone)

    # ---- 引擎事件 ----

    def _on_engine_event(self, event: object) -> None:
        if isinstance(event, TasksLoaded):
            self._tasks_page.load_tasks(event.tasks)
            self._history_page.load_tasks(event.tasks)
            self._sidebar.set_badge(PAGE_TASKS, self._tasks_page.active_count())
        elif isinstance(event, TaskUpserted):
            self._tasks_page.upsert(event.task)
            self._history_page.upsert(event.task)
            self._sidebar.set_badge(PAGE_TASKS, self._tasks_page.active_count())
            self._notify_if_hidden(event.task)
        elif isinstance(event, TaskRemoved):
            self._tasks_page.remove(event.task_id)
            self._sidebar.set_badge(PAGE_TASKS, self._tasks_page.active_count())
        elif isinstance(event, HistoryCleared):
            self._history_page.clear_entries()
        elif isinstance(event, ProbeFinished):
            self._new_page.on_probe_finished(event.result)
            self._sniffer_page.on_probe_finished(event.result)
        elif isinstance(event, ProbeFailed):
            self._new_page.on_probe_failed(event.request_id, event.message)
            self._sniffer_page.on_probe_failed(event.request_id, event.message)
        else:
            LOGGER.debug("未处理引擎事件: %r", event)

    # ---- 用户操作 ----

    def _on_probe_requested(self, payload: dict) -> None:
        if self._engine is None:
            self._show_toast("引擎未启动，无法解析", "danger")
            return
        self._engine.probe(
            str(payload.get("request_id", "")),
            str(payload.get("url", "")),
            payload.get("headers") or {},
        )

    def _on_task_requested(self, payload: dict) -> None:
        if self._engine is None:
            self._show_toast("引擎未启动，无法下载", "danger")
            return
        self._engine.start_task(payload)
        self.go_to(PAGE_TASKS)
        self._show_toast("已加入下载队列", "success")

    def _on_sniff_download(self, url: str, headers: dict, title: str) -> None:
        """嗅探结果单个下载：填入新建页并自动探测，自动继承请求头。"""
        self._new_page.prepare(url, headers)
        self.go_to(PAGE_NEW)
        self._show_toast("已填入链接与请求头，确认后开始下载", "info")

    def _on_sniff_batch(self, items: list) -> None:
        """嗅探批量下载：探测协议与直播状态后自动入队。"""
        if self._engine is None:
            self._show_toast("引擎未启动，无法批量下载", "danger")
            return
        for item in items:
            self._engine.probe_and_start(
                {
                    **item,
                    "output_dir": self._config.download_dir,
                }
            )

    def apply_download_dir(self, directory: str) -> None:
        """首次引导或外部修改后同步默认下载目录。"""
        self._config.download_dir = directory
        self._new_page.set_download_dir(directory)
        self._settings_page.refresh_download_dir(directory)

    def show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def pause_all_tasks(self) -> None:
        self._tasks_page.pause_all()

    def quit_application(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self._start_exit_watchdog()
        if self._tray is not None:
            self._tray.hide()
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _start_exit_watchdog(self) -> None:
        """兜底：Qt / QtWebEngine 若在退出阶段卡住，强制结束进程。"""
        if self._watchdog_started:
            return
        self._watchdog_started = True
        threading.Thread(
            target=_force_exit_after_timeout,
            args=(10.0,),
            name="muxer-exit-watchdog",
            daemon=True,
        ).start()

    def _setup_tray(self, enabled: bool) -> None:
        if not enabled:
            return
        try:
            from muxer.ui.tray import TrayController
        except ImportError:  # pragma: no cover - 无托盘依赖时静默降级
            return
        if not TrayController.is_available():
            return
        self._tray = TrayController(self)

    def _notify_if_hidden(self, task) -> None:
        """窗口在托盘时，用系统气泡提示任务结束。"""
        if self._tray is None or self.isVisible():
            return
        if task.status is TaskStatus.COMPLETED:
            title = "录制完成" if task.live else "下载完成"
            self._tray.show_message(title, task.title)
        elif task.status is TaskStatus.FAILED:
            self._tray.show_message("任务失败", f"{task.title}：{task.error or '未知错误'}")

    def _on_clear_history(self) -> None:
        if self._engine is None:
            self._show_toast("引擎未启动，无法清空历史", "danger")
            return
        self._engine.clear_history()
        self._show_toast("正在清空历史记录", "info")

    def _show_logs(self) -> None:
        dialog = LogDialog(self._log_store, self)
        dialog.exec()
        dialog.deleteLater()

    def _on_redownload_requested(self, payload: dict) -> None:
        if self._engine is None:
            self._show_toast("引擎未启动，无法重新下载", "danger")
            return
        self._engine.start_task(payload)
        self.go_to(PAGE_TASKS)
        self._show_toast("已重新加入下载队列", "success")

    def _on_pause_requested(self, task_id: str) -> None:
        if self._engine is not None:
            self._engine.pause(task_id)

    def _on_resume_requested(self, task_id: str) -> None:
        if self._engine is not None:
            self._engine.resume(task_id)

    def _on_stop_requested(self, task_id: str) -> None:
        if self._engine is not None:
            self._engine.stop(task_id)

    def _on_cancel_requested(self, task_id: str) -> None:
        if self._engine is not None:
            self._engine.cancel(task_id)

    def _on_archive_requested(self) -> None:
        if self._engine is not None:
            self._engine.archive_completed()

    def _on_rate_limit_changed(self, kbps: int) -> None:
        if self._engine is not None:
            self._engine.set_rate_limit(kbps)

    def _open_path(self, path: str) -> None:
        """打开文件所在目录；路径不存在时给出提示。"""
        target = Path(path) if path else None
        if target is None:
            self._show_toast("该任务还没有输出位置", "warning")
            return
        directory = target if target.is_dir() else target.parent
        if not directory.exists():
            self._show_toast(f"目录不存在：{directory}", "warning")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def _toggle_theme(self) -> None:
        new_mode = "light" if theme_manager.current().is_dark else "dark"
        theme_manager.apply(new_mode)
        self._config.theme = new_mode
        save_config(self._config)

    # ---- 窗口行为 ----

    def _edges_at(self, position: QPoint) -> Qt.Edge:
        if self.isMaximized():
            return Qt.Edge(0)
        border = self.RESIZE_BORDER
        edges = Qt.Edge(0)
        if position.x() <= border:
            edges |= Qt.Edge.LeftEdge
        elif position.x() >= self.width() - border:
            edges |= Qt.Edge.RightEdge
        if position.y() <= border:
            edges |= Qt.Edge.TopEdge
        elif position.y() >= self.height() - border:
            edges |= Qt.Edge.BottomEdge
        return edges

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            edges = self._edges_at(event.position().toPoint())
            if edges:
                handle = self.windowHandle()
                if handle is not None:
                    handle.startSystemResize(edges)
                    event.accept()
                    return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        edges = self._edges_at(event.position().toPoint())
        self.setCursor(self._cursor_for_edges(edges))
        super().mouseMoveEvent(event)

    def _cursor_for_edges(self, edges: Qt.Edge) -> Qt.CursorShape:
        left = bool(edges & Qt.Edge.LeftEdge)
        right = bool(edges & Qt.Edge.RightEdge)
        top = bool(edges & Qt.Edge.TopEdge)
        bottom = bool(edges & Qt.Edge.BottomEdge)
        if (left and top) or (right and bottom):
            return Qt.CursorShape.SizeFDiagCursor
        if (right and top) or (left and bottom):
            return Qt.CursorShape.SizeBDiagCursor
        if left or right:
            return Qt.CursorShape.SizeHorCursor
        if top or bottom:
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.ArrowCursor

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._apply_window_state()

    def _apply_window_state(self) -> None:
        maximized = self.isMaximized()
        margin = 0 if maximized else self.EDGE_MARGIN
        self._root.setContentsMargins(margin, margin, margin, margin)
        self._body.setProperty("maximized", "true" if maximized else "false")
        self._body.style().unpolish(self._body)
        self._body.style().polish(self._body)
        self._shadow.setEnabled(not maximized)
        self._titlebar.sync_state()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if (
            self._tray is not None
            and not self._quitting
            and self._config.minimize_to_tray
        ):
            self.hide()
            event.ignore()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self._tray.show_message(
                    "Muxer 仍在后台运行", "双击托盘图标可重新打开"
                )
            return

        if not self._quitting:
            self._quitting = True
            self._start_exit_watchdog()
        if self._tray is not None:
            self._tray.hide()
        if self._sniffer_page is not None:
            self._sniffer_page.release_webengine()
        if self._engine is not None:
            self._engine.shutdown()
            self._engine = None
        if self._log_store is not None:
            self._log_store.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()
        super().closeEvent(event)

    def _refresh_theme(self) -> None:
        theme = theme_manager.current()
        self._shadow.setColor(QColor(0, 0, 0, 110 if theme.is_dark else 60))


def _force_exit_after_timeout(seconds: float) -> None:
    """等待正常退出；超时后强制结束，避免残留无窗口进程。"""
    time.sleep(seconds)
    LOGGER.warning("退出超时，强制结束进程")
    logging.shutdown()
    os._exit(0)
