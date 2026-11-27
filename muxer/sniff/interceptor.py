"""媒体请求拦截与 URL 过滤。

只做粗筛，真正的类型 / 大小由引擎探测；避免把 TS 分片等
高频请求塞满界面，所以分片扩展名默认忽略。
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from PySide6.QtCore import Signal
from PySide6.QtWebEngineCore import QWebEngineUrlRequestInterceptor

LOGGER = logging.getLogger(__name__)

MEDIA_SUFFIXES = (
    ".m3u8",
    ".mpd",
    ".flv",
    ".mp4",
    ".m4v",
    ".webm",
    ".mkv",
    ".mov",
)
SEGMENT_SUFFIXES = (".ts", ".m4s", ".aac", ".key", ".mp4a", ".vtt", ".jpg", ".png", ".webp")
QUERY_HINTS = (
    "format=m3u8",
    "format=mpd",
    "format=flv",
    "type=m3u8",
    "type=mpd",
    "type=flv",
    "m3u8",
    "manifest.mpd",
)
EXCLUDED_PREFIXES = ("data:", "blob:", "about:", "chrome-extension:", "devtools:")


def normalize_url(url: str) -> str:
    """去掉 fragment，统一去重键。"""
    return url.split("#", 1)[0].strip()


def is_media_url(url: str) -> bool:
    """按扩展名与查询参数判断是否为候选媒体地址。"""
    lowered = url.strip().lower()
    if not lowered or lowered.startswith(EXCLUDED_PREFIXES):
        return False
    parsed = urlparse(lowered)
    if parsed.scheme not in ("http", "https"):
        return False
    path = parsed.path
    if path.endswith(SEGMENT_SUFFIXES):
        return False
    if path.endswith(MEDIA_SUFFIXES):
        return True
    query = parsed.query
    return any(hint in query for hint in QUERY_HINTS)


def _header(headers, name: str) -> str:
    """安全读取请求头（Qt 返回的映射类型可能不同）。"""
    try:
        value = headers.get(name, "")
    except (AttributeError, TypeError):
        return ""
    return str(value or "")


class MediaRequestInterceptor(QWebEngineUrlRequestInterceptor):
    """把命中的媒体请求推给界面。"""

    candidateFound = Signal(str, str, str)  # url, referer, cookie

    def interceptRequest(self, info) -> None:  # noqa: N802 - Qt 命名
        try:
            url = info.requestUrl().toString()
        except RuntimeError:
            return
        if not is_media_url(url):
            return
        headers = info.httpHeaders()
        referer = _header(headers, "Referer")
        if not referer:
            try:
                referer = info.firstPartyUrl().toString()
            except RuntimeError:
                referer = ""
        cookie = _header(headers, "Cookie")
        self.candidateFound.emit(url, referer, cookie)
