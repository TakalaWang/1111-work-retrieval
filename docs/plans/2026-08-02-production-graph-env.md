# Production Graph Environment Toggle Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Allow the production API to enable the promoted Skill Graph with `SEARCH_ENABLE_GRAPH=true` while failing startup when its immutable artifacts or adapter contract are incomplete.

**Architecture:** Extend the existing runtime manifest and S3 materializer to admit a promoted Skill Graph component. Wrap the incumbent Tantivy retriever with the already-specified bounded, one-hop Graph bridge algorithm only when the strict environment flag is true; otherwise preserve the current BM25 path byte-for-byte. Pass the flag to ECS through an explicit CDK parameter sourced from the protected GitHub production environment.

**Tech Stack:** Python 3.12, Tantivy, immutable JSONL artifacts, pytest, AWS CDK, Vitest, GitHub Actions.

---

### Task 1: Make Graph artifacts an explicit runtime contract

**Files:**

- Modify: `packages/search-core/src/work_retrieval_core/manifest.py`
- Modify: `packages/search-core/src/work_retrieval_core/artifacts.py`
- Modify: `packages/contract/runtime-manifest.schema.json`
- Modify: `scripts/promote_runtime_artifacts.py`
- Modify: `scripts/skill_graph_pipeline.py`
- Modify: `scripts/graph_candidate_runner.py`
- Modify: `scripts/graph_ablation_runner.py`
- Create: `packages/search-core/src/work_retrieval_core/graph_policy.py`
- Test: `packages/search-core/tests/test_manifest.py`
- Test: `packages/search-core/tests/test_artifacts.py`
- Test: `tests/test_promote_runtime_artifacts.py`

1. Add failing tests for an enabled, publishable Graph component and selected Graph artifacts.
2. Verify the tests fail because Graph is hard-disabled.
3. Parse the existing `skillGraph` schema and include its immutable prefix only when requested.
4. Add a deterministic approval step that requires an external organizer attestation and emits the
   exact six-file production component without rewriting the research Graph.
5. Bind offline evaluation, runtime manifests and serving to one canonical algorithm/policy SHA.
6. Keep positive promotion evidence mandatory and all other unsupported challengers disabled.
7. Run the targeted manifest, materialization and promotion tests.

### Task 2: Serve the bounded Graph-conditioned retriever

**Files:**

- Create: `packages/search-core/src/work_retrieval_core/graph.py`
- Modify: `packages/search-core/src/work_retrieval_core/adapters.py`
- Modify: `apps/api/src/work_retrieval_api/production.py`
- Test: `packages/search-core/tests/test_graph.py`

1. Add a failing fixture covering exact query anchors, one typed hop, temporal/filter-preserving Tantivy re-query, protected baseline Top-3 and bounded novel admission.
2. Implement the smallest reusable Graph index and retriever wrapper from the committed offline algorithm.
3. Validate Graph component paths and JSONL rows before serving.
4. Add an offline-versus-production golden ranking parity test using the shared tokenizer and policy.
5. Ensure malformed Graph data closes the wrapped retriever and fails startup.
6. Run the targeted adapter tests.

### Task 3: Add the strict environment flag

**Files:**

- Modify: `apps/api/src/work_retrieval_api/runtime.py`
- Modify: `apps/api/tests/test_runtime.py`
- Modify: `.env.example`

1. Add failing tests for default-off, `true`, invalid values and enabled-with-disabled-manifest startup failure.
2. Parse only literal `true` or `false` as `SEARCH_ENABLE_GRAPH`.
3. Materialize Graph artifacts and select the Graph adapter only when true.
4. Add the default-off example without credentials or fallback behavior.
5. Run API runtime tests.

### Task 4: Inject and verify the production setting

**Files:**

- Modify: `infra/lib/platform-stack.ts`
- Modify: `infra/test/platform-stack.test.ts`
- Modify: `.github/workflows/deploy.yml`
- Modify: `README.md`

1. Add failing CDK assertions for a strict `EnableGraph` parameter and ECS `SEARCH_ENABLE_GRAPH` environment value.
2. Validate the protected GitHub environment variable and pass it to CDK.
3. Document that the current immutable runtime has Graph disabled and that enabling it requires a promoted Graph bundle.
4. Run frozen installs, formatting, lint, strict type checks, Python/Node tests, contract drift checks, builds and CDK synth.
5. Commit, push, open a PR, wait for CI and merge only if all checks pass.
