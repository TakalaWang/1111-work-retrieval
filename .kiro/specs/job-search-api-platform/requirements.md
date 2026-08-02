# Job Search API Platform Requirements

This document is authoritative for the production scaffold. Experimental retrieval behavior is
out of scope until it is promoted through a separate, evidence-backed change.

## R1. Stable search boundary

- `packages/search-core` exposes immutable `SearchQuery` data and a synchronous `SearchEngine`
  protocol with `search(query, limit)` and `close()`.
- Until the evaluated production engine is approved, the explicitly temporary runtime may return
  the first ten real Aurora job IDs for every valid query. It must be labeled as fake search in the
  UI and documentation, must not claim retrieval quality, and must not become an automatic fallback.
- API startup fails when its explicitly supplied factory cannot connect to PostgreSQL or load ten
  valid seed IDs. Test doubles remain confined to tests.

## R2. HTTP contract

- `POST /api/v1/jobs/search` accepts `query`, optional `location_code`, and optional `duty_code`.
- Query text is trimmed, non-empty, and at most 512 characters. Filters are non-null string arrays;
  values are trimmed, non-empty, de-duplicated, and retain first-seen order.
- Unknown fields and legacy aliases are rejected. JSON bodies are limited to 16 KiB and other
  media types return `415`.
- Successful responses contain `request_id` and at most ten unique, consecutively ranked ASCII
  decimal job IDs from the authoritative snapshot.
- Every failure uses the shared error envelope and does not expose exceptions, SQL, or paths.
- Access logs record metadata and latency, never query text.
- `GET /healthz`, `GET /readyz`, and `GET /openapi.json` remain available.

## R3. Contract-first consumers

- The repository commits deterministic OpenAPI JSON and generated TypeScript types.
- The SvelteKit UI calls the relative `/api/v1/jobs/search` path and renders loading, results,
  empty results, and request-ID-bearing failures.
- The browser rejects invalid JSON and any successful response that violates the numeric job-ID,
  result-count, uniqueness, or consecutive-rank invariants.
- CI rejects stale OpenAPI or generated TypeScript output. No mock server is a supported runtime.

## R4. PostgreSQL and artifacts

- PostgreSQL 16/Aurora PostgreSQL is the only relational database; SQLite is forbidden.
- SQLAlchemy owns PostgreSQL domain models, Alembic owns migrations, and Pydantic owns the HTTP
  contract; model classes are not shared across those boundaries.
- The `jobs` table owns the authoritative 39-field source snapshot. `job_id` is its text primary
  key and zero-based `source_row` is unique, non-null ingestion lineage.
- Source text uses PostgreSQL `TEXT` without guessed length limits. `salary_min` and `salary_max`
  use nullable `NUMERIC(12,2)` so source decimals remain exact; `source_modified_at` is a naive
  source timestamp. Fields proven complete by the source audit are non-null and all other source
  fields remain nullable.
- Revision `0001_baseline` establishes version history; `0002_create_jobs` creates only `jobs`, its
  primary key, and source-row uniqueness. No speculative lookup indexes or normalized child tables
  are included. The temporary runtime uses a `NullPool` SQLAlchemy reader only during startup to
  load ten seed IDs, then immediately closes it so Aurora auto-pause is not blocked.
- Loading a verified snapshot into `jobs` does not add the future job-detail API. That API requires
  a separate contract and implementation change.
- Runtime models, embeddings, and indexes are private immutable S3 objects under
  `runtime/<manifest-sha256>/...`, described by the committed manifest schema.

## R5. AWS platform

- `WorkRetrievalData` owns the shared VPC, private versioned artifact bucket, database security
  group, and private encrypted Aurora Serverless v2 with Secrets Manager, Data API, S3 import,
  0–4 ACU scaling, and a ten-minute auto-pause.
- Deploying `WorkRetrievalData` alone creates no NAT gateway or running application-plane resources.
  Its only VPC endpoint is the free S3 gateway endpoint. It owns the retained ECR repository needed
  to break the initial image/deployment cycle, but creates no interface endpoints, ALB, CloudFront,
  WAF, ECS, Auto Scaling group, web bucket, application logs, or GitHub OIDC role.
- `WorkRetrievalPlatform` reuses the exact VPC, artifact bucket, Aurora cluster, and database
  security group from `WorkRetrievalData`; it never creates a second database or artifact bucket.
  It owns the active CPU Fargate API service, zero-capacity GPU ECS on EC2 scaffold, interface
  endpoints, ALB, CloudFront `/api/*` routing, WAF, CloudWatch, the web bucket, and the
  least-privilege GitHub OIDC role.
- The GitHub OIDC role may assume only the standard deploy, file-publishing, image-publishing, and
  lookup roles for the default CDK bootstrap qualifier, account, and region; `cdk-*` is
  forbidden.
- ALB ingress accepts only the CloudFront origin-facing managed prefix list.
- CPU desired capacity defaults to one; GPU min/max/desired capacities remain zero. Image URI,
  artifact manifest SHA-256, and GPU instance type are required deployment inputs.

## R6. Delivery safety

- Pull requests run CI; pushes to `main`, including merged pull requests, also deploy automatically.
- Deployment is available only from `main` through the protected `production` environment,
  `DEPLOY_ENABLED=true`, and a validated immutable runtime manifest. `workflow_dispatch` remains
  available for intentional redeployment and additionally requires exact human confirmation.
- The full deployment workflow explicitly deploys `WorkRetrievalData` and then
  `WorkRetrievalPlatform`; application parameters are scoped only to `WorkRetrievalPlatform`.
- A scaffold, successful build, CDK synthesis, or merged change must not be described as deployed.

## R7. Repository documentation and reproducibility

- `README.md` is the reviewer entry point and states environment setup, executable examples,
  dependency lockfiles, delivery status, and data/model/index versions without implying absent
  runtime capabilities.
- `docs/architecture.md` is the canonical overview of module ownership, target request flow,
  verified job import flow, contract generation, infrastructure ownership, and trust boundaries.
- `docs/benchmark.md` separates reproducible repository acceptance from retrieval evaluation. It
  must not publish a benchmark command or metrics until the production engine, versioned evaluation
  set, model, index, runtime manifest, and committed runner exist.
- Every future retrieval result records source commit, dependency locks, corpus and evaluation-set
  checksums, runtime artifact checksums, retrieval configuration, execution parameters, hardware,
  and metric definitions.
