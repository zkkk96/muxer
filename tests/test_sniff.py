"""嗅探过滤规则与"探测并入队"链路测试。"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from muxer.core.events import ProbeFailed, ProbeFinished, TaskUpserted
from muxer.core.ffmpeg import locate, probe_duration
from muxer.core.http import HttpClient
from muxer.core.models import TaskStatus
from muxer.core.scheduler import Scheduler
from muxer.core.settings import EngineSettings
from muxer.core.storage import Storage
from muxer.sniff.interceptor import is_media_url, normalize_url
from tests.http_fixture import StaticServer
from tests.test_hls_download import FFMPEG, generate_hls

DRM_MPD = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT6S">
  <Period id="0">
    <AdaptationSet contentType="video" mimeType="video/mp4">
      <ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011" value="cenc"/>
      <Representation id="0" bandwidth="2000000" width="320" height="240">
        <SegmentTemplate timescale="1000" startNumber="1" initialization="init.m4s"
          media="chunk-$Number%05d$.m4s">
          <SegmentTimeline><S t="0" d="2000" r="2"/></SegmentTimeline>
        </SegmentTemplate>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""


class MediaUrlFilterTests(unittest.TestCase):
    def test_media_extensions(self) -> None:
        self.assertTrue(is_media_url("https://x/a.m3u8"))
        self.assertTrue(is_media_url("https://x/master.mpd?token=1"))
        self.assertTrue(is_media_url("https://x/live.flv"))
        self.assertTrue(is_media_url("https://x/video.mp4"))
        self.assertTrue(is_media_url("https://x/watch?format=m3u8&id=1"))

    def test_non_media(self) -> None:
        self.assertFalse(is_media_url("https://x/segment1.ts"))
        self.assertFalse(is_media_url("https://x/chunk.m4s"))
        self.assertFalse(is_media_url("https://x/poster.jpg"))
        self.assertFalse(is_media_url("blob:https://x/123"))
        self.assertFalse(is_media_url("data:video/mp4;base64,AAA"))
        self.assertFalse(is_media_url("https://x/page.html"))

    def test_normalize(self) -> None:
        self.assertEqual(
            normalize_url("https://x/a.m3u8?t=1#frag"), "https://x/a.m3u8?t=1"
        )


@unittest.skipUnless(FFMPEG, "需要 ffmpeg 才能生成 HLS 样例")
class ProbeAndStartTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="muxer-sniff-"))
        cls.media_dir = cls.root / "media"
        generate_hls(cls.media_dir)
        (cls.media_dir / "drm.mpd").write_text(DRM_MPD, encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muxer-sniff-run-"))
        self.events: list[object] = []
        self.settings = EngineSettings(
            db_path=self.tmp / "muxer.db",
            tasks_root=self.tmp / "tasks",
            task_concurrency=2,
            segment_concurrency=4,
            rate_limit_kbps=0,
            ffmpeg_path=FFMPEG or "",
        )
        self.storage = Storage(self.settings.db_path)
        self.http = HttpClient(retries=2, read_timeout=10)
        await self.http.start()
        self.scheduler = Scheduler(self.settings, self.http, self.storage, self.events.append)
        await self.scheduler.start()

    async def asyncTearDown(self) -> None:
        await self.scheduler.shutdown()
        await self.http.close()
        self.storage.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _wait(self, predicate, timeout: float = 90.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in reversed(self.events):
                if predicate(event):
                    return event
            await asyncio.sleep(0.05)
        self.fail(f"等待事件超时，最近事件: {self.events[-5:]}")

    async def test_probe_and_start_hls(self) -> None:
        server = StaticServer(self.media_dir)
        server.start()
        try:
            self.scheduler.probe_and_start(
                {
                    "request_id": "r1",
                    "url": server.url("index.m3u8"),
                    "headers": {"Referer": "https://example.com/watch"},
                    "output_dir": str(self.tmp / "out"),
                }
            )
            finished = await self._wait(
                lambda event: isinstance(event, ProbeFinished), timeout=30
            )
            self.assertTrue(finished.result.supported)
            self.assertEqual(finished.result.message, "已加入下载队列")

            done = await self._wait(
                lambda event: isinstance(event, TaskUpserted)
                and event.task.status is TaskStatus.COMPLETED,
                timeout=90,
            )
            output = Path(done.task.output_path)
            self.assertTrue(output.exists())
            duration = await probe_duration(locate(FFMPEG).ffprobe, output)
            self.assertAlmostEqual(duration or 0, 6.0, delta=1.0)
        finally:
            server.stop()

    async def test_probe_and_start_rejects_drm(self) -> None:
        server = StaticServer(self.media_dir)
        server.start()
        try:
            self.scheduler.probe_and_start(
                {
                    "request_id": "drm",
                    "url": server.url("drm.mpd"),
                    "headers": {},
                    "output_dir": str(self.tmp / "out"),
                }
            )
            failed = await self._wait(
                lambda event: isinstance(event, ProbeFailed), timeout=30
            )
            self.assertIn("DRM", failed.message)
            self.assertFalse(
                any(
                    isinstance(event, TaskUpserted)
                    and event.task.status is TaskStatus.QUEUED
                    for event in self.events
                )
            )
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
