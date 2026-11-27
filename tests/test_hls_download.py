"""HLS 端到端测试：ffmpeg 生成样例 → 本地静态服务 → 调度器下载 → ffmpeg 合并。

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
from muxer.core.ratelimit import RateLimiter
from muxer.core.http import HttpClient
from muxer.core.models import TaskStatus
from muxer.core.scheduler import Scheduler
from muxer.core.settings import EngineSettings
from muxer.core.storage import Storage
from muxer.protocols.base import DownloadContext
from muxer.protocols.hls.analyzer import analyze
from muxer.protocols.hls.playlist import MediaPlaylist, Segment
from muxer.protocols.hls.source import HLSSource
from tests.http_fixture import StaticServer

FFMPEG = shutil.which("ffmpeg")


def probe_stream_types(path: Path) -> set[str]:
    """返回媒体文件包含的轨道类型集合。"""
    ffprobe = locate(FFMPEG or "").ffprobe if FFMPEG else None
    if not ffprobe:
        return set()
    process = subprocess.run(
        [
            ffprobe,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    try:
        payload = json.loads(process.stdout or "{}")
    except json.JSONDecodeError:
        return set()
    return {stream.get("codec_type", "") for stream in payload.get("streams", [])}


def generate_hls(directory: Path, key_info: Path | None = None) -> None:
    """用测试源生成 6 秒、3 个分片的点播 HLS。"""
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
        "hls",
        "-hls_time",
        "2",
        "-hls_list_size",
        "0",
        "-hls_playlist_type",
        "vod",
        "-hls_segment_filename",
        str(directory / "seg%d.ts"),
    ]
    if key_info is not None:
        command += ["-hls_key_info_file", str(key_info)]
    command.append(str(directory / "index.m3u8"))
    subprocess.run(command, check=True, capture_output=True)


@unittest.skipUnless(FFMPEG, "需要 ffmpeg 才能生成 HLS 样例")
class HlsDownloadTests(unittest.IsolatedAsyncioTestCase):
    """点播 HLS 的解析、下载与合并。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="muxer-hls-"))
        cls.plain_dir = cls.root / "plain"
        generate_hls(cls.plain_dir)

        # 由同一份媒体生成两个码率目录 + 主播放列表，用于验证变体选择
        cls.master_dir = cls.root / "master"
        shutil.copytree(cls.plain_dir, cls.master_dir / "high")
        shutil.copytree(cls.plain_dir, cls.master_dir / "low")
        master_text = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"
