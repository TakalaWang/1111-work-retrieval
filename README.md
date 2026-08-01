# 1111-work-retrieval

Production platform for 1111 job retrieval. This repository contains the FastAPI application,
SvelteKit web interface, PostgreSQL data access, immutable runtime-artifact contract, container
image, and AWS CDK infrastructure.

Search ranking is intentionally temporary: the production entrypoint returns the same first ten
real Aurora job IDs for every valid query. It is explicit, deterministic, and visibly labelled in
the web interface; it is not a production retrieval algorithm. The job-detail path reads all 39
source fields from PostgreSQL.

## Boundaries

- PostgreSQL/Aurora is the only relational database. SQLite is not supported.
- SQLAlchemy owns PostgreSQL domain models and pooled reads, Alembic owns migrations, and Pydantic
  owns the HTTP contract. These layers do not share model classes.
- `GET /api/v1/jobs/{job_id}` returns the complete persisted job record. Snapshot ingestion is an
  operator-only workflow and is never exposed as an HTTP write route.
- Runtime models, embeddings, and indexes live in private S3 objects under
  `runtime/<manifest-sha256>/...`; they are not committed to Git.
- There is no SQLite path, experimental ranking implementation, mock server, or automatic
  fallback.
- The deployable CPU API runs on Fargate. The GPU ECS-on-EC2 capacity provider remains explicitly
  at `0/0/0` until its production retrieval runtime and quota are approved.

## Repository layout

| Path                   | Responsibility                                                                      |
| ---------------------- | ----------------------------------------------------------------------------------- |
| `apps/api`             | FastAPI request validation, error envelopes, health endpoints, and OpenAPI          |
| `apps/web`             | Thin SvelteKit search UI                                                            |
| `packages/search-core` | Immutable search types and the `SearchEngine` protocol                              |
| `packages/database`    | SQLAlchemy job model, PostgreSQL settings, and pooled read repository               |
| `packages/contract`    | Committed OpenAPI, generated TypeScript types, and artifact manifest schema         |
| `database`             | Alembic configuration, baseline, and `jobs` table migration                         |
| `infra`                | AWS CDK stack for Aurora, S3, ECR, GPU ECS, ALB, CloudFront, WAF, and observability |

## Prerequisites

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Node.js 24 and pnpm 10.28.0
- PostgreSQL 16 for migration verification
- Docker for the production image build
- AWS CLI v2 and an AWS CDK-bootstrapped `competition` account for operator workflows

## Local checks

Copy `.env.example` to `.env` and export it in your shell when running Alembic against the
standard local PostgreSQL database. The file contains fixed AWS resource names for reference but
no AWS credentials or production secrets.

```bash
uv sync --frozen --all-packages
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv run python -m work_retrieval_api.export_openapi packages/contract/openapi.json --check

pnpm install --frozen-lockfile
pnpm lint
pnpm --dir packages/contract generate:check
pnpm --dir apps/web check
pnpm --dir apps/web test
pnpm --dir apps/web build
pnpm --dir infra test
pnpm --dir infra build
pnpm --dir infra synth
docker build --tag work-retrieval-api:local .
```

Run all migrations against an explicit PostgreSQL database:

```bash
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/work_retrieval \
  uv run alembic -c database/alembic.ini upgrade head
```

To intentionally update the committed API contract after changing the FastAPI schema:

```bash
uv run python -m work_retrieval_api.export_openapi packages/contract/openapi.json
pnpm --dir packages/contract generate
```

Commit both the OpenAPI document and generated TypeScript types. CI rejects drift.

## Runtime behavior

The container entrypoint is `work_retrieval_api.main:app`. Startup requires all five database
settings below and fails closed if PostgreSQL is unavailable or fewer than ten jobs exist:

```text
DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
```

The public paths are:

- `POST /api/v1/jobs/search` — temporary deterministic ten-job response.
- `GET /api/v1/jobs/{job_id}` — complete 39-field job detail.
- `GET /healthz` and `GET /readyz` — process and initialized-runtime health.

Copy `.env.example` for local names only. It contains no production credentials. Aurora
credentials are injected from Secrets Manager by ECS.

## Runtime artifacts

