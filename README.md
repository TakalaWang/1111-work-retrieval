# 1111 Work Retrieval

1111 職缺搜尋平台 repository：
[github.com/TakalaWang/1111-work-retrieval](https://github.com/TakalaWang/1111-work-retrieval)

## 現況

- `WorkRetrievalData` 與 `WorkRetrievalPlatform` 均已在 AWS competition account
  `378849533305`、`us-west-2` 完成部署（CloudFormation `CREATE_COMPLETE`）。
- Web 與 API 目前可由 [https://dukvebbbaov1r.cloudfront.net](https://dukvebbbaov1r.cloudfront.net)
  同源存取。
- Qwen3 Embedding endpoint `qwen3-embedding-8b-20260801-031826` 與 reranker endpoint
  `work-retrieval-qwen3-reranker-8b` 均為 `InService`。
- 現行搜尋仍是明確標示的暫時實作：每個合法 query 固定回傳 Aurora 中前十個真實 job ID；這不是
  production retrieval algorithm，也不是 retrieval benchmark。職缺詳情 API 尚未實作。
- Embedding／reranker endpoint 已上線不代表它們已整合成正式 `SearchEngine`，也不代表任何 retrieval
  品質指標已發布。

以上是 2026-08-01 的部署 readback。進行操作或宣稱目前線上狀態前，仍應重新讀取 AWS stack、endpoint、
Git commit 與 public smoke 結果；各層狀態必須分別確認。

## 原始碼與文件

| 路徑                                           | 內容                                                 |
| ---------------------------------------------- | ---------------------------------------------------- |
| [`apps/api`](apps/api)                         | FastAPI request validation、lifecycle 與 OpenAPI     |
| [`apps/web`](apps/web)                         | SvelteKit 搜尋介面                                   |
| [`packages/search-core`](packages/search-core) | `SearchEngine` contract 與 search types              |
| [`packages/database`](packages/database)       | SQLAlchemy `Job` model 與 PostgreSQL read repository |
| [`packages/contract`](packages/contract)       | OpenAPI、TypeScript types 與 runtime manifest schema |
| [`database`](database)                         | PostgreSQL Alembic migrations                        |
| [`infra`](infra)                               | AWS CDK infrastructure                               |
| [`scripts`](scripts)                           | 職缺資料驗證、AWS importer 與 artifact promotion     |
| [`docs/architecture.md`](docs/architecture.md) | 系統架構與資料流程                                   |
| [`docs/benchmark.md`](docs/benchmark.md)       | Benchmark 重現範圍與版本證據要求                     |

PostgreSQL／Aurora 是唯一 relational database；不支援 SQLite。SQLAlchemy、Alembic 與 Pydantic
分別負責 persistence model、schema migration 與 HTTP contract。Runtime model、embedding 與大型 index
放在私有 S3 immutable prefix `runtime/<manifest-sha256>/...`，不提交到 Git，也沒有 mock／automatic
fallback runtime。

## 環境設定

- Python `3.12.x` 與 [uv](https://docs.astral.sh/uv/)
- Node.js `24.x` 與 pnpm `10.28.0`
- PostgreSQL `16`
- production image 驗證需要 Docker
- AWS operator workflow 需要 AWS CLI v2 與已 bootstrap 的 `competition` profile

```bash
git clone https://github.com/TakalaWang/1111-work-retrieval.git
cd 1111-work-retrieval

uv sync --frozen --all-packages
pnpm install --frozen-lockfile

cp .env.example .env
set -a
source .env
set +a
```

依賴版本由 [`uv.lock`](uv.lock) 與 [`pnpm-lock.yaml`](pnpm-lock.yaml) 鎖定；`.env.example` 只有本機設定名稱，
不含 production secret。

## 本機驗證

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
docker build --platform linux/amd64 --tag work-retrieval-api:local .
```

在明確指定的 PostgreSQL 16 database 上驗證 migration：

```bash
export DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/work_retrieval
uv run alembic -c database/alembic.ini upgrade head
uv run alembic -c database/alembic.ini upgrade head
uv run alembic -c database/alembic.ini check
```

啟動 Web UI：

```bash
pnpm --dir apps/web dev
```

變更 FastAPI schema 後，重新產生並提交 API contract consumers：

```bash
uv run python -m work_retrieval_api.export_openapi packages/contract/openapi.json
pnpm --dir packages/contract generate
git diff -- packages/contract/openapi.json packages/contract/types.d.ts
```

## Runtime contract

Container entrypoint 是 `work_retrieval_api.main:app`。啟動時必須提供以下五個 database settings；
PostgreSQL 無法連線或可用職缺少於十筆時會 fail closed：

```text
DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
```

公開路徑：

- `POST /api/v1/jobs/search`：暫時的 deterministic 十筆結果。
- `GET /healthz` 與 `GET /readyz`：process 與 initialized-runtime health。

Aurora credentials 由 ECS 經 Secrets Manager 注入，不保存於 image、Git 或 workflow。

## 資料與 runtime artifacts

| 項目                      | 已驗證版本                                                                                                                                                                               |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Source code               | 每次交付以 Git commit SHA 固定                                                                                                                                                           |
| Job dataset               | 1,218,635 rows；SHA-256 `53937f7bf076789c4cd7e3be34fb89875336108d57707b5a93182181e1087089`                                                                                               |
| Database schema           | Alembic `0002_create_jobs`；Aurora PostgreSQL 16                                                                                                                                         |
| Runtime manifest contract | [`runtime-manifest.schema.json`](packages/contract/runtime-manifest.schema.json)，repository schema version `2`；promotion tooling 已完成，正式 release 仍需明確 spec 與人工 `--execute` |
| Embedding endpoint        | `qwen3-embedding-8b-20260801-031826`；`InService`                                                                                                                                        |
| Reranker endpoint         | `work-retrieval-qwen3-reranker-8b`；`InService`                                                                                                                                          |
| Production retrieval      | 尚未整合或發布                                                                                                                                                                           |

Runtime v2 promotion 只接受一份已固定 source manifest SHA、selected inventory SHA、component
manifest SHA 與 challenger promotion evidence 的 release spec。Dry-run 會執行完整 contract 與 component
manifest 驗證，但不寫入 AWS：

先將 pinned EVA build artifacts 轉成 core 會直接解析的 runtime layout；這一步會合併 global
`job-ids.json`、建立 taxonomy、保留原始 `a02a…` build manifest provenance，並產生 source manifest
與 release spec。預設 hardlink，跨檔案系統時須明確指定 `--link-mode copy`：

```bash
uv run python scripts/materialize_runtime_components.py \
  --whole-build-root artifacts/experiments/qwen3-8b/full \
  --tantivy-build-root artifacts/experiments/tantivy-bm25-temporal-v1 \
  --city-taxonomy-csv dataset/城市對照表.csv \
  --duty-taxonomy-csv dataset/職務對照表.csv \
  --output-root artifacts/runtime-source \
  --source-manifest-key one111-search/runtime-source/<immutable-build-id>/manifest.json
```

```bash
uv run python scripts/promote_runtime_artifacts.py \
  --release-spec artifacts/runtime-source/runtime-release-spec.json
```

如要以本機 fixture／下載後的 immutable bundle 離線驗證，可額外指定：

```bash
uv run python scripts/promote_runtime_artifacts.py \
  --release-spec artifacts/runtime-source/runtime-release-spec.json \
  --source-manifest-file artifacts/runtime-source/manifest.json \
  --source-root artifacts/runtime-source
```

只有 dry-run 完整通過後，才使用已登入的 `competition` profile 在 `us-west-2` 明確發布：

```bash
uv run python scripts/promote_runtime_artifacts.py \
  --release-spec artifacts/runtime-source/runtime-release-spec.json \
  --execute
```

發布順序固定為逐物件 checksum copy/readback、完整且分頁的 data-only inventory audit、`manifest.json`
最後寫入，再對 manifest body 與完整 prefix readback。component manifests 使用 serving parser 的 exact-key
路徑 contract；root inventory 以 path、SHA-256、size 完整固定 whole-Qwen layout/job IDs/shards、Tantivy
taxonomy/index files 與任何啟用 challenger，且不得含 query history、GT/qrels/judgments、test JD、raw logs
或 secrets。任何 incomplete、
`publication_allowed=false`、未通過正向 NDCG@10 promotion evidence、非 temporal
Tantivy、Graph cutoff 越界或 object inventory drift 都會 fail closed；不會自動發布部分 release。

職缺 snapshot 的 authoritative S3 object：

```text
s3://workretrievaldata-runtimebucket404c5ee4-hkvrjx5fbkij/data/jobs/53937f7bf076789c4cd7e3be34fb89875336108d57707b5a93182181e1087089/jobs.csv
```

AWS readback 已驗證 1,218,635 rows、1,218,635 distinct job IDs、source rows `0..1218634`，以及
`alembic_version`、`jobs` 兩張 public tables。固定資料匯入命令為：

```bash
AWS_PROFILE=competition AWS_DEFAULT_REGION=us-west-2 \
  uv run python scripts/import_jobs_to_aws.py \
  "/Users/takala/code/1111 work retrieval/dataset/職缺.csv"
```

Runtime artifact promotion 預設只做 dry-run：

```bash
uv run python scripts/promote_runtime_artifacts.py
uv run python scripts/promote_runtime_artifacts.py --execute
```

Importer 與 promotion script 都固定 account、region、來源 identity 與完整性檢查；不要繞過其驗證。

## Benchmark 重現

目前沒有可發布的 retrieval benchmark，因為正式 `SearchEngine`、versioned evaluation queries／qrels 與
單一 committed benchmark runner 尚未齊備。Repository tests、migration checks、contract checks、CDK
synth 與 endpoint smoke 都是 acceptance evidence，不是 Recall、MRR、nDCG 或 latency benchmark。

可重現的 acceptance commands 與未來 benchmark 所需的 artifact、provenance、metrics 見
[`docs/benchmark.md`](docs/benchmark.md)。

## 手動 production deployment

`.github/workflows/deploy.yml` 僅支援 `workflow_dispatch`，且只允許由 `main` 經 GitHub `production`
environment 執行。它同時要求：

- repository variable `DEPLOY_ENABLED=true`
- environment variable `AWS_DEPLOY_ROLE_ARN`
- confirmation input 必須精確等於 `DEPLOY`
- 64-character lowercase artifact manifest SHA-256
- CPU desired count 至少為 `1`
- GPU min／max／desired 目前必須全部為 `0`

流程依序執行 frozen installs、static web build、OIDC authentication、DataStack deploy、runtime manifest
驗證、`linux/amd64` API image build／push、ECR scan、digest-pinned PlatformStack deploy、web sync、等待
CloudFront invalidation，最後才執行 public health、readiness、web 與 search smoke。

Workflow 自行 build image，不接受 caller-supplied image URI；CDK 只接收 ECR digest URI。任何 push 或
merge 都不會自動部署。ECR scan、stack deployment、CloudFront publication 與 public smoke 是彼此獨立的
gate，不可用前一項成功代表後一項已完成。

GitHub OIDC 使用 repository-ID-bound immutable subject；不要改回可變動的 owner／repository-name
subject。`production` environment 已限制為 `main`。目前 private-repository billing plan 不支援 required
reviewers，因此現有 gate 是 main-only environment、`DEPLOY_ENABLED=true` 與精確的 `DEPLOY` confirmation；
若方案之後支援，再啟用 required reviewers。

系統元件、request flow、資料匯入與 infrastructure ownership 詳見
[`docs/architecture.md`](docs/architecture.md)，貢獻規則見 [CONTRIBUTING.md](CONTRIBUTING.md)。
