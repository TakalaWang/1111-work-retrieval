# 1111-work-retrieval

Production platform scaffold for 1111 job retrieval. This repository defines the stable
interfaces, HTTP contract, web shell, PostgreSQL job schema, and AWS infrastructure
needed for teams to work in parallel.

The repository does **not** contain a production search implementation. Its persistent data
plane and complete job snapshot are deployed, but the application plane and API are not. The API
can only start when a `SearchEngine` factory is supplied explicitly.

## Boundaries

- PostgreSQL/Aurora is the only relational database. SQLite is not supported.
- SQLAlchemy owns PostgreSQL domain models, Alembic owns migrations, and Pydantic owns the HTTP
  contract. These layers do not share model classes.
- SQLAlchemy defines the authoritative `Job` model, and Alembic revisions `0001_baseline` and
  `0002_create_jobs` create its PostgreSQL `jobs` table. No runtime engine/session factory exists.
- The complete verified job snapshot is stored in Aurora. A job-detail API does not exist.
- Search responses contain at most ten unique, consecutively ranked ASCII-decimal job IDs. The API
  and browser both reject malformed engine or response data instead of degrading silently.
- Runtime models, embeddings, and indexes live in private S3 objects under
  `runtime/<manifest-sha256>/...`; they are not committed to Git.
- There is no in-memory retriever, experimental ranking path, mock runtime, or automatic
  fallback.
- The GPU ECS service is provisioned with desired capacity `0` until a production image and
  immutable artifact manifest are approved.

## Repository layout

| Path                   | Responsibility                                                                      |
| ---------------------- | ----------------------------------------------------------------------------------- |
| `apps/api`             | FastAPI request validation, error envelopes, health endpoints, and OpenAPI          |
| `apps/web`             | Thin SvelteKit search UI                                                            |
| `packages/search-core` | Immutable search types and the `SearchEngine` protocol                              |
| `packages/database`    | SQLAlchemy declarative base and authoritative `Job` model                           |
| `packages/contract`    | Committed OpenAPI, generated TypeScript types, and artifact manifest schema         |
| `database`             | Alembic configuration, baseline, and `jobs` table migration                         |
| `infra`                | AWS CDK stack for Aurora, S3, ECR, GPU ECS, ALB, CloudFront, WAF, and observability |

## Prerequisites

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Node.js 24 and pnpm 10.28.0
- PostgreSQL 16 for migration verification

## Local checks

Copy `.env.example` to `.env` and export it in your shell when running Alembic against the
standard local PostgreSQL database. The file contains no AWS credentials or production
configuration.

```bash
uv sync --frozen --all-packages
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run python -m work_retrieval_api.export_openapi packages/contract/openapi.json --check

pnpm install --frozen-lockfile
pnpm format:check
pnpm lint
pnpm --dir packages/contract generate:check
pnpm --dir apps/web check
pnpm --dir apps/web test
pnpm --dir apps/web build
pnpm --dir infra test
pnpm --dir infra build
pnpm --dir infra synth
```

Run migrations twice and verify SQLAlchemy/Alembic drift against an explicit PostgreSQL database:

```bash
export DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/work_retrieval
uv run alembic -c database/alembic.ini upgrade head
uv run alembic -c database/alembic.ini upgrade head
uv run alembic -c database/alembic.ini check
```

To intentionally update the committed API contract after changing the FastAPI schema:

```bash
uv run python -m work_retrieval_api.export_openapi packages/contract/openapi.json
pnpm --dir packages/contract generate
```

Commit both the OpenAPI document and generated TypeScript types. CI rejects drift.

## Production data status

The minimum data plane is deployed in competition account `378849533305`, region `us-west-2`,
as CloudFormation stack `WorkRetrievalData`. It contains Aurora PostgreSQL 16.13 Serverless v2
and a private, encrypted, versioned S3 bucket; it contains no ALB, CloudFront, WAF, ECS, ECR, or
GPU capacity.

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

## Application deployment status

Deployment is manual-only. The workflow requires the `production` GitHub environment,
`DEPLOY_ENABLED=true`, an exact confirmation input, a prebuilt API image URI, and a SHA-256
artifact manifest identifier. Configure required reviewers on the `production` environment
and restrict that environment to `main` before enabling it. An operator must bootstrap the AWS
account's GitHub OIDC provider and the first application stack deployment; that stack then owns the
repository's deploy role. Later workflow runs deploy the application stack, publish the static web
build to its private S3 bucket, and invalidate CloudFront. No push to `main` deploys this
repository.

See [CONTRIBUTING.md](CONTRIBUTING.md) and the authoritative platform specification in
[`.kiro/specs/job-search-api-platform`](.kiro/specs/job-search-api-platform).
