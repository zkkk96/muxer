"""直链下载的端到端测试：完成、暂停续传、取消。"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from muxer.core.events import HistoryCleared, TaskRemoved, TasksLoaded, TaskUpserted
from muxer.core.http import HttpClient
from muxer.core.models import TaskStatus
from muxer.core.scheduler import Scheduler
from muxer.core.settings import EngineSettings
from muxer.core.storage import Storage
from tests.http_fixture import RangeFileServer


class DirectDownloadTests(unittest.IsolatedAsyncioTestCase):
    """调度器 + 直链源 + HTTP 层的组合测试。"""

    file_size = 4 * 1024 * 1024

    async def asyncSetUp(self) -> None:
        self.data = bytes(range(256)) * (self.file_size // 256)
        self.server = RangeFileServer(self.data, delay=0.02)
        self.server.start()

        self.tmp = Path(tempfile.mkdtemp(prefix="muxer-test-"))
        self.events: list[object] = []
        self.settings = EngineSettings(
            db_path=self.tmp / "muxer.db",
            tasks_root=self.tmp / "tasks",
            task_concurrency=2,
            rate_limit_kbps=0,
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
        self.server.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 工具 ----

    def _submit(self) -> str:
        self.scheduler.submit(
            {
                "url": self.server.url(),
                "title": "sample.bin",
                "protocol": "direct",
                "output_dir": str(self.tmp / "out"),
                "size": len(self.data),
            }
        )
        created = self._find(
            lambda event: isinstance(event, TaskUpserted)
            and event.task.status is TaskStatus.QUEUED
        )
        self.assertIsNotNone(created, "未收到任务创建事件")
        return created.task.id

    async def _wait(self, predicate, timeout: float = 20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = self._find(predicate)
            if event is not None:
                return event
            await asyncio.sleep(0.02)
        self.fail(f"等待事件超时，最近的 5 条事件: {self.events[-5:]}")

    def _find(self, predicate):
        for event in reversed(self.events):
            if predicate(event):
                return event
        return None

    # ---- 用例 ----

    async def test_download_completes(self) -> None:
        task_id = self._submit()
        done = await self._wait(
            lambda event: isinstance(event, TaskUpserted)
            and event.task.id == task_id
            and event.task.status is TaskStatus.COMPLETED
        )
        output = Path(done.task.output_path)
        self.assertTrue(output.exists(), "输出文件不存在")
        self.assertEqual(output.read_bytes(), self.data, "下载内容与源文件不一致")
        self.assertFalse((self.tmp / "tasks" / task_id).exists(), "临时目录未清理")

        record = next(item for item in self.storage.load_all() if item.id == task_id)
        self.assertEqual(record.status, TaskStatus.COMPLETED)
        self.assertEqual(record.bytes_done, len(self.data))

    async def test_pause_and_resume(self) -> None:
        task_id = self._submit()
        threshold = 256 * 1024
        await self._wait(
            lambda event: isinstance(event, TaskUpserted)
            and event.task.id == task_id
            and event.task.status is TaskStatus.DOWNLOADING
            and event.task.bytes_done >= threshold
        )

        self.scheduler.pause(task_id)
        paused = await self._wait(
            lambda event: isinstance(event, TaskUpserted)
            and event.task.id == task_id
            and event.task.status is TaskStatus.PAUSED
        )
        self.assertGreater(paused.task.bytes_done, 0)
        self.assertLess(paused.task.bytes_done, len(self.data))

        part = self.tmp / "tasks" / task_id / "payload.part"
        self.assertTrue(part.exists(), "暂停后应保留临时文件")
        paused_size = part.stat().st_size

        self.scheduler.resume(task_id)
        done = await self._wait(
            lambda event: isinstance(event, TaskUpserted)
            and event.task.id == task_id
            and event.task.status is TaskStatus.COMPLETED
        )
        self.assertGreaterEqual(done.task.bytes_done, paused_size)
        self.assertEqual(Path(done.task.output_path).read_bytes(), self.data)

    async def test_cancel_removes_partial(self) -> None:
        task_id = self._submit()
        await self._wait(
            lambda event: isinstance(event, TaskUpserted)
            and event.task.id == task_id
            and event.task.status is TaskStatus.DOWNLOADING
            and event.task.bytes_done > 0
        )

        self.scheduler.cancel(task_id)
        removed = await self._wait(
            lambda event: isinstance(event, TaskRemoved) and event.task_id == task_id
        )
        self.assertEqual(removed.task_id, task_id)
        self.assertFalse((self.tmp / "tasks" / task_id).exists(), "取消后临时目录应被删除")
        self.assertEqual(list((self.tmp / "out").glob("*")), [], "取消后不应留下输出文件")
        self.assertEqual(self.storage.load_all(), [], "取消后应删除任务记录")

    async def test_probe_reports_size(self) -> None:
        info = await self.http.probe(self.server.url())
        self.assertEqual(info.protocol, "direct")
        self.assertEqual(info.content_length, len(self.data))
        self.assertEqual(info.filename, "file.bin")

    async def test_tasks_loaded_event(self) -> None:
        loaded = self._find(lambda event: isinstance(event, TasksLoaded))
        self.assertIsNotNone(loaded, "启动时应发出 TasksLoaded")
        self.assertEqual(loaded.tasks, ())


class StorageTests(unittest.TestCase):
    """任务持久化的基本行为。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muxer-store-"))
        self.storage = Storage(self.tmp / "muxer.db")

    def tearDown(self) -> None:
        self.storage.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_load_archive(self) -> None:
        from muxer.core.models import TaskRecord

        record = TaskRecord(
            id="abc",
            url="https://example.com/a.bin",
            title="a.bin",
            protocol="direct",
            status=TaskStatus.COMPLETED,
            output_dir=str(self.tmp),
            bytes_done=10,
            bytes_total=10,
            output_path=str(self.tmp / "a.bin"),
            options={"quality": "", "container": ""},
        )
        self.storage.save(record)

        loaded = self.storage.load_all()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].title, "a.bin")
        self.assertEqual(loaded[0].status, TaskStatus.COMPLETED)

        self.storage.archive_completed()
        self.assertTrue(self.storage.load_all()[0].archived)

        self.storage.delete("abc")
        self.assertEqual(self.storage.load_all(), [])

    def test_clear_finished_keeps_active(self) -> None:
        from muxer.core.models import TaskRecord

        for task_id, status in (
            ("done", TaskStatus.COMPLETED),
            ("bad", TaskStatus.FAILED),
            ("paused", TaskStatus.PAUSED),
        ):
            self.storage.save(
                TaskRecord(
                    id=task_id,
                    url="https://example.com/a.bin",
                    title=f"{task_id}.bin",
                    protocol="direct",
                    status=status,
                    output_dir=str(self.tmp),
                )
            )
        self.assertEqual(self.storage.clear_finished(), 2)
        remaining = self.storage.load_all()
        self.assertEqual([item.id for item in remaining], ["paused"])


