"""HTTP 状态码友好文案与探测失败行为。"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from muxer.core.http import HttpClient, HttpError, describe_http_status
from tests.http_fixture import FlvStreamServer, StaticServer


class HttpStatusMessageTests(unittest.TestCase):
    def test_known_status(self) -> None:
        self.assertIn("404", describe_http_status(404))
        self.assertIn("不存在", describe_http_status(404))
        self.assertIn("Referer", describe_http_status(403))
        self.assertIn("429", describe_http_status(429))

    def test_unknown_status(self) -> None:
        self.assertEqual(describe_http_status(418), "HTTP 418")


class HeadZeroLengthTests(unittest.IsolatedAsyncioTestCase):
    """HEAD 返回 Content-Length: 0 的直播服务应回退 GET 并识别为直播。"""

    async def test_probe_falls_back_to_get(self) -> None:
        server = FlvStreamServer(b"flv" * 1024)
        server.start()
        http = HttpClient(retries=0, read_timeout=5)
        await http.start()
        try:
            info = await http.probe(server.url())
            self.assertEqual(info.protocol, "flv")
            self.assertIsNone(info.content_length)
            self.assertIn("video/x-flv", info.content_type)
        finally:
            await http.close()
            server.stop()


class ProbeFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_missing_resource(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="muxer-http-"))
        server = StaticServer(tmp)
        server.start()
        http = HttpClient(retries=1, read_timeout=5)
        await http.start()
        try:
            with self.assertRaises(HttpError) as context:
                await http.probe(server.url("missing.mp4"))
            message = str(context.exception)
            self.assertIn("404", message)
            self.assertIn("不存在", message)
        finally:
            await http.close()
            server.stop()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
