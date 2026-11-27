"""测试用的本地 HTTP 服务：支持 Range，按块慢速发送，便于验证暂停/续传。"""

from __future__ import annotations

import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _RangeHandler(BaseHTTPRequestHandler):
    """内存文件的只读服务，实现基础 Range 语义。"""

    protocol_version = "HTTP/1.0"
    data: bytes = b""
    chunk_size: int = 64 * 1024
    delay: float = 0.0
    content_type: str = "application/octet-stream"

    def do_HEAD(self) -> None:  # noqa: N802 - 基类命名
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.data)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", self.content_type)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - 基类命名
        start = 0
        range_header = self.headers.get("Range", "")
        match = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if match:
            start = min(int(match.group(1)), len(self.data))

        remaining = self.data[start:]
        if start > 0:
            self.send_response(206)
            self.send_header(
                "Content-Range", f"bytes {start}-{len(self.data) - 1}/{len(self.data)}"
            )
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(remaining)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", self.content_type)
        self.end_headers()

        try:
            for offset in range(0, len(remaining), self.chunk_size):
                self.wfile.write(remaining[offset : offset + self.chunk_size])
                self.wfile.flush()
                if self.delay:
                    time.sleep(self.delay)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def log_message(self, fmt: str, *args) -> None:  # noqa: D401 - 静默
        return


class RangeFileServer:
    """在后台线程里运行 Range 服务。"""

    def __init__(self, data: bytes, chunk_size: int = 64 * 1024, delay: float = 0.0):
        handler = type(
            "BoundRangeHandler",
            (_RangeHandler,),
            {"data": data, "chunk_size": chunk_size, "delay": delay},
        )
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def url(self, name: str = "file.bin") -> str:
        return f"http://127.0.0.1:{self.port}/{name}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=3)

class StaticServer:
    """把目录作为静态站点提供服务，用于 HLS 分片测试。"""

    def __init__(self, directory: Path):
        from functools import partial
        from http.server import SimpleHTTPRequestHandler

        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, fmt: str, *args) -> None:  # noqa: D401 - 静默
                return

        handler = partial(QuietHandler, directory=str(directory))
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def url(self, name: str = "index.m3u8") -> str:
        from urllib.parse import quote

        return f"http://127.0.0.1:{self.port}/{quote(name)}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=3)


class LiveHlsServer:
    """模拟不断增长的 HLS 直播播放列表。

    每次请求 index.m3u8 就把可见分片数加一，按 window 模拟滑动窗口；
    endlist_when_full 为真时，等全部分片可见后追加 ENDLIST。
    """

    def __init__(
        self,
        directory: Path,
        initial: int = 1,
        step: int = 1,
        window: int = 2,
        endlist_when_full: bool = False,
    ):
        self._directory = directory
        self._segments = sorted(
            directory.glob("seg*.ts"),
            key=lambda item: int("".join(ch for ch in item.stem if ch.isdigit()) or 0),
        )
        self._initial = initial
        self._step = step
        self._window = window
        self._endlist_when_full = endlist_when_full
        self._visible = 0
        self._lock = threading.Lock()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def _make_handler(self):
        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_HEAD(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path.endswith(".m3u8"):
                    body = server_ref.playlist().encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                name = path.rsplit("/", 1)[-1]
                file_path = server_ref._directory / name  # noqa: SLF001 - 测试辅助
                if not file_path.exists():
                    self.send_error(404)
                    return
                data = file_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, fmt: str, *args) -> None:  # noqa: D401
                return

        return Handler

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def url(self, name: str = "index.m3u8") -> str:
        return f"http://127.0.0.1:{self.port}/{name}"

    def playlist(self) -> str:
        with self._lock:
            self._visible = min(
                len(self._segments), self._visible + (self._step if self._visible else self._initial)
            )
            visible = self._visible
            media_sequence = max(0, visible - self._window)
            endlist = self._endlist_when_full and visible >= len(self._segments)
            lines = [
                "#EXTM3U",
                "#EXT-X-VERSION:3",
                "#EXT-X-TARGETDURATION:2",
                f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}",
            ]
            for index in range(media_sequence, visible):
                lines.append("#EXTINF:2.0,")
                lines.append(f"seg{index}.ts")
            if endlist:
                lines.append("#EXT-X-ENDLIST")
            return "\n".join(lines) + "\n"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=3)


class FlvStreamServer:
    """不回 Content-Length、发完数据后保持连接，模拟直播 FLV。"""

    def __init__(self, data: bytes, chunk_size: int = 16384, delay: float = 0.01):
        self._stop = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_HEAD(self) -> None:  # noqa: N802
                # 模拟 ZLMediaKit：HEAD 返回 200 + Content-Length: 0，GET 才是持续流
                self.send_response(200)
                self.send_header("Content-Type", "video/x-flv")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "video/x-flv")
                self.end_headers()
                try:
                    for offset in range(0, len(data), chunk_size):
                        self.wfile.write(data[offset : offset + chunk_size])
                        self.wfile.flush()
                        time.sleep(delay)
                    while not self._stop.is_set():
                        time.sleep(0.1)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass

            def log_message(self, fmt: str, *args) -> None:  # noqa: D401
                return

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def url(self, name: str = "live.flv") -> str:
        return f"http://127.0.0.1:{self.port}/{name}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=3)
