# Reproducible retrieval pipelines

This document covers the production Tantivy/whole-Qwen materialization path and the offline
challenger pipelines. A challenger cannot enter serving without an immutable promotion report.
Production reuses the independently sealed `2026-07-24-clean-v1` whole-job cache; it does not
silently relabel or rebuild those source vectors.

## Shared publication contract

- Build from a pinned source snapshot and a closed JSON manifest.
- Write into a new output directory. Existing artifacts are never overwritten.
- Write every file atomically, then validate row identity, shape, SHA-256 and byte size.
- Upload only to a content-addressed S3 prefix after local validation.
- Read every uploaded byte back with `ExpectedBucketOwner=378849533305`; an S3 object existing is
  not sufficient evidence that it is correct.
- Keep experiment artifacts outside the production runtime prefix until an ablation passes its
  promotion gate. The serving process never discovers or enables challengers automatically.

## Production sealed whole-Qwen baseline

The authoritative input is the 1,218,635-row EVA cache: 122 contiguous 4096-dimensional float16
shards, sealed source manifest SHA
`a02a23655fe8e5cc6b08afde35e93898ff94c62b88bbf7522e09f2c15378715c` and source-inventory SHA
`f762cc4d676e16aa04789e1573713ef30d66e72f3a7f96c5bcd7e7e6133a2adb`. Its ordered 15-field
document includes the complete job content. `scripts/materialize_runtime_components.py` validates
every source inventory byte, row boundary, job order and shard SHA, then derives serving shards by
taking the first 1024 dimensions, normalizing in float32 and sealing as float16. The derived
component records both source and derived per-shard SHA-256 values. Source bytes are never mutated.

`scripts/whole_embedding_pipeline.py` remains an optional, isolated full-JD-v2 rebuild experiment.
It is not called by the production reproduction command and its output cannot replace the sealed
incumbent without a separate approval and manifest-contract change.

```bash
TOKENIZER_SHA256=$(sha256sum \
  /models/Qwen3-Embedding-8B/snapshots/1d8ad4ca9b3dd8059ad90a75d4983776a23d44af/tokenizer.json \
  | cut -d ' ' -f 1)

uv run --with torch==2.13.0 --with sentence-transformers==5.6.0 \
  python scripts/whole_embedding_pipeline.py build \
  --jobs-csv /data/jobs.csv \
  --output artifacts/incumbents/whole-qwen/<dataset-sha256> \
  --tokenizer-sha256 "$TOKENIZER_SHA256" \
  --backend cuda \
  --devices cuda:0,cuda:1 \
  --model-snapshot /models/Qwen3-Embedding-8B/snapshots/1d8ad4ca9b3dd8059ad90a75d4983776a23d44af

# The same build through the pinned competition endpoint:
AWS_PROFILE=competition uv run python scripts/whole_embedding_pipeline.py build \
  --jobs-csv /data/jobs.csv \
  --output artifacts/incumbents/whole-qwen/<dataset-sha256> \
  --tokenizer-sha256 "$TOKENIZER_SHA256" \
  --backend sagemaker \
  --endpoint qwen3-embedding-8b-20260801-031826 \
  --profile competition

uv run python scripts/whole_embedding_pipeline.py validate \
  --jobs-csv /data/jobs.csv \
  --output artifacts/incumbents/whole-qwen/<dataset-sha256>

AWS_PROFILE=competition uv run python scripts/whole_embedding_pipeline.py publish-s3 \
  --jobs-csv /data/jobs.csv \
  --output artifacts/incumbents/whole-qwen/<dataset-sha256> \
  --bucket <artifact-bucket> \
  --prefix experiments/whole-qwen/<manifest-sha256> \
  --profile competition
```

`publish-s3` is a content-addressed experiment upload, not runtime activation. Run `verify-s3`
with the same source and arguments before constructing a new root runtime manifest.

## Production full-JD Tantivy baseline

