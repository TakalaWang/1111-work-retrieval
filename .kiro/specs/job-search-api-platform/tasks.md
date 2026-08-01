# Job Search API Platform Tasks

## Scaffold acceptance

- [x] Define the Python 3.12 uv workspace and immutable `SearchEngine` boundary.
- [x] Define FastAPI validation, response/error envelopes, lifecycle, logging, and OpenAPI export.
- [x] Commit the OpenAPI contract, generated TypeScript types, and runtime manifest schema.
- [x] Add the thin SvelteKit request and result states without a mock runtime.
- [x] Add PostgreSQL-only revision `0001_baseline` without domain tables.
- [x] Establish a SQLAlchemy declarative base for PostgreSQL domain models and connect its metadata
      to Alembic.
- [x] Define the authoritative 39-field `jobs` model and `0002_create_jobs` migration with exact
      salary decimals and source-row lineage, without speculative indexes or child tables.
- [x] Define the Aurora, S3, ECR, GPU ECS, ALB, CloudFront, WAF, logging, and OIDC CDK skeleton.
- [x] Add parallel CI and guarded manual-only production delivery.
- [x] Enforce ranked numeric job-ID invariants and malformed-JSON rejection at API/browser
      boundaries.
- [x] Restrict GitHub OIDC assumption to the four standard CDK bootstrap roles.
- [x] Publish a reviewer-facing README plus canonical architecture, data-flow, and benchmark
      reproducibility documents with explicit implementation and deployment boundaries.

The scaffold is accepted only when a fresh checkout passes frozen installs, formatting, lint,
strict type checking, tests, contract drift checks, two PostgreSQL 16 upgrades, `alembic check`,
web build, CDK tests, and CDK synthesis without generated Git differences.

## Next implementation lanes

These are intentionally not part of the scaffold and require their own approved specifications:

- [ ] Implement and evaluate a production `SearchEngine`, including normalization, ranking,
      lineage, and final corpus audit.
- [x] Import and read back the verified complete job snapshot in Aurora PostgreSQL.
- [ ] Define the runtime session lifecycle only when a consuming application path is approved.
- [ ] Specify and implement the future job-detail-by-ID API as a separate contract change.
- [ ] Build and publish the API/GPU image and immutable runtime manifest.
- [ ] Establish latency, relevance, availability, and cost gates before setting GPU capacity above
      zero or Aurora minimum capacity above zero.
- [ ] Add a versioned evaluation set and one committed benchmark runner after the production engine,
      model, index, and runtime manifest exist; publish no retrieval metrics before then.
- [ ] Configure GitHub production reviewers and repository variables, then perform and verify the
      first controlled rollout.

Do not satisfy these tasks with SQLite, test doubles in runtime code, legacy request aliases,
experimental code copied from another repository, or an unverified fallback path.
