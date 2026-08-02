# IPS LambdaRank reproducibility contract

This repository preserves the earlier Learning-to-Rank method as an offline, fail-closed research
contract. It is not loaded by the API, is absent from the runtime manifest and does not add
LightGBM to production dependencies. The existing BM25 ordering remains authoritative.

## Why the model is not active

The chronological, context-purged experiment retained only 27 trainable query groups and 338
exposed `(context, job)` pairs: 289 label-0 rows, 40 clicks and 9 applications. Of 144 training
contexts, 56 had fewer than two exposed candidates and 61 had no positive candidate. The GT1
validation result regressed from the temporal BM25 baseline:

| Variant                    |  NDCG@10 | Precision@10 |    Top-1 |      MRR |
| -------------------------- | -------: | -----------: | -------: | -------: |
| Temporal BM25              | 0.057719 |     0.029128 | 0.082569 | 0.101809 |
| IPS LambdaRank, no Graph   | 0.020470 |     0.015826 | 0.011468 | 0.058096 |
| IPS LambdaRank, with Graph | 0.027741 |     0.020872 | 0.020642 | 0.066902 |

The five-context GT2 validation was too small for promotion, and GT3 had no strictly
post-threshold validation searches. The model therefore failed promotion; this document is not a
claim of ranking improvement.

Research lineage:

- Report SHA-256: `12760fefcb6d66f1ebd11040c511697237b30a05236bbae2073d4435d5357a72`
- No-Graph manifest SHA-256: `4b14296945463dee0ae4367efab38d8bda026af5f5fa68a0025c80d6141f7bc0`
- With-Graph manifest SHA-256: `c7b2a99ff610b69855f81675e0e6c5735d1cb87afd6e27f8ad74e76df8705768`
- Candidate hashes: temporal BM25 `b1ed923b1b9d7d801c9a75f16f064b7e8d8675903ad43f501340753428853fb6`,
  Qwen MRL-1024 `f64bd4b25a83916937f324803cfe471664fadee9ac72a0736c22abf98fd58537`,
  Graph `48d84c272ef8a227f7b40a4de529d841f6b12a167273c3a3b04e9a59ed265164`

## Label and bias contract

The unit is one aggregated `(context_id, job_index)` pair. Labels remain ordinal:

- `0`: exposed but not observed clicking or applying; treated as a weak observation, not a true
  negative.
- `1`: the same pair has a browse/click event.
- `2`: the same pair has an application event.

For repeated observations, use the maximum label and minimum observed exposure rank. A positive
without a matched train-window exposure is excluded. Query history may supply time-safe training
signals but must never replay job IDs into candidates or answers.

The preserved weight is:

```text
propensity(rank) = max(1 / log2(rank + 1), 0.1)
IPS weight       = 1 / propensity(rank)
label-0 weight   = IPS weight * 0.25
```

This log-discount curve is a clipped heuristic examination prior. It is not a propensity learned
from randomized traffic and is not Doubly Robust. Production promotion requires randomized or
otherwise identifiable propensity evidence plus clipping-sensitivity and DR comparisons.

## Feature contract

[`scripts/ltr_feature_contract.py`](../scripts/ltr_feature_contract.py) reproduces only feature and
IPS arrays using Python, NumPy and immutable input hashes. It does not import LightGBM, train a
model or change runtime ranking.

The frozen feature order and schema SHA-256
`9a795b700eb96669ac3937ce2eb787a5b20f82cd0c094e60856ec2f673741f4a` are:

1. `lexical_best_rr`
2. `dense_best_rr`
3. `graph_best_rr`
4. `source_count`
5. `source_family_count`
6. `concept_coverage`
7. `structured_intents`
8. `whole_literal`
9. `title_literal`
10. `graph_path_max`
11. `freshness`
12. `future_snapshot`

Reciprocal-rank features use `1 / (60 + best_rank)`. Candidate features must be generated before
labels are joined; labels and exposure ranks are used only for training arrays and IPS weights.

Each JSONL input row has exactly these fields:

```json
{
  "context_id": 4565,
  "job_index": 123,
  "label": 1,
  "exposure_rank": 3,
  "lane_ranks": { "lexical_temporal": 1, "dense_mrl_1024": 8 },
  "concept_count": 2,
  "concept_ranks": { "python": 4 },
  "structured_intents": 1,
  "graph_path_scores": { "python": 0.8 },
  "freshness": 0.5,
  "future_snapshot": false
}
```

The lineage JSON must have schema version 1, train cutoff, split SHA, global job-row-order SHA,
candidate and source-target SHA maps, and both `uses_test_jd` and
`uses_query_history_replay` set to `false`. Unknown keys, duplicate pairs, invalid ranks, invalid
labels or hash mismatch fail closed.

## Reproduction

Run the dependency-free mathematical self-check:

```bash
uv run python scripts/ltr_feature_contract.py self-check
```

Build immutable arrays from pre-joined train-only evidence:

```bash
uv run python scripts/ltr_feature_contract.py build \
  --input artifacts/ltr/train-evidence.jsonl \
  --expected-input-sha256 <sha256> \
  --lineage artifacts/ltr/lineage.json \
  --output artifacts/ltr/features
```

The output seals float32 features and IPS weights, int32 labels/groups/exposure positions and
int64 context/job row identifiers. Its manifest always records `runtime_activation=false` and
`promotion_allowed=false`.

To train a future challenger, install LightGBM only in an isolated experiment environment, keep
the objective `lambdarank` and label gain `[0, 1, 3]`, and publish the model only after a
chronological, query-group-purged evaluation improves NDCG@10 without material Top-1/MRR or latency
regressions. Runtime activation requires a separately reviewed adapter and immutable promotion
attestation.
