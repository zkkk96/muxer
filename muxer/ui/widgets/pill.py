"""状态胶囊：任务状态、协议类型等短标签。"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QWidget

from muxer.ui.theme import manager as theme_manager
from muxer.ui.theme.tokens import Theme

# tone -> (前景色字段, 背景色字段)
TONES: dict[str, tuple[str, str]] = {
    "neutral": ("text_secondary", "surface_muted"),
    "accent": ("accent", "accent_soft"),
    "success": ("success", "success_soft"),
    "warning": ("warning", "warning_soft"),
    "danger": ("danger", "danger_soft"),
}


class Pill(QLabel):
    """圆角小标签，颜色随主题与语义自动刷新。"""

    def __init__(self, text: str = "", tone: str = "neutral", parent: QWidget | None = None):
        super().__init__(text, parent)
        self._tone = tone
        self.setAlignment(self.alignment())
        self.setFixedHeight(22)
        theme_manager.subscribe(self._refresh)
        self.destroyed.connect(lambda: theme_manager.unsubscribe(self._refresh))
        self._refresh()

    def set_tone(self, tone: str) -> None:
        """切换语义色。"""
        self._tone = tone if tone in TONES else "neutral"
        self._refresh()

    def tone(self) -> str:
        return self._tone

    def _refresh(self) -> None:
        theme: Theme = theme_manager.current()
        foreground_field, background_field = TONES.get(self._tone, TONES["neutral"])
        foreground = getattr(theme, foreground_field)
        background = getattr(theme, background_field)
        self.setStyleSheet(
            "QLabel {"
            f" color: {foreground};"
            f" background: {background};"
            " border: none;"
            " border-radius: 11px;"
            " padding: 0px 9px;"
            " font-size: 12px;"
            " font-weight: 500;"
            "}"
        )