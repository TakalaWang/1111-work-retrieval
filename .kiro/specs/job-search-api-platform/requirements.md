# Job Search API Platform Requirements

This document is authoritative for the production scaffold. Experimental retrieval behavior is
out of scope until it is promoted through a separate, evidence-backed change.

## R1. Stable search boundary

- `packages/search-core` exposes immutable `SearchQuery` data and a synchronous `SearchEngine`
  protocol with `search(query, limit)` and `close()`.
- No concrete engine, in-memory implementation, runtime mock, or automatic fallback ships in
  production code.
- API startup fails when its explicitly supplied engine factory cannot initialize required
  artifacts.

## R2. HTTP contract

- `POST /api/v1/jobs/search` accepts `query`, optional `location_code`, and optional `duty_code`.
- Query text is trimmed, non-empty, and at most 512 characters. Filters are non-null string arrays;
  values are trimmed, non-empty, de-duplicated, and retain first-seen order.
- Unknown fields and legacy aliases are rejected. JSON bodies are limited to 16 KiB and other
  media types return `415`.
- Successful responses contain `request_id` and at most ten unique, consecutively ranked job IDs.
- Search-result and detail job IDs are non-empty ASCII decimal strings.
- `GET /api/v1/job-details/{job_id}` reads persisted details. The public API has no import, upsert,
  or request-time CSV path.
- Every failure uses the shared error envelope and does not expose exceptions, SQL, or paths.
- Access logs record metadata and latency, never query text.
- `GET /healthz`, `GET /readyz`, and `GET /openapi.json` remain available.

## R3. Contract-first consumers

- The repository commits deterministic OpenAPI JSON and generated TypeScript types.
- The SvelteKit UI calls relative search and persisted-detail paths and renders loading, results,
  partial detail failures, empty results, and request-ID-bearing failures.
- CI rejects stale OpenAPI or generated TypeScript output. No mock server is a supported runtime.

## R4. PostgreSQL and artifacts

- PostgreSQL 16/Aurora PostgreSQL is the only relational database; SQLite is forbidden.
- SQLAlchemy owns PostgreSQL domain models, Alembic owns migrations, and Pydantic owns the HTTP
  contract; model classes are not shared across those boundaries.
- The `jobs` table stores a numeric text primary key, JSONB source details, and server-owned
  creation/update timestamps. SQLAlchemy metadata must remain drift-free with Alembic.
- The initial Alembic revision establishes version history; revision `0002_jobs` creates `jobs`.
- The pooled read repository uses psycopg, pre-ping, and bounded connection/query timeouts.
- Bulk source ingestion is a separate controlled operator workflow and is not implemented as a
  public serving fallback.
- Runtime models, embeddings, and indexes are private immutable S3 objects under
  `runtime/<manifest-sha256>/...`, described by the committed manifest schema.

## R5. AWS platform

- CDK defines private, encrypted Aurora Serverless v2 with Secrets Manager and Data API; scaling is
  0–4 ACU with a ten-minute auto-pause.
- CDK defines a private artifact bucket, ECR, GPU ECS on EC2 capacity, ALB, CloudFront `/api/*`
  routing, WAF, CloudWatch, and a least-privilege GitHub OIDC role.
- ECS receives database connection coordinates and Secrets Manager-backed credentials.
- ALB ingress accepts only the CloudFront origin-facing managed prefix list.
- GPU desired capacity defaults to zero. Image URI, artifact manifest SHA-256, and GPU instance type
  are required deployment inputs.

## R6. Delivery safety

- Pull requests and pushes may run CI, but pushes never deploy.
- Deployment is available only through `workflow_dispatch`, the protected `production`
  environment, `DEPLOY_ENABLED=true`, exact human confirmation, and validated immutable inputs.
- A scaffold, successful build, CDK synthesis, or merged change must not be described as deployed.
