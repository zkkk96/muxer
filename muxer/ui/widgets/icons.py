"""SVG 图标加载与着色。

图标文件用 ``currentColor`` 描边，加载时替换成主题色，
这样同一份 SVG 可以适配浅色、深色和不同语义色。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from muxer.config import ICONS_DIR, UI_ICONS_DIR

LOGGER = logging.getLogger(__name__)

_pixmap_cache: dict[tuple[str, int, str, int], QPixmap] = {}


def _read_svg(name: str) -> str:
    """读取 UI 图标源文件。"""
    path = UI_ICONS_DIR / f"{name}.svg"
    return path.read_text(encoding="utf-8")


def colored_svg(name: str, color: str) -> QByteArray:
    """把 SVG 中的 currentColor 替换为指定颜色。"""
    try:
        source = _read_svg(name)
    except OSError as exc:
        LOGGER.error("图标读取失败 %s: %s", name, exc)
        return QByteArray()
    return QByteArray(source.replace("currentColor", color).encode("utf-8"))


def pixmap(name: str, size: int, color: str, dpr: float = 2.0) -> QPixmap:
    """渲染指定尺寸的图标位图，按设备像素比缩放保证清晰。"""
    dpr_key = int(round(dpr * 100))
    cache_key = (name, size, color, dpr_key)
    cached = _pixmap_cache.get(cache_key)
    if cached is not None:
        return cached

    pixel_size = max(1, int(round(size * dpr)))
    image = QImage(pixel_size, pixel_size, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)

    data = colored_svg(name, color)
    if not data.isEmpty():
        renderer = QSvgRenderer(data)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        renderer.render(painter, QRectF(0, 0, pixel_size, pixel_size))
        painter.end()

    result = QPixmap.fromImage(image)
    result.setDevicePixelRatio(dpr)
    _pixmap_cache[cache_key] = result
    return result


def icon(name: str, color: str, size: int = 18) -> QIcon:
    """构造可用于按钮的 QIcon。"""
    return QIcon(pixmap(name, size, color))


def app_icon() -> QIcon:
    """应用窗体和托盘使用的多尺寸图标。"""
    ico_path = ICONS_DIR / "muxer.ico"
    if ico_path.exists():
        return QIcon(str(ico_path))
    return QIcon(str(ICONS_DIR / "app-256.png"))


def clear_cache() -> None:
    """主题整体更换时清空缓存，避免位图无限增长。"""
    _pixmap_cache.clear()