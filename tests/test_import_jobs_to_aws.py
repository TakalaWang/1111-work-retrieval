from __future__ import annotations

import csv
import hashlib
import re
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
    assert int(importer.SOURCE_SHA256[:16], 16) == importer.IMPORT_ADVISORY_LOCK_ID
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


def test_import_and_replace_sql_is_advisory_locked_atomic_and_lossless() -> None:
    sql = importer.import_and_replace_sql("private-bucket")

    assert "aws_s3.table_import_from_s3" in sql
    assert importer.object_key() in sql
    assert "jobs_import" in sql
    assert "HEADER true" in sql
    assert "TRUNCATE jobs" in sql
    assert "INSERT INTO jobs" in sql
    assert "DROP TABLE jobs_import" in sql
    assert "COMMENT ON TABLE jobs IS" in sql
    assert "jobs_invalidate_source_identity" in sql
    assert "COMMENT ON TABLE public.jobs IS NULL" in sql
    assert "AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE" in sql
    assert f"pg_try_advisory_xact_lock({importer.IMPORT_ADVISORY_LOCK_ID})" in sql
    assert sql.index("pg_try_advisory_xact_lock") < sql.index("DROP TABLE IF EXISTS jobs_import")
    assert "another verified jobs import already holds the advisory lock" in sql
    function_body = re.search(r"AS \$function\$(.*?)\$function\$", sql, re.DOTALL)
    assert function_body is not None
    assert function_body.group(1) == importer.INTEGRITY_GUARD_BODY
    assert importer.INTEGRITY_GUARD_BODY in importer.final_stats_sql()
    assert importer.SOURCE_IDENTITY in sql
    assert "::numeric(12, 2)" in sql
    assert "::timestamp without time zone" in sql
    assert "NULLIF(NULLIF(" in sql
    assert sql.count("inserted_count bigint;") == 1


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


def test_matching_database_snapshot_skips_destructive_reimport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "jobs.csv"
    source.write_bytes(b"verified")
    executed: list[str] = []

    monkeypatch.setattr(importer, "validate_source", lambda _: None)
    monkeypatch.setattr(importer, "verify_account", lambda: None)
    monkeypatch.setattr(
        importer,
        "stack_outputs",
        lambda: ("bucket", "cluster", "secret"),
    )
    monkeypatch.setattr(importer, "ensure_source_object", lambda *_: None)
    monkeypatch.setattr(
        importer,
        "query_one",
        lambda _cluster, _secret, sql: (
            {"version_num": importer.ALEMBIC_REVISION}
            if "alembic_version" in sql
            else {
                "row_count": importer.SOURCE_ROWS,
                "distinct_job_ids": importer.SOURCE_ROWS,
                "min_source_row": 0,
                "max_source_row": importer.SOURCE_ROWS - 1,
                "source_identity": importer.SOURCE_IDENTITY,
                "integrity_guard": True,
            }
        ),
    )
    monkeypatch.setattr(
        importer,
        "execute_sql",
        lambda _cluster, _secret, sql, **_: executed.append(sql),
    )

    importer.run(source)

    assert executed == []


def test_same_shape_database_with_different_source_is_reimported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "jobs.csv"
    source.write_bytes(b"verified")
    executed: list[str] = []
    queries = 0

    monkeypatch.setattr(importer, "validate_source", lambda _: None)
    monkeypatch.setattr(importer, "verify_account", lambda: None)
    monkeypatch.setattr(importer, "stack_outputs", lambda: ("bucket", "cluster", "secret"))
    monkeypatch.setattr(importer, "ensure_source_object", lambda *_: None)

    def fake_query(_cluster: str, _secret: str, sql: str) -> dict[str, object]:
        nonlocal queries
        queries += 1
        if "alembic_version" in sql:
            return {"version_num": importer.ALEMBIC_REVISION}
        return {
            "row_count": importer.SOURCE_ROWS,
            "distinct_job_ids": importer.SOURCE_ROWS,
            "min_source_row": 0,
            "max_source_row": importer.SOURCE_ROWS - 1,
            "source_identity": ("sha256:different" if queries == 2 else importer.SOURCE_IDENTITY),
            "integrity_guard": True,
        }

    monkeypatch.setattr(importer, "query_one", fake_query)
    monkeypatch.setattr(
        importer,
        "execute_sql",
        lambda _cluster, _secret, sql, **_: executed.append(sql),
    )
    monkeypatch.setattr(
        importer,
        "wait_for",
        lambda _cluster, _secret, _sql, _accepted, _operation: {
            "row_count": importer.SOURCE_ROWS,
            "distinct_job_ids": importer.SOURCE_ROWS,
            "min_source_row": 0,
            "max_source_row": importer.SOURCE_ROWS - 1,
            "source_identity": importer.SOURCE_IDENTITY,
            "integrity_guard": True,
        },
    )

    importer.run(source)

    assert any("TRUNCATE jobs" in sql for sql in executed)
    imports = [sql for sql in executed if "aws_s3.table_import_from_s3" in sql]
    assert len(imports) == 1
    assert "pg_try_advisory_xact_lock" in imports[0]


def test_matching_marker_without_integrity_guard_is_reimported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "jobs.csv"
    source.write_bytes(b"verified")
    executed: list[str] = []
    final_checks = 0

    monkeypatch.setattr(importer, "validate_source", lambda _: None)
    monkeypatch.setattr(importer, "verify_account", lambda: None)
    monkeypatch.setattr(importer, "stack_outputs", lambda: ("bucket", "cluster", "secret"))
    monkeypatch.setattr(importer, "ensure_source_object", lambda *_: None)

    def fake_query(_cluster: str, _secret: str, sql: str) -> dict[str, object]:
        nonlocal final_checks
        if "alembic_version" in sql:
            return {"version_num": importer.ALEMBIC_REVISION}
        final_checks += 1
        return {
            "row_count": importer.SOURCE_ROWS,
            "distinct_job_ids": importer.SOURCE_ROWS,
            "min_source_row": 0,
            "max_source_row": importer.SOURCE_ROWS - 1,
            "source_identity": importer.SOURCE_IDENTITY,
            "integrity_guard": final_checks > 1,
        }

    monkeypatch.setattr(importer, "query_one", fake_query)
    monkeypatch.setattr(
        importer,
        "execute_sql",
        lambda _cluster, _secret, sql, **_: executed.append(sql),
    )
    monkeypatch.setattr(
        importer,
        "wait_for",
        lambda _cluster, _secret, _sql, _accepted, _operation: {
            "row_count": importer.SOURCE_ROWS,
            "distinct_job_ids": importer.SOURCE_ROWS,
            "min_source_row": 0,
            "max_source_row": importer.SOURCE_ROWS - 1,
            "source_identity": importer.SOURCE_IDENTITY,
            "integrity_guard": True,
        },
    )

    importer.run(source)

    assert any("TRUNCATE jobs" in sql for sql in executed)
