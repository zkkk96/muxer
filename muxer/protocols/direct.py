"""直链下载源（M2 支持 direct / FLV 两种协议标签）。

行为：
- 以 64KB 分块流式写入临时文件，支持 Range 断点续传
- 服务器忽略 Range 时自动重头下载
- 网络中断按指数退避重试，磁盘错误不重试
- 完成后移动到目标目录并生成唯一文件名
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from muxer.core.files import ensure_space, sanitize_filename, unique_path
from muxer.core.http import HttpError, filename_from_url
from muxer.protocols.base import (
    DownloadCancelled,
    DownloadContext,
    DownloadFailed,
    DownloadPaused,
)

LOGGER = logging.getLogger(__name__)

CHUNK_SIZE = 64 * 1024
MAX_ATTEMPTS = 5

CONTENT_TYPE_SUFFIX = (
    ("video/mp4", ".mp4"),
    ("video/x-flv", ".flv"),
    ("video/webm", ".webm"),
    ("video/mp2t", ".ts"),
    ("video/x-matroska", ".mkv"),
    ("audio/mp4", ".m4a"),
    ("audio/mpeg", ".mp3"),
    ("audio/aac", ".aac"),
)


class DirectSource:
    """普通 HTTP 资源下载。"""

    protocol = "direct"

    async def download(self, ctx: DownloadContext) -> Path:
        task = ctx.task
        ctx.tmp_dir.mkdir(parents=True, exist_ok=True)
        part_path = ctx.tmp_dir / "payload.part"
        offset = part_path.stat().st_size if part_path.exists() else 0
        total = task.bytes_total
        content_type = ""
        attempts = 0

        output_dir = Path(task.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        if total > offset:
            try:
                ensure_space(output_dir, total - offset)
            except OSError as exc:
                raise DownloadFailed(str(exc)) from exc

        while True:
            try:
                async with ctx.http.stream(task.url, task.headers, offset) as response:
                    if offset > 0 and response.status == 200:
                        LOGGER.warning("服务器忽略 Range，重新从头下载: %s", task.url)
                        offset = 0
                        part_path.write_bytes(b"")
                    total = _total_size(response, offset) or total
                    content_type = response.headers.get("Content-Type", "") or content_type
                    ctx.report(offset, total or 0, 0, 0)

                    with part_path.open("ab") as handle:
                        async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                            if ctx.is_cancelled():
                                raise DownloadCancelled()
                            if ctx.is_paused():
                                raise DownloadPaused()
                            _write(handle, chunk)
                            offset += len(chunk)
                            ctx.report(offset, total or 0, 0, 0)
                            await ctx.rate_limiter.acquire(len(chunk))
                break
            except (HttpError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if ctx.is_cancelled():
                    raise DownloadCancelled() from exc
                if ctx.is_paused():
                    raise DownloadPaused() from exc
                attempts += 1
                if attempts >= MAX_ATTEMPTS:
                    raise DownloadFailed(f"下载失败：{exc}") from exc
                delay = min(20.0, 0.5 * (2**attempts))
                LOGGER.warning("连接中断，%0.1fs 后重试(%d/%d): %s", delay, attempts, MAX_ATTEMPTS, exc)
                await asyncio.sleep(delay)
                offset = part_path.stat().st_size if part_path.exists() else 0

        if total and offset != total:
            raise DownloadFailed(f"文件长度不一致：期望 {total} 字节，实际 {offset} 字节")

        destination = unique_path(output_dir, _final_name(task, content_type))
        try:
            shutil.move(str(part_path), str(destination))
        except OSError as exc:
            raise DownloadFailed(f"移动文件失败：{exc}") from exc
        LOGGER.info("下载完成: %s -> %s", task.url, destination)
        return destination


def _write(handle, chunk: bytes) -> None:
    """磁盘写入失败视为不可恢复错误。"""
    try:
        handle.write(chunk)
        handle.flush()
    except OSError as exc:
        raise DownloadFailed(f"写入临时文件失败：{exc}") from exc


def _total_size(response: aiohttp.ClientResponse, offset: int) -> int | None:
    """从 Content-Range 或 Content-Length 推算文件总长。"""
    content_range = response.headers.get("Content-Range", "")
    match = re.search(r"/(\d+)$", content_range)
    if match:
        return int(match.group(1))
    length = response.headers.get("Content-Length")
    if length is None:
        return None
    try:
        value = int(length)
    except ValueError:
        return None
    return offset + value if response.status == 206 else value


def _final_name(task, content_type: str) -> str:
    """优先使用探测到的标题，缺扩展名时按 Content-Type / URL 补全。"""
    source = task.title or filename_from_url(task.url) or "download"
    title = sanitize_filename(source)
    if Path(title).suffix:
        return title
    suffix = _suffix_for(content_type) or Path(urlparse(task.url).path).suffix or ".bin"
    return f"{title}{suffix}"


def _suffix_for(content_type: str) -> str:
    lowered = content_type.lower()
    for needle, suffix in CONTENT_TYPE_SUFFIX:
        if needle in lowered:
            return suffix
    return ""