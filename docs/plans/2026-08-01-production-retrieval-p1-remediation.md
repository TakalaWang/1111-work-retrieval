# Production Retrieval P1 Remediation Implementation Plan

**Status:** Implemented in source; immutable artifacts and deployment remain separate promotion gates.

**Goal:** Make the production search path preserve the approved temporal-BM25 Top 10 while enforcing immutable 1024d dense, lexical, endpoint, and deployment contracts.

**Architecture:** Temporal BM25 remains the only Top-10 incumbent. Dense retrieval is an optional bounded shadow/tail lane, never an equal-RRF override. Runtime startup rejects unsupported enabled challengers and verifies every index, correction, embedding, and SageMaker identity contract before serving.

**Tech Stack:** Python 3.12, Tantivy, NumPy, boto3/SageMaker, FastAPI, AWS CDK, pytest, Vitest.

---

### Task 1: Lock ranking and manifest failures with tests

**Files:**

- Modify: `packages/search-core/tests/test_engine.py`
- Modify: `packages/search-core/tests/test_adapters.py`

1. Add a failing test proving dense cannot reorder a full lexical Top 10.
2. Add failing tests for unsupported enabled Graph/reranker/LTR/guardrails.
3. Add failing tests for 1024d prefix normalization and exact-scan bounds.
4. Run the focused tests and confirm the expected failures.

### Task 2: Implement bounded BM25-incumbent serving

**Files:**

- Modify: `packages/search-core/src/work_retrieval_core/engine.py`
- Modify: `packages/search-core/src/work_retrieval_core/adapters.py`
- Modify: `packages/search-core/src/work_retrieval_core/manifest.py`

1. Make dense optional and shadow/tail-only.
2. Pin promoted whole embeddings to 1024d MRL while accepting 4096d endpoint output only for deterministic prefix normalization.
3. Add non-blocking in-flight, eligible-row, and elapsed-time bounds to exact scan.
4. Reject unsupported enabled challengers.
5. Run focused tests.

### Task 3: Complete lexical and endpoint identity contracts

**Files:**

- Modify: `packages/search-core/src/work_retrieval_core/serialization.py`
- Modify: `packages/search-core/src/work_retrieval_core/adapters.py`
- Modify: `apps/api/src/work_retrieval_api/production.py`
- Modify: `packages/search-core/tests/test_adapters.py`
- Modify: `apps/api/tests/test_production.py`

1. Add all semantically searchable JD fields to the pinned serializer policy.
2. Pin Tantivy tokenizer/source-field/query-correction lineage and validate `meta.json`.
3. Compile original-preserving, train-only corpus corrections and expose their trace.
4. Verify SageMaker endpoint/config/model/container/environment identity before constructing the encoder.
5. Run focused tests.

### Task 4: Make GPU deployment rollable

**Files:**

- Modify: `infra/lib/platform-stack.ts`
- Modify: `infra/test/platform-stack.test.ts`
- Modify: `.github/workflows/deploy.yml`

1. Change the default GPU maximum capacity to two.
2. Require maximum capacity to exceed desired tasks by at least one.
3. Run infra tests and synth.

### Task 5: Verify and commit

1. Run Python tests, Ruff, formatting, strict mypy, OpenAPI check, infra tests/build/synth, and `git diff --check`.
2. Confirm pipeline-agent files were not modified by this change.
3. Commit only the remediation files.
