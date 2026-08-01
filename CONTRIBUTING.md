# Contributing

## Set up

```bash
uv sync --frozen --all-packages
pnpm install --frozen-lockfile
cp .env.example .env
set -a
source .env
set +a
```

Use Python 3.12, Node.js 24, and the committed lockfiles. Do not add a dependency when the
standard library, platform, or an existing dependency already covers the requirement.

## Architecture rules

- Keep HTTP concerns in `apps/api` and retrieval contracts in `packages/search-core`.
- Inject `SearchEngine` explicitly. Runtime fallback engines and production test doubles are
  forbidden.
- Use PostgreSQL/Aurora only. Do not introduce SQLite code, files, migrations, or CI paths.
- Define PostgreSQL domain models with SQLAlchemy in `packages/database`; manage schema changes
  with Alembic and HTTP contracts with Pydantic. Do not reuse one layer's classes in another.
- Do not add a runtime SQLAlchemy engine or session factory until a domain access path requires
  one.
- Keep models, embeddings, and large indexes in the versioned S3 runtime prefix, never in Git or
  PostgreSQL.
- Keep experiments, ablations, evaluators, and unfinished ranking implementations outside this
  production repository.
- Do not add legacy request aliases (`ks`, `c0`, `d0`, or `empStr`) or compatibility shims.

## Before opening a pull request

```bash
uv run ruff check .
uv run mypy
uv run pytest
uv run python -m work_retrieval_api.export_openapi packages/contract/openapi.json --check
pnpm lint
pnpm --dir packages/contract generate:check
pnpm --dir apps/web check
pnpm --dir apps/web test
pnpm --dir apps/web build
pnpm --dir infra test
pnpm --dir infra build
pnpm --dir infra synth
```

When an API schema changes, regenerate and commit its consumers:

```bash
uv run python -m work_retrieval_api.export_openapi packages/contract/openapi.json
pnpm --dir packages/contract generate
git diff -- packages/contract/openapi.json packages/contract/types.d.ts
```

Schema changes require a new Alembic revision. Verify it on PostgreSQL 16 by upgrading from a
fresh database; never edit an applied migration.

Production job imports must use `scripts/import_jobs_to_aws.py` unchanged with the `competition`
profile in account `378849533305` and `us-west-2`. The script rejects any other account, region,
source checksum, header, or row count. Do not use Data API row-by-row inserts or bypass its staging
validation and atomic replacement.

## Pull requests

Keep each pull request within one responsibility boundary when possible. State the contract or
behavior changed, the commands run, and whether AWS resources or runtime artifacts are affected.
Passing CI is not evidence that anything was deployed.
