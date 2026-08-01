from __future__ import annotations

import csv
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import import_jobs_to_aws as importer


def test_import_contract_is_fixed_to_the_verified_snapshot() -> None:
    assert importer.AWS_ACCOUNT == "378849533305"
    assert importer.AWS_REGION == "us-west-2"
    assert importer.AWS_PROFILE == "competition"
    assert importer.STACK_NAME == "WorkRetrievalData"
    assert importer.DATABASE_NAME == "work_retrieval"
    assert importer.SOURCE_SHA256 == (
        "53937f7bf076789c4cd7e3be34fb89875336108d57707b5a93182181e1087089"
    )
    assert importer.SOURCE_BYTES == 1_285_945_103
    assert importer.SOURCE_ROWS == 1_218_635
    assert importer.SOURCE_CHECKSUM_SHA256 == ("U5N/e/B2eJxM1+O+NPuJh1M2EI1XcHtakxghgeEIcIk=")
    assert len(importer.SOURCE_HEADER) == 39
    assert importer.SOURCE_HEADER[0] == "職缺編號"
    assert importer.SOURCE_HEADER[-1] == "職缺最後修改時間"


def test_validate_source_checks_the_complete_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "jobs.csv"
    with source.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(importer.SOURCE_HEADER)
        row = [f"value-{index}" for index in range(39)]
        writer.writerow(row)

    payload = source.read_bytes()
    monkeypatch.setattr(importer, "SOURCE_BYTES", len(payload))
    monkeypatch.setattr(importer, "SOURCE_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(importer, "SOURCE_ROWS", 1)

    importer.validate_source(source)


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        (None, "39 columns"),
        ("NULL", "required field job_id"),
        ("", "required field job_id"),
    ],
)
def test_validate_source_rejects_bad_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str | None,
    message: str,
) -> None:
    source = tmp_path / "jobs.csv"
    row = [f"value-{index}" for index in range(39)]
    if replacement is None:
        row.pop()
    else:
        row[0] = replacement
    with source.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(importer.SOURCE_HEADER)
        writer.writerow(row)

    payload = source.read_bytes()
    monkeypatch.setattr(importer, "SOURCE_BYTES", len(payload))
    monkeypatch.setattr(importer, "SOURCE_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(importer, "SOURCE_ROWS", 1)

    with pytest.raises(RuntimeError, match=message):
        importer.validate_source(source)


def test_import_and_replace_sql_are_bulk_atomic_and_lossless() -> None:
    import_sql = importer.import_sql("private-bucket")
    replace_sql = importer.replace_sql()

    assert "aws_s3.table_import_from_s3" in import_sql
    assert importer.object_key() in import_sql
    assert "jobs_import" in import_sql
    assert "HEADER true" in import_sql
    assert "TRUNCATE jobs" in replace_sql
    assert "INSERT INTO jobs" in replace_sql
    assert "DROP TABLE jobs_import" in replace_sql
    assert "::numeric(12, 2)" in replace_sql
    assert "::timestamp without time zone" in replace_sql
    assert "NULLIF(NULLIF(" in replace_sql


@pytest.mark.parametrize("stored_checksum", [None, "forged-checksum"])
def test_existing_object_without_the_exact_stored_checksum_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stored_checksum: str | None,
) -> None:
    source = tmp_path / "jobs.csv"
    source.write_bytes(b"source")
    calls: list[list[str]] = []

    def fake_aws(arguments: list[str], **_: object) -> dict[str, object]:
        calls.append(arguments)
        head: dict[str, object] = {
            "ContentLength": importer.SOURCE_BYTES,
            "Metadata": {"sha256": importer.SOURCE_SHA256},
        }
        if stored_checksum is not None:
            head["ChecksumSHA256"] = stored_checksum
        return head

    monkeypatch.setattr(importer, "aws", fake_aws)

    with pytest.raises(RuntimeError, match="different object"):
        importer.ensure_source_object(source, "private-bucket")

    assert [arguments[:2] for arguments in calls] == [["s3api", "head-object"]]
    assert "--checksum-mode" in calls[0]


def test_existing_object_with_exact_size_metadata_and_checksum_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "jobs.csv"
    source.write_bytes(b"source")
    calls: list[list[str]] = []

    def fake_aws(arguments: list[str], **_: object) -> dict[str, object]:
        calls.append(arguments)
        return {
            "ContentLength": importer.SOURCE_BYTES,
            "Metadata": {"sha256": importer.SOURCE_SHA256},
            "ChecksumSHA256": importer.SOURCE_CHECKSUM_SHA256,
        }

    monkeypatch.setattr(importer, "aws", fake_aws)

    importer.ensure_source_object(source, "private-bucket")

    assert [arguments[:2] for arguments in calls] == [["s3api", "head-object"]]


def test_new_s3_object_uses_a_conditional_put(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "jobs.csv"
    source.write_bytes(b"source")
    calls: list[list[str]] = []

    def fake_aws(arguments: list[str], **_: object) -> dict[str, object]:
        calls.append(arguments)
        if len(calls) == 1:
            raise importer.AwsError("missing", "Not Found")
        if arguments[:2] == ["s3api", "head-object"]:
            return {
                "ContentLength": importer.SOURCE_BYTES,
                "Metadata": {"sha256": importer.SOURCE_SHA256},
                "ChecksumSHA256": importer.SOURCE_CHECKSUM_SHA256,
            }
        return {}

    monkeypatch.setattr(importer, "aws", fake_aws)

    importer.ensure_source_object(source, "private-bucket")

    put = calls[1]
    assert put[:2] == ["s3api", "put-object"]
    assert put[put.index("--if-none-match") + 1] == "*"
    assert put[put.index("--checksum-algorithm") + 1] == "SHA256"
    assert put[put.index("--checksum-sha256") + 1] == importer.SOURCE_CHECKSUM_SHA256


def test_long_sql_requests_continue_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_aws(arguments: list[str], **_: object) -> dict[str, object]:
        calls.append(arguments)
        return {}

    monkeypatch.setattr(importer, "aws", fake_aws)
    importer.execute_sql("cluster", "secret", "SELECT 1", long_running=True)

    assert "--continue-after-timeout" in calls[0]


def test_aws_error_surfaces_bounded_stderr_without_command_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = "  Access   denied\nfor caller  " + "x" * 2_100

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["aws"], 1, stdout="", stderr=stderr)

    monkeypatch.setattr("import_jobs_to_aws.subprocess.run", fake_run)

    with pytest.raises(importer.AwsError) as caught:
        importer.aws(["rds-data", "execute-statement", "--secret-arn", "must-not-appear"])

    assert "Access denied for caller" in str(caught.value)
    assert "\n" not in str(caught.value)
    assert "must-not-appear" not in str(caught.value)
    assert len(str(caught.value)) < 2_100
    assert caught.value.stderr == stderr.strip()


def test_polling_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def fake_query(*_: object) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        return {"row_count": 0}

    monkeypatch.setattr(importer, "POLL_ATTEMPTS", 2)
    monkeypatch.setattr(importer, "query_one", fake_query)
    monkeypatch.setattr(importer, "sleep", lambda _: None)

    with pytest.raises(RuntimeError, match="timed out"):
        importer.wait_for(
            "cluster",
            "secret",
            "SELECT 1",
            lambda row: row["row_count"] == importer.SOURCE_ROWS,
            "import",
        )

    assert attempts == 2
