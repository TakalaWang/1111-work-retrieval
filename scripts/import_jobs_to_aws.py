#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from time import sleep
from typing import cast

AWS_ACCOUNT = "378849533305"
AWS_REGION = "us-west-2"
AWS_PROFILE = "competition"
STACK_NAME = "WorkRetrievalData"
DATABASE_NAME = "work_retrieval"
SOURCE_SHA256 = "53937f7bf076789c4cd7e3be34fb89875336108d57707b5a93182181e1087089"
SOURCE_BYTES = 1_285_945_103
SOURCE_ROWS = 1_218_635
POLL_ATTEMPTS = 240
POLL_SECONDS = 15
ALEMBIC_REVISION = "0002_create_jobs"

SOURCE_HEADER = (
    "職缺編號",
    "職務名稱",
    "職務內容",
    "薪資",
    "薪資下限",
    "薪資上限",
    "職務大類",
    "職務中類",
    "職務小類",
    "職缺屬性",
    "工時",
    "工時說明",
    "工作城市",
    "學歷需求",
    "科系需求1",
    "科系需求2",
    "科系需求3",
    "工作經驗需求",
    "語言能力一",
    "語言能力一聽",
    "語言能力一說",
    "語言能力一讀",
    "語言能力一寫",
    "語言能力二",
    "語言能力二聽",
    "語言能力二說",
    "語言能力二讀",
    "語言能力二寫",
    "電腦技能資料",
    "專業證照",
    "工作技能",
    "附加條件",
    "管理人數",
    "是否需外派",
    "廠商編號",
    "產業大類",
    "產業中類",
    "產業小類",
    "職缺最後修改時間",
)

SOURCE_COLUMNS = (
    "job_id",
    "title",
    "description",
    "salary_text",
    "salary_min",
    "salary_max",
    "duty_major",
    "duty_middle",
    "duty_minor",
    "job_attribute",
    "work_hours",
    "work_hours_description",
    "work_city",
    "education_requirement",
    "major_requirement_1",
    "major_requirement_2",
    "major_requirement_3",
    "experience_requirement",
    "language_1",
    "language_1_listening",
    "language_1_speaking",
    "language_1_reading",
    "language_1_writing",
    "language_2",
    "language_2_listening",
    "language_2_speaking",
    "language_2_reading",
    "language_2_writing",
    "computer_skills",
    "professional_certifications",
    "work_skills",
    "additional_conditions",
    "management_count",
    "requires_travel",
    "vendor_id",
    "industry_major",
    "industry_middle",
    "industry_minor",
    "source_modified_at",
)

REQUIRED_FIELDS = {
    0: "job_id",
    1: "title",
    3: "salary_text",
    34: "vendor_id",
    38: "source_modified_at",
}


class AwsError(RuntimeError):
    def __init__(self, message: str, stderr: str) -> None:
        super().__init__(message)
        self.stderr = stderr


def validate_source(source: Path) -> None:
    if not source.is_file():
        raise RuntimeError(f"source is not a regular file: {source}")
    if source.stat().st_size != SOURCE_BYTES:
        raise RuntimeError(f"source size must be {SOURCE_BYTES} bytes")

    digest = hashlib.sha256()
    bytes_read = 0

    def decoded_lines() -> Iterator[str]:
        nonlocal bytes_read
        with source.open("rb") as stream:
            for raw_line in stream:
                digest.update(raw_line)
                bytes_read += len(raw_line)
                try:
                    yield raw_line.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise RuntimeError("source must be valid UTF-8") from error

    try:
        reader = csv.reader(decoded_lines())
        header = next(reader, None)
        if header != list(SOURCE_HEADER):
            raise RuntimeError("source header does not match the exact 39-column contract")
        rows = 0
        for rows, row in enumerate(reader, start=1):
            if len(row) != len(SOURCE_HEADER):
                raise RuntimeError(f"source row {rows} must contain exactly 39 columns")
            for index, name in REQUIRED_FIELDS.items():
                if not row[index].strip() or row[index] == "NULL":
                    raise RuntimeError(f"source row {rows} has empty required field {name}")
    except csv.Error as error:
        raise RuntimeError(f"invalid CSV: {error}") from error

    if bytes_read != SOURCE_BYTES:
        raise RuntimeError("source changed while it was being read")
    if rows != SOURCE_ROWS:
        raise RuntimeError(f"source must contain exactly {SOURCE_ROWS} data rows")
    if digest.hexdigest() != SOURCE_SHA256:
        raise RuntimeError("source SHA-256 does not match the verified snapshot")


