"""运行日志窗口：实时查看、清空与导出。"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from muxer.config import APP_DATA_DIR
from muxer.ui.logging_bridge import LogStore

LOGGER = logging.getLogger(__name__)
MAX_VISIBLE_LINES = 2000


class LogDialog(QDialog):
    """展示 LogStore 中的日志，并支持导出到文件。"""

    def __init__(self, store: LogStore, parent: QWidget | None = None):
        super().__init__(parent)
        self._store = store
        self.setWindowTitle("运行日志")
        self.setObjectName("logDialog")
        self.setModal(True)
        self.resize(820, 540)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        self._edit = QPlainTextEdit(self)
        self._edit.setObjectName("logText")
        self._edit.setReadOnly(True)
        self._edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._edit.document().setMaximumBlockCount(MAX_VISIBLE_LINES)
        self._edit.setPlainText(store.text())
        layout.addWidget(self._edit, 1)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        clear_button = QPushButton("清空", self)
        clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_button.clicked.connect(self._clear)
        export_button = QPushButton("导出日志", self)
        export_button.setCursor(Qt.CursorShape.PointingHandCursor)
        export_button.clicked.connect(self._export)
        folder_button = QPushButton("打开日志目录", self)
        folder_button.setCursor(Qt.CursorShape.PointingHandCursor)
        folder_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(APP_DATA_DIR / "logs")))
        )
        close_button = QPushButton("关闭", self)
        close_button.setObjectName("primaryButton")
        close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        close_button.clicked.connect(self.accept)

        actions.addWidget(clear_button)
        actions.addWidget(export_button)
        actions.addWidget(folder_button)
        actions.addStretch(1)
        actions.addWidget(close_button)
        layout.addLayout(actions)

        store.appended.connect(self._on_appended)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        self._scroll_to_bottom()

    def _on_appended(self, line: str) -> None:
        bar = self._edit.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        self._edit.appendPlainText(line)
        if at_bottom:
            bar.setValue(bar.maximum())

    def _scroll_to_bottom(self) -> None:
        bar = self._edit.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _clear(self) -> None:
        self._store.clear()
        self._edit.clear()

    def _export(self) -> None:
        default_path = Path(APP_DATA_DIR) / "muxer-log.txt"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出日志", str(default_path), "文本文件 (*.txt);;所有文件 (*)"
        )
        if not path:
            return
        try:
            Path(path).write_text(self._store.text(), encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", f"无法写入文件：{exc}")
            return
        LOGGER.info("日志已导出: %s", path)
        QMessageBox.information(self, "导出完成", f"日志已保存到：\n{path}")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        try:
            self._store.appended.disconnect(self._on_appended)
        except (RuntimeError, TypeError):
            pass
        super().closeEvent(event)