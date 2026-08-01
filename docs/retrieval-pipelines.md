# Reproducible retrieval pipelines

This document covers the two offline challenger pipelines. Neither can enter the serving runtime
without a completed, immutable promotion report. The production baseline remains full-JD Tantivy
BM25 plus whole-JD Qwen dense retrieval.

## Shared publication contract

- Build from a pinned source snapshot and a closed JSON manifest.
- Write into a new output directory. Existing artifacts are never overwritten.
- Write every file atomically, then validate row identity, shape, SHA-256 and byte size.
- Upload only to a content-addressed S3 prefix after local validation.
- Read every uploaded byte back with `ExpectedBucketOwner=378849533305`; an S3 object existing is
  not sufficient evidence that it is correct.
- Keep experiment artifacts outside the production runtime prefix until an ablation passes its
  promotion gate. The serving process never discovers or enables challengers automatically.

## LLM evidence-locked entity and skill graph

`scripts/skill_graph_pipeline.py` consumes a train-only LLM extraction JSONL. Each skill and typed
relation must include an exact evidence span contained in the source JD. Its extraction manifest
must explicitly state that test-period JDs, ground truth and behavior logs were not used, and its
maximum source timestamp must be strictly before `2026-06-08T00:00:00+08:00`.

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
  --output artifacts/challengers/skill-graph/<source-sha256> \
  --minimum-support 20

uv run python scripts/skill_graph_pipeline.py validate \
  --output artifacts/challengers/skill-graph/<source-sha256>

uv run python scripts/skill_graph_pipeline.py trace \
  --output artifacts/challengers/skill-graph/<source-sha256> \
  --skill python \
  --limit 20
```

The graph must remain disabled when graph-on does not materially improve the committed time-split
NDCG@10 ablation. A recall-only gain or a qualitative trace does not pass this gate.

## Qwen multi-view embeddings

`scripts/multiview_embedding_pipeline.py` builds the optional occupation, skill, requirement and
content views. This supplements the production whole-JD Qwen vector; it does not replace it. Input
records must be sorted by `(job_row, view kind, view_index)`. Every job has one stable, contiguous
row and at least one contiguous record for every view kind, so an embedding can never silently map
to a different job.

The model is pinned to `Qwen/Qwen3-Embedding-8B` revision
`1d8ad4ca9b3dd8059ad90a75d4983776a23d44af`. It emits 4096 dimensions, then uses the first 1024
MRL dimensions and normalizes them before float16 serialization. Publication requires a
SHA-pinned report showing a positive NDCG@10 delta against the 4096-dimensional whole-JD reference.

Build on two explicit local GPUs:

```bash
uv run --with torch==2.13.0 --with sentence-transformers==5.6.0 \
  python scripts/multiview_embedding_pipeline.py build \
  --records artifacts/input/job-views.jsonl \
  --input-manifest artifacts/input/job-views.manifest.json \
  --promotion-report artifacts/evaluations/multiview-promotion.json \
  --promotion-report-sha256 <approved-report-sha256> \
  --output artifacts/challengers/multiview/<records-sha256> \
  --backend cuda \
  --devices cuda:0,cuda:1 \
  --model-snapshot /models/Qwen3-Embedding-8B/snapshots/1d8ad4ca9b3dd8059ad90a75d4983776a23d44af
```

Build through the pinned SageMaker endpoint in the competition account:

```bash
AWS_PROFILE=competition uv run python scripts/multiview_embedding_pipeline.py build \
  --records artifacts/input/job-views.jsonl \
  --input-manifest artifacts/input/job-views.manifest.json \
  --promotion-report artifacts/evaluations/multiview-promotion.json \
  --promotion-report-sha256 <approved-report-sha256> \
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

After a reviewed immutable upload, validate the complete remote bytes, including the manifest:

```bash
AWS_PROFILE=competition uv run python scripts/multiview_embedding_pipeline.py verify-s3 \
  --output artifacts/challengers/multiview/<records-sha256> \
  --bucket <artifact-bucket> \
  --prefix experiments/multiview/<manifest-sha256> \
  --expected-owner 378849533305 \
  --manifest-sha256 <manifest-sha256>
```

SageMaker endpoint availability, local artifact validation, S3 publication, graph/multi-view
ablation approval and production runtime activation are independent states and must be reported
separately.
