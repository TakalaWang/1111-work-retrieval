# Job Search API Platform Design

## Architecture

```text
Browser -> CloudFront + WAF -> /api/* -> ALB -> GPU ECS service -> SearchEngine
                              static UI

GPU ECS -> S3 runtime/<manifest-sha256>/...
GPU ECS -> pooled PostgreSQL repository -> Aurora PostgreSQL
```

The API owns transport validation and observability. `search-core` owns only the stable interface
between transport and the future retrieval implementation. Ranking, normalization, lineage, and
corpus audit belong to that future engine and are deliberately absent from this scaffold.

## Runtime flow

1. Application startup calls the required engine and job-repository factories once; initialization
   failure aborts startup and closes resources already acquired.
2. FastAPI validates and normalizes the request at the trust boundary.
3. The async route invokes the synchronous engine through a worker thread with `limit=10`.
4. The route validates engine output for count, uniqueness, and valid job IDs before responding.
5. Detail requests read only persisted records through the injected repository.
6. Shutdown closes both resources once. No alternate engine or data fallback is selected.

Health reports process liveness. Readiness is true only after successful engine initialization.
Request IDs cross response headers, response envelopes, and structured metadata-only logs.

## Contracts

The committed OpenAPI document is exported from the FastAPI app without initializing an engine.
TypeScript API types are generated from that document. The artifact manifest JSON Schema requires
content-addressed runtime assets so a deployment resolves one immutable set rather than mutable
"latest" files.

## Data and infrastructure

Aurora stays in private subnets, uses encrypted storage and Secrets Manager credentials, and
enables Data API for controlled administration. Revision `0002_jobs` adds the single JSONB-backed
job-detail table. SQLAlchemy owns that model and a pooled, bounded read repository; Alembic owns
migration history and Pydantic separately owns HTTP validation. Snapshot ingestion remains outside
the public serving process.

The artifact bucket blocks all public access and enforces encryption. CloudFront is the public
origin for UI and API traffic; the ALB requires both CloudFront's origin-facing managed prefix
list and a generated origin header. WAF applies AWS managed common rules. ECS receives database
credentials from Secrets Manager and uses an EC2 GPU
capacity provider and starts at zero instances/tasks to avoid cost before a real image and
artifacts exist.

## Delivery

CI independently verifies Python, web/contract, PostgreSQL migration, and infrastructure paths.
The deployment workflow is manual and uses GitHub OIDC—never long-lived AWS credentials. The
`production` environment supplies approval, while repository variables and inputs keep deployment
disabled by default. Synthesis and tests prove configuration shape only, not live AWS state.