if __name__ == "__main__":
    unittest.main()

class RestoreAndArchiveTests(unittest.IsolatedAsyncioTestCase):
    """重启恢复与归档行为。"""

    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muxer-restore-"))
        self.settings = EngineSettings(
            db_path=self.tmp / "muxer.db",
            tasks_root=self.tmp / "tasks",
            task_concurrency=1,
            rate_limit_kbps=0,
        )
        self.http = HttpClient(retries=1, read_timeout=5)

    async def asyncTearDown(self) -> None:
        await self.http.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_stale_task_restored_as_paused(self) -> None:
        from muxer.core.models import TaskRecord

        storage = Storage(self.settings.db_path)
        storage.save(
            TaskRecord(
                id="stale",
                url="https://example.com/a.bin",
                title="a.bin",
                protocol="direct",
                status=TaskStatus.DOWNLOADING,
                output_dir=str(self.tmp / "out"),
                bytes_done=100,
                bytes_total=1000,
            )
        )
        storage.close()

        events: list[object] = []
        storage2 = Storage(self.settings.db_path)
        await self.http.start()
        scheduler = Scheduler(self.settings, self.http, storage2, events.append)
        await scheduler.start()
        try:
            loaded = next(event for event in events if isinstance(event, TasksLoaded))
            self.assertEqual(loaded.tasks[0].status, TaskStatus.PAUSED)
            self.assertIn("上次退出", loaded.tasks[0].error)
            self.assertEqual(storage2.load_all()[0].status, TaskStatus.PAUSED)
        finally:
            await scheduler.shutdown()
            storage2.close()

    async def test_clear_history_emits_events(self) -> None:
        from muxer.core.models import TaskRecord

        storage = Storage(self.settings.db_path)
        for task_id, status in (("done", TaskStatus.COMPLETED), ("bad", TaskStatus.FAILED)):
            storage.save(
                TaskRecord(
                    id=task_id,
                    url="https://example.com/a.bin",
                    title=f"{task_id}.bin",
                    protocol="direct",
                    status=status,
                    output_dir=str(self.tmp / "out"),
                    bytes_done=10,
                    bytes_total=10,
                )
            )
        await self.http.start()

        events: list[object] = []
        scheduler = Scheduler(self.settings, self.http, storage, events.append)
        await scheduler.start()
        try:
            scheduler.clear_history()
            removed = {event.task_id for event in events if isinstance(event, TaskRemoved)}
            self.assertEqual(removed, {"done", "bad"})
            self.assertTrue(any(isinstance(event, HistoryCleared) for event in events))
            self.assertEqual(storage.load_all(), [])
        finally:
            await scheduler.shutdown()
            storage.close()

    async def test_archive_completed_emits_removed(self) -> None:
        from muxer.core.models import TaskRecord

        storage = Storage(self.settings.db_path)
        storage.save(
            TaskRecord(
                id="done",
                url="https://example.com/a.bin",
                title="a.bin",
                protocol="direct",
                status=TaskStatus.COMPLETED,
                output_dir=str(self.tmp / "out"),
                bytes_done=10,
                bytes_total=10,
            )
        )
        await self.http.start()

        events: list[object] = []
        scheduler = Scheduler(self.settings, self.http, storage, events.append)
        await scheduler.start()
        try:
            self.assertEqual(scheduler.active_count(), 0)
            scheduler.archive_completed()
            removed = [event for event in events if isinstance(event, TaskRemoved)]
            self.assertEqual([event.task_id for event in removed], ["done"])
            self.assertTrue(storage.load_all()[0].archived)
        finally:
            await scheduler.shutdown()
            storage.close()
