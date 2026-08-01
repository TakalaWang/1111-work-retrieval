# Job Data Aurora Import Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Persist every row and field from the authoritative `職缺.csv` snapshot in the competition AWS account's Aurora PostgreSQL database without implementing the future job-detail API.

**Architecture:** Add one explicit SQLAlchemy `jobs` model and a forward-only Alembic migration. Split the deployable data plane from the fixed-cost application plane so this task creates only VPC/S3/Aurora, then upload the immutable source CSV to the private S3 bucket, import it into a staging table with Aurora's native `aws_s3` extension, validate the complete snapshot, and atomically replace `jobs`. The operator script fails unless the AWS account, `us-west-2` region, source SHA-256, header, row count, stack outputs, and final database counts match expected values.

**Tech Stack:** Python 3.12 standard library, SQLAlchemy 2, Alembic, psycopg 3, PostgreSQL 16/Aurora PostgreSQL, AWS CLI, AWS CDK, S3, RDS Data API.

---

### Task 1: Define the job domain table

**Files:**

- Modify: `packages/database/src/work_retrieval_database/models.py`
- Modify: `packages/database/src/work_retrieval_database/__init__.py`
- Modify: `packages/database/tests/test_models.py`
- Create: `database/versions/0002_create_jobs.py`
- Modify: `.kiro/specs/job-search-api-platform/requirements.md`
- Modify: `.kiro/specs/job-search-api-platform/design.md`
- Modify: `.kiro/specs/job-search-api-platform/tasks.md`

1. Write failing model tests for a `jobs` table with `job_id` primary key, unique non-null `source_row`, 38 source-detail fields, exact `NUMERIC(12,2)` salary bounds, and millisecond source timestamp.
2. Run `uv run pytest packages/database/tests/test_models.py` and confirm failure.
3. Implement the minimum SQLAlchemy model and export it.
4. Add an Alembic migration with only the primary key and source-row uniqueness; do not add speculative lookup indexes or normalized child tables.
5. Update Kiro documents to make the snapshot ownership and API exclusion explicit.
6. Run Python lint, strict typecheck, model tests, and Alembic upgrade/check against PostgreSQL 16.
7. Commit the task.

### Task 2: Implement a fail-closed Aurora bulk importer

**Files:**

- Create: `scripts/import_jobs_to_aws.py`
- Create: `tests/test_import_jobs_to_aws.py`
- Modify: `pyproject.toml`
- Modify: `infra/lib/platform-stack.ts`
- Modify: `infra/test/platform-stack.test.ts`

1. Write failing tests for the exact 39-column header, competition account `378849533305`, region `us-west-2`, source SHA-256 `53937f7bf076789c4cd7e3be34fb89875336108d57707b5a93182181e1087089`, 1,218,635 rows, and generated SQL validation.
2. Run the focused tests and confirm failure.
3. Implement one stdlib operator script that validates the local CSV, resolves CloudFormation outputs and the RDS secret, uploads to a content-addressed S3 key, uses Data API plus `aws_s3.table_import_from_s3`, validates staging, atomically replaces `jobs`, and validates final count/distinct/source-row range. It must never accept another account or silently fall back.
4. Grant the Aurora cluster read-only import access to the private runtime bucket and output its secret ARN. Keep the object private and immutable by SHA prefix.
5. Run focused Python and CDK tests, then full lint/type/test/synth checks.
6. Commit the task.

### Task 2B: Split the minimum deployable data plane

**Files:**

- Create: `infra/lib/data-stack.ts`
- Modify: `infra/lib/platform-stack.ts`
- Modify: `infra/bin/platform.ts`
- Create: `infra/test/data-stack.test.ts`
- Modify: `infra/test/platform-stack.test.ts`
- Modify: `.kiro/specs/job-search-api-platform/design.md`
- Modify: `.kiro/specs/job-search-api-platform/requirements.md`
- Modify: `.github/workflows/deploy.yml`

1. Write failing CDK assertions for `WorkRetrievalData` owning VPC, private/versioned S3, and private encrypted Aurora with Data API, Secrets Manager, S3 import, zero-to-four ACU scaling, auto-pause, and deletion protection.
2. Move those resources from `PlatformStack` into `DataStack`; pass the same VPC, bucket, cluster, and database security group into `PlatformStack` so later application deployment references the existing database instead of creating another.
3. Keep public subnets available for the later ALB but deploy no ALB, CloudFront, WAF, ECR, ECS, GPU, interface VPC endpoints, or web bucket when only `WorkRetrievalData` is selected.
4. Ensure manual full-platform deployment explicitly deploys both stacks, while this task can deploy only `WorkRetrievalData` with no API image parameters.
5. Update Kiro design/requirements and verify synth assertions prove the data-only template contains none of the fixed-cost application-plane resources.
6. Commit the task.

### Task 3: Perform and verify the competition AWS import

**Files:**

- Modify: `README.md`
- Modify: `CONTRIBUTING.md`

1. Verify `competition` resolves to caller account `378849533305` in `us-west-2`.
2. Deploy only `WorkRetrievalData`; do not create the ALB, CloudFront, WAF, interface endpoints, API capacity, or a job-detail API.
3. Apply Alembic `0002_create_jobs` to Aurora through an authenticated in-scope administration path.
4. Run the importer against `/Users/takala/code/1111 work retrieval/dataset/職缺.csv`.
5. Read back source object metadata, `count(*)`, `count(distinct job_id)`, source-row bounds, and representative rows from Aurora. Require 1,218,635 rows and no duplicate IDs.
6. Document only the verified invocation and operational boundary, then run the complete acceptance suite.
7. Request spec review, then code-quality review; fix all findings.
8. Commit, merge to `main`, push, and verify remote CI. Do not claim the future detail API exists.

Status: AWS deployment, migration, immutable S3 upload, atomic Aurora import, and independent
readback completed on 2026-08-01. Final repository acceptance, review, merge, push, and remote CI
verification remain.