The promotion script accepts exactly the approved complete job embeddings, Qwen3-Embedding-8B
snapshot, and Tantivy index. It rejects SQLite, query history, behavior data, rerankers, and all
other experiment artifacts. Dry-run is the default:

```bash
uv run python scripts/promote_runtime_artifacts.py
uv run python scripts/promote_runtime_artifacts.py --execute
```

The script is pinned to profile `competition`, account `378849533305`, and region `us-west-2`.
Execution performs conditional server-side copies, verifies native S3 SHA-256 checksums, and
publishes canonical `runtime/<manifest-sha256>/manifest.json` last.

## Production data status

The last recorded data-plane readback in competition account `378849533305`, region `us-west-2`,
verified CloudFormation stack `WorkRetrievalData`, Aurora PostgreSQL 16.13 Serverless v2, and its
private, encrypted, versioned S3 bucket. Treat this as historical evidence and read AWS back again
before making an operational claim.

The authoritative snapshot is stored at:

```text
s3://workretrievaldata-runtimebucket404c5ee4-hkvrjx5fbkij/data/jobs/53937f7bf076789c4cd7e3be34fb89875336108d57707b5a93182181e1087089/jobs.csv
```

AWS readback on 2026-08-01 verified 1,218,635 rows, 1,218,635 distinct job IDs, source rows
`0..1218634`, Alembic revision `0002_create_jobs`, and only `alembic_version` plus `jobs` in the
public schema. The S3 object is 1,285,945,103 bytes and its SHA-256 metadata matches its prefix.
The importer is intentionally pinned to the `competition` profile, account, region, source path,
checksum, header, and row count:

```bash
AWS_PROFILE=competition AWS_DEFAULT_REGION=us-west-2 \
  uv run python scripts/import_jobs_to_aws.py \
  "/Users/takala/code/1111 work retrieval/dataset/職缺.csv"
```

## Manual production deployment

`.github/workflows/deploy.yml` is manual-only and runs only from `main` through the protected
`production` GitHub environment. It also requires repository variable `DEPLOY_ENABLED=true`,
environment variable `AWS_DEPLOY_ROLE_ARN`, and confirmation text `DEPLOY`. No push or merge
automatically deploys.

The workflow performs these operations in order:

1. Frozen Python and Node installs, followed by the static web build.
2. OIDC authentication to account `378849533305` in `us-west-2` and an idempotent
   `WorkRetrievalData` deployment.
3. Root Dockerfile build and push to the DataStack ECR repository, immutable digest resolution,
   and fail-closed ECR scan requiring zero critical or high findings.
4. `WorkRetrievalPlatform` deployment with CPU desired count `1` and GPU capacities `0/0/0`.
5. Static-site sync, CloudFront invalidation, and public health, readiness, web, search, and
   39-field detail smoke checks.

Required dispatch inputs are the promoted artifact manifest SHA and GPU instance type; the CPU and
GPU capacity inputs default to `1` and `0/0/0`. The workflow currently rejects non-zero GPU values.
It builds the API image itself—there is no caller-supplied image URI.

The GitHub OIDC provider and deploy role are owned by `WorkRetrievalPlatform`, so a trusted
operator must perform the first CDK bootstrap/data-image/platform cycle with local profile
`competition`. The repository OIDC customization must then be set and read back with
`use_immutable_subject=true`; its immutable prefix is
`repo:TakalaWang@50894789/1111-work-retrieval@1318865130`. The workflow deliberately has no
repository-administration token, so this is a bootstrap prerequisite rather than a workflow
mutation. After that, set `AWS_DEPLOY_ROLE_ARN` from `GitHubDeployRoleArn` and restrict the GitHub
`production` environment to `main`. Enable required reviewers when the repository billing plan
supports them; the current private-repository plan rejects that rule, so deployment remains gated
by the main-only environment, `DEPLOY_ENABLED=true`, and the exact `DEPLOY` confirmation input.
Workflow definitions and passing CI are not deployment evidence; use the emitted stack outputs and
smoke results to establish live state.

See [CONTRIBUTING.md](CONTRIBUTING.md) and the authoritative platform specification in
[`.kiro/specs/job-search-api-platform`](.kiro/specs/job-search-api-platform).
