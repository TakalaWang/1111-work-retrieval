# 1111 Work Retrieval

1111 職缺搜尋平台 repository：
[github.com/TakalaWang/1111-work-retrieval](https://github.com/TakalaWang/1111-work-retrieval)

## 現況

- `WorkRetrievalData` 與 `WorkRetrievalPlatform` 均已在 AWS competition account
  `378849533305`、`us-west-2` 完成部署；本次 `WorkRetrievalPlatform` readback 為
  CloudFormation `UPDATE_COMPLETE`。
- Web 與 API 目前可由 [https://1111.takalawang.dev](https://1111.takalawang.dev)
  同源存取。
- Qwen3 Embedding endpoint `qwen3-embedding-8b-20260801-031826` 與 reranker endpoint
  `work-retrieval-qwen3-reranker-8b` 均為 `InService`。
- `main` commit `6d2bd0e8aaace42ed043f673ad4efd66587131bd` 已由 production workflow
  [run 30718253906](https://github.com/TakalaWang/1111-work-retrieval/actions/runs/30718253906) 成功部署；runtime
  manifest 為 `964ae7e235bfdf90f639a216991757f905554ce35b83f4069aa68cb2d8d2ddbf`，ECS 使用的
  digest-pinned image 為 `sha256:6fa7c4814e1abee26da888868cd1f064828c48bae6f49d3a1617395069f1392b`。
- 線上 compute profile 為 `cpu-incumbent`：CPU Fargate service 的 desired／running／pending 為
  `1/1/0`，GPU ASG min／max／desired 與 GPU service desired／running／pending 均為 `0/0/0`。
- ECR scan 已完成且 Critical／High 均為 `0`；public health、精確 manifest readiness、兩組不同 query
  ranking、job detail 與 Web UI smoke 均通過。
- Embedding／reranker endpoint 已上線不代表對應 challenger 已通過 promotion，也不代表任何 retrieval
  品質指標已發布；正式 runtime 仍依 manifest 開關與 live readback 判定。

以上是 2026-08-02 的部署 readback。未來進行操作或再次宣稱線上狀態前，仍應重新讀取 AWS stack、ECS
service、image digest、runtime manifest、Git commit 與 public smoke 結果；各層狀態必須分別確認。

## 原始碼與文件

| 路徑                                                         | 內容                                                 |
| ------------------------------------------------------------ | ---------------------------------------------------- |
| [`apps/api`](apps/api)                                       | FastAPI request validation、lifecycle 與 OpenAPI     |
| [`apps/web`](apps/web)                                       | SvelteKit 搜尋介面                                   |
| [`packages/search-core`](packages/search-core)               | Tantivy、Qwen、artifact bootstrap、fusion 與 audit   |
| [`packages/database`](packages/database)                     | SQLAlchemy `Job` model 與 PostgreSQL read repository |
| [`packages/contract`](packages/contract)                     | OpenAPI、TypeScript types 與 runtime manifest schema |
| [`database`](database)                                       | PostgreSQL Alembic migrations                        |
| [`infra`](infra)                                             | AWS CDK infrastructure                               |
| [`scripts`](scripts)                                         | 職缺資料驗證、檢索 artifact 建置／消融與 promotion   |
| [`docs/architecture.md`](docs/architecture.md)               | 系統架構與資料流程                                   |
| [`docs/benchmark.md`](docs/benchmark.md)                     | Benchmark 重現範圍與版本證據要求                     |
| [`docs/retrieval-pipelines.md`](docs/retrieval-pipelines.md) | Graph 與 multi-view embedding 重現、驗證及 AWS 契約  |

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

## 從競賽 ZIP 一鍵部署

把主辦方下載的 ZIP 放在 repository 的 `inputs/`（此目錄內的 ZIP 已由 Git 忽略），例如：

```bash
mkdir -p inputs
cp ~/Downloads/1111-competition-data.zip inputs/competition.zip
```

這個命令以競賽既有的 `WorkRetrievalData` AWS stack 與 Alembic `0002_create_jobs` 為前提；它不是空 AWS
account 的 infrastructure bootstrap。先確認目前是已拉到最新的 `main`、worktree 無修改，且
`competition` AWS profile 與 `gh auth status` 都已登入。接著執行唯一的 production release bootstrap：

```bash
scripts/bootstrap_competition_release.sh \
  inputs/competition.zip \
  artifacts/bootstrap-$(date +%Y%m%d-%H%M%S) \
  artifacts/evaluations/temporal-v3-fixed-339.attestation.json \
  <externally-approved-attestation-sha256> \
  DEPLOY \
  your-alert-email@example.com
```

最後一個 email 可省略；若有填寫，AWS SNS 會寄出 subscription confirmation，必須點擊信中的確認連結後，
5xx 與 unhealthy-host alarms 才會寄信。要更換 email，使用新值重新執行 deployment workflow；不要把 email
或 AWS credentials 寫入 `.env`、commit 或 artifact manifest。

Temporal-v3 attestation 必須由固定 339-query 的外部 evaluator 產生，並與獨立核准的 SHA-256
一起傳入。它必須 pin canonical queries、evaluation split、qrels、baseline/candidate TREC
run、evaluated manifest，以及由 dataset/order、compiler/index-builder/revalidation source-file SHA 與索引
policy 組成的 stable fingerprint；NDCG@10 必須正成長，Precision@10、Top-1 與 MRR 不得退步。
Bootstrap 不會自己製造分數、不會自己核准 attestation SHA，缺件、bytes 不符或任一指標
退步都會在寫入 PostgreSQL 與發布 runtime 前 fail closed。該 development evidence 不宣稱是官方分數。
上面的 attestation 不是單獨 JSON；它必須與 verifier 封存的 9 件 evidence 放在同一目錄。
`scripts/verify_temporal_v3_promotion.py create` 會複製固定 inputs/runs/manifests、建立完整
size/SHA inventory，從 qrels 與 run bytes 重算指標，而且只在所有 gate 通過時寫出
`attestation.json`。完整命令與檔案契約見
[`docs/retrieval-pipelines.md`](docs/retrieval-pipelines.md#temporal-v3-fixed-339-promotion-evidence)。

這個命令會依序且 fail-closed 地：

1. 不使用 `extractall`，只從 ZIP 安全取出唯一的 `職缺.csv`、`城市對照表.csv`、`職務對照表.csv`；
2. 驗證 1,218,635 筆固定 snapshot 的 bytes、SHA-256、39 欄 schema 與 taxonomy；
3. 從 content-addressed S3 prefix 下載並逐檔驗證既有 sealed Whole-JD Qwen cache，不重算 embedding；
4. 重建 temporal-v3 Tantivy，並在任何外部寫入前驗證固定 339-query attestation 的核准 bytes、
   stable policy/source lineage、正 NDCG@10 delta 與其餘三個非退步指標。索引包含 location/duty、
   學歷、月薪、明確工作性質、班別、無經驗、管理人數 hard filters 與 180 天時序欄位。官方
   CSV 沒有可見性欄位，因此這個封閉競賽 JD pool 被明確當作 eligible corpus；建置的
   `visibility=1` 是該前提假設，不是來源欄位。真實廠商 feed 必須提供並重驗當下可見性；
5. 將 CSV idempotently 匯入 Aurora PostgreSQL；單一 transaction 先取得固定 advisory lock，取不到即
   fail closed，再完成 staging、replacement 與 source SHA marker；statement trigger 會在任何 DML 後使
   marker 失效，只有 rows／ID／source-row 邊界、marker 與 guard 全部相符才回報 `unchanged`；
6. materialize、dry-run，將 content-addressed source bundle 逐檔 SHA-256 上傳且 manifest 最後寫入，
   再發布並 read-back immutable runtime bundle；
7. 以 GitHub OIDC 觸發 `main` 的 production workflow，等待 image scan、CDK/ECS/CloudFront 與 public smoke
   全部成功才結束。

`NEW_WORK_ROOT` 必須不存在；中途失敗時保留該目錄供稽核，不會偷偷沿用 partial output。這個 production
bootstrap 只發布已核准的 temporal BM25 incumbent；Whole-Dense 與 Graph serving adapter 預設關閉，
multi-view、LTR 與 reranker 目前沒有 production serving adapter。Graph 的建置與 Graph-on/off 實驗是下方
獨立的 offline 流程，不會由這個命令產生；只有含正向 promotion evidence 的 Graph runtime bundle 才能配合
`SEARCH_ENABLE_GRAPH=true` 開進 Top-10。

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

Container entrypoint 是 `work_retrieval_api.main:app`。啟動時必須提供 PostgreSQL 與 immutable S3 runtime；
只有顯式啟用 dense shadow 時才要求 SageMaker query encoder settings。任何啟用中的 artifact、容量、
database 或 endpoint 契約不成立都 fail closed：

```text
DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
ARTIFACT_BUCKET, ARTIFACT_MANIFEST_SHA256, AWS_REGION
SEARCH_RUNTIME_ROOT, SEARCH_RUNTIME_MANIFEST_PATH, SEARCH_PORT_FACTORY
SEARCH_ENABLE_DENSE_SHADOW, SEARCH_ENABLE_MULTIVIEW_MAXSIM
EMBEDDING_ENDPOINT_NAME, EMBEDDING_ENDPOINT_CONFIG_NAME, EMBEDDING_MODEL_NAME
```

公開路徑：

- `POST /api/v1/jobs/search`：Tantivy full-JD BM25 incumbent Top 10；whole-Qwen dense 預設關閉，啟用時僅作
  shadow/tail evidence，不得改排 incumbent Top 10。
- `GET /healthz`：process health；`GET /readyz`：initialized-runtime health 與實際載入的 root manifest SHA-256。

Aurora credentials 由 ECS 經 Secrets Manager 注入，不保存於 image、Git 或 workflow。

BM25 index、whole-JD embedding、LLM extraction、skill Graph、query-correction promotion 與 graph-on/off
ablation 的可重現命令都保留在 [`scripts`](scripts)，完整契約見
[`docs/retrieval-pipelines.md`](docs/retrieval-pipelines.md)。BM25 預設不依賴 LLM；Graph extraction 使用
職務分層的 deterministic 5,000 筆代表樣本（hard cap 10,000），Graph 與 query correction 都必須經
fixed-input NDCG@10 正向驗證才可啟用。

Query hard-filter grammar、JD/query-log coverage、外派與年資不進 production 的理由，以及
`job_information_completeness` 的未上線 promotion gate 見
[`docs/typed-constraint-evidence.md`](docs/typed-constraint-evidence.md)。

## 資料與 runtime artifacts

| 項目                      | 已驗證版本                                                                                                                                                                                        |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Source code               | 每次交付以 Git commit SHA 固定                                                                                                                                                                    |
| Job dataset               | 1,218,635 rows；SHA-256 `53937f7bf076789c4cd7e3be34fb89875336108d57707b5a93182181e1087089`                                                                                                        |
| Database schema           | Alembic `0002_create_jobs`；Aurora PostgreSQL 16                                                                                                                                                  |
| Runtime manifest contract | [`runtime-manifest.schema.json`](packages/contract/runtime-manifest.schema.json)，repository schema version `2`；live manifest `964ae7e235bfdf90f639a216991757f905554ce35b83f4069aa68cb2d8d2ddbf` |
| Embedding endpoint        | `qwen3-embedding-8b-20260801-031826`；`InService`                                                                                                                                                 |
| Reranker endpoint         | `work-retrieval-qwen3-reranker-8b`；`InService`                                                                                                                                                   |
| Production retrieval      | temporal BM25 `cpu-incumbent` 已部署；main `6d2bd0e8aaace42ed043f673ad4efd66587131bd`、run `30718253906`、CPU `1/1/0`、GPU `0/0/0`、public smoke 通過                                             |

Runtime v2 promotion 只接受一份已固定 source manifest SHA、selected inventory SHA、component
manifest SHA 與 challenger promotion evidence 的 release spec。Dry-run 會執行完整 contract 與 component
manifest 驗證，但不寫入 AWS：

正式路徑重用 sealed EVA whole-job cache，不重新呼叫 embedding model。Materializer 驗證 source manifest、
source inventory、122 個 4096d shards 與 global job order，然後只衍生 first-1024 + float32 L2 normalize +
float16 serving shards；source bytes 永不覆寫。Tantivy 必須是已核准的 temporal-v3 build，query correction
預設關閉，只有帶 organizer 正向 NDCG@10 attestation 才可啟用。

一鍵、離線、無 AWS 寫入的 materialize + promotion dry-run：

```bash
scripts/reproduce_runtime_release.sh \
  artifacts/experiments/qwen3-8b/full \
  artifacts/evidence/sealed-whole-source-inventory.json \
  artifacts/experiments/tantivy-bm25-temporal-v3 \
  artifacts/runtime-source \
  <approved-tantivy-component-sha256> \
  <approved-tantivy-build-sha256> \
  <approved-tantivy-index-sha256>
```

`output-root` 必須不存在，避免混入舊 artifact。Wrapper 只產生 immutable local bundle 並執行完整 dry-run；
不會上傳 S3、切換 runtime 或重算 embedding。若只重跑 promotion validation：

```bash
uv run python scripts/promote_runtime_artifacts.py \
  --release-spec artifacts/runtime-source/runtime-release-spec.json \
  --source-manifest-file artifacts/runtime-source/manifest.json \
  --source-root artifacts/runtime-source \
  --approved-tantivy-build-sha256 <approved-tantivy-build-sha256> \
  --approved-tantivy-index-sha256 <approved-tantivy-index-sha256>
```

只有 dry-run 完整通過後，才使用已登入的 `competition` profile 在 `us-west-2` 明確發布：

```bash
uv run python scripts/promote_runtime_artifacts.py \
  --release-spec artifacts/runtime-source/runtime-release-spec.json \
  --source-manifest-file artifacts/runtime-source/manifest.json \
  --source-root artifacts/runtime-source \
  --approved-tantivy-build-sha256 <approved-tantivy-build-sha256> \
  --approved-tantivy-index-sha256 <approved-tantivy-index-sha256> \
  --stage-source \
  --execute
```

發布順序固定為 content-addressed source 逐物件 checksum upload/readback、source `manifest.json` 最後寫入，
再逐物件 copy/readback、完整且分頁的 runtime data-only inventory audit、runtime `manifest.json`
最後寫入與完整 prefix readback。component manifests 使用 serving parser 的 exact-key
路徑 contract；root inventory 以 path、SHA-256、size 完整固定 whole-Qwen layout/job IDs/shards、Tantivy
taxonomy/index files 與任何啟用 challenger，且不得含 query history、GT/qrels/judgments、test JD、raw logs
或 secrets。任何 incomplete、
`publication_allowed=false`、未通過正向 NDCG@10 promotion evidence、非 temporal
Tantivy、Graph cutoff 越界或 object inventory drift 都會 fail closed；不會自動發布部分 release。

職缺 snapshot 的 authoritative S3 object：

```text
s3://workretrievaldata-runtimebucket404c5ee4-hkvrjx5fbkij/data/jobs/53937f7bf076789c4cd7e3be34fb89875336108d57707b5a93182181e1087089/jobs.csv
```

AWS readback 已驗證 1,218,635 rows、1,218,635 distinct job IDs、source rows `0..1218634`、exact source
marker 與會在任何 `INSERT`／`UPDATE`／`DELETE`／`TRUNCATE` 後使 marker 失效的 statement trigger，以及
`alembic_version`、`jobs` 兩張 public tables。若只需重跑 importer，`WORK_ROOT` 指向前述 bootstrap
建立的工作目錄：

```bash
WORK_ROOT=artifacts/bootstrap-YYYYMMDD-HHMMSS
AWS_PROFILE=competition AWS_DEFAULT_REGION=us-west-2 \
  uv run python scripts/import_jobs_to_aws.py \
  "$WORK_ROOT/dataset/職缺.csv"
```

Importer 與 promotion script 都固定 account、region、來源 identity 與完整性檢查；不要繞過其驗證。

## Benchmark 重現

Repository 已提供單一 committed Graph-on/off runner；目前仍沒有可發布的 retrieval benchmark，因為
主辦方 versioned evaluation queries／qrels／evaluator 尚未提供。Repository tests、migration checks、contract checks、CDK
synth 與 endpoint smoke 都是 acceptance evidence，不是 Recall、MRR、nDCG 或 latency benchmark。
339-context 的非官方 development proxy、六指標 paired delta、H>1000 診斷與 promotion 決策另見
[`docs/development-graph-ablation-report.md`](docs/development-graph-ablation-report.md)。

Graph-on/off 的正式單一入口是 `scripts/run_graph_ablation.sh`：它只接受 exact-key canonical query
JSONL（`qid`、`query`、含 timezone 的 `as_of`、`location_codes`、`duty_codes`），先由已驗證的 temporal-v3
Tantivy 重建 `graph_off`，再由 train-only Graph 產生 bounded typed bridge terms，以相同時間、地區、職務、
可見性與 180-day freshness 邊界回查該 Tantivy eligible universe；因此 `graph_on` 可納入不在 baseline
或歷史 Graph Job 節點中的新職缺，但不能沿 train Job edge 直接回傳舊職缺。兩者預設交給 repository 內建的
`scripts/evaluate_trec_runs.py` 計算 NDCG@10、Precision@10、Top-1 與 MRR；正式 organizer evaluator 可用
`GRAPH_EVALUATOR_COMMAND`、`GRAPH_EVALUATOR_ID`、`GRAPH_EVALUATOR_KIND=organizer` 覆寫，
runner 同時強制讀取 pinned extraction evidence 與 extraction manifest，重建並逐 bytes 驗證六張 Graph 表，
不接受只在 Graph 內部自洽的預建 artifact。
離線 runner 與 production adapter 共用同一份 Graph serving policy、implementation SHA 與固定輸入 golden
ranking parity test。正向 organizer 結果仍須經 `skill_graph_pipeline.py approve` 驗證外部 attestation，並以
原始 candidate manifest 綁定實際評測的六個檔案，才會產生 immutable production component；不能直接把研究
manifest 改成 publishable。
manifest 的 canonical qid universe 必須一致；off／on TREC 可省略各自 manifest 明確宣告的 zero-result
qid，且 on 可救回 off 的 zero-result query，evaluator 仍須以完整 canonical query count 計分。Repository 不猜主辦方 query CSV
schema；organizer-specific adapter 必須先把官方 CSV 轉成 canonical JSONL，完整命令與契約見
[`docs/retrieval-pipelines.md`](docs/retrieval-pipelines.md)。預先算好的 `graph_off` 不是此入口的輸入。

可重現的 acceptance commands 與未來 benchmark 所需的 artifact、provenance、metrics 見
[`docs/benchmark.md`](docs/benchmark.md)。

## Production deployment

`.github/workflows/deploy.yml` 會在 `main` 收到 merge commit 後自動執行，也保留 `workflow_dispatch`
供同一個 commit 手動重部署；兩條路徑都只允許由 `main` 經 GitHub `production` environment 執行。它同時要求：

- protected environment variable `DEPLOY_ENABLED=true`
- environment variable `AWS_DEPLOY_ROLE_ARN`
- protected environment variable `ARTIFACT_MANIFEST_SHA`，供自動部署指定已核准的 immutable runtime
- protected environment variable `SEARCH_ENABLE_GRAPH=true|false`；未設定時為 `false`
- 手動重部署的 confirmation input 必須精確等於 `DEPLOY`
- resolved artifact manifest 必須是 64-character lowercase SHA-256
- 自動部署可由 `ALARM_EMAIL` environment variable 設定 alarm email；手動重部署則使用 input。若有填寫，
  收件者必須完成 AWS SNS subscription confirmation
- 自動部署可由 `COMPUTE_PROFILE` environment variable 選擇 `cpu-incumbent` 或 `gpu-shadow`，未設定時為
  `cpu-incumbent`；手動重部署使用同名 input
- `cpu-incumbent` 固定啟動一個 2 vCPU／16 GiB Fargate task，GPU ASG/service 固定為 `0/0/0`
- `gpu-shadow` 固定 CPU desired `0`、GPU ASG min/max `1/2`、GPU service desired `1`；不允許 caller
  自行拼出混合 profile

流程依序執行 frozen installs、static web build、OIDC authentication、DataStack deploy、runtime manifest
驗證、`linux/amd64` API image build／push、ECR scan、digest-pinned PlatformStack deploy、web sync、等待
CloudFront invalidation，最後才執行 public health、web 與 search smoke；`cpu-incumbent` 是已 promotion 的
temporal BM25 hot path。Graph 可由 `SEARCH_ENABLE_GRAPH=true` 選用，但 immutable runtime manifest 必須同時
包含通過正向 NDCG@10、organizer attestation、serving-policy SHA 驗證的 publishable Skill Graph；缺檔、未核准或驗證失敗會讓部署／服務
啟動直接失敗，不會退回 BM25。現有 runtime bundle 的 Graph 仍為 `false`，Dense、LTR 與 reranker 也維持關閉；public readiness 回傳的
`artifact_manifest_sha256` 必須精確等於本次 workflow resolved manifest，舊 runtime 健康不能通過 deployment gate。

Workflow 自行 build image，不接受 caller-supplied image URI；CDK 只接收 ECR digest URI。`main` 的每次
push（包含 PR merge）都會自動部署；其他 branch 不會部署。ECR scan、stack deployment、CloudFront
publication 與 public smoke 是彼此獨立的 gate，不可用前一項成功代表後一項已完成。

GitHub OIDC 使用 repository-ID-bound immutable subject；不要改回可變動的 owner／repository-name
subject。`production` environment 已限制為 `main`。目前 private-repository billing plan 不支援 required
reviewers，因此自動路徑的 gate 是 main-only environment、`DEPLOY_ENABLED=true` 與已核准的 immutable
manifest；手動路徑另要求精確的 `DEPLOY` confirmation。若方案之後支援，再啟用 required reviewers。

系統元件、request flow、資料匯入與 infrastructure ownership 詳見
[`docs/architecture.md`](docs/architecture.md)，貢獻規則見 [CONTRIBUTING.md](CONTRIBUTING.md)。