`scripts/tantivy_index_pipeline.py` rebuilds the fielded index from the same source CSV and row
order as whole-Qwen. Title, three duty fields, skill/certificate fields, three industry fields and
all remaining full-JD fields (including job content) are deterministically pre-tokenized. Location,
duty, education, job attribute, work shift, no-experience and management are raw hard-filter
fields; monthly salary uses separate indexed lower-bound and recall-bound unsigned fields. The
official competition CSV has no visibility column, so this closed competition JD pool is explicitly
treated as the already-eligible corpus and the builder writes constant `visibility=1`. That value is
a corpus-eligibility assumption, not source-derived evidence. A live provider feed must supply and
revalidate its real visibility state before using this pipeline. Update time and row index are
unsigned fast fields. The component pins the
tokenizer/source-field policy hash and the complete index-tree SHA. Exact grammar, coverage and
unpromoted ambiguous cues are pinned in [`typed-constraint-evidence.md`](typed-constraint-evidence.md).

### Temporal-v3 fixed-339 promotion evidence

The deployment gate never trusts metrics copied into a standalone JSON file. First generate and
seal the candidate run without opening the qrels. After that seal exists, create the evidence bundle:

```bash
uv run python scripts/verify_temporal_v3_promotion.py create \
  --output artifacts/evaluations/temporal-v3-fixed-339 \
  --canonical-queries <fixed-canonical-queries.jsonl> \
  --evaluation-split <fixed-split-manifest.json> \
  --qrels <fixed-gt1.qrels> \
  --baseline-run <sealed-temporal-v2.run> \
  --baseline-run-manifest <sealed-temporal-v2.manifest.json> \
  --candidate-run <sealed-temporal-v3.run> \
  --candidate-run-manifest <sealed-temporal-v3.manifest.json> \
  --candidate-manifest <evaluated-temporal-v3-component/manifest.json> \
  --candidate-build-manifest <evaluated-temporal-v3-component/build-manifest.json>
```

`create` accepts only the repository-pinned canonical-query, split, qrels, temporal-v2 baseline and
jobs SHA values. It seals nine evidence files with byte size and SHA-256, recomputes NDCG@10,
Precision@10, Top-1, MRR, zero-result contexts and underfilled-Top-10 contexts directly from the run
and qrels bytes, and writes `attestation.json` only when NDCG@10 is positive and every remaining
ranking/coverage guardrail is non-regressing. Candidate lineage includes the dataset/order, stable
index policy and exact compiler, adapter, engine, serializer, index-builder and run-generator source
file SHA values. Physical Tantivy segment hashes are still verified inside each build, but are not
used as the cross-rebuild identity because Tantivy segment bytes are nondeterministic.

At deployment, pass the bundle's `attestation.json` and its independently read-back SHA-256 to the
bootstrap. `verify` re-reads all nine inventoried files and recomputes every metric before any
PostgreSQL or runtime publication write.

The BM25 baseline has no LLM dependency: its default component declares
`query_corrections={"enabled":false}` and can be built, validated and evaluated before any Graph
artifact exists. This keeps the lexical ablation honest. Query correction is a separate candidate,
never learned from query history, and cannot enter the component merely because the LLM produced
it. Enabling it requires both a train-JD-only candidate and an immutable organizer-evaluated
attestation showing a significant positive NDCG@10 delta. Missing, mismatched or negative evidence
fails closed; runtime never silently falls back to unapproved correction rules.

```bash
uv run python scripts/tantivy_index_pipeline.py build \
  --jobs-csv /data/jobs.csv \
  --location-taxonomy-csv /data/城市對照表.csv \
  --duty-taxonomy-csv /data/職務對照表.csv \
  --output artifacts/incumbents/tantivy/<dataset-sha256>

uv run python scripts/tantivy_index_pipeline.py validate \
  --jobs-csv /data/jobs.csv \
  --output artifacts/incumbents/tantivy/<dataset-sha256>

AWS_PROFILE=competition uv run python scripts/tantivy_index_pipeline.py publish-s3 \
  --jobs-csv /data/jobs.csv \
  --output artifacts/incumbents/tantivy/<dataset-sha256> \
  --bucket <artifact-bucket> \
  --prefix experiments/tantivy/<manifest-sha256> \
  --profile competition
```

