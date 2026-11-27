"""SQLite 任务持久化。

只记录任务级状态（分片级状态在 M3 的任务目录 manifest 中）,
进度写入由调度器节流，避免高频磁盘操作。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from muxer.core.models import TaskRecord, TaskStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    title       TEXT NOT NULL,
    protocol    TEXT NOT NULL,
    status      TEXT NOT NULL,
    bytes_done  INTEGER NOT NULL DEFAULT 0,
    bytes_total INTEGER NOT NULL DEFAULT 0,
    segments_done  INTEGER NOT NULL DEFAULT 0,
    segments_total INTEGER NOT NULL DEFAULT 0,
    live_seconds   REAL NOT NULL DEFAULT 0,
    output_dir  TEXT NOT NULL,
    output_path TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    headers     TEXT NOT NULL DEFAULT '{}',
    options     TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    archived    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
"""


class Storage:
    """任务表的读写封装，仅在引擎线程内使用。"""

    def __init__(self, path: Path):
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.executescript(SCHEMA)
        self._connection.commit()
        self._migrate()

    def _migrate(self) -> None:
        """为旧版本数据库补齐新增列。"""
        columns = {
            row[1] for row in self._connection.execute("PRAGMA table_info(tasks)").fetchall()
        }
        integer_columns = ("segments_done", "segments_total")
        for name in integer_columns:
            if name not in columns:
                self._connection.execute(
                    f"ALTER TABLE tasks ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
                )
        if "live_seconds" not in columns:
            self._connection.execute(
                "ALTER TABLE tasks ADD COLUMN live_seconds REAL NOT NULL DEFAULT 0"
            )
        self._connection.commit()

    def save(self, task: TaskRecord) -> None:
        """插入或更新一条任务。"""
        self._connection.execute(
            """
            INSERT INTO tasks (id, url, title, protocol, status, bytes_done, bytes_total,
                               segments_done, segments_total, live_seconds,
                               output_dir, output_path, error, headers, options,
                               created_at, updated_at, archived)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                url=excluded.url,
                title=excluded.title,
                protocol=excluded.protocol,
                status=excluded.status,
                bytes_done=excluded.bytes_done,
                bytes_total=excluded.bytes_total,
                segments_done=excluded.segments_done,
                segments_total=excluded.segments_total,
                live_seconds=excluded.live_seconds,
                output_dir=excluded.output_dir,
                output_path=excluded.output_path,
                error=excluded.error,
                headers=excluded.headers,
                options=excluded.options,
                updated_at=excluded.updated_at,
                archived=excluded.archived
            """,
            (
                task.id,
                task.url,
                task.title,
                task.protocol,
                task.status.value,
                task.bytes_done,
                task.bytes_total,
                task.segments_done,
                task.segments_total,
                task.live_seconds,
                task.output_dir,
                task.output_path,
                task.error,
                json.dumps(task.headers, ensure_ascii=False),
                json.dumps(task.options, ensure_ascii=False),
                task.created_at,
                task.updated_at,
                1 if task.archived else 0,
            ),
        )
        self._connection.commit()

    def delete(self, task_id: str) -> None:
        """删除一条任务记录。"""
        self._connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self._connection.commit()

    def load_all(self) -> list[TaskRecord]:
        """按创建时间升序读取全部任务（含已归档）。"""
        cursor = self._connection.execute("SELECT * FROM tasks ORDER BY created_at ASC")
        return [self._row_to_record(row) for row in cursor.fetchall()]

    def clear_finished(self) -> int:
        """删除全部完成 / 失败记录，返回删除条数。"""
        cursor = self._connection.execute(
            "DELETE FROM tasks WHERE status IN (?, ?)",
            (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value),
        )
        self._connection.commit()
        return cursor.rowcount

    def archive_completed(self) -> None:
        """把所有已完成任务标记为归档，任务页不再展示，历史页仍保留。"""
        self._connection.execute(
            "UPDATE tasks SET archived = 1 WHERE status = ?", (TaskStatus.COMPLETED.value,)
        )
        self._connection.commit()

    def close(self) -> None:
        """关闭连接。"""
        self._connection.close()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            id=row["id"],
            url=row["url"],
            title=row["title"],
            protocol=row["protocol"],
            status=TaskStatus(row["status"]),
            output_dir=row["output_dir"],
            bytes_done=row["bytes_done"],
            bytes_total=row["bytes_total"],
            output_path=row["output_path"],
            error=row["error"],
            headers=json.loads(row["headers"] or "{}"),
            options=json.loads(row["options"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            archived=bool(row["archived"]),
        )