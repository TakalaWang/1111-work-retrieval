# Repository Quality Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove the public job-import path and make the API, PostgreSQL model, frontend runtime guards, CI, and ECS database configuration enforce one read-only production contract.

**Architecture:** Keep ranking behind the existing `SearchEngine` protocol. Make `packages/database` the single owner of the `jobs` model and pooled PostgreSQL reads, inject that repository into FastAPI, and expose only search and read operations. Keep the current bounded per-result detail reads instead of adding a speculative batch API.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, SQLAlchemy 2, psycopg 3, PostgreSQL 16, SvelteKit 2/Svelte 5, TypeScript, Vitest, AWS CDK, GitHub Actions.

---

### Task 1: Close the job-ID contract gap

**Files:**

- Modify: `apps/api/src/work_retrieval_api/models.py`
- Modify: `apps/api/src/work_retrieval_api/app.py`
- Modify: `apps/api/tests/test_app.py`
- Modify: `apps/web/src/lib/search.ts`
- Modify: `apps/web/src/lib/search.test.ts`

1. Add failing API tests proving non-ASCII, non-numeric, duplicate, and over-limit engine IDs fail closed.
2. Add failing web tests proving malformed ranks, duplicate IDs, and non-numeric IDs are rejected at the browser boundary.
3. Reuse the existing `JobId` type for API outputs and require ASCII decimal engine IDs.
4. Validate the complete ranked response shape in one frontend predicate: at most ten unique numeric IDs and consecutive ranks starting at one.
5. Run the focused Python and Vitest suites.

### Task 2: Establish one PostgreSQL ownership path

**Files:**

- Modify: `packages/database/src/work_retrieval_database/models.py`
- Modify: `packages/database/src/work_retrieval_database/__init__.py`
- Create: `packages/database/src/work_retrieval_database/repository.py`
- Modify: `packages/database/pyproject.toml`
- Modify: `packages/database/tests/test_models.py`
- Create: `packages/database/tests/test_repository.py`
- Modify: `apps/api/pyproject.toml`
- Modify: `apps/api/src/work_retrieval_api/jobs.py`

1. Add failing metadata tests for the existing `jobs` migration.
2. Define the exact SQLAlchemy `Job` model for `job_id`, JSONB details, and server-owned timestamps.
3. Add one pooled, `pool_pre_ping` PostgreSQL repository with bounded connection/query timeouts and safe database errors.
4. Add a localhost-only PostgreSQL integration check for reading a stored job and returning `None` for a missing job.
5. Move psycopg ownership to the database package and make the API depend on that workspace package.

### Task 3: Remove request-time CSV mutation

**Files:**

- Modify: `apps/api/src/work_retrieval_api/__init__.py`
- Modify: `apps/api/src/work_retrieval_api/app.py`
- Modify: `apps/api/src/work_retrieval_api/jobs.py`
- Modify: `apps/api/src/work_retrieval_api/models.py`
- Modify: `apps/api/tests/test_app.py`
- Delete: `apps/api/tests/test_jobs.py`
- Regenerate: `packages/contract/openapi.json`
- Regenerate: `packages/contract/types.d.ts`

1. Change the injected boundary from `JobImporter` to read-only `JobRepository`.
2. Require both engine and repository factories at startup; do not keep an unconfigured runtime fallback.
3. Delete `POST /api/v1/jobs/pull`, CSV scanning, upsert code, request model, and importer-only errors.
4. Keep `GET /api/v1/job-details/{job_id}` and map database unavailability to a sanitized `503`.
5. Close both resources with `ExitStack` so one failing close cannot leak the other.
6. Regenerate and verify OpenAPI/TypeScript contracts.

### Task 4: Wire ECS and CI to the real data contract

**Files:**

- Modify: `infra/lib/platform-stack.ts`
- Modify: `infra/test/platform-stack.test.ts`
- Modify: `.github/workflows/ci.yml`
- Modify: `packages/contract/scripts/check-generated.mjs`

1. Inject database host, port, name, username, and password into the ECS task, using Secrets Manager for credentials.
2. Audit rate limiting at the actual trust boundary; defer it because regional WAF behind
   CloudFront has no trustworthy viewer IP in the current design.
3. Assert the generated task-definition shape in CDK tests.
4. Limit GitHub OIDC assumption to the four standard CDK bootstrap roles instead of `cdk-*`.
5. Run `alembic check` and the repository PostgreSQL integration test in the existing PostgreSQL CI job.
6. Convert contract-script URLs with Node's `fileURLToPath` so paths containing spaces work correctly.

### Task 5: Synchronize maintained documentation

**Files:**

- Modify: `README.md`
- Modify: `CONTRIBUTING.md`
- Modify: `.kiro/specs/job-search-api-platform/requirements.md`
- Modify: `.kiro/specs/job-search-api-platform/design.md`
- Modify: `.kiro/specs/job-search-api-platform/tasks.md`

1. Remove claims that the database has no domain table.
2. Document the read-only API and explicit repository injection.
3. State that bulk snapshot ingestion is a separate controlled operator workflow, not a public request path.
4. Keep deployment, experiment, and promotion boundaries unchanged.

### Task 6: Verify the whole repository

1. Run Ruff, strict mypy, all Python tests, OpenAPI drift checks, and PostgreSQL migration/model checks.
2. Run Prettier, ESLint, Svelte check, Vitest, web build, and generated-contract checks.
3. Run CDK tests, TypeScript build, and synth.
4. Run dependency audits, `git diff --check`, inspect the full diff, and report remaining risks without claiming deployment.
