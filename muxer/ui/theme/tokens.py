"""设计令牌：颜色、圆角、字号、字体。

所有视觉常量集中在这里，QSS 模板与自绘控件都从这里取值，
保证换主题时只有一处需要改。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from PySide6.QtGui import QColor, QFontDatabase


@dataclass(frozen=True)
class Theme:
    """一套完整的配色与排版参数。"""

    is_dark: bool

    # 背景与描边
    bg: str
    surface: str
    surface_muted: str
    border: str
    border_strong: str

    # 文字
    text: str
    text_secondary: str
    text_tertiary: str

    # 主色（Claude 珊瑚色系）
    accent: str
    accent_hover: str
    accent_soft: str

    # 语义色
    success: str
    success_soft: str
    warning: str
    warning_soft: str
    danger: str
    danger_soft: str

    # 圆角
    radius_sm: int = 6
    radius_md: int = 10
    radius_lg: int = 14

    # 字号
    fs_display: int = 28
    fs_title: int = 17
    fs_body: int = 14
    fs_caption: int = 12

    # 字体族（运行时探测后填充）
    font_sans: str = field(default="Segoe UI")
    font_serif: str = field(default="Segoe UI")
    font_mono: str = field(default="Consolas")


LIGHT = Theme(
    is_dark=False,
    bg="#F5F4EF",
    surface="#FFFFFF",
    surface_muted="#FAF9F5",
    border="#E8E4DA",
    border_strong="#DCD7CB",
    text="#1F1E1D",
    text_secondary="#6B6A64",
    text_tertiary="#97958D",
    accent="#D97757",
    accent_hover="#C2603F",
    accent_soft="#F7E9E2",
    success="#3D8168",
    success_soft="#E6F2ED",
    warning="#A9761A",
    warning_soft="#FBF0DC",
    danger="#B4453B",
    danger_soft="#F8E7E4",
)

DARK = Theme(
    is_dark=True,
    bg="#262624",
    surface="#30302E",
    surface_muted="#2A2A28",
    border="#3A3A37",
    border_strong="#4A4A46",
    text="#F5F4EF",
    text_secondary="#B4B2A9",
    text_tertiary="#8A887F",
    accent="#D97757",
    accent_hover="#E08A6E",
    accent_soft="#3D2A23",
    success="#6FBFA0",
    success_soft="#233A32",
    warning="#D9A441",
    warning_soft="#3A3222",
    danger="#E07A70",
    danger_soft="#3B2624",
)


@lru_cache(maxsize=1)
def _installed_families() -> set[str]:
    """缓存系统字体族列表，避免每次切换主题都查询。"""
    return set(QFontDatabase.families())


def _pick(candidates: tuple[str, ...], fallback: str) -> str:
    """按优先级挑选第一个已安装的字体族。"""
    families = _installed_families()
    for family in candidates:
        if family in families:
            return family
    return fallback


@lru_cache(maxsize=1)
def resolve_fonts() -> tuple[str, str, str]:
    """探测正文 / 标题衬线 / 等宽三种字体族。"""
    sans = _pick(
        ("Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans SC", "PingFang SC", "Segoe UI"),
        "Segoe UI",
    )
    serif = _pick(
        ("Source Serif 4", "Source Serif Pro", "Noto Serif SC", "Songti SC", "STSong"),
        sans,
    )
    mono = _pick(("Cascadia Mono", "JetBrains Mono", "Consolas"), "Consolas")
    return sans, serif, mono


def themed(base: Theme) -> Theme:
    """给主题补上运行时探测出的字体族。

    QFontDatabase 需要 QGuiApplication 就绪后才能查询；模块导入阶段
    （此时还没有 QApplication）直接返回基础字体，避免进程崩溃。
    """
    from dataclasses import replace

    from PySide6.QtGui import QGuiApplication

    if QGuiApplication.instance() is None:
        return base
    sans, serif, mono = resolve_fonts()
    return replace(base, font_sans=sans, font_serif=serif, font_mono=mono)


def qcolor(value: str, alpha: int = 255) -> QColor:
    """把十六进制颜色转成 QColor，可选叠加透明度。"""
    color = QColor(value)
    color.setAlpha(alpha)
    return color