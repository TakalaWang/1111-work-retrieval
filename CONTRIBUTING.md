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
- Inject `SearchEngine` explicitly. Production has no DB-seed, single-lane, mock, or legacy
  fallback.
- Keep search results to at most ten unique, consecutively ranked ASCII-decimal job IDs. Preserve
  fail-closed validation at both the API and browser boundaries.
- Use PostgreSQL/Aurora only. Do not introduce SQLite code, files, migrations, or CI paths.
- Define PostgreSQL domain models with SQLAlchemy in `packages/database`; manage schema changes
  with Alembic and HTTP contracts with Pydantic. Do not reuse one layer's classes in another.
- Keep database engine/session ownership inside `SqlAlchemyJobReader`; use `NullPool`, bounded
  statements, and batch metadata reads for candidate revalidation.
- Keep models, embeddings, and large indexes in the versioned S3 runtime prefix, never in Git or
  PostgreSQL.
- Keep only reproducible, promotion-gated pipeline entrypoints in this repository; unfinished
  ranking implementations must not enter the serving path.
- Do not add legacy request aliases (`ks`, `c0`, `d0`, or `empStr`) or compatibility shims.

## Before opening a pull request

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run python -m work_retrieval_api.export_openapi packages/contract/openapi.json --check

pnpm format:check
pnpm lint
pnpm --dir packages/contract generate:check
pnpm --dir apps/web check
pnpm --dir apps/web test
pnpm --dir apps/web build
pnpm --dir infra test
pnpm --dir infra build
pnpm --dir infra synth
docker build --platform linux/amd64 --tag work-retrieval-api:verify .
```

When an API schema changes, regenerate and commit its consumers:

```bash
uv run python -m work_retrieval_api.export_openapi packages/contract/openapi.json
pnpm --dir packages/contract generate
git diff -- packages/contract/openapi.json packages/contract/types.d.ts
```

Schema changes require a new Alembic revision. Verify it on PostgreSQL 16 by upgrading a fresh
database twice and running `alembic check`; never edit an applied migration.

Production job imports must use `scripts/import_jobs_to_aws.py` unchanged with the `competition`
profile in account `378849533305` and `us-west-2`. The script rejects any other account, region,
source checksum, header, or row count. Do not use Data API row-by-row inserts or bypass its staging
validation and atomic replacement.

Runtime artifact promotion must use `scripts/promote_runtime_artifacts.py`. Review its dry-run
manifest SHA before using `--execute`; never broaden its source allowlist to query history,
behavior data, rerankers, SQLite, or unrelated experiments.

## Documentation evidence

- Keep `README.md` as the handoff entry point, `docs/architecture.md` as the canonical system and
  data-flow description, and `docs/benchmark.md` as the benchmark evidence contract. Update them
  together when module ownership, runtime flow, data identity, deployment status, or executable
  benchmark capability changes.
- Keep deployed infrastructure, public reachability, endpoint availability, retrieval integration,
  and benchmark results as separate claims. Read back the relevant AWS or public state before
  updating a live-status statement.
- Acceptance tests and endpoint smoke checks must never be labeled as retrieval quality or latency
  benchmarks.
- Benchmark results require the exact Git commit, dependency locks, corpus checksum, evaluation-set
  checksum, runtime manifest and artifact checksums, retrieval configuration, command, and hardware.
- Use `not implemented`, `not published`, or `not deployed` when evidence is absent. Do not infer
  runtime behavior from contracts, CDK synthesis, comments, historical plans, or a different branch.

## Deployment changes

- Keep production deployment under `workflow_dispatch`; never add a push-triggered deploy.
- Preserve the `main` ref check, `production` environment, `DEPLOY_ENABLED=true`, exact `DEPLOY`
  confirmation, fixed account `378849533305`, and region `us-west-2`.
- Preserve GitHub's immutable OIDC subject customization and repository-ID-bound production
  environment subject; do not replace it with a mutable owner/repository-name subject.
- The workflow owns the `linux/amd64` image build and push. CDK must receive an ECR digest URI,
  never a mutable tag or caller-provided image URI.
- DataStack deploys first and owns the retained ECR repository. PlatformStack receives the immutable
  image, artifact SHA, CPU desired count, GPU type, and explicit GPU `0/0/0` capacities.
- Preserve the runtime-manifest existence and checksum check, ECR scan gate requiring zero critical
  or high findings, CloudFront invalidation wait, and public health/readiness/web/search smoke.
- ECR scan completion, stack deployment, web publication, endpoint availability, retrieval
  integration, and public smoke are separate evidence boundaries.
- Never commit AWS credentials, database passwords, generated CDK outputs, or local `.env` files.

## Pull requests

Keep each pull request within one responsibility boundary when possible. State the contract or
behavior changed, the commands run, and whether AWS resources or runtime artifacts are affected.
Passing CI is not evidence that anything was deployed, and a successful production deployment does
not prove that its changes have been merged into `main`.
