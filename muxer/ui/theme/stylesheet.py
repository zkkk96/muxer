"""把设计令牌渲染进 QSS 模板。

Qt 的样式表不支持 CSS 变量，所以用 ``{{token}}`` 占位符 + 字符串替换，
切换主题时重新渲染并整体 setStyleSheet。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from muxer.config import UI_ICONS_DIR
from muxer.ui.theme.tokens import Theme

LOGGER = logging.getLogger(__name__)

QSS_DIR = Path(__file__).resolve().parent / "qss"
QSS_FILES = ("base.qss", "sidebar.qss", "controls.qss", "cards.qss")
TOKEN_PATTERN = re.compile(r"\{\{(\w+)\}\}")


def token_map(theme: Theme) -> dict[str, str]:
    """生成模板需要的全部键值。"""
    values: dict[str, str] = {}
    for attribute in (
        "bg",
        "surface",
        "surface_muted",
        "border",
        "border_strong",
        "text",
        "text_secondary",
        "text_tertiary",
        "accent",
        "accent_hover",
        "accent_soft",
        "success",
        "success_soft",
        "warning",
        "warning_soft",
        "danger",
        "danger_soft",
        "font_sans",
        "font_serif",
        "font_mono",
    ):
        values[attribute] = str(getattr(theme, attribute))
    for attribute in (
        "radius_sm",
        "radius_md",
        "radius_lg",
        "fs_display",
        "fs_title",
        "fs_body",
        "fs_caption",
    ):
        values[attribute] = str(getattr(theme, attribute))

    # QSS 里用 url("...") 引用的小图标；url() 无法解析 currentColor，按主题切换固定色版本
    chevron = "chevron-down-light.svg" if theme.is_dark else "chevron-down-dark.svg"
    values["icon_chevron"] = (UI_ICONS_DIR / chevron).as_posix()
    values["icon_check"] = (UI_ICONS_DIR / "check-white.svg").as_posix()
    return values


def render_template(template: str, values: dict[str, str]) -> str:
    """替换 ``{{token}}``；未知 token 原样保留并记日志，便于排查。"""

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            LOGGER.warning("QSS 模板存在未知占位符: %s", key)
            return match.group(0)
        return values[key]

    return TOKEN_PATTERN.sub(replace, template)


def build_stylesheet(theme: Theme) -> str:
    """读取全部 QSS 片段并合并成一份样式表。"""
    values = token_map(theme)
    parts: list[str] = []
    for filename in QSS_FILES:
        path = QSS_DIR / filename
        try:
            parts.append(render_template(path.read_text(encoding="utf-8"), values))
        except OSError as exc:
            LOGGER.error("读取样式表失败 %s: %s", path, exc)
    return "\n".join(parts)