After a fixed-input correction ablation passes, build the train-only candidate, create its
promotion attestation, and supply both immutable files to a new Tantivy build:

```bash
uv run python scripts/query_correction_pipeline.py build \
  --evidence artifacts/input/skill-extraction/<model-and-source-sha256>/evidence.jsonl \
  --extraction-manifest artifacts/input/skill-extraction/<model-and-source-sha256>/manifest.json \
  --split-manifest artifacts/evaluations/split.json \
  --minimum-support 3 \
  --output artifacts/challengers/query-correction/candidate.json

uv run python scripts/query_correction_pipeline.py approve \
  --candidate artifacts/challengers/query-correction/candidate.json \
  --split-manifest artifacts/evaluations/split.json \
  --promotion-report artifacts/evaluations/query-correction-promotion.json \
  --promotion-report-sha256 <approved-report-sha256> \
  --attestation artifacts/evaluations/query-correction-promotion.attestation.json

uv run python scripts/tantivy_index_pipeline.py build \
  --jobs-csv /data/jobs.csv \
  --output artifacts/incumbents/tantivy-corrected/<dataset-sha256> \
  --query-correction-candidate artifacts/challengers/query-correction/candidate.json \
  --query-correction-attestation \
    artifacts/evaluations/query-correction-promotion.attestation.json
```

## LLM evidence-locked entity and skill graph

`scripts/skill_graph_pipeline.py` consumes a train-only LLM extraction JSONL. Each skill and typed
relation must include an exact evidence span contained in the source JD. Its extraction manifest
must explicitly state that test-period JDs, ground truth and behavior logs were not used, and its
maximum source timestamp must be strictly before the cutoff supplied by a versioned split manifest.
The repository never hard-codes the production cutoff. The competition demo split can set
`train_cutoff_exclusive` and `evaluation_start_inclusive` to `2026-06-08T00:00:00+08:00`.
This JSONL layout is a portable correctness/reference artifact; the production serving runtime
does not load it while Graph is disabled. It does not replace the research extractor or invent a
second online traversal algorithm.

The formal GenAI entry point is `scripts/llm_skill_extraction_pipeline.py`. `prepare` applies the
dynamic split cutoff and serializes the exact full JD. It deterministically selects a representative
duty-stratified sample using square-root support allocation plus a split-pinned stable hash. The
default is 5,000 JDs and the hard cap is 10,000; every non-empty duty stratum receives one record or
the build fails when the number of strata cannot fit the cap. The manifest pins eligible train
counts, selected counts per duty, source bytes and sampling identity, so the sample is auditable and
reproducible rather than a convenient prefix of the CSV.

`extract` calls a pinned Bedrock model and stores one immutable response per request, so a restart
resumes after validating existing response lineage. Final evidence is streamed to a temporary
JSONL and atomically sealed; full request/response payloads are never accumulated in memory. Calls
are intentionally sequential with SDK standard exponential backoff capped at four total attempts,
which bounds pressure on the shared Bedrock quota and keeps per-record cost attribution exact.
Surface and evidence span must occur verbatim in the JD; invalid skills/relations are counted and
excluded. OOV surfaces remain open-vocabulary.
The current policy is honestly named `open_surface_per_jd_llm_canonicalization_v1`: it does not
claim a cross-JD clustering stage. The Graph represents the sampled train evidence only; it does
not pretend that all 1.2 million jobs received LLM extraction.

Capacity planning is explicit: the default run has at most 5,000 logical model requests and the
hard-cap run at most 10,000, each with no more than four SDK attempts. The constrained call uses a
64,000-token output safety budget so a large valid tool payload is not truncated; actual
input/output token totals are written into the extraction manifest. Estimate wall time as
remaining records multiplied by observed per-call p95 latency, and cost from those pinned usage
totals and the selected model's price. No fixed dollar or ETA claim is valid before `model_id` and
observed latency are known.

