# Job Search API Platform Tasks

## Scaffold acceptance

- [x] Define the Python 3.12 uv workspace and immutable `SearchEngine` boundary.
- [x] Define FastAPI validation, response/error envelopes, lifecycle, logging, and OpenAPI export.
- [x] Commit the OpenAPI contract, generated TypeScript types, and runtime manifest schema.
- [x] Add the thin SvelteKit request and result states without a browser mock server.
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
- [x] Add the approved temporary fake SearchEngine using ten real Aurora IDs, with explicit UI
      disclosure and no automatic fallback.
- [x] Define the startup-only PostgreSQL reader lifecycle with `NullPool` and immediate disposal.
- [ ] Specify and implement the future job-detail-by-ID API as a separate contract change.
- [x] Build and publish the API image and immutable runtime manifest.
- [ ] Establish latency, relevance, availability, and cost gates before setting GPU capacity above
      zero or Aurora minimum capacity above zero.
- [ ] Add a versioned evaluation set and one committed benchmark runner after the production engine,
      model, index, and runtime manifest exist; publish no retrieval metrics before then.
- [x] Configure the production environment, repository variables, main-only policy, and perform a
      verified controlled rollout. Required reviewers remain unavailable on the current GitHub plan.

Do not satisfy these tasks with SQLite, unapproved runtime doubles, legacy request aliases,
experimental code copied from another repository, or an unverified fallback path. The temporary
fake SearchEngine above is the sole approved exception and must be replaced, not retained as a
fallback, when the evaluated production engine is integrated.
