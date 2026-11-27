"""DASH 端到端测试：ffmpeg 生成 MPD → 本地静态服务 → 调度器下载合并。

依赖 PATH 中的 ffmpeg；缺失时整类跳过。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from muxer.core.events import ProbeFinished, TaskUpserted
from muxer.core.ffmpeg import locate, probe_duration
from muxer.core.http import HttpClient
from muxer.core.models import TaskStatus
from muxer.core.scheduler import Scheduler
from muxer.core.settings import EngineSettings
from muxer.core.storage import Storage
from muxer.protocols.dash.analyzer import analyze
from tests.http_fixture import StaticServer

FFMPEG = shutil.which("ffmpeg")


def generate_dash(directory: Path, use_timeline: bool = True) -> None:
    """生成 6 秒、双轨（视频 + 音频）的 DASH 点播样例。"""
    directory.mkdir(parents=True, exist_ok=True)
    command = [
        FFMPEG,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x240:rate=15",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=44100",
        "-t",
        "6",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "15",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        "-shortest",
        "-f",
        "dash",
        "-seg_duration",
        "2",
        "-use_template",
        "1",
        "-use_timeline",
        "1" if use_timeline else "0",
        "-init_seg_name",
        "init-stream$RepresentationID$.m4s",
        "-media_seg_name",
        "chunk-stream$RepresentationID$-$Number%05d$.m4s",
        "manifest.mpd",
    ]
    subprocess.run(command, check=True, capture_output=True, cwd=str(directory))


def probe_stream_types(path: Path) -> set[str]:
    """返回媒体文件包含的轨道类型集合。"""
    ffprobe = locate(FFMPEG or "").ffprobe if FFMPEG else None
    if not ffprobe:
        return set()
    process = subprocess.run(
        [ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True,
        text=True,
    )
    try:
        payload = json.loads(process.stdout or "{}")
    except json.JSONDecodeError:
        return set()
    return {stream.get("codec_type", "") for stream in payload.get("streams", [])}


MULTI_VARIANT_MPD = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT6S" profiles="urn:mpeg:dash:profile:isoff-live:2011">
  <Period id="0">
    <AdaptationSet contentType="video" mimeType="video/mp4" codecs="avc1.640028">
      <Representation id="high" bandwidth="2000000" width="1920" height="1080">
        <SegmentTemplate timescale="1000" startNumber="1"
          initialization="init-stream0.m4s"
          media="chunk-stream0-$Number%05d$.m4s">
          <SegmentTimeline><S t="0" d="2000" r="2"/></SegmentTimeline>
        </SegmentTemplate>
      </Representation>
      <Representation id="low" bandwidth="1200000" width="1280" height="720">
        <SegmentTemplate timescale="1000" startNumber="1"
          initialization="init-stream0.m4s"
          media="chunk-stream0-$Number%05d$.m4s">
          <SegmentTimeline><S t="0" d="2000" r="2"/></SegmentTimeline>
        </SegmentTemplate>
      </Representation>
    </AdaptationSet>
    <AdaptationSet contentType="audio" mimeType="audio/mp4" codecs="mp4a.40.2">
      <Representation id="1" bandwidth="64000">
        <SegmentTemplate timescale="1000" startNumber="1"
          initialization="init-stream1.m4s"
          media="chunk-stream1-$Number%05d$.m4s">
          <SegmentTimeline><S t="0" d="2000" r="2"/></SegmentTimeline>
        </SegmentTemplate>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""


@unittest.skipUnless(FFMPEG, "需要 ffmpeg 才能生成 DASH 样例")
class DashDownloadTests(unittest.IsolatedAsyncioTestCase):
    """DASH 点播的解析、下载与合并。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="muxer-dash-"))
        cls.timeline_dir = cls.root / "timeline"
        generate_dash(cls.timeline_dir, use_timeline=True)
        (cls.timeline_dir / "multi.mpd").write_text(MULTI_VARIANT_MPD, encoding="utf-8")

        cls.plain_dir = cls.root / "plain"
        generate_dash(cls.plain_dir, use_timeline=False)

        drm_mpd = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT6S">
  <Period id="0">
    <AdaptationSet contentType="video" mimeType="video/mp4">
      <ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011" value="cenc"/>
      <Representation id="0" bandwidth="2000000" width="320" height="240">
        <SegmentTemplate timescale="1000" startNumber="1"
          initialization="init-stream0.m4s"
          media="chunk-stream0-$Number%05d$.m4s">
          <SegmentTimeline><S t="0" d="2000" r="2"/></SegmentTimeline>
        </SegmentTemplate>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""
        (cls.timeline_dir / "drm.mpd").write_text(drm_mpd, encoding="utf-8")

        live_mpd = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="dynamic" minimumUpdatePeriod="PT2S">
  <Period id="0">
    <AdaptationSet contentType="video" mimeType="video/mp4">
      <Representation id="0" bandwidth="2000000" width="320" height="240">
        <SegmentTemplate timescale="1000" startNumber="1"
          initialization="init-stream0.m4s"
          media="chunk-stream0-$Number%05d$.m4s">
          <SegmentTimeline><S t="0" d="2000" r="2"/></SegmentTimeline>
        </SegmentTemplate>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""
        (cls.timeline_dir / "live.mpd").write_text(live_mpd, encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muxer-dash-run-"))
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

    # ---- 工具 ----

    async def _wait(self, predicate, timeout: float = 90.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in reversed(self.events):
                if predicate(event):
                    return event
            await asyncio.sleep(0.05)
        self.fail(f"等待事件超时，最近事件: {self.events[-5:]}")

    def _submit(self, url: str, title: str, variant: str = "") -> str:
        self.scheduler.submit(
            {
                "url": url,
                "title": title,
                "protocol": "dash",
                "output_dir": str(self.tmp / "out"),
                "size": 0,
                "quality": "1080P",
                "container": "mp4",
                "variant": variant,
            }
        )
        created = next(
            event
            for event in reversed(self.events)
            if isinstance(event, TaskUpserted) and event.task.status is TaskStatus.QUEUED
        )
        return created.task.id

    # ---- 用例 ----

    async def test_analyze_timeline_manifest(self) -> None:
        server = StaticServer(self.timeline_dir)
        server.start()
        try:
            analysis = await analyze(self.http, server.url("manifest.mpd"))
            self.assertFalse(analysis.live)
            self.assertFalse(analysis.drm)
            self.assertIsNotNone(analysis.video_rep)
            self.assertIsNotNone(analysis.audio_rep)
            self.assertIsNotNone(analysis.video_plan)
            self.assertIsNotNone(analysis.audio_plan)
            self.assertEqual(len(analysis.video_plan.media_urls), 3)
            self.assertEqual(len(analysis.audio_plan.media_urls), 3)
            self.assertAlmostEqual(analysis.duration, 6.0, delta=0.5)
            self.assertGreaterEqual(len(analysis.variants), 1)
        finally:
            server.stop()

    async def test_download_timeline_manifest(self) -> None:
        server = StaticServer(self.timeline_dir)
        server.start()
        try:
            task_id = self._submit(server.url("manifest.mpd"), "dash-sample")
            done = await self._wait(
                lambda event: isinstance(event, TaskUpserted)
                and event.task.id == task_id
                and event.task.status is TaskStatus.COMPLETED
            )
            output = Path(done.task.output_path)
            self.assertTrue(output.exists())
            self.assertGreater(output.stat().st_size, 10_000)
            types = probe_stream_types(output)
            self.assertIn("video", types)
            self.assertIn("audio", types)
            duration = await probe_duration(locate(FFMPEG).ffprobe, output)
            self.assertAlmostEqual(duration or 0, 6.0, delta=1.0)
        finally:
            server.stop()

    async def test_download_duration_template(self) -> None:
        server = StaticServer(self.plain_dir)
        server.start()
        try:
            analysis = await analyze(self.http, server.url("manifest.mpd"))
            self.assertTrue(analysis.video_plan.is_fmp4)
            self.assertEqual(len(analysis.video_plan.media_urls), 3)
            task_id = self._submit(server.url("manifest.mpd"), "dash-no-timeline")
            done = await self._wait(
                lambda event: isinstance(event, TaskUpserted)
                and event.task.id == task_id
                and event.task.status is TaskStatus.COMPLETED
            )
            self.assertTrue(Path(done.task.output_path).exists())
        finally:
            server.stop()

    async def test_drm_and_live_detection(self) -> None:
        server = StaticServer(self.timeline_dir)
        server.start()
        try:
            drm = await analyze(self.http, server.url("drm.mpd"))
            self.assertTrue(drm.drm)
            self.assertFalse(drm.live)

            live = await analyze(self.http, server.url("live.mpd"))
            self.assertTrue(live.live)
            self.assertFalse(live.drm)
        finally:
            server.stop()

    async def test_variant_selection_and_probe(self) -> None:
        server = StaticServer(self.timeline_dir)
        server.start()
        try:
            preferred = await analyze(
                self.http, server.url("multi.mpd"), preferred_variant="low"
            )
            self.assertEqual(preferred.video_rep.id, "low")
            self.assertAlmostEqual(preferred.video_rep.bandwidth / 1_000_000, 1.2, delta=0.01)

            self.scheduler.probe("dash-req", server.url("multi.mpd"), {})
            event = await self._wait(
                lambda item: isinstance(item, ProbeFinished), timeout=30
            )
            self.assertTrue(event.result.supported)
            self.assertEqual(event.result.protocol, "dash")
            self.assertEqual(len(event.result.variants), 2)

            task_id = self._submit(server.url("multi.mpd"), "dash-low", variant="low")
            done = await self._wait(
                lambda item: isinstance(item, TaskUpserted)
                and item.task.id == task_id
                and item.task.status is TaskStatus.COMPLETED
            )
            self.assertTrue(Path(done.task.output_path).exists())
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()