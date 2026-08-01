# Runtime Artifact Contract v2 Implementation Plan

**Goal:** Publish one immutable, fail-closed competition runtime bundle that can prove the lineage and promotion state of lexical, dense, optional Graph, and other challenger artifacts before AWS can serve them.

**Architecture:** Keep the existing content-addressed `runtime/<manifest-sha256>/` layout and server-side S3 copy path. Replace the metadata-light v1 manifest with a v2 release contract. Every object remains identified by relative path, byte size, and SHA-256. Promotion validates component lineage first, copies and reads back every object, runs an exact paginated data-only inventory audit, writes `manifest.json` last, then reads back the manifest body, native S3 checksum, and final prefix. A dry run performs the same contract checks but never calls a mutating AWS command.

**Scope:** `packages/contract/runtime-manifest.schema.json`, `scripts/promote_runtime_artifacts.py`, its tests/fixtures, and the operator command. Search runtime and API behavior are explicitly out of scope.

## Contract decisions

- `release.complete` and `release.publication_allowed` are literal `true`; an incomplete bundle cannot be represented as publishable runtime state.
- Whole-job Qwen embeddings are required and pinned to model revision plus the serving parser's exact `document_fields`, `query_prompt`, global job-ID path, and contiguous vector-shard layout. The research/EVA build manifest is provenance input, not a runtime layout manifest. Every serving file must match the root SHA-256/size inventory and remain under the component directory downloaded at startup.
- Multi-view embeddings are optional. When enabled, occupation, skill, requirement, and content are all required at 1024 dimensions; `complete`, `publication_allowed`, MRL evidence, and a body-verified positive NDCG@10 promotion report must all pass.
- Tantivy is required to be a temporal hard-filter index. Its `index_directory`, exact `index_files`, schema fields, field boosts, dynamic request-time mode, 180-day lower bound, and `2026-06-08T23:59:59.999+08:00` Demo reference time are pinned. Future-dated rows remain eligible but receive freshness `0`.
- Graph is optional. When enabled, it must use exactly the approved JD snapshot SHA, exclusive `2026-06-08T00:00:00+08:00` cutoff, observed maximum source timestamp, schema, component inventory, and body-verified positive NDCG@10 promotion evidence.
- Multi-view, Graph, reranker, LTR, and guardrails have explicit enabled flags. Every enabled challenger requires positive promotion evidence; disabled challengers carry only `enabled=false`. Guardrails remain disabled until the serving parser has a matching artifact adapter. Query-neighbor history and behavior priors are not runtime v2 challengers.
- Root artifacts must equal the paths reachable from incumbent/challenger component manifests and evidence reports. Runtime bundles reject credentials, secrets, raw logs, query history, GT/qrels/judgments, test JD, mutable `latest` aliases, and absolute URIs.

## Tasks

1. Add schema fixtures and tests for the valid base release, valid multi-view/Graph release, and each fail-closed gate.
2. Upgrade the manifest builder and source inventory mapping to v2 while preserving the existing server-side copy and checksum verification.
3. Make dry-run fixture execution offline and non-mutating; keep account/region verification mandatory for `--execute`.
4. Run an exact paginated data-only audit after copy, upload the manifest last, then download it and compare exact canonical bytes in addition to native checksum metadata and the final prefix inventory.
5. Document one dry-run command and one explicit `competition/us-west-2` execution command.
6. Run Python tests, schema checks, formatting/lint, infra tests, and a clean diff review before committing.
