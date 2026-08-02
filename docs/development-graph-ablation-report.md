# Development Graph Ablation Report

## Scope and interpretation

This is a frozen-input **development proxy**, not the organizer holdout and not an official competition score. The
comparison uses the same 339 frozen contexts, temporal/filter boundary, candidate budget and GT1 evaluation contract
for Graph-off and Graph-on. No raw queries, labels or judgments are included in this report.

The evaluated Graph-on variant uses train-only, evidence-locked skill extraction, normalized aliases and bounded typed
traversal to generate bridge terms. Those terms re-query the current Tantivy eligible universe; historical Graph job
nodes never directly become results.

## Paired six-metric result

| Variant / delta          |   NDCG@10 | Precision@10 |    Top-1 |       MRR | Recall@100 | Recall@1000 |
| ------------------------ | --------: | -----------: | -------: | --------: | ---------: | ----------: |
| Graph-off                |  0.105671 |     0.085170 | 0.133902 |  0.187320 |   0.262792 |    0.431478 |
| Graph-on                 |  0.105515 |     0.084941 | 0.133902 |  0.187278 |   0.261985 |    0.431478 |
| Graph-on minus Graph-off | -0.000157 |    -0.000229 | 0.000000 | -0.000042 |  -0.000807 |    0.000000 |

Graph-on produced 213 candidates absent from Graph-off, but none was observed relevant at rank 1000. It therefore
degraded four metrics, left Top-1 and Recall@1000 unchanged, and supplies no ranking-promotion evidence.

## H>1000 alias-edge diagnostic

A separate train-only alias-edge diagnostic found 7,362 candidates outside each context's BM25 Top-1000, including 66
observed-relevant jobs. The unranked candidate-pool recall over `BM25@1000 ∪ alias candidates` increased from
`0.431478` to `0.435697` (`+0.004219`). All contexts with novel hits and an eligible baseline were already at the
1,000-result capacity, so the baseline-preserving admission rule admitted no candidate: all six admitted-ranking
deltas are `0.000000`.

This is positive deep-candidate evidence, not evidence that Graph can safely replace a ranked BM25 member. The result
supports keeping Graph as an auditable offline/tail-expansion lane while leaving the production Top-10 unchanged.

## Decision

- `promotion_allowed=false`
- `publication_allowed=false`
- Production remains the temporal BM25 incumbent; Graph is not enabled in live ranking.
- An organizer holdout result with positive NDCG@10 and safe candidate admission is required before promotion.

## Reproduction and lineage

The committed one-command entry point is
[`scripts/run_graph_ablation.sh`](../scripts/run_graph_ablation.sh). It rebuilds Graph-off from the declared temporal
Tantivy component, regenerates Graph-on and its trace, checks byte-identical non-Graph inputs, and delegates scoring to
the declared evaluator. The runner does not manufacture judgments or promotion approval.

| Evidence artifact                  | SHA-256                                                            |
| ---------------------------------- | ------------------------------------------------------------------ |
| Formal alias-edge report           | `deceec60742a133d49ebe0d9c9e284f0a022413f87adaf66eca271dea2c9eff4` |
| Typed evidence-locked Graph report | `08a858d84b0edb714a60982b1d1ab9ca890823ad29eb464180de7ba1f95c643a` |
| Typed traversal trace              | `8e6f127ab16479268837640bfba1c36dd216258ebe65ab36ad8b8e0508057258` |
| Alias-edge candidate trace         | `da48bbf99da875b7dbe2184a757222a3de8da767ac209ed86ed457cb6da3c544` |
| Alias-edge decomposition trace     | `087e19139cbdd149622d78ddc8ef082905520b6c78da6a25053063eae69358e7` |

The hashes identify the frozen development evidence without exposing local filesystem locations or evaluation rows.