```bash
uv run python scripts/llm_skill_extraction_pipeline.py prepare \
  --jobs-csv /data/jobs.csv \
  --split-manifest artifacts/evaluations/split.json \
  --output artifacts/input/skill-extraction-prepared/<dataset-sha256> \
  --source-timezone Asia/Taipei \
  --max-records 5000

AWS_PROFILE=competition uv run python scripts/llm_skill_extraction_pipeline.py extract \
  --prepared artifacts/input/skill-extraction-prepared/<dataset-sha256> \
  --split-manifest artifacts/evaluations/split.json \
  --output artifacts/input/skill-extraction/<model-and-source-sha256> \
  --model-id <pinned-bedrock-model-id> \
  --profile competition
```

The graph has these immutable JSONL tables:

| Table                     | Node or edge                  | Required identity            |
| ------------------------- | ----------------------------- | ---------------------------- |
| `jobs.jsonl`              | Job entity node               | `job_id`                     |
| `skills.jsonl`            | Canonical skill node          | normalized skill             |
| `job-skills.jsonl`        | Job → skill evidence edge     | `(job_id, skill)`            |
| `duty-skills.jsonl`       | Duty → skill aggregate edge   | `(duty, skill)`              |
| `skill-relations.jsonl`   | Typed skill → skill edge      | `(source, type, target)`     |
| `relation-evidence.jsonl` | Relation → source JD evidence | relation identity + `job_id` |

Both skill nodes and skill relations must meet `--minimum-support`. Relation weight is normalized
support `support / sqrt(source_support * target_support)`; duty-skill weight is the fraction of
train jobs in that duty that contain the skill. Traversal is bounded to one typed hop. A trace
returns the anchor skill, relation edge, related skill, related jobs, candidate-side Tantivy
retrieval evidence and train-JD relation evidence; the candidate path can therefore be replayed
without calling the LLM again. Validation, tracing, publication and ablation all require the pinned extraction evidence
and extraction manifest: they reload that evidence, deterministically rebuild all six tables and
require byte-identical canonical JSONL, so a self-consistent but fabricated Graph fails closed.

```bash
uv run python scripts/skill_graph_pipeline.py build \
  --evidence artifacts/input/skill-extraction/<model-and-source-sha256>/evidence.jsonl \
  --extraction-manifest artifacts/input/skill-extraction/<model-and-source-sha256>/manifest.json \
  --split-manifest artifacts/evaluations/split.json \
  --output artifacts/challengers/skill-graph/<source-sha256> \
  --minimum-support 20

uv run python scripts/skill_graph_pipeline.py validate \
  --split-manifest artifacts/evaluations/split.json \
  --evidence artifacts/input/skill-extraction/<model-and-source-sha256>/evidence.jsonl \
  --extraction-manifest artifacts/input/skill-extraction/<model-and-source-sha256>/manifest.json \
  --output artifacts/challengers/skill-graph/<source-sha256>

uv run python scripts/skill_graph_pipeline.py trace \
  --split-manifest artifacts/evaluations/split.json \
  --evidence artifacts/input/skill-extraction/<model-and-source-sha256>/evidence.jsonl \
  --extraction-manifest artifacts/input/skill-extraction/<model-and-source-sha256>/manifest.json \
  --output artifacts/challengers/skill-graph/<source-sha256> \
  --skill python \
  --limit 20
```

The Graph uses the same content-addressed experiment publication contract as embeddings:

```bash
AWS_PROFILE=competition uv run python scripts/skill_graph_pipeline.py publish-s3 \
  --output artifacts/challengers/skill-graph/<source-sha256> \
  --split-manifest artifacts/evaluations/split.json \
  --evidence artifacts/input/skill-extraction/<model-and-source-sha256>/evidence.jsonl \
  --extraction-manifest artifacts/input/skill-extraction/<model-and-source-sha256>/manifest.json \
  --bucket <artifact-bucket> \
  --prefix experiments/skill-graph/<manifest-sha256> \
  --profile competition
```

The graph must remain disabled when graph-on does not materially improve the committed time-split
NDCG@10 ablation. A recall-only gain or a qualitative trace does not pass this gate.

