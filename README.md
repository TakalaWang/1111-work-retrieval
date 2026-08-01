# 1111 Work Retrieval

1111 職缺搜尋平台 repository：
[github.com/TakalaWang/1111-work-retrieval](https://github.com/TakalaWang/1111-work-retrieval)

## 原始碼與文件

| 路徑                                           | 內容                                                 |
| ---------------------------------------------- | ---------------------------------------------------- |
| [`apps/api`](apps/api)                         | FastAPI request validation、lifecycle 與 OpenAPI     |
| [`apps/web`](apps/web)                         | SvelteKit 搜尋介面                                   |
| [`packages/search-core`](packages/search-core) | `SearchEngine` contract 與 search types              |
| [`packages/database`](packages/database)       | SQLAlchemy `Job` model                               |
| [`packages/contract`](packages/contract)       | OpenAPI、TypeScript types 與 runtime manifest schema |
| [`database`](database)                         | PostgreSQL Alembic migrations                        |
| [`infra`](infra)                               | AWS CDK infrastructure                               |
| [`scripts`](scripts)                           | 職缺資料驗證與 AWS importer                          |
| [`docs/architecture.md`](docs/architecture.md) | 系統架構與資料流程                                   |
| [`docs/benchmark.md`](docs/benchmark.md)       | Benchmark 重現範圍與版本證據要求                     |

## 環境設定

- Python `3.12.x` 與 [uv](https://docs.astral.sh/uv/)
- Node.js `24.x` 與 pnpm `10.28.0`
- PostgreSQL `16`

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

依賴版本由 [`uv.lock`](uv.lock) 與 [`pnpm-lock.yaml`](pnpm-lock.yaml) 鎖定。

## 執行範例

啟動 Web UI：

```bash
pnpm --dir apps/web dev
```

驗證 API contract：

```bash
uv run python -m work_retrieval_api.export_openapi packages/contract/openapi.json --check
pnpm --dir packages/contract generate:check
```

驗證 PostgreSQL migration：

```bash
uv run alembic -c database/alembic.ini upgrade head
uv run alembic -c database/alembic.ini upgrade head
uv run alembic -c database/alembic.ini check
```

目前 repository 尚未提供 production `SearchEngine`，因此 API 沒有可直接啟動的 production
搜尋範例。

## Benchmark 重現

目前尚未提供 retrieval benchmark，因為 production `SearchEngine`、evaluation set、模型與索引尚未
發布。現階段可重現 repository acceptance：

```bash
git checkout <commit-sha>
uv sync --frozen --all-packages
pnpm install --frozen-lockfile

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
```

Retrieval benchmark 的 artifact、provenance 與指標要求見
[`docs/benchmark.md`](docs/benchmark.md)。

## 資料、模型與索引版本

| 項目                      | 版本                                                                                                 |
| ------------------------- | ---------------------------------------------------------------------------------------------------- |
| Source code               | Git commit SHA                                                                                       |
| Job dataset               | 1,218,635 rows；SHA-256 `53937f7bf076789c4cd7e3be34fb89875336108d57707b5a93182181e1087089`           |
| Database schema           | Alembic `0002_create_jobs`；PostgreSQL 16                                                            |
| Runtime manifest format   | [`runtime-manifest.schema.json`](packages/contract/runtime-manifest.schema.json)，schema version `1` |
| Retrieval model           | 尚未發布                                                                                             |
| Search index / embeddings | 尚未發布                                                                                             |

系統元件、request flow、資料匯入與 infrastructure flow 詳見
[`docs/architecture.md`](docs/architecture.md)。
