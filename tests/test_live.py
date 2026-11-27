"""直播录制测试：HLS 滑动窗口与 HTTP-FLV。"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import replace
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
from tests.http_fixture import FlvStreamServer, LiveHlsServer, StaticServer
from tests.test_hls_download import FFMPEG, generate_hls


def generate_flv(path: Path) -> None:
    """生成 4 秒带音轨的 FLV 文件。"""
    subprocess.run(
        [
            FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
            "-t", "4", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k",
            "-shortest", "-f", "flv", str(path),
        ],
        check=True,
        capture_output=True,
    )


@unittest.skipUnless(FFMPEG, "需要 ffmpeg 才能生成直播样例")
class LiveStreamTestCase(unittest.IsolatedAsyncioTestCase):
    """共用调度器与等待工具。"""

    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muxer-live-run-"))
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

    async def _wait(self, predicate, timeout: float = 60.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in reversed(self.events):
                if predicate(event):
                    return event
            await asyncio.sleep(0.05)
        self.fail(f"等待事件超时，最近事件: {self.events[-5:]}")

    def _snapshot(self, task_id: str):
        for event in reversed(self.events):
            if isinstance(event, TaskUpserted) and event.task.id == task_id:
                return event.task
        return None

    def _submit(self, url: str, title: str, protocol: str, live: bool = True) -> str:
        self.scheduler.submit(
            {
                "url": url,
                "title": title,
                "protocol": protocol,
                "output_dir": str(self.tmp / "out"),
                "size": 0,
                "quality": "",
                "container": "mp4",
                "live": live,
            }
        )
        created = next(
            event
            for event in reversed(self.events)
            if isinstance(event, TaskUpserted) and event.task.status is TaskStatus.QUEUED
        )
        return created.task.id


class LiveHlsTests(LiveStreamTestCase):
    """HLS 直播录制。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="muxer-live-hls-"))
        cls.media_dir = cls.root / "media"
        generate_hls(cls.media_dir)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    async def test_probe_marks_live_supported(self) -> None:
        server = LiveHlsServer(self.media_dir)
        server.start()
        try:
            self.scheduler.probe("live-hls", server.url(), {})
            event = await self._wait(
                lambda item: isinstance(item, ProbeFinished), timeout=30
            )
            self.assertTrue(event.result.live)
            self.assertTrue(event.result.supported)
            self.assertEqual(event.result.protocol, "hls")
        finally:
            server.stop()

    async def test_record_until_endlist(self) -> None:
        server = LiveHlsServer(self.media_dir, endlist_when_full=True)
        server.start()
        try:
            task_id = self._submit(server.url(), "live-hls-auto", "hls")
            done = await self._wait(
                lambda event: isinstance(event, TaskUpserted)
                and event.task.id == task_id
                and event.task.status is TaskStatus.COMPLETED,
                timeout=90,
            )
            self.assertTrue(done.task.live)
            self.assertGreater(done.task.live_seconds, 0)
            output = Path(done.task.output_path)
            self.assertTrue(output.exists())
            duration = await probe_duration(locate(FFMPEG).ffprobe, output)
            self.assertAlmostEqual(duration or 0, 6.0, delta=1.0)
        finally:
            server.stop()

    async def test_live_max_duration_auto_finishes(self) -> None:
        server = LiveHlsServer(self.media_dir, endlist_when_full=False)
        server.start()
        storage = Storage(self.tmp / "limit.db")
        settings = replace(
            self.settings,
            db_path=self.tmp / "limit.db",
            tasks_root=self.tmp / "limit_tasks",
            live_max_seconds=3.0,
        )
        http = HttpClient(retries=1, read_timeout=10)
        await http.start()
        events: list[object] = []
        scheduler = Scheduler(settings, http, storage, events.append)
        await scheduler.start()
        try:
            scheduler.submit({
                "url": server.url(),
                "title": "live-limit",
                "protocol": "hls",
                "output_dir": str(self.tmp / "out"),
                "size": 0,
                "live": True,
            })
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                completed = [
                    event.task
                    for event in events
                    if isinstance(event, TaskUpserted)
                    and event.task.status is TaskStatus.COMPLETED
                ]
                if completed:
                    break
                await asyncio.sleep(0.05)
            else:
                self.fail("到达录制时长上限后未自动完成")
            output = Path(completed[0].output_path)
            self.assertTrue(output.exists())
            duration = await probe_duration(locate(FFMPEG).ffprobe, output)
            self.assertIsNotNone(duration)
        finally:
            await scheduler.shutdown()
            await http.close()
            storage.close()
            server.stop()

    async def test_manual_stop_finishes(self) -> None:
        server = LiveHlsServer(self.media_dir, endlist_when_full=False)
        server.start()
        try:
            task_id = self._submit(server.url(), "live-hls-manual", "hls")
            await self._wait(
                lambda event: isinstance(event, TaskUpserted)
                and event.task.id == task_id
                and event.task.status is TaskStatus.DOWNLOADING
                and event.task.live_seconds >= 2.0
                and event.task.bytes_done > 0,
                timeout=60,
            )
            self.scheduler.stop(task_id)
            done = await self._wait(
                lambda event: isinstance(event, TaskUpserted)
                and event.task.id == task_id
                and event.task.status is TaskStatus.COMPLETED,
                timeout=60,
            )
            output = Path(done.task.output_path)
            self.assertTrue(output.exists())
            duration = await probe_duration(locate(FFMPEG).ffprobe, output)
            self.assertGreater(duration or 0, 0.5)
        finally:
            server.stop()


