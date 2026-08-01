# 1111-work-retrieval

Production platform scaffold for 1111 job retrieval. This repository defines the stable
interfaces, read-only HTTP contract, web shell, PostgreSQL job store, and AWS infrastructure
needed for teams to work in parallel.

The repository does **not** contain a production search implementation. The API can only start
when `SearchEngine` and job-repository factories are supplied explicitly; source code and CI do
not prove that the platform is deployed.

## Boundaries

- PostgreSQL/Aurora is the only relational database. SQLite is not supported.
- SQLAlchemy owns the PostgreSQL `jobs` model and pooled read repository, Alembic owns migrations,
  and Pydantic owns the HTTP contract. These layers do not share transport model classes.
- The public API is read-only. Snapshot ingestion is an operator workflow and never runs from an
  HTTP request.
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
| `packages/database`    | SQLAlchemy job model and pooled PostgreSQL read repository                          |
| `packages/contract`    | Committed OpenAPI, generated TypeScript types, and artifact manifest schema         |
| `database`             | Alembic configuration and PostgreSQL job-table migrations                           |
| `infra`                | AWS CDK stack for Aurora, S3, ECR, GPU ECS, ALB, CloudFront, WAF, and observability |

## Prerequisites

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Node.js 24 and pnpm 10.28.0
- PostgreSQL 16 for migration verification

## Local checks

```bash
uv sync --frozen --all-packages
uv run ruff check .
uv run ruff format --check .
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
```

Run the migrations against an explicit PostgreSQL database:

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

## Job data access

`GET /api/v1/job-details/{job_id}` reads a previously persisted record. The API has no import or
upsert route. A controlled snapshot-ingestion workflow must validate and load `jobs` separately.

`PostgresJobRepository.from_environment` requires `DATABASE_HOST`, `DATABASE_PORT`,
`DATABASE_NAME`, `DATABASE_USER`, and `DATABASE_PASSWORD`; CDK injects credentials through ECS
Secrets Manager. Supply that classmethod together with the required `SearchEngine` factory to
`create_app`.

## Deployment status

Deployment automation is manual-only. The workflow requires the `production` GitHub environment,
`DEPLOY_ENABLED=true`, an exact confirmation input, a prebuilt API image URI, and a SHA-256
artifact manifest identifier. Configure required reviewers on the `production` environment
and restrict that environment to `main` before enabling it. An operator must bootstrap the AWS
account's GitHub OIDC provider and the first stack deployment; the stack then owns the repository's
deploy role. Later workflow runs deploy the stack, publish the static web build to its private S3
bucket, and invalidate CloudFront. No push to `main` deploys this repository.

See [CONTRIBUTING.md](CONTRIBUTING.md) and the authoritative platform specification in
[`.kiro/specs/job-search-api-platform`](.kiro/specs/job-search-api-platform).
