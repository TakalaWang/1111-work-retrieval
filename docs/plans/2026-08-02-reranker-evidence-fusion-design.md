# Reranker Evidence Fusion Design

## Decision

The Qwen3 reranker remains a bounded second-stage signal. It must not replace the
first-stage order without a measured promotion gate. The experiment compares the
same frozen 339 queries, candidate membership, job text, and reranker scores while
varying only candidate depth, fusion policy, and BM25 prefix protection.

## Candidate and score contract

- Build at most 50 unique candidates from the temporal BM25 and whole-JD Qwen
  Dense rankings with weighted RRF (`BM25=4`, `Dense=1`, `k=60`).
- Score the largest pool once with the pinned Qwen3-Reranker-8B v7 endpoint.
- Reuse those immutable scores for depths 20, 30, and 50.
- Never send history, labels, qrels, clicks, or applications to the endpoint.
- Preserve hard-filter and 180-day eligibility before candidate generation.

## Compared ranking policies

1. `direct`: reranker rank replaces the candidate-pool order.
2. `rank_fusion`: prior weighted-RRF rank and reranker rank are combined using
   RRF; raw scores are not mixed across queries.
3. `protected_rank_fusion`: the same fusion with BM25 prefix depths 3 or 10 kept
   at their original positions. Protection depth 0 is the requested unprotected
   variant.

The sweep changes one variable at a time: pool depth 20/30/50, BM25 protection
0/3/10, and reranker rank weight. The first-stage weight and endpoint output stay
fixed.

## Promotion rule

Primary selection uses GT1 NDCG@10, then Top-1 and MRR. A production-active
variant must not regress NDCG@10, Precision@10, Top-1, or MRR against the sealed
incumbent. If none passes, production keeps reranking in shadow; the winning
non-regressing fusion code may be retained behind the active gate.

## Serving behavior

Production accepts no unverified fallback. The reranker pool is bounded at the
experiment-selected depth, the response must be a permutation of the request,
and failure is fail-closed. Shadow mode records the challenger trace without
changing results. Active mode uses only the experimentally promoted fusion
policy and appends the untouched suffix.
