from pathlib import Path

import pytest
from work_retrieval_api.jobs import (
    JOB_FIELDS,
    CsvJobImporter,
    JobNotFoundError,
    JobRecord,
)


class MemoryStore:
    def __init__(self) -> None:
        self.records: dict[str, JobRecord] = {}

    def upsert(self, record: JobRecord) -> None:
        self.records[record.job_id] = record

    def get(self, job_id: str) -> JobRecord | None:
        return self.records.get(job_id)

    def close(self) -> None:
        pass


def test_csv_job_is_mapped_by_position_and_upserted(tmp_path: Path) -> None:
    values = ["53256270", "口譯人員", *(f"值{index}" for index in range(2, 39))]
    values[14] = "NULL"
    csv_path = tmp_path / "jobs.csv"
    csv_path.write_text(",".join(values) + "\n", encoding="utf-8")
    store = MemoryStore()

    record = CsvJobImporter(str(csv_path), store).import_job("53256270")

    assert record.details["職務名稱"] == "口譯人員"
    assert record.details[JOB_FIELDS[14]] is None
    assert store.records["53256270"] == record


def test_unknown_job_id_is_not_found(tmp_path: Path) -> None:
    csv_path = tmp_path / "jobs.csv"
    csv_path.write_text("職缺編號,職務名稱\n", encoding="utf-8")

    with pytest.raises(JobNotFoundError):
        CsvJobImporter(str(csv_path), MemoryStore()).import_job("53256270")