`scripts/graph_candidate_runner.py` manufactures `graph_on` from the frozen `graph_off` TREC run,
the frozen train-only Graph and the exact SHA-pinned temporal-v3 Tantivy component. Anchors enter by
three audited routes, in priority order: exact query-to-skill alias match; explicit duty filter to
the frozen Duty→Skill aggregate; or consensus from at least two of at most ten Graph-covered
baseline seed jobs. At most one typed incident edge turns those anchors into bounded bridge terms.
The Graph's historical Job→Skill edges only identify seed anchors and never supply candidate job
IDs. Every bridge term instead re-queries the current Tantivy eligible universe with the canonical
query's identical `as_of`, location, duty and compiled typed constraints, plus visibility and the
180-day freshness boundary. A resulting job may therefore be novel to both `graph_off` and the train-only Graph while
remaining inside the same production eligibility contract.

Expansion is bounded to eight anchors, ten typed edges per anchor, sixteen bridge terms and fifty
Tantivy hits per term. Equal-weight RRF aggregates baseline and graph evidence; the baseline Top-3
is protected and Graph may replace at most two jobs when the baseline has ten results. A short
baseline is first filled, and every remaining candidate stays in the tail. Every graph candidate
trace records its anchor evidence, typed relation and evidence spans, traversal direction, bridge
term, Tantivy rank/score, retained/omitted path counts and score contributions, and the complete
filter boundary. Only five best paths are expanded; their contribution plus the exact omitted
contribution reproduces the total Graph score. No qrels, history or train Job edge may create
a candidate. Every output score is a strictly decreasing rank score for deterministic TREC
evaluation; the original fusion score stays in the trace. The manifest pins the baseline run and
manifest, Graph, current Tantivy artifacts, trace, algorithm, coverage statistics, limits, relation
types and fusion values. Job IDs must be canonical positive ASCII decimals; query IDs, ranks,
duplicates and non-contiguous query blocks fail closed.

The one-command ablation wrapper does not accept a precomputed `graph_off`, manufacture qrels or
manufacture scores. It first retrieves `graph_off` from the declared temporal-v3 Tantivy component,
then generates `graph_on`, then invokes an organizer-provided evaluator or a declared train-only
semantic-proxy evaluator. The ablation runner independently regenerates the challenger run,
manifest and trace and requires all three to be byte-identical before evaluation; an arbitrary
precomputed challenger cannot pass. Both manifests pin the identical canonical qid universe and
their own ordered zero-result qids; standard TREC files may omit only the set declared by that
variant. Graph-on may rescue a Graph-off zero-result query but may not drop a non-empty baseline
query. The evaluator's reported query count must still match the full canonical universe.
Both run manifests pin byte-identical non-Graph inputs. A positive metric gate is reported, but
automatic promotion/publication remains disabled until a separate organizer attestation exists.
Optional shuffled-Graph, placebo-edge and path-mask controls are emitted as explicitly disabled
until their runs are supplied.
`non_graph_inputs` should pin query set, eligible universe, filters, candidate budget, lexical/dense
artifacts, fusion, reranker and every other ranking configuration used by both runs.
The report pins the candidate Graph manifest and records whether a significant organizer-evaluator
result meets the declared NDCG@10 threshold as `metric_gate_passed`. It deliberately leaves
`promotion_allowed` and `publication_allowed` false until a separate organizer attestation is
available. The pending Graph build manifest itself is never rewritten.

The query boundary is canonical UTF-8 JSONL with exactly these keys on every line:

```json
{
  "qid": "q1",
  "query": "Python 資料工程師",
  "as_of": "2026-06-08T23:59:59.999+08:00",
  "location_codes": ["100100"],
  "duty_codes": ["140200"]
}
```

`qid` is a unique safe TREC identifier, `as_of` must include a timezone and fall inside the declared
evaluation window, and both code arrays must be unique canonical ASCII decimals in numeric order.
Empty code arrays mean that filter was not requested. The repository deliberately does not guess
the organizer CSV columns: an organizer-specific adapter must convert the official CSV into this
canonical JSONL; the resulting JSONL bytes are part of the pinned evaluation input.

