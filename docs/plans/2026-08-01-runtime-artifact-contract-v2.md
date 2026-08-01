# Runtime Artifact Contract v2 Implementation Plan

**Goal:** Publish one immutable, fail-closed competition runtime bundle that can prove the lineage and promotion state of lexical, dense, optional Graph, and other challenger artifacts before AWS can serve them.

**Architecture:** Keep the existing content-addressed `runtime/<manifest-sha256>/` layout and server-side S3 copy path. Replace the metadata-light v1 manifest with a v2 release contract. Every object remains identified by relative path, byte size, and SHA-256. Promotion validates component lineage first, copies and reads back every object, writes `manifest.json` last, then reads back the manifest body and native S3 checksum. A dry run performs the same contract checks but never calls a mutating AWS command.

**Scope:** `packages/contract/runtime-manifest.schema.json`, `scripts/promote_runtime_artifacts.py`, its tests/fixtures, and the operator command. Search runtime and API behavior are explicitly out of scope.

## Contract decisions

- `release.complete` and `release.publication_allowed` are literal `true`; an incomplete bundle cannot be represented as publishable runtime state.
- Whole-job Qwen embeddings are required and pinned to model revision, tokenizer, model-contract, view-policy, and component-manifest hashes.
- Multi-view embeddings are optional. When enabled, occupation, skill, requirement, and content are all required at 1024 dimensions; `complete`, `publication_allowed`, and MRL acceptance evidence must all pass.
- Tantivy is required to be a temporal hard-filter index. Its component manifest and index-content hashes, dynamic request-time mode, 180-day eligibility window, and `2026-06-08T23:59:59.999+08:00` Demo reference time are pinned.
- Graph is optional. When enabled, it must be train-only, declare an exclusive cutoff and schema, identify its component manifest, and carry accepted promotion evidence.
- Every other challenger has an explicit enabled flag. Enabled challengers require accepted evidence; disabled challengers cannot carry misleading acceptance evidence.
- Runtime manifests contain no bucket names, credentials, endpoint secrets, mutable `latest` aliases, or absolute URIs.

## Tasks

1. Add schema fixtures and tests for the valid base release, valid multi-view/Graph release, and each fail-closed gate.
2. Upgrade the manifest builder and source inventory mapping to v2 while preserving the existing server-side copy and checksum verification.
3. Make dry-run fixture execution offline and non-mutating; keep account/region verification mandatory for `--execute`.
4. Upload the manifest only after all objects pass readback, then download it and compare exact canonical bytes in addition to HEAD checksum metadata.
5. Document one dry-run command and one explicit `competition/us-west-2` execution command.
6. Run Python tests, schema checks, formatting/lint, infra tests, and a clean diff review before committing.
