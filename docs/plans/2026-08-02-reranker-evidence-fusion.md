# Reranker Evidence Fusion Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Measure and implement a rank-aware Qwen reranker fusion, including an explicit BM25 protection-off ablation, without promoting a regressing variant.

**Architecture:** One sealed Top-50 Qwen v7 score cache feeds an offline grid over pool depth, prior/reranker rank weights, and BM25 protected-prefix depth. The selected pure rank-fusion policy is implemented in `search-core`; runtime remains shadow unless every promotion guard passes.

**Tech Stack:** Python 3.12, NumPy, DuckDB, boto3/SageMaker, pytest, uv.

---

### Task 1: Build the sealed fusion ablation

**Files:**
- Create: `/Users/takala/code/1111 work retrieval/src/temporal_reranker_fusion_ablation.py`
- Create: `/Users/takala/code/1111 work retrieval/tests/test_temporal_reranker_fusion_ablation.py`

1. Write failing tests for weighted-RRF pool membership, deterministic rank fusion, prefix depths 0/1/3/10, fixed suffix membership, and invalid score masks.
2. Run `uv run pytest tests/test_temporal_reranker_fusion_ablation.py -q` and verify failure.
3. Implement the minimum pure ranking functions and a resumable v7 Top-50 scoring cache with frozen input and endpoint lineage hashes.
4. Run the focused test and `uv run python src/temporal_reranker_fusion_ablation.py self-check`.

### Task 2: Score once and evaluate the grid

**Files:**
- Create: `/Users/takala/code/1111 work retrieval/artifacts/experiments/temporal-reranker-fusion-v7/*`
- Modify: `/Users/takala/code/1111 work retrieval/docs/experiments/2026-08-01-ppt-evidence-ledger.md`

1. Run Top-50 scoring against `work-retrieval-qwen3-reranker-8b-v7`; resume safely after interruption.
2. Evaluate pool depths 20/30/50, BM25 protection 0/1/3/10, direct replacement, and rank-fusion weights over identical cached scores.
3. Write one immutable JSON report containing all metrics, paired confidence intervals, latency, input hashes, output hashes, and the selected/promotion decision.
4. Record the result and negative findings in the evidence ledger.

### Task 3: Implement the selected production policy

**Files:**
- Modify: `packages/search-core/src/work_retrieval_core/engine.py`
- Modify: `packages/search-core/tests/test_engine.py`

1. Write failing unit tests for the exact winning pool depth, weights, prefix protection, deterministic ties, suffix preservation, shadow behavior, and fail-closed active behavior.
2. Run `uv run pytest packages/search-core/tests/test_engine.py -q` and verify failure.
3. Add the smallest pure rank-fusion helper. Keep Qwen suitability, train-only
   position-adjusted popularity, and deterministic JD completeness as separate
   typed signals.
4. Require hard-constraint agreement and either occupation/title agreement or
   BM25+Dense cross-modal support before a candidate may be promoted. Graph or
   multi-view skill-only similarity may remain tail evidence but cannot promote a
   different occupation.
5. Keep active serving disabled unless the sealed report passes every promotion gate.
6. Run the focused tests.

### Task 4: Verify and document

**Files:**
- Modify: `docs/retrieval-pipelines.md`
- Modify: `README.md`

1. Document the measured candidate depth, fusion rule, BM25 protection decision, latency, and active/shadow status.
2. Run `uv run pytest -q`, formatting, type checking, web tests, and infrastructure tests.
3. Review the diff for experiment-only artifacts or secrets; none may enter production.
4. Commit the production change only after spec and code-quality review.

### Task 5: Audit competition repository completeness

**Files:**
- Modify only if required: `README.md`
- Modify only if required: `docs/retrieval-pipelines.md`

1. Verify source code, environment setup, runnable examples, one-command benchmark
   reproduction, and data/model/index version lineage against real paths.
2. Verify dependency lockfiles for every runtime.
3. Verify architecture and data-flow documentation.
4. Verify Graph-on versus Graph-off metrics and their reproduction command.
5. Fix only evidenced P0/P1 gaps and rerun every documented command.