```bash
scripts/run_graph_ablation.sh \
  artifacts/evaluations/split.json \
  artifacts/challengers/skill-graph/<source-sha256> \
  artifacts/input/skill-extraction/<model-and-source-sha256>/evidence.jsonl \
  artifacts/input/skill-extraction/<model-and-source-sha256>/manifest.json \
  artifacts/evaluations/qrels.txt \
  artifacts/evaluations/queries.canonical.jsonl \
  dataset/職缺.csv \
  artifacts/experiments/tantivy-bm25-temporal-v3 \
  0.001 \
  artifacts/evaluations/graph-ablation/<experiment-id>
```

The default evaluator is the committed `scripts/evaluate_trec_runs.py`, which reports NDCG@10,
Precision@10, Top-1 and MRR over the complete qrels query universe. To bind an organizer-owned
evaluator and permit the separate promotion attestation, set
`GRAPH_EVALUATOR_COMMAND`, `GRAPH_EVALUATOR_ID` and `GRAPH_EVALUATOR_KIND=organizer` for the same
command. The default is deliberately recorded as `train_semantic_proxy`, never as an official
organizer score.

The output directory must not already exist. It contains `baseline/graph-off.run`, its immutable
manifest, `generated/graph-on.run`, the Graph-on manifest, `generated/graph-traces.jsonl`, and
`graph-ablation.json`. Baseline lineage pins the canonical queries, jobs CSV, validated Tantivy
component/index/job order/taxonomy and retrieval policy. The expensive GenAI extraction remains a
frozen SHA-pinned input; this offline run performs no LLM or embedding recomputation.

A passing organizer result is still not a serving artifact. The organizer must provide a separate
exact-key attestation pinned to the candidate Graph manifest, ablation report, run hashes,
significance result, serving algorithm and canonical serving-policy SHA. Only then may the pending
Graph be copied into a new immutable six-file production component:

```bash
uv run python scripts/skill_graph_pipeline.py approve \
  --graph-output artifacts/challengers/skill-graph/<source-sha256> \
  --split-manifest artifacts/evaluations/split.json \
  --evidence artifacts/input/skill-extraction/<model-and-source-sha256>/evidence.jsonl \
  --extraction-manifest artifacts/input/skill-extraction/<model-and-source-sha256>/manifest.json \
  --ablation-report artifacts/evaluations/graph-ablation/<experiment-id>/graph-ablation.json \
  --ablation-report-sha256 <sha256> \
  --organizer-attestation artifacts/evaluations/graph-organizer-attestation.json \
  --organizer-attestation-sha256 <sha256> \
  --runtime-prefix graphs/skill-graph/<component-id> \
  --candidate-manifest-runtime-path evidence/skill-graph/<component-id>-candidate.json \
  --promotion-report-runtime-path evidence/skill-graph/<component-id>.json \
  --organizer-attestation-runtime-path evidence/skill-graph/<component-id>-organizer.json \
  --output artifacts/promoted/skill-graph/<component-id>
```

Approval never rewrites the research Graph. Negative, non-significant, proxy-evaluated, policy-
drifted or hand-edited evidence fails closed. The original candidate manifest cryptographically
binds the exact six evaluated files; serving-policy and implementation hashes bind the evaluated
algorithm to production. The resulting component manifest, candidate manifest, promotion report,
organizer attestation and six Graph files must all be selected into the next immutable runtime
release before `SEARCH_ENABLE_GRAPH=true` can start.

## Qwen multi-view embeddings

`scripts/multiview_embedding_pipeline.py` builds the optional occupation, skill, requirement and
content views from the complete JD fields. This supplements the production whole-JD Qwen vector;
it does not replace it. Input records are sorted by `(job_row, view kind, view_index)`. Empty views
are omitted, present view indexes are contiguous, and a SHA-pinned `jobs.jsonl` preserves the exact
job-row identity.

The model is pinned to `Qwen/Qwen3-Embedding-8B` revision
`1d8ad4ca9b3dd8059ad90a75d4983776a23d44af`. It emits 4096 dimensions, then uses the first 1024
MRL dimensions and normalizes them before float16 serialization. Artifact construction is not
promotion-gated: the build manifest is always `pending_multiview_ablation`. A separate immutable
attestation can only be created after a SHA-pinned report shows a positive NDCG@10 delta against
the promoted 1024-dimensional whole-JD incumbent using an organizer evaluator and paired
significance result. The fixed 4096-to-1024 MRL prefix selection remains build lineage, not the
multi-view promotion result. This avoids requiring the candidate before it can exist.

Build deterministic input views with the pinned Qwen tokenizer:

```bash
uv run --with transformers==5.14.1 \
  python scripts/multiview_embedding_pipeline.py build-records \
  --jobs-csv /data/jobs.csv \
  --output artifacts/input/job-views/<dataset-sha256> \
  --model-snapshot /models/Qwen3-Embedding-8B/snapshots/1d8ad4ca9b3dd8059ad90a75d4983776a23d44af
```

Build on two explicit local GPUs:

```bash
uv run --with torch==2.13.0 --with sentence-transformers==5.6.0 \
  python scripts/multiview_embedding_pipeline.py build \
  --records artifacts/input/job-views/<dataset-sha256>/records.jsonl \
  --input-manifest artifacts/input/job-views/<dataset-sha256>/manifest.json \
  --output artifacts/challengers/multiview/<records-sha256> \
  --backend cuda \
  --devices cuda:0,cuda:1 \
  --model-snapshot /models/Qwen3-Embedding-8B/snapshots/1d8ad4ca9b3dd8059ad90a75d4983776a23d44af
```

Build through the pinned SageMaker endpoint in the competition account:

```bash
AWS_PROFILE=competition uv run python scripts/multiview_embedding_pipeline.py build \
  --records artifacts/input/job-views/<dataset-sha256>/records.jsonl \
  --input-manifest artifacts/input/job-views/<dataset-sha256>/manifest.json \
  --output artifacts/challengers/multiview/<records-sha256> \
  --backend sagemaker \
  --endpoint qwen3-embedding-8b-20260801-031826 \
  --profile competition \
  --region us-west-2 \
  --expected-account 378849533305
```

Validate locally before upload:

```bash
uv run python scripts/multiview_embedding_pipeline.py verify \
  --output artifacts/challengers/multiview/<records-sha256>
sha256sum artifacts/challengers/multiview/<records-sha256>/manifest.json
```

After evaluation, create a separate promotion attestation. This does not mutate the candidate:

```bash
uv run python scripts/multiview_embedding_pipeline.py approve \
  --output artifacts/challengers/multiview/<records-sha256> \
  --promotion-report artifacts/evaluations/multiview-promotion.json \
  --promotion-report-sha256 <approved-report-sha256> \
  --attestation artifacts/evaluations/multiview-promotion.attestation.json
```

Upload only to an experiment prefix whose final segment is the manifest SHA. Files are uploaded
and read back first; `manifest.json` is the last commit marker. This does not activate runtime.

```bash
AWS_PROFILE=competition uv run python scripts/multiview_embedding_pipeline.py publish-s3 \
  --output artifacts/challengers/multiview/<records-sha256> \
  --bucket <artifact-bucket> \
  --prefix experiments/multiview/<manifest-sha256> \
  --profile competition
```

Validate the complete remote bytes, including the manifest:

```bash
AWS_PROFILE=competition uv run python scripts/multiview_embedding_pipeline.py verify-s3 \
  --output artifacts/challengers/multiview/<records-sha256> \
  --bucket <artifact-bucket> \
  --prefix experiments/multiview/<manifest-sha256> \
  --expected-owner 378849533305 \
  --manifest-sha256 <manifest-sha256> \
  --profile competition
```

SageMaker endpoint availability, local artifact validation, S3 publication, graph/multi-view
ablation approval and production runtime activation are independent states and must be reported
separately.