high/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=1280x720,CODECS="avc1.4d401f,mp4a.40.2"
low/index.m3u8
"""
        (cls.master_dir / "index.m3u8").write_text(master_text, encoding="utf-8")

        # fMP4 分片（EXT-X-MAP）
        cls.fmp4_dir = cls.root / "fmp4"
        cls.fmp4_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                "-t", "6", "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-g", "15", "-c:a", "aac", "-b:a", "64k",
                "-shortest", "-f", "hls", "-hls_time", "2", "-hls_list_size", "0",
                "-hls_playlist_type", "vod", "-hls_segment_type", "fmp4",
                "-hls_fmp4_init_filename", "init.mp4",
                "-hls_segment_filename", "seg%d.m4s",
                "index.m3u8",
            ],
            check=True,
            capture_output=True,
            cwd=str(cls.fmp4_dir),
        )

        # 独立音轨：视频、音频各自一份播放列表 + 主列表
        cls.split_dir = cls.root / "split"
        video_dir = cls.split_dir / "video"
        audio_dir = cls.split_dir / "audio"
        video_dir.mkdir(parents=True, exist_ok=True)
        audio_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15",
                "-t", "6", "-an", "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-g", "15", "-f", "hls",
                "-hls_time", "2", "-hls_list_size", "0", "-hls_playlist_type", "vod",
                "-hls_segment_filename", str(video_dir / "seg%d.ts"),
                str(video_dir / "index.m3u8"),
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                "-t", "6", "-vn", "-c:a", "aac", "-b:a", "64k", "-f", "hls",
                "-hls_time", "2", "-hls_list_size", "0", "-hls_playlist_type", "vod",
                "-hls_segment_filename", str(audio_dir / "seg%d.ts"),
                str(audio_dir / "index.m3u8"),
            ],
            check=True,
            capture_output=True,
        )
        split_master = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="Main",DEFAULT=YES,URI="audio/index.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=320x240,CODECS="avc1.4d401f,mp4a.40.2",AUDIO="aud"
video/index.m3u8
"""
        (cls.split_dir / "index.m3u8").write_text(split_master, encoding="utf-8")

        cls.encrypted_dir = cls.root / "encrypted"
        key = bytes(range(32, 48))
        (cls.encrypted_dir).mkdir(parents=True, exist_ok=True)
        key_path = cls.encrypted_dir / "key.bin"
        key_path.write_bytes(key)
        key_info = cls.encrypted_dir / "keyinfo.txt"
        # 第 1 行是写进播放列表的 URI，第 2 行是本地密钥路径，第 3 行是 IV
        key_info.write_text(
            f"key.bin\n{key_path}\n0102030405060708090a0b0c0d0e0f10\n",
            encoding="utf-8",
        )
        generate_hls(cls.encrypted_dir, key_info)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muxer-hls-run-"))
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
                "protocol": "hls",
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

    async def test_download_plain_playlist(self) -> None:
        server = StaticServer(self.plain_dir)
        server.start()
        try:
            analysis = await analyze(self.http, server.url())
            self.assertFalse(analysis.live)
            self.assertFalse(analysis.encrypted)
            self.assertGreaterEqual(len(analysis.media.segments), 3)
            self.assertAlmostEqual(analysis.duration, 6.0, delta=0.5)

            task_id = self._submit(server.url(), "plain-sample")
            done = await self._wait(
                lambda event: isinstance(event, TaskUpserted)
                and event.task.id == task_id
                and event.task.status is TaskStatus.COMPLETED
            )
            output = Path(done.task.output_path)
            self.assertTrue(output.exists())
            self.assertGreater(output.stat().st_size, 10_000)
            duration = await probe_duration(locate(FFMPEG).ffprobe, output)
            self.assertIsNotNone(duration)
            self.assertAlmostEqual(duration, 6.0, delta=1.0)
            phases = [
                event.task.status
                for event in self.events
                if isinstance(event, TaskUpserted) and event.task.id == task_id
            ]
            self.assertIn(TaskStatus.POSTPROCESSING, phases, "合并阶段应推送状态")
        finally:
            server.stop()

    async def test_download_aes128_playlist(self) -> None:
        server = StaticServer(self.encrypted_dir)
        server.start()
        try:
            analysis = await analyze(self.http, server.url())
            self.assertTrue(analysis.encrypted)
            self.assertFalse(analysis.sample_aes)

            task_id = self._submit(server.url(), "encrypted-sample")
            done = await self._wait(
                lambda event: isinstance(event, TaskUpserted)
                and event.task.id == task_id
                and event.task.status is TaskStatus.COMPLETED
            )
            output = Path(done.task.output_path)
            self.assertTrue(output.exists())
            duration = await probe_duration(locate(FFMPEG).ffprobe, output)
            self.assertIsNotNone(duration, "解密失败会导致输出无法被 ffprobe 识别")
            self.assertAlmostEqual(duration, 6.0, delta=1.0)
        finally:
            server.stop()

    async def test_download_fmp4_playlist(self) -> None:
        server = StaticServer(self.fmp4_dir)
        server.start()
        try:
            analysis = await analyze(self.http, server.url())
            self.assertTrue(analysis.media.has_map)
            self.assertGreaterEqual(len(analysis.media.segments), 3)

            task_id = self._submit(server.url(), "fmp4-sample")
            done = await self._wait(
                lambda event: isinstance(event, TaskUpserted)
                and event.task.id == task_id
                and event.task.status is TaskStatus.COMPLETED
            )
            output = Path(done.task.output_path)
            self.assertTrue(output.exists())
            duration = await probe_duration(locate(FFMPEG).ffprobe, output)
            self.assertAlmostEqual(duration or 0, 6.0, delta=1.0)
        finally:
            server.stop()

    async def test_download_split_audio(self) -> None:
        server = StaticServer(self.split_dir)
        server.start()
        try:
            analysis = await analyze(self.http, server.url())
            self.assertIsNotNone(analysis.audio)
            self.assertIsNotNone(analysis.audio_playlist)

            task_id = self._submit(server.url(), "split-sample")
            done = await self._wait(
                lambda event: isinstance(event, TaskUpserted)
                and event.task.id == task_id
                and event.task.status is TaskStatus.COMPLETED
            )
            output = Path(done.task.output_path)
            self.assertTrue(output.exists())
            types = probe_stream_types(output)
            self.assertIn("video", types)
            self.assertIn("audio", types)
        finally:
            server.stop()

    async def test_master_variant_selection(self) -> None:
        server = StaticServer(self.master_dir)
        server.start()
        try:
            default = await analyze(self.http, server.url())
            self.assertEqual(len(default.variants), 2)
            self.assertEqual(default.variant.resolution, "1920x1080")

            low_uri = server.url("low/index.m3u8")
            preferred = await analyze(self.http, server.url(), preferred_variant=low_uri)
            self.assertEqual(preferred.variant.resolution, "1280x720")

            task_id = self._submit(
                server.url(),
                "master-sample",
                variant=low_uri,
            )
            done = await self._wait(
                lambda event: isinstance(event, TaskUpserted)
                and event.task.id == task_id
                and event.task.status is TaskStatus.COMPLETED
            )
            self.assertTrue(Path(done.task.output_path).exists())
        finally:
            server.stop()

    async def test_probe_reports_variants(self) -> None:
        server = StaticServer(self.plain_dir)
        server.start()
        try:
            self.scheduler.probe("req-1", server.url(), {})
            event = await self._wait(
                lambda item: isinstance(item, ProbeFinished), timeout=30
            )
            result = event.result
            self.assertTrue(result.supported)
            self.assertEqual(result.protocol, "hls")
            self.assertGreater(len(result.variants), 0)
            self.assertAlmostEqual(result.duration_seconds or 0, 6.0, delta=0.5)
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()