class FlvLiveTests(LiveStreamTestCase):
    """HTTP-FLV 点播与直播。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="muxer-live-flv-"))
        cls.flv_path = cls.root / "sample.flv"
        generate_flv(cls.flv_path)
        cls.data = cls.flv_path.read_bytes()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    async def test_probe_detects_live_flv(self) -> None:
        server = FlvStreamServer(self.data)
        server.start()
        try:
            self.scheduler.probe("live-flv", server.url(), {})
            event = await self._wait(
                lambda item: isinstance(item, ProbeFinished), timeout=30
            )
            self.assertTrue(event.result.live)
            self.assertTrue(event.result.supported)
            self.assertEqual(event.result.protocol, "flv")
            self.assertIn("video/x-flv", event.result.content_type)
        finally:
            server.stop()

    async def test_record_flv_until_stop(self) -> None:
        server = FlvStreamServer(self.data)
        server.start()
        try:
            task_id = self._submit(server.url(), "live-flv", "flv")
            await self._wait(
                lambda event: isinstance(event, TaskUpserted)
                and event.task.id == task_id
                and event.task.status is TaskStatus.DOWNLOADING
                and event.task.bytes_done >= 64 * 1024
                and event.task.live_seconds > 0,
                timeout=30,
            )
            self.scheduler.stop(task_id)
            done = await self._wait(
                lambda event: isinstance(event, TaskUpserted)
                and event.task.id == task_id
                and event.task.status is TaskStatus.COMPLETED,
                timeout=60,
            )
            output = Path(done.task.output_path)
            self.assertTrue(output.exists())
            self.assertGreater(output.stat().st_size, 1000)
            duration = await probe_duration(locate(FFMPEG).ffprobe, output)
            self.assertIsNotNone(duration)
            self.assertGreater(duration or 0, 0.3)
        finally:
            server.stop()

    async def test_vod_flv_downloads_to_mp4(self) -> None:
        server = StaticServer(self.root)
        server.start()
        try:
            self.scheduler.probe("vod-flv", server.url("sample.flv"), {})
            probe = await self._wait(
                lambda item: isinstance(item, ProbeFinished), timeout=30
            )
            self.assertFalse(probe.result.live)

            task_id = self._submit(
                server.url("sample.flv"), "vod-flv", "flv", live=False
            )
            done = await self._wait(
                lambda event: isinstance(event, TaskUpserted)
                and event.task.id == task_id
                and event.task.status is TaskStatus.COMPLETED,
                timeout=60,
            )
            output = Path(done.task.output_path)
            self.assertTrue(output.exists())
            duration = await probe_duration(locate(FFMPEG).ffprobe, output)
            self.assertAlmostEqual(duration or 0, 4.0, delta=1.0)
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()