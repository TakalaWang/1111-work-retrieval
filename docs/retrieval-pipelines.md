# Reproducible retrieval pipelines

This document covers the production Tantivy/whole-Qwen artifact builders and the two offline
challenger pipelines. A challenger cannot enter serving without an immutable promotion report.
Legacy `2026-07-24-clean-v1`, 15-field, 4096-dimensional or unpinned artifacts fail the current
contracts and cannot be relabeled as the new baseline.

## Shared publication contract

- Build from a pinned source snapshot and a closed JSON manifest.
- Write into a new output directory. Existing artifacts are never overwritten.
- Write every file atomically, then validate row identity, shape, SHA-256 and byte size.
- Upload only to a content-addressed S3 prefix after local validation.
- Read every uploaded byte back with `ExpectedBucketOwner=378849533305`; an S3 object existing is
  not sufficient evidence that it is correct.
- Keep experiment artifacts outside the production runtime prefix until an ablation passes its
  promotion gate. The serving process never discovers or enables challengers automatically.

## Production whole-JD Qwen baseline

`scripts/whole_embedding_pipeline.py` reads the source CSV directly through the core
`2026-08-01-full-jd-v2` serializer. All 34 ordered JD fields, including the complete job content,
are required. The build pins source bytes, job row order, document template, tokenizer, model and
revision. Qwen emits the 4096-dimensional source representation; the builder selects its first
1024 MRL dimensions and independently L2-normalizes before float16 storage. The component pins
`source_dimension=4096`, `dimension=1024` and
`projection=mrl_prefix_then_l2_normalize`.

Each completed shard has a SHA/size/source-job-slice sidecar. A restarted build validates and
reuses only sealed shards, deletes only an incomplete shard temporary file, and atomically writes
the final build/component manifests last.

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
duty and visibility remain raw hard-filter fields; update time and row index are unsigned fast
fields. The component pins the tokenizer/source-field policy hash and the complete index-tree SHA.

Query correction is not learned from query history. It is derived only from supported
surface-to-canonical pairs in the train-JD LLM evidence, and its maximum evidence timestamp must
remain before the split cutoff.

```bash
uv run python scripts/tantivy_index_pipeline.py build \
  --jobs-csv /data/jobs.csv \
  --skill-evidence artifacts/input/skill-extraction/evidence.jsonl \
  --skill-extraction-manifest artifacts/input/skill-extraction/manifest.json \
  --split-manifest artifacts/evaluations/split.json \
  --output artifacts/incumbents/tantivy/<dataset-sha256> \
  --correction-minimum-support 3

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
dynamic split cutoff and serializes the exact full JD. `extract` calls a pinned Bedrock model and
stores one immutable response per request, so a restart resumes after validating existing response
lineage. Surface and evidence span must occur verbatim in the JD; invalid skills/relations are
counted and excluded. OOV surfaces remain open-vocabulary. The current policy is honestly named
`open_surface_per_jd_llm_canonicalization_v1`: it does not claim a cross-JD clustering stage.

```bash
uv run python scripts/llm_skill_extraction_pipeline.py prepare \
  --jobs-csv /data/jobs.csv \
  --split-manifest artifacts/evaluations/split.json \
  --output artifacts/input/skill-extraction-prepared/<dataset-sha256>

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
train jobs in that duty that contain the skill. Online traversal is bounded to one hop. A graph
candidate is explainable because the trace returns the anchor, edge type, weight, source job and
the source evidence span.

```bash
uv run python scripts/skill_graph_pipeline.py build \
  --evidence artifacts/input/skill-extraction.jsonl \
  --extraction-manifest artifacts/input/skill-extraction.manifest.json \
  --split-manifest artifacts/evaluations/split.json \
  --output artifacts/challengers/skill-graph/<source-sha256> \
  --minimum-support 20

uv run python scripts/skill_graph_pipeline.py validate \
  --split-manifest artifacts/evaluations/split.json \
  --output artifacts/challengers/skill-graph/<source-sha256>

uv run python scripts/skill_graph_pipeline.py trace \
  --split-manifest artifacts/evaluations/split.json \
  --output artifacts/challengers/skill-graph/<source-sha256> \
  --skill python \
  --limit 20
```

The Graph uses the same content-addressed experiment publication contract as embeddings:

```bash
AWS_PROFILE=competition uv run python scripts/skill_graph_pipeline.py publish-s3 \
  --output artifacts/challengers/skill-graph/<source-sha256> \
  --split-manifest artifacts/evaluations/split.json \
  --bucket <artifact-bucket> \
  --prefix experiments/skill-graph/<manifest-sha256> \
  --profile competition
```

The graph must remain disabled when graph-on does not materially improve the committed time-split
NDCG@10 ablation. A recall-only gain or a qualitative trace does not pass this gate.

The one-command ablation runner does not manufacture qrels or scores. It requires a SHA-pinned
organizer evaluator or a declared train-only semantic-proxy evaluator. Both run manifests must pin
byte-identical non-Graph inputs; only `graph_manifest_sha256` may differ. Organizer promotion also
requires the evaluator's paired significance result. Optional shuffled-Graph, placebo-edge and
path-mask controls are emitted as explicitly disabled until their runs are supplied.
`non_graph_inputs` should pin query set, eligible universe, filters, candidate budget, lexical/dense
artifacts, fusion, reranker and every other ranking configuration used by both runs.
The report is the separate publication attestation: it pins the candidate Graph manifest and sets
`publication_allowed` only for a significant organizer-evaluator result meeting the declared
NDCG@10 threshold. The pending Graph build manifest itself is never rewritten.

```bash
uv run python scripts/graph_ablation_runner.py \
  --split-manifest artifacts/evaluations/split.json \
  --graph-output artifacts/challengers/skill-graph/<source-sha256> \
  --qrels artifacts/evaluations/qrels.txt \
  --graph-off-run artifacts/evaluations/graph-off.run \
  --graph-off-manifest artifacts/evaluations/graph-off.manifest.json \
  --graph-on-run artifacts/evaluations/graph-on.run \
  --graph-on-manifest artifacts/evaluations/graph-on.manifest.json \
  --evaluator-command 'uv run python <organizer-evaluator.py>' \
  --evaluator-id organizer-v1 \
  --evaluator-kind organizer \
  --minimum-ndcg-delta 0.001 \
  --output artifacts/evaluations/graph-ablation.json
```

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
