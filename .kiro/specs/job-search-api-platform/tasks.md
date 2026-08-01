# Job Search API Platform Tasks

## Scaffold acceptance

- [x] Define the Python 3.12 uv workspace and immutable `SearchEngine` boundary.
- [x] Define FastAPI validation, response/error envelopes, lifecycle, logging, and OpenAPI export.
- [x] Commit the OpenAPI contract, generated TypeScript types, and runtime manifest schema.
- [x] Add the thin SvelteKit request and result states without a mock runtime.
- [x] Add a PostgreSQL-only Alembic baseline and the `jobs` table migration.
- [x] Keep the SQLAlchemy `Job` model, pooled read repository, and Alembic metadata drift-free.
- [x] Expose persisted job details through a read-only API and keep ingestion out of HTTP serving.
- [x] Define the Aurora, S3, ECR, GPU ECS, ALB, CloudFront, WAF, logging, and OIDC CDK skeleton.
- [x] Add parallel CI and guarded manual-only production delivery.

The scaffold is accepted only when a fresh checkout passes frozen installs, lint, strict type
checking, tests, contract drift checks, two PostgreSQL 16 upgrades, web build, CDK tests, and CDK
synthesis without generated Git differences.

## Next implementation lanes

These are intentionally not part of the scaffold and require their own approved specifications:

- [ ] Implement and evaluate a production `SearchEngine`, including normalization, ranking,
      lineage, and final corpus audit.
- [ ] Define and verify the controlled bulk snapshot-ingestion operator workflow.
- [ ] Build and publish the API/GPU image and immutable runtime manifest.
- [ ] Establish latency, relevance, availability, and cost gates before setting GPU capacity above
      zero or Aurora minimum capacity above zero.
- [ ] Configure GitHub production reviewers and repository variables, then perform and verify the
      first controlled rollout.

Do not satisfy these tasks with SQLite, test doubles in runtime code, legacy request aliases,
experimental code copied from another repository, or an unverified fallback path.
