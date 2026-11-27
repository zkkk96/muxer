"""协议层共用的命名与展示工具。"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

GENERIC_STEMS = {"index", "master", "playlist", "manifest", "prog_index", "stream"}


def title_from_url(url: str, fallback: str = "未命名媒体") -> str:
    """取路径最后一段；遇到 index/master 这类通用名时退回目录名。"""
    path = Path(unquote(urlparse(url).path))
    stem = path.stem
    if not stem or stem.lower() in GENERIC_STEMS:
        directory = path.parent.name
        stem = directory if directory not in ("", "/") else ""
    return stem or fallback


def resolution_label(value: str) -> str:
    """``1920x1080`` -> ``1080P``；无法解析时原样返回。"""
    try:
        height = int(value.lower().split("x", 1)[1])
    except (IndexError, ValueError):
        return value
    if height >= 2160:
        return "4K"
    if height >= 1440:
        return "2K"
    return f"{height}P"


def codec_label(codecs: str) -> str:
    """从 RFC 6381 编码串里提取常见编码名称。"""
    lowered = codecs.lower()
    if "avc1" in lowered:
        return "H.264"
    if "hvc1" in lowered or "hev1" in lowered:
        return "H.265"
    if "av01" in lowered:
        return "AV1"
    if "vp09" in lowered:
        return "VP9"
    if "mp4a" in lowered:
        return "AAC"
    return ""


def variant_label(resolution: str = "", bandwidth: int = 0, codecs: str = "") -> str:
    """生成 ``1080P · 5.2 Mbps · H.264`` 形式的展示标签。"""
    parts: list[str] = []
    if resolution:
        parts.append(resolution_label(resolution))
    if bandwidth:
        parts.append(f"{bandwidth / 1_000_000:.1f} Mbps")
    codec = codec_label(codecs)
    if codec:
        parts.append(codec)
    return " · ".join(parts) or "默认码率"