def object_key() -> str:
    return f"data/jobs/{SOURCE_SHA256}/jobs.csv"


def aws(arguments: list[str], *, allow_statement_timeout: bool = False) -> dict[str, object]:
    command = [
        "aws",
        *arguments,
        "--profile",
        AWS_PROFILE,
        "--region",
        AWS_REGION,
        "--output",
        "json",
        "--no-cli-pager",
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        if allow_statement_timeout and "StatementTimeoutException" in result.stderr:
            return {}
        raise AwsError(f"AWS CLI command failed: {' '.join(command[:3])}", result.stderr.strip())
    if not result.stdout.strip():
        return {}
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("AWS CLI returned an unexpected JSON shape")
    return cast(dict[str, object], value)


def verify_account() -> None:
    account = aws(["sts", "get-caller-identity"]).get("Account")
    if account != AWS_ACCOUNT:
        raise RuntimeError(f"AWS caller must be account {AWS_ACCOUNT}, got {account!r}")


def stack_outputs() -> tuple[str, str, str]:
    response = aws(["cloudformation", "describe-stacks", "--stack-name", STACK_NAME])
    stacks = response.get("Stacks")
    if not isinstance(stacks, list) or len(stacks) != 1 or not isinstance(stacks[0], dict):
        raise RuntimeError(f"CloudFormation stack {STACK_NAME} was not found exactly once")
    raw_outputs = stacks[0].get("Outputs")
    if not isinstance(raw_outputs, list):
        raise RuntimeError("CloudFormation stack outputs are missing")
    outputs = {
        item["OutputKey"]: item["OutputValue"]
        for item in raw_outputs
        if isinstance(item, dict)
        and isinstance(item.get("OutputKey"), str)
        and isinstance(item.get("OutputValue"), str)
    }
    required = ("RuntimeBucketName", "DatabaseClusterArn", "DatabaseSecretArn")
    if any(name not in outputs for name in required):
        raise RuntimeError(f"CloudFormation outputs must include {', '.join(required)}")
    bucket, cluster, secret = (outputs[name] for name in required)
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise RuntimeError("RuntimeBucketName is invalid")
    rds_prefix = f"arn:aws:rds:{AWS_REGION}:{AWS_ACCOUNT}:cluster:"
    secret_prefix = f"arn:aws:secretsmanager:{AWS_REGION}:{AWS_ACCOUNT}:secret:"
    if not cluster.startswith(rds_prefix) or not secret.startswith(secret_prefix):
        raise RuntimeError("database outputs do not belong to the required account and region")
    return bucket, cluster, secret


def ensure_source_object(source: Path, bucket: str) -> None:
    head_arguments = [
        "s3api",
        "head-object",
        "--bucket",
        bucket,
        "--key",
        object_key(),
        "--expected-bucket-owner",
        AWS_ACCOUNT,
    ]
    try:
        existing = aws(head_arguments)
    except AwsError as error:
        if not any(marker in error.stderr for marker in ("(404)", "Not Found", "NoSuchKey")):
            raise
    else:
        _verify_source_object(existing)
        return

    try:
        aws(
            [
                "s3api",
                "put-object",
                "--bucket",
                bucket,
                "--key",
                object_key(),
                "--body",
                str(source),
                "--metadata",
                f"sha256={SOURCE_SHA256}",
                "--if-none-match",
                "*",
                "--expected-bucket-owner",
                AWS_ACCOUNT,
            ]
        )
    except AwsError as error:
        if "PreconditionFailed" not in error.stderr:
            raise
    _verify_source_object(aws(head_arguments))


def _verify_source_object(head: dict[str, object]) -> None:
    metadata = head.get("Metadata")
    sha256 = metadata.get("sha256") if isinstance(metadata, dict) else None
    if head.get("ContentLength") != SOURCE_BYTES or sha256 != SOURCE_SHA256:
        raise RuntimeError("S3 key already contains a different object")


def execute_sql(cluster: str, secret: str, sql: str, *, long_running: bool = False) -> None:
    arguments = [
        "rds-data",
        "execute-statement",
        "--resource-arn",
        cluster,
        "--secret-arn",
        secret,
        "--database",
        DATABASE_NAME,
        "--sql",
        sql,
    ]
    if long_running:
        arguments.append("--continue-after-timeout")
    aws(arguments, allow_statement_timeout=long_running)


def query_one(cluster: str, secret: str, sql: str) -> dict[str, object]:
    response = aws(
        [
            "rds-data",
            "execute-statement",
            "--resource-arn",
            cluster,
            "--secret-arn",
            secret,
            "--database",
            DATABASE_NAME,
            "--sql",
            sql,
            "--format-records-as",
            "JSON",
        ]
    )
    formatted = response.get("formattedRecords")
    if not isinstance(formatted, str):
        raise RuntimeError("Data API query did not return formattedRecords")
    rows = json.loads(formatted)
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise RuntimeError("Data API query must return exactly one row")
    return cast(dict[str, object], rows[0])


def wait_for(
    cluster: str,
    secret: str,
    sql: str,
    accepted: Callable[[dict[str, object]], bool],
    operation: str,
) -> dict[str, object]:
    for attempt in range(POLL_ATTEMPTS):
        row = query_one(cluster, secret, sql)
        if accepted(row):
            return row
        if attempt + 1 < POLL_ATTEMPTS:
            sleep(POLL_SECONDS)
    raise RuntimeError(f"{operation} timed out after {POLL_ATTEMPTS} checks")


def create_stage_sql() -> str:
    columns = ",\n    ".join(f"{name} TEXT" for name in SOURCE_COLUMNS)
    return f"""CREATE TABLE jobs_import (
    {columns},
    source_row INTEGER GENERATED ALWAYS AS IDENTITY (START WITH 0 MINVALUE 0)
)"""


def import_sql(bucket: str) -> str:
    columns = ",".join(SOURCE_COLUMNS)
    return f"""SELECT aws_s3.table_import_from_s3(
    'jobs_import',
    '{columns}',
    '(FORMAT csv, HEADER true, ENCODING ''UTF8'', NULL ''__WORK_RETRIEVAL_NEVER_NULL__'')',
    aws_commons.create_s3_uri('{_sql_literal(bucket)}', '{object_key()}', '{AWS_REGION}')
)"""


def stage_stats_sql() -> str:
    invalid = " OR ".join(
        f"NULLIF(NULLIF({name}, ''), 'NULL') IS NULL" for name in REQUIRED_FIELDS.values()
    )
    return f"""SELECT
    count(*) AS row_count,
    count(DISTINCT job_id) AS distinct_job_ids,
    min(source_row) AS min_source_row,
    max(source_row) AS max_source_row,
    count(*) FILTER (WHERE {invalid}) AS invalid_required
FROM jobs_import"""


def final_stats_sql() -> str:
    return """SELECT
    count(*) AS row_count,
    count(DISTINCT job_id) AS distinct_job_ids,
    min(source_row) AS min_source_row,
    max(source_row) AS max_source_row
FROM jobs"""


def replace_sql() -> str:
    target_columns = ", ".join((*SOURCE_COLUMNS, "source_row"))
    expressions = []
    for name in SOURCE_COLUMNS:
        normalized = f"NULLIF(NULLIF({name}, ''), 'NULL')"
        if name in {"salary_min", "salary_max"}:
            normalized += "::numeric(12, 2)"
        elif name == "source_modified_at":
            normalized += "::timestamp without time zone"
        expressions.append(normalized)
    select_columns = ",\n        ".join((*expressions, "source_row"))
    invalid_required = " OR ".join(
        f"NULLIF(NULLIF({name}, ''), 'NULL') IS NULL" for name in REQUIRED_FIELDS.values()
    )
    return f"""DO $$
DECLARE
    stage_count bigint;
    stage_distinct bigint;
    stage_min integer;
    stage_max integer;
    stage_invalid bigint;
    inserted_count bigint;
BEGIN
    SELECT count(*), count(DISTINCT job_id), min(source_row), max(source_row),
           count(*) FILTER (WHERE {invalid_required})
      INTO stage_count, stage_distinct, stage_min, stage_max, stage_invalid
      FROM jobs_import;
    IF stage_count <> {SOURCE_ROWS} OR stage_distinct <> {SOURCE_ROWS}
       OR stage_min <> 0 OR stage_max <> {SOURCE_ROWS - 1} OR stage_invalid <> 0 THEN
        RAISE EXCEPTION 'staging validation failed';
    END IF;

    TRUNCATE jobs;
    INSERT INTO jobs ({target_columns})
    SELECT
        {select_columns}
    FROM jobs_import;
    GET DIAGNOSTICS inserted_count = ROW_COUNT;
    IF inserted_count <> {SOURCE_ROWS} THEN
        RAISE EXCEPTION 'final insert count mismatch';
    END IF;
    DROP TABLE jobs_import;
END
$$"""


def _sql_literal(value: str) -> str:
    return value.replace("'", "''")


def _stats_match(row: dict[str, object], *, include_invalid: bool) -> bool:
    expected = {
        "row_count": SOURCE_ROWS,
        "distinct_job_ids": SOURCE_ROWS,
        "min_source_row": 0,
        "max_source_row": SOURCE_ROWS - 1,
    }
    if include_invalid:
        expected["invalid_required"] = 0
    return all(row.get(name) == value for name, value in expected.items())


def run(source: Path) -> None:
    validate_source(source)
    verify_account()
    bucket, cluster, secret = stack_outputs()
    version = query_one(cluster, secret, "SELECT version_num FROM alembic_version")
    if version.get("version_num") != ALEMBIC_REVISION:
        raise RuntimeError(f"database must be at Alembic revision {ALEMBIC_REVISION}")

    ensure_source_object(source, bucket)
    execute_sql(cluster, secret, "CREATE EXTENSION IF NOT EXISTS aws_s3 CASCADE", long_running=True)
    wait_for(
        cluster,
        secret,
        "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'aws_s3') AS ready",
        lambda row: row.get("ready") is True,
        "aws_s3 extension creation",
    )
    execute_sql(cluster, secret, "DROP TABLE IF EXISTS jobs_import", long_running=True)
    wait_for(
        cluster,
        secret,
        "SELECT to_regclass('public.jobs_import') IS NULL AS ready",
        lambda row: row.get("ready") is True,
        "staging table removal",
    )
    execute_sql(cluster, secret, create_stage_sql(), long_running=True)
    wait_for(
        cluster,
        secret,
        "SELECT to_regclass('public.jobs_import') IS NOT NULL AS ready",
        lambda row: row.get("ready") is True,
        "staging table creation",
    )
    execute_sql(cluster, secret, import_sql(bucket), long_running=True)
    wait_for(
        cluster,
        secret,
        stage_stats_sql(),
        lambda row: _stats_match(row, include_invalid=True),
        "S3 import",
    )
    execute_sql(cluster, secret, replace_sql(), long_running=True)
    wait_for(
        cluster,
        secret,
        "SELECT to_regclass('public.jobs_import') IS NULL AS completed",
        lambda row: row.get("completed") is True,
        "atomic replacement",
    )
    final = query_one(cluster, secret, final_stats_sql())
    if not _stats_match(final, include_invalid=False):
        raise RuntimeError(f"final jobs validation failed: {final}")
    print(
        json.dumps(
            {"bucket": bucket, "key": object_key(), **final},
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Import the verified job snapshot into Aurora")
    parser.add_argument("source", type=Path)
    arguments = parser.parse_args()
    run(arguments.source)


if __name__ == "__main__":
    main()
