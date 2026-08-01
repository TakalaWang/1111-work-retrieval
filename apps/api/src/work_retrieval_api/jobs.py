from __future__ import annotations

import csv
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import psycopg
from psycopg.types.json import Jsonb

JOB_FIELDS = (
    "職缺編號", "職務名稱", "職務內容", "薪資", "薪資下限", "薪資上限",
    "職務大類", "職務中類", "職務小類", "職缺屬性", "工時", "工時說明",
    "工作城市", "學歷需求", "科系需求1", "科系需求2", "科系需求3",
    "工作經驗需求", "語言能力一", "語言能力一聽", "語言能力一說",
    "語言能力一讀", "語言能力一寫", "語言能力二", "語言能力二聽",
    "語言能力二說", "語言能力二讀", "語言能力二寫", "電腦技能資料",
    "專業證照", "工作技能", "附加條件", "管理人數", "是否需外派",
    "廠商編號", "產業大類", "產業中類", "產業小類", "職缺最後修改時間",
)


class JobNotFoundError(LookupError):
    pass


class JobImportUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: str
    details: Mapping[str, str | None]


@runtime_checkable
class JobStore(Protocol):
    def upsert(self, record: JobRecord) -> None: ...
    def get(self, job_id: str) -> JobRecord | None: ...
    def close(self) -> None: ...


@runtime_checkable
class JobImporter(Protocol):
    def import_job(self, job_id: str) -> JobRecord: ...
    def get_job(self, job_id: str) -> JobRecord: ...
    def close(self) -> None: ...


class PostgresJobStore:
    def __init__(self, database_url: str) -> None:
        if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError("DATABASE_URL must use PostgreSQL")
        self._database_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)

    def upsert(self, record: JobRecord) -> None:
        with psycopg.connect(self._database_url) as connection:
            connection.execute(
                """
                INSERT INTO jobs (job_id, details) VALUES (%s, %s)
                ON CONFLICT (job_id) DO UPDATE
                SET details = EXCLUDED.details, updated_at = CURRENT_TIMESTAMP
                """,
                (record.job_id, Jsonb(dict(record.details))),
            )

    def get(self, job_id: str) -> JobRecord | None:
        with psycopg.connect(self._database_url) as connection:
            row = connection.execute(
                "SELECT details FROM jobs WHERE job_id = %s", (job_id,)
            ).fetchone()
        return None if row is None else JobRecord(job_id, row[0])

    def close(self) -> None:
        pass


class CsvJobImporter:
    def __init__(self, csv_path: str, store: JobStore) -> None:
        self._csv_path = Path(csv_path) if csv_path else None
        self._store = store

    def import_job(self, job_id: str) -> JobRecord:
        if self._csv_path is None:
            raise JobImportUnavailableError("JOB_CSV_PATH is not configured")
        try:
            with self._csv_path.open(newline="", encoding="utf-8-sig") as source:
                for row in csv.reader(source):
                    if row and row[0].strip() == job_id:
                        if len(row) != len(JOB_FIELDS):
                            raise JobImportUnavailableError(
                                f"CSV row has {len(row)} columns; expected {len(JOB_FIELDS)}"
                            )
                        details = {
                            field: None if value.strip().upper() == "NULL" else value.strip()
                            for field, value in zip(JOB_FIELDS[1:], row[1:], strict=True)
                        }
                        record = JobRecord(job_id, details)
                        self._store.upsert(record)
                        return record
        except OSError as error:
            raise JobImportUnavailableError("CSV file cannot be read") from error
        raise JobNotFoundError(job_id)

    def get_job(self, job_id: str) -> JobRecord:
        record = self._store.get(job_id)
        if record is None:
            raise JobNotFoundError(job_id)
        return record

    def close(self) -> None:
        self._store.close()


def job_importer_from_environment() -> CsvJobImporter:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise JobImportUnavailableError("DATABASE_URL is not configured")
    return CsvJobImporter(
        csv_path=os.environ.get("JOB_CSV_PATH", ""),
        store=PostgresJobStore(database_url),
    )