class HlsResumeTests(unittest.IsolatedAsyncioTestCase):
    """断点续传：已存在的分片文件应被跳过。"""

    async def test_resume_skips_existing_segments(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="muxer-hls-resume-"))
        media_dir = tmp / "media"
        media_dir.mkdir()
        (media_dir / "seg1.ts").write_bytes(b"segment-one")
        (media_dir / "seg2.ts").write_bytes(b"segment-two")
        server = StaticServer(media_dir)
        server.start()

        http = HttpClient(retries=1, read_timeout=5)
        await http.start()
        try:
            playlist = MediaPlaylist(
                url=server.url("index.m3u8"),
                target_duration=2.0,
                media_sequence=0,
                segments=[
                    # 第一个分片刻意指向不存在的文件：如果被重新下载，测试会失败
                    Segment(index=0, uri=server.url("missing.ts"), duration=2.0, sequence=0),
                    Segment(index=1, uri=server.url("seg1.ts"), duration=2.0, sequence=1),
                    Segment(index=2, uri=server.url("seg2.ts"), duration=2.0, sequence=2),
                ],
                endlist=True,
            )
            video_dir = tmp / "video"
            video_dir.mkdir()
            cached = video_dir / "seg000000.ts"
            cached.write_bytes(b"cached-segment")

            class Reporter:
                def __init__(self) -> None:
                    self.done = 0

                def advance(self, segments: int, byte_count: int = 0) -> None:
                    self.done += segments

                def add(self, byte_count: int) -> None:
                    self.done += 1

            reporter = Reporter()
            ctx = DownloadContext(
                task=type("Task", (), {"headers": {}})(),
                http=http,
                tmp_dir=tmp,
                rate_limiter=RateLimiter(0),
                is_paused=lambda: False,
                is_cancelled=lambda: False,
                report=lambda *args: None,
                phase=lambda *_args: None,
                report_live=lambda _seconds: None,
                is_finishing=lambda: False,
            )
            source = HLSSource(locate(FFMPEG or ""))
            await source._download_track(ctx, playlist, video_dir, reporter, {})  # noqa: SLF001

            self.assertEqual(cached.read_bytes(), b"cached-segment", "已有分片不应被覆盖")
            self.assertTrue((video_dir / "seg000001.ts").exists())
            self.assertTrue((video_dir / "seg000002.ts").exists())
            self.assertEqual(reporter.done, 3)
        finally:
            await http.close()
            server.stop()
            shutil.rmtree(tmp, ignore_errors=True)
