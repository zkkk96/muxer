"""系统托盘：入口菜单与完成通知。"""

from __future__ import annotations

from PySide6.QtCore import QObject
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from muxer.ui.widgets import icons


class TrayController(QObject):
    """持有托盘图标；动作全部转给主窗口处理。"""

    def __init__(self, window, parent: QObject | None = None):
        super().__init__(parent)
        self._window = window
        self._tray = QSystemTrayIcon(icons.app_icon(), self)
        self._tray.setToolTip("Muxer")

        menu = QMenu()
        show_action = QAction("显示主窗口", menu)
        show_action.triggered.connect(window.show_from_tray)
        pause_action = QAction("暂停全部任务", menu)
        pause_action.triggered.connect(window.pause_all_tasks)
        quit_action = QAction("退出 Muxer", menu)
        quit_action.triggered.connect(window.quit_application)

        menu.addAction(show_action)
        menu.addAction(pause_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_activated)
        self._tray.show()

    @staticmethod
    def is_available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def show_message(self, title: str, message: str) -> None:
        """任务完成 / 失败气泡。"""
        self._tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 4000)

    def hide(self) -> None:
        self._tray.hide()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.Trigger,
        ):
            self._window.show_from_tray()