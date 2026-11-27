"""引擎线程：在自己的线程里跑 asyncio 事件循环。

UI 通过线程安全的命令接口提交操作，引擎通过 Qt 信号回推事件。
这一层只做适配，不包含下载逻辑。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from muxer.config import APP_DATA_DIR, AppConfig
from muxer.core.http import HttpClient
from muxer.core.scheduler import Scheduler
from muxer.core.settings import EngineSettings
from muxer.core.storage import Storage

LOGGER = logging.getLogger(__name__)


class EngineThread(QThread):
    """持有事件循环与调度器的后台线程。"""

    event = Signal(object)

    def __init__(self, config: AppConfig, parent=None, data_dir: Path | None = None):
        super().__init__(parent)
        self._config = config
        self._data_dir = data_dir or APP_DATA_DIR
        self._loop: asyncio.AbstractEventLoop | None = None
        self._scheduler: Scheduler | None = None
        self._shutdown_event: asyncio.Event | None = None
        self._lock = threading.Lock()
        self._pending: list[tuple[str, tuple]] = []
        self._ready = threading.Event()

    # ---- 线程主体 ----

    def run(self) -> None:  # noqa: D401 - Qt 回调
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        except Exception:  # noqa: BLE001 - 线程入口必须兜底
            LOGGER.exception("引擎线程异常退出")
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # noqa: BLE001 - 关闭阶段的异常不阻断退出
                LOGGER.exception("关闭异步生成器失败")
            loop.close()
            self._loop = None

    async def _main(self) -> None:
        settings = EngineSettings(
            db_path=self._data_dir / "muxer.db",
            tasks_root=self._data_dir / "tasks",
            task_concurrency=self._config.task_concurrency,
            segment_concurrency=self._config.segment_concurrency,
            rate_limit_kbps=self._config.rate_limit_kbps,
            ffmpeg_path=self._config.ffmpeg_path,
            live_max_seconds=self._config.live_max_minutes * 60.0,
        )
        storage = Storage(settings.db_path)
        http = HttpClient(
            user_agent=settings.user_agent,
            connect_timeout=settings.connect_timeout,
            read_timeout=settings.read_timeout,
            retries=settings.retries,
        )
        await http.start()
        self._scheduler = Scheduler(settings, http, storage, self._emit)
        await self._scheduler.start()

        self._shutdown_event = asyncio.Event()
        with self._lock:
            pending = self._pending
            self._pending = []
            self._ready.set()
        for name, args in pending:
            self._invoke(name, args)

        await self._shutdown_event.wait()
        await self._scheduler.shutdown()
        await http.close()
        storage.close()
        self._scheduler = None
        LOGGER.info("引擎已关闭")

    # ---- 命令接口（可被任意线程调用） ----

    def start_task(self, payload: dict) -> None:
        self._dispatch("submit", payload)

    def probe(self, request_id: str, url: str, headers: dict[str, str] | None = None) -> None:
        self._dispatch("probe", request_id, url, headers or {})

    def probe_and_start(self, payload: dict) -> None:
        """嗅探批量下载：探测后自动入队。"""
        self._dispatch("probe_and_start", payload)

    def pause(self, task_id: str) -> None:
        self._dispatch("pause", task_id)

    def resume(self, task_id: str) -> None:
        self._dispatch("resume", task_id)

    def stop(self, task_id: str) -> None:
        """完成直播录制。"""
        self._dispatch("stop", task_id)

    def cancel(self, task_id: str) -> None:
        self._dispatch("cancel", task_id)

    def archive_completed(self) -> None:
        self._dispatch("archive_completed")

    def clear_history(self) -> None:
        self._dispatch("clear_history")

    def set_rate_limit(self, kbps: int) -> None:
        self._dispatch("set_rate_limit", kbps)

    def shutdown(self, timeout_ms: int = 8000) -> None:
        """请求引擎停止并等待线程退出；供窗口关闭时调用。"""
        loop = self._loop
        if loop is not None and loop.is_running() and self._shutdown_event is not None:
            loop.call_soon_threadsafe(self._shutdown_event.set)
        elif not self._ready.wait(timeout=2.0):
            LOGGER.warning("引擎尚未就绪，直接等待线程结束")
        self.wait(timeout_ms)

    # ---- 内部 ----

    def _emit(self, event: object) -> None:
        """在引擎线程内发射，跨线程投递由 Qt 负责排队。"""
        self.event.emit(event)

    def _dispatch(self, name: str, *args) -> None:
        loop = self._loop
        if not self._ready.is_set() or loop is None or not loop.is_running():
            with self._lock:
                self._pending.append((name, args))
            return
        loop.call_soon_threadsafe(self._invoke, name, args)

    def _invoke(self, name: str, args: tuple) -> None:
        """在事件循环线程内执行调度器方法。"""
        scheduler = self._scheduler
        if scheduler is None:
            return
        try:
            getattr(scheduler, name)(*args)
        except Exception:  # noqa: BLE001 - 单条命令失败不应影响循环
            LOGGER.exception("执行引擎命令失败: %s", name)