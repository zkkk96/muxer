"""把标准 logging 输出桥接到界面，并保留有上限的内存缓冲。"""

from __future__ import annotations

import logging
from collections import deque

from PySide6.QtCore import QObject, Signal, Slot

MAX_BUFFER_LINES = 2000


class _Emitter(QObject):
    """独立的信号载体，避免 Handler 与 QObject 多继承。"""

    message = Signal(str, str)


class QtLogHandler(logging.Handler):
    """任意线程调用 emit，Qt 会把信号排队到界面线程。"""

    def __init__(self) -> None:
        super().__init__()
        self.emitter = _Emitter()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:  # noqa: BLE001 - 日志格式化失败不应影响业务
            self.handleError(record)
            return
        self.emitter.message.emit(record.levelname, text)


class LogStore(QObject):
    """内存日志缓冲，供日志窗口读取与导出。"""

    appended = Signal(str)

    def __init__(self, parent: QObject | None = None, max_lines: int = MAX_BUFFER_LINES):
        super().__init__(parent)
        self._lines: deque[str] = deque(maxlen=max_lines)
        self._handler = QtLogHandler()
        self._handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        self._handler.emitter.message.connect(self._append)
        logging.getLogger().addHandler(self._handler)

    @Slot(str, str)
    def _append(self, _level: str, text: str) -> None:
        self._lines.append(text)
        self.appended.emit(text)

    def text(self) -> str:
        return "\n".join(self._lines)

    def line_count(self) -> int:
        return len(self._lines)

    def clear(self) -> None:
        self._lines.clear()

    def close(self) -> None:
        logging.getLogger().removeHandler(self._handler)
        self._handler.close()