# Job Search API Platform Design

The reviewer-facing architecture and data-flow diagrams live in
[`docs/architecture.md`](../../../docs/architecture.md). Benchmark reproducibility and its current
artifact gaps live in [`docs/benchmark.md`](../../../docs/benchmark.md). This specification retains
the normative design decisions behind those documents.

## Architecture

```text
Browser -> CloudFront + WAF -> /api/* -> ALB -> CPU Fargate API -> temporary fake SearchEngine
                              static UI

API startup -> Aurora PostgreSQL -> ten real seed IDs -> close NullPool reader
GPU ECS scaffold (desired 0) -> S3 runtime/<manifest-sha256>/...
SageMaker embedding + reranker endpoints (InService, not integrated into SearchEngine)
```

The API owns transport validation and observability. `search-core` owns only the stable interface
between transport and retrieval. The user-approved temporary engine ignores query content and
returns a stable slice of real Aurora IDs; the Web UI labels that behavior explicitly. Ranking,
normalization, lineage, and corpus audit belong to the future evaluated engine and remain absent.

## Runtime flow

1. Application startup calls the required factory once. It opens a `NullPool` PostgreSQL reader,
   loads ten source-ordered job IDs, closes the reader, and aborts startup on any failure.
2. FastAPI validates and normalizes the request at the trust boundary.
3. The async route invokes the synchronous engine through a worker thread with `limit=10`.
4. The route validates engine output for count, uniqueness, and ASCII-decimal job IDs before
   responding.
5. Shutdown closes the engine once. No alternate engine is selected on failure.

Health reports process liveness. Readiness is true only after successful engine initialization.
Request IDs cross response headers, response envelopes, and structured metadata-only logs.

## Contracts

The committed OpenAPI document is exported from the FastAPI app without initializing an engine.
TypeScript API types are generated from that document. The browser still validates untrusted JSON
at runtime, including result count, numeric IDs, uniqueness, consecutive ranks, and parse failures.
The artifact manifest JSON Schema requires content-addressed runtime assets so a deployment
resolves one immutable set rather than mutable "latest" files.

## Data and infrastructure

`WorkRetrievalData` is the independently deployable persistent data plane. It owns the shared VPC,
the private immutable runtime bucket, the database security group, and Aurora. Aurora stays in
private isolated subnets, uses encrypted storage and Secrets Manager credentials, enables Data API
and S3 import, and can pause at zero ACU. The data-only template keeps the future public subnets but
has no NAT gateway and only the free S3 gateway endpoint. It contains no application-plane or
GitHub federation resources.

`WorkRetrievalPlatform` consumes cross-stack references to the exact VPC, bucket, cluster, and
database security group. Interface endpoints and the ECS-to-database ingress rule are scoped to the
platform stack so deleting or omitting the platform does not leave fixed-cost application support
inside the data plane. The platform never creates a second Aurora cluster or runtime bucket.

`0001_baseline` creates only `alembic_version`; `0002_create_jobs` adds the single authoritative job
snapshot table. Its columns preserve the 39 source fields, exact decimal salary bounds, the source
timestamp, and a unique zero-based `source_row` for import lineage. `job_id` is the only primary
key. No search indexes, normalized children, or unrelated tables are inferred from the snapshot.

SQLAlchemy owns this database model and exposes its metadata to Alembic, while Alembic owns
migration history and Pydantic separately owns HTTP validation. The only current application read
selects seed IDs at startup with `NullPool`; snapshot persistence does not add the future job-detail
endpoint, which waits for a separate approved contract change.

The artifact bucket blocks all public access and enforces encryption. In the platform stack,
CloudFront is the public origin for UI and API traffic; the ALB requires both CloudFront's
origin-facing managed prefix list and a generated origin header. WAF applies AWS managed common
rules. One CPU Fargate task serves the temporary API. ECS also defines an EC2 GPU capacity provider
at zero instances/tasks; the two available G-family endpoint slots are currently used by the
separate SageMaker embedding and reranker endpoints.

## Delivery

CI independently verifies Python, web/contract, PostgreSQL migration, and infrastructure paths.
The deployment workflow is manual and uses GitHub OIDC—never long-lived AWS credentials. The
`production` environment supplies approval, while repository variables and inputs keep deployment
disabled by default. The deploy role can assume only the four standard CDK bootstrap roles for the
default qualifier, account, and region. A full deployment names both stacks and scopes image
and GPU parameters only to `WorkRetrievalPlatform`. Synthesis and tests prove configuration shape
only, not live AWS state.

## Documentation contract

`README.md` is the single entry point for setup, examples, status, lockfiles, and artifact versions.
The architecture document owns cross-module and data-flow explanations; the benchmark document owns
evaluation provenance. Repository acceptance checks are never presented as retrieval metrics, and
contracts or infrastructure definitions are never presented as deployed runtime evidence.
