"""HTTP 状态码友好文案与探测失败行为。"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from muxer.core.http import HttpClient, HttpError, describe_http_status
from tests.http_fixture import StaticServer


class HttpStatusMessageTests(unittest.TestCase):
    def test_known_status(self) -> None:
        self.assertIn("404", describe_http_status(404))
        self.assertIn("不存在", describe_http_status(404))
        self.assertIn("Referer", describe_http_status(403))
        self.assertIn("429", describe_http_status(429))

    def test_unknown_status(self) -> None:
        self.assertEqual(describe_http_status(418), "HTTP 418")


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
