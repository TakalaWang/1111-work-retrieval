# Constraint-Aware Evidence Fusion Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Restore the evidence-fusion pipeline on the 180-day eligible universe, require whole/multi-view Dense and a job-specific Qwen reranker, and enforce explicit education and minimum-monthly-salary intent before Top-K.

**Architecture:** Compile free-text queries into preserved lexical text plus deterministic structured constraints. Apply visibility, time, taxonomy, education, and salary filters inside every candidate lane before Top-K; fuse BM25 and whole-Dense evidence, admit multi-view evidence without bypassing constraints, then rerank a bounded pool with an explicitly attested job-search instruction. History/activity may only reorder current, reranker-scored members and must exclude identical normalized queries.

**Tech Stack:** Python 3.12, Tantivy 0.26, NumPy, Qwen3-Embedding-8B, Qwen3-Reranker-8B on SageMaker/vLLM, PostgreSQL, pytest.

---

### Task 1: Repair the reranker instruction contract

**Files:**

- Modify: `scripts/deploy_sagemaker_reranker.py`
- Modify: `tests/test_deploy_sagemaker_reranker.py`
- Modify: legacy experiment `src/temporal_qwen_reranker_ablation.py`
- Test: legacy `tests/test_temporal_qwen_reranker_ablation.py`

1. Add a failing deployment test proving the public rerank request accepts only `model`, `query`, and `documents`; vLLM 0.20.2 does not expose a request-level `instruction` field in `RerankRequest`.
2. Put the immutable job-search instruction directly in the SageMaker chat template and pin its SHA-256 in the model environment and readback evidence.
3. Deploy a separate v7 model, endpoint config, and endpoint so v6 scorers remain available. The v7 endpoint uses the independently available `ml.g5.4xlarge` quota.
4. Run a black-box invariance probe with a 4,600-character top-level `instruction`. Require identical prompt-token usage and ranking, with only a measured `1e-3` numerical-score jitter allowance, proving the field is not part of the variable contract.
5. Measure and pin the exact fixed smoke prompt-token count, then run only the predeclared fixed Top-10 ablation. Never reuse raw-score caches across template policies.

### Task 2: Compile explicit education and salary intent

**Files:**

- Create: `packages/search-core/src/work_retrieval_core/constraints.py`
- Modify: `packages/search-core/src/work_retrieval_core/engine.py`
- Test: `packages/search-core/tests/test_constraints.py`
- Test: `packages/search-core/tests/test_engine.py`

1. Add failing tests for `大學`, `大學學歷`, `碩士`, `月薪五萬`, `月薪50000以上`, and mixed occupation queries.
2. Add ambiguity tests proving university names (`台灣大學`, `大學眼科`), student intent (`大學生`), and job-title salary text are not silently converted into hard constraints.
3. Parse an education seeker level and a minimum monthly salary without removing the original query. Explicit constraints are derived once from the original request into immutable `CandidateRequest` state and appear in the audit trace; the lexical query compiler cannot inject or override them.
4. Reject malformed, impossible, or non-monthly numeric interpretations rather than guessing.

### Task 3: Put constraints in the eligible universe

**Files:**

- Modify: `scripts/tantivy_index_pipeline.py`
- Modify: `packages/search-core/src/work_retrieval_core/adapters.py`
- Modify: `packages/database/src/work_retrieval_database/repository.py`
- Test: `tests/test_tantivy_index_pipeline.py`
- Test: `packages/search-core/tests/test_adapters.py`
- Test: `packages/database/tests/test_repository.py`

1. Add indexed/fast salary and normalized education fields derived only from organizer JD columns.
2. Encode accepted education categories deterministically. For explicit seeker degree, reject higher-only requirements; preserve `不拘`/missing according to a documented corpus rule.
3. Require `salary_min >= requested minimum` for explicit monthly minimum intent; exclude missing, hourly, daily, or non-comparable pay from this strict route.
4. Apply all constraints in Tantivy and Dense eligible-row materialization before Top-K, then revalidate authoritative PostgreSQL metadata on returned candidates.
5. Seal the new schema and parsing policy in the component manifest so old indexes fail closed.

### Task 4: Restore evidence fusion with required Dense lanes

**Files:**

- Modify: `packages/search-core/src/work_retrieval_core/engine.py`
- Modify: `apps/api/src/work_retrieval_api/production.py`
- Test: `packages/search-core/tests/test_engine.py`
- Test: `apps/api/tests/test_runtime.py`

1. Preserve the 180-day BM25 incumbent and exact/all-term evidence.
2. Add whole-Qwen and multi-view MaxSim candidates from the identical eligible universe.
3. Reproduce the predeclared historical score-fusion challenger (`lexical=0.4`, `whole_dense=0.6`) without a new same-data weight sweep; keep per-lane raw ranks and scores in `CandidateEvidence`.
4. Rerank a bounded Top-50 union. No Dense/reranker failure may silently return a differently configured ranking profile.
5. Keep Graph optional and tail-only until its paired gate passes.

### Task 5: Evaluate activity/history and LTR without leakage

**Files:**

- Reuse legacy experiment scripts under `src/`
- Create only the minimum new paired-ablation script if no existing entry point can express the matrix.

1. Freeze the canonical 339 contexts, qrels, as-of policy, candidates, and evaluator before variants run.
2. Evaluate: 180-day BM25; +whole Dense fusion; +protected multi-view; +fixed reranker prompt; +exclude-identical history/activity; +LTR.
3. History/activity cannot add membership, cannot use identical normalized queries, and cannot bypass reranker or eligibility. LTR uses chronological/purged training only.
4. Report NDCG@10, Precision@10, Top-1, MRR, Recall@100, Recall@1000, paired deltas, and lineage. Promote by fixed gate, not by architectural preference.

### Task 6: Release the verified winner

**Files:**

- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/retrieval-pipelines.md`
- Modify: runtime manifest and promotion scripts only for components that passed.

1. Document the selected winner and every disabled negative module.
2. Keep build/import/embedding/index/reranker commands one-click and fail-closed.
3. Run Python, frontend, infra, manifest, and live smoke checks.
4. Deploy only after local and artifact verification, then read back the live manifest and use materially different constraint queries for evidence.
