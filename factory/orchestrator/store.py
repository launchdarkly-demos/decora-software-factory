from __future__ import annotations

import sqlite3
from pathlib import Path

from factory.errors import FactoryError
from factory.models import JobState


class JobStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    stage TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def save(self, job: JobState) -> None:
        payload = job.model_dump_json()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (id, stage, payload, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    stage = excluded.stage,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (job.id, job.stage.value, payload, str(job.updated_at)),
            )

    def load(self, job_id: str) -> JobState:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise FactoryError(f"unknown job {job_id}")
        return JobState.model_validate_json(row["payload"])

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn
