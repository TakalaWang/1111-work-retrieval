# Sealed Incumbent Runtime Artifact Alignment Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make production reuse the sealed canonical 15-field EVA whole-job cache, derive deterministic MRL1024 serving shards without mutating the source, and combine it with temporal-v2 Tantivy whose query corrections are disabled by default or enabled only with verified promotion attestation.

**Architecture:** The authoritative whole-job input is the sealed 4096d cache manifest `a02a23655fe8e5cc6b08afde35e93898ff94c62b88bbf7522e09f2c15378715c`: 1,218,635 rows, 122 contiguous shards, 367 inventoried files, and `2026-07-24-clean-v1` canonical 15-field documents including full job content. Materialization validates that source, then performs only deterministic 1024-prefix slicing, float32 L2 renormalization, and float16 sealing into new derived shards whose manifest pins the source manifest, projection policy, row order, and per-shard hashes. Tantivy remains an independently built temporal-v2 component; no default command re-embeds whole jobs.

**Tech Stack:** Python 3.12, pytest, NumPy, Tantivy, JSON Schema, Ruff, mypy.

---

### Task 1: Establish the sealed incumbent and current Tantivy contracts

**Files:**
- Test: `tests/test_materialize_runtime_components.py`

**Steps:**
1. Pin the sealed source manifest SHA, dataset SHA, 1,218,635 rows, 122 shards, 4096 dimensions, clean-v1 policy, and 15 ordered fields.
2. Preserve a small contract fixture with the same sealed manifest/inventory shape for deterministic projection tests.
3. Generate the Tantivy side with the current `tantivy_index_pipeline` output.
4. Add failing assertions for derived-source lineage, temporal-v2 paths, and disabled query corrections.

### Task 2: Materialize a deterministic derived whole component

**Files:**
- Modify: `scripts/materialize_runtime_components.py`
- Test: `tests/test_materialize_runtime_components.py`

**Steps:**
1. Exact-validate the sealed 4096d whole manifest plus every referenced shard/job-ID file against its approved inventory.
2. Verify global job IDs, row order, dataset lineage, contiguous shard indices, source SHA, and source per-shard hashes.
3. Derive new 1024d shards by prefix slice, float32 L2 renormalization, and float16 sealing; never overwrite source files.
4. Emit a serving manifest that pins source manifest SHA, clean-v1 policy, projection policy, row order, and every derived shard SHA.
5. Consume the temporal-v2 Tantivy component/build manifest directly and copy correction candidate as `index` plus attestation as `evidence` only when enabled.
6. Remove v1 Tantivy paths, external taxonomy reconstruction, and mandatory correction input.

### Task 3: Align promotion validation

**Files:**
- Modify: `scripts/promote_runtime_artifacts.py`
- Modify: `tests/test_promote_runtime_artifacts.py`

**Steps:**
1. Replace fixed v1 paths with the clean-v1-derived whole prefix and temporal-v2 paths.
2. Validate the sealed source manifest and the derived whole manifest as distinct immutable lineage artifacts; read the Tantivy component-declared build manifest as provenance.
3. Validate the exact disabled-or-attested correction union and its inventory kinds/checksums.
4. Change materialization lineage from one mandatory correction SHA to the exact correction mode and optional artifact/attestation hashes.
5. Add rejection tests for legacy v1, incomplete enabled corrections, wrong kind/SHA, and invalid promotion attestation.
6. Run focused promotion and runtime contract tests.

### Task 4: Add one reproducible local command

**Files:**
- Create: `scripts/reproduce_runtime_release.sh`
- Modify: `README.md`
- Test: `tests/test_materialize_runtime_components.py`

**Steps:**
1. Add a strict shell wrapper that requires the local/downloaded sealed whole cache, current Tantivy root, output root, source manifest key, and approved hashes.
2. Run materialization followed by offline promotion dry-run only; never upload, deploy, or invoke whole re-embedding.
3. Document the clean-v1 incumbent, deterministic derived MRL1024 output, temporal-v2 inputs, disabled/attested corrections, S3 publication boundary, and fail-closed approvals.
4. Remove the false requirement that production regenerate 34-field whole embeddings.

### Task 5: Verify and commit

**Files:**
- Review all changed files.

**Steps:**
1. Run focused artifact and builder round-trip tests.
2. Run the full Python pytest suite.
3. Run `uv run ruff check .` and `uv run ruff format --check .`.
4. Run `uv run mypy`.
5. Run OpenAPI export and contract generated-file checks.
6. Review `git diff --check`, repository status, and the final requirement checklist.
7. Commit the verified changes on `codex/retrieval-artifacts-v2`.
