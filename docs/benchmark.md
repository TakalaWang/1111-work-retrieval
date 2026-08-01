# Benchmark Reproduction

## Current status

本 repository **目前無法重現 retrieval benchmark**，也沒有可發布的 retrieval 指標。原因是以下四個
必要 inputs 尚未同時存在：

| Required input                       | Current state |
| ------------------------------------ | ------------- |
| Production `SearchEngine`            | 未實作        |
| Versioned evaluation queries / qrels | 未提供        |
| Immutable model artifacts            | 未發布        |
| Immutable index / embeddings         | 未發布        |

因此目前沒有官方 benchmark command。Repository tests、migration checks、contract checks 與 CDK synth
是 acceptance evidence，不是 Recall、MRR、nDCG 或 latency benchmark。

## What can be reproduced now

### 1. Pin source and dependencies

```bash
git clone https://github.com/TakalaWang/1111-work-retrieval.git
cd 1111-work-retrieval
git checkout <commit-sha-under-review>
git rev-parse HEAD

uv sync --frozen --all-packages
pnpm install --frozen-lockfile
```

保留 `git rev-parse HEAD` 的輸出；Python 與 Node dependency graphs 分別由 `uv.lock` 與
`pnpm-lock.yaml` 固定。

### 2. Reproduce repository acceptance

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
```

### 3. Reproduce PostgreSQL schema validation

使用 PostgreSQL 16，建立 `work_retrieval` database，並設定：

```bash
export DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/work_retrieval
uv run alembic -c database/alembic.ini upgrade head
uv run alembic -c database/alembic.ini upgrade head
uv run alembic -c database/alembic.ini check
```

預期 revision 為 `0002_create_jobs`。完整資料快照的已驗證版本為：

```text
rows: 1218635
distinct_job_ids: 1218635
source_row: 0..1218634
bytes: 1285945103
sha256: 53937f7bf076789c4cd7e3be34fb89875336108d57707b5a93182181e1087089
```

這證明資料與 schema identity；它不測量搜尋品質。

## Requirements for a reproducible retrieval benchmark

未來新增 benchmark 時，一次結果必須固定並輸出以下 provenance：

| Dimension        | Required evidence                                                            |
| ---------------- | ---------------------------------------------------------------------------- |
| Code             | Git commit SHA 與 clean worktree                                             |
| Dependencies     | `uv.lock`、`pnpm-lock.yaml` checksum                                         |
| Corpus           | dataset URI、row count、bytes、SHA-256、schema revision                      |
| Evaluation set   | query/qrels URI、format version、row count、SHA-256                          |
| Runtime          | manifest schema version、manifest SHA-256、每個 model/index artifact SHA-256 |
| Retrieval config | normalization、filters、candidate count、ranking parameters                  |
| Execution        | single committed command、seed、warm-up、repetitions                         |
| Environment      | CPU/GPU model、RAM/VRAM、OS、driver 與 relevant runtime versions             |

至少報告：

- `Recall@10`
- `MRR@10`
- `nDCG@10`
- p50 / p95 latency
- failed-query count 與 total-query count

結果應輸出 machine-readable JSON，包含上述 provenance、metric definitions 與 raw aggregate counts。
在 engine、evaluation set、runtime manifest 與單一 committed runner 都加入前，不新增手寫 benchmark 數字或
推測性的重現命令。
