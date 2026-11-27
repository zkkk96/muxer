"""HTTP 层：会话复用、重试退避、URL 探测、断点流。

只依赖 aiohttp，不涉及 Qt，方便单测。
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import unquote, urlparse

import aiohttp

from muxer.core.settings import DEFAULT_USER_AGENT

LOGGER = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
CONTENT_TYPE_PROTOCOL = (
    ("mpegurl", "hls"),
    ("dash+xml", "dash"),
    ("video/x-flv", "flv"),
)
EXTENSION_PROTOCOL = {".m3u8": "hls", ".mpd": "dash", ".flv": "flv"}


class HttpError(RuntimeError):
    """HTTP 请求最终失败。"""


HTTP_STATUS_HINTS = {
    400: "请求无效",
    401: "需要登录或授权",
    403: "无权限访问，可能需要 Referer / Cookie",
    404: "资源不存在，链接可能已失效",
    405: "服务器不接受该请求方法",
    408: "请求超时",
    410: "资源已下线",
    429: "请求过于频繁，请稍后再试",
    500: "服务器内部错误",
    502: "网关错误",
    503: "服务暂不可用",
    504: "网关超时",
}


def describe_http_status(status: int) -> str:
    """把 HTTP 状态码转换成人话。"""
    hint = HTTP_STATUS_HINTS.get(status)
    return f"{hint}（HTTP {status}）" if hint else f"HTTP {status}"


@dataclass(frozen=True)
class ProbeInfo:
    """探测到的远端资源信息。"""

    url: str
    final_url: str
    status: int
    content_type: str
    content_length: int | None
    accept_ranges: bool
    filename: str
    protocol: str


def detect_protocol(url: str, content_type: str) -> str:
    """按扩展名优先、Content-Type 兜底判断协议。"""
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in EXTENSION_PROTOCOL:
        return EXTENSION_PROTOCOL[suffix]
    lowered = content_type.lower()
    for needle, protocol in CONTENT_TYPE_PROTOCOL:
        if needle in lowered:
            return protocol
    return "direct"


def filename_from_url(url: str) -> str:
    """从 URL 路径中提取文件名，失败时返回空串。"""
    name = Path(unquote(urlparse(url).path)).name
    return name if name and "." in name else ""


def filename_from_disposition(value: str) -> str:
    """解析 Content-Disposition 中的文件名，支持 RFC 5987。"""
    if not value:
        return ""
    match = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)", value)
    if match:
        return unquote(match.group(1).strip().strip('"'))
    match = re.search(r'filename\s*=\s*"([^"]+)"', value)
    if match:
        return match.group(1)
    match = re.search(r"filename\s*=\s*([^;]+)", value)
    return match.group(1).strip() if match else ""


class HttpClient:
    """aiohttp 会话的薄封装。"""

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        retries: int = 5,
    ):
        self._user_agent = user_agent
        self._timeout = aiohttp.ClientTimeout(total=None, connect=connect_timeout, sock_read=read_timeout)
        self._retries = retries
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        """建立连接池。"""
        connector = aiohttp.TCPConnector(limit=64, limit_per_host=8, enable_cleanup_closed=True)
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=self._timeout,
            headers={"User-Agent": self._user_agent},
            trust_env=True,
        )

    async def close(self) -> None:
        """释放连接池。"""
        if self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("HttpClient 尚未 start()")
        return self._session

    async def probe(self, url: str, headers: dict[str, str] | None = None) -> ProbeInfo:
        """用 HEAD 探测；服务器不支持时退回 Range: bytes=0-0 的 GET。"""
        merged = self._merge(headers)
        response = await self._request("HEAD", url, merged)
        try:
            length = _content_length(response)
            # 有些直播服务（如 ZLMediaKit）对 HEAD 返回 200 + Content-Length: 0，
            # 但真正的 GET 是无长度的持续流；0 视为未知，回退到 GET 再判断
            if response.status >= 400 or length is None or length == 0:
                response.release()
                response = await self._request("GET", url, {**merged, "Range": "bytes=0-0"})
                length = _content_length(response)
            final_url = str(response.url)
            content_type = response.headers.get("Content-Type", "")
            disposition = response.headers.get("Content-Disposition", "")
            accept_ranges = response.headers.get("Accept-Ranges", "").lower() == "bytes"
            if response.status >= 400:
                raise HttpError(describe_http_status(response.status))
            filename = filename_from_disposition(disposition) or filename_from_url(final_url)
            protocol = detect_protocol(final_url, content_type)
            return ProbeInfo(
                url=url,
                final_url=final_url,
                status=response.status,
                content_type=content_type,
                content_length=length,
                accept_ranges=accept_ranges,
                filename=filename,
                protocol=protocol,
            )
        finally:
            response.release()

    async def fetch_bytes(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        limit: int | None = None,
    ) -> bytes:
        """读取完整响应体，可选截断长度（用于播放列表头部探测）。"""
        response = await self._request("GET", url, self._merge(headers))
        try:
            if response.status >= 400:
                raise HttpError(describe_http_status(response.status))
            if limit is None:
                return await response.read()
            buffer = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                buffer.extend(chunk)
                if len(buffer) >= limit:
                    break
            return bytes(buffer[:limit])
        finally:
            response.release()

    async def fetch_text(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        limit: int = 4 * 1024 * 1024,
    ) -> str:
        """按 UTF-8（自动去除 BOM）读取文本资源。"""
        data = await self.fetch_bytes(url, headers, limit)
        return data.decode("utf-8-sig", errors="replace")

    @asynccontextmanager
    async def stream(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        offset: int = 0,
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        """以流式方式打开下载请求，支持 Range 断点。"""
        merged = self._merge(headers)
        if offset > 0:
            merged["Range"] = f"bytes={offset}-"
        response = await self._request("GET", url, merged)
        if response.status >= 400:
            status = response.status
            response.release()
            raise HttpError(describe_http_status(status))
        try:
            yield response
        finally:
            response.release()

    async def _request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
    ) -> aiohttp.ClientResponse:
        """带指数退避重试的请求；返回值由调用方负责 release。"""
        attempt = 0
        while True:
            try:
                response = await self.session.request(method, url, headers=headers)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                attempt += 1
                if attempt > self._retries:
                    raise HttpError(f"请求失败：{exc}") from exc
                delay = _backoff(attempt)
                LOGGER.warning("请求异常，%0.1fs 后重试(%d/%d): %s", delay, attempt, self._retries, exc)
                await asyncio.sleep(delay)
                continue

            if response.status in RETRYABLE_STATUS:
                if attempt >= self._retries:
                    return response
                delay = _retry_after(response) or _backoff(attempt + 1)
                response.release()
                attempt += 1
                LOGGER.warning("HTTP %d，%0.1fs 后重试(%d/%d)", response.status, delay, attempt, self._retries)
                await asyncio.sleep(delay)
                continue
            return response

    def _merge(self, headers: dict[str, str] | None) -> dict[str, str]:
        merged = {"User-Agent": self._user_agent}
        if headers:
            merged.update({key: value for key, value in headers.items() if value})
        return merged


def _content_length(response: aiohttp.ClientResponse) -> int | None:
    """读取 Content-Length；206 响应结合 Content-Range 推算总长。"""
    header = response.headers.get("Content-Length")
    if response.status == 206:
        content_range = response.headers.get("Content-Range", "")
        match = re.search(r"/(\d+)$", content_range)
        if match:
            return int(match.group(1))
    if header is None:
        return None
    try:
        return int(header)
    except ValueError:
        return None


def _retry_after(response: aiohttp.ClientResponse) -> float | None:
    """解析 Retry-After（仅支持秒数形式）。"""
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return min(30.0, max(0.5, float(value)))
    except ValueError:
        return None


def _backoff(attempt: int) -> float:
    """0.5 * 2^n，封顶 20 秒。"""
    return min(20.0, 0.5 * (2 ** min(attempt, 6)))