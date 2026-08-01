# Contributing

## Set up

```bash
uv sync --frozen --all-packages
pnpm install --frozen-lockfile
```

Use Python 3.12, Node.js 24, and the committed lockfiles. Do not add a dependency when the
standard library, platform, or an existing dependency already covers the requirement.

## Architecture rules

- Keep HTTP concerns in `apps/api` and retrieval contracts in `packages/search-core`.
- Inject `SearchEngine` explicitly. Runtime fallback engines and production test doubles are
  forbidden.
- Use PostgreSQL/Aurora only. Do not introduce SQLite code, files, migrations, or CI paths.
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

## Pull requests

Keep each pull request within one responsibility boundary when possible. State the contract or
behavior changed, the commands run, and whether AWS resources or runtime artifacts are affected.
Passing CI is not evidence that anything was deployed.
