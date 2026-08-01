# Benchmark Reproduction

## Current status

本 branch 已實作 production `SearchEngine` 與 reference adapters，但 **不能只靠此 branch 發布 retrieval
分數**。以下 inputs 必須在同一份 immutable release 中同時存在：

| Required input                       | Current state                                             |
| ------------------------------------ | --------------------------------------------------------- |
| Production `SearchEngine`            | 已實作                                                    |
| Versioned evaluation queries / qrels | 未提供                                                    |
| Immutable model artifacts            | 由 artifact promotion branch 管理，尚未在此文件宣稱已發布 |
| Immutable index / embeddings         | 由 artifact promotion branch 管理，尚未在此文件宣稱已發布 |

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

競賽主指標至少報告：

- `NDCG@10`
- `Precision@10`
- `Top-1`
- `MRR`
- p50 / p95 latency
- failed-query count 與 total-query count

Graph 的擴張性分析可額外報告 `Recall@100`、`Precision@100`、`Recall@1000` 與 `Precision@1000`，但不能
用這些 diagnostic metrics 取代競賽 Top-10 主指標。正式報告必須以同一 time split、candidate budget 與
seed 提供 `graph_on` / `graph_off` 雙重 ablation；Graph 若沒有正向且可重現的主指標差異，就維持 production
off，而不是用架構敘事掩蓋負增益。

結果應輸出 machine-readable JSON，包含上述 provenance、metric definitions 與 raw aggregate counts。
在 engine、evaluation set、runtime manifest 與單一 committed runner 都加入前，不新增手寫 benchmark 數字或
推測性的重現命令。
