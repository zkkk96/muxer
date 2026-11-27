"""主题状态与切换。

自绘控件在 ``subscribe()`` 里注册回调，主题变化时收到通知重绘；
QSS 由 ``apply()`` 统一推送给 QApplication。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from muxer.ui.theme.stylesheet import build_stylesheet
from muxer.ui.theme.tokens import DARK, LIGHT, Theme, themed

LOGGER = logging.getLogger(__name__)

VALID_MODES = ("light", "dark", "system")

_theme: Theme = themed(LIGHT)
_mode: str = "system"
_subscribers: list[Callable[[], None]] = []


def current() -> Theme:
    """当前生效的主题（跟随系统时会解析成具体一档）。"""
    return _theme


def mode() -> str:
    """用户选择的主题模式：light / dark / system。"""
    return _mode


def subscribe(callback: Callable[[], None]) -> None:
    """注册主题变化回调；重复注册会被忽略。"""
    if callback not in _subscribers:
        _subscribers.append(callback)


def unsubscribe(callback: Callable[[], None]) -> None:
    """注销回调，控件销毁时调用。"""
    if callback in _subscribers:
        _subscribers.remove(callback)


def is_dark_requested() -> bool:
    """系统当前是否为深色外观。"""
    app = QApplication.instance()
    if app is None:
        return False
    return app.styleHints().colorScheme() == Qt.ColorScheme.Dark


def resolve_theme(selected_mode: str) -> Theme:
    """把模式解析成具体主题，未知值按 system 处理。"""
    if selected_mode == "dark":
        return themed(DARK)
    if selected_mode == "light":
        return themed(LIGHT)
    return themed(DARK if is_dark_requested() else LIGHT)


def apply(selected_mode: str) -> None:
    """切换主题并广播：更新 QSS、通知自绘控件。"""
    global _theme, _mode
    _mode = selected_mode if selected_mode in VALID_MODES else "system"
    _theme = resolve_theme(_mode)

    app = QApplication.instance()
    if app is not None:
        app.setStyleSheet(build_stylesheet(_theme))
    LOGGER.info("主题切换: mode=%s dark=%s", _mode, _theme.is_dark)

    for callback in list(_subscribers):
        try:
            callback()
        except RuntimeError as exc:
            # 控件可能已被销毁，注销掉即可，不影响其他订阅者
            LOGGER.debug("主题回调失效，已移除: %s", exc)
            unsubscribe(callback)