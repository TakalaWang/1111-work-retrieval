# Production Application Deployment Implementation Plan

> **Required skill:** Use `superpowers:executing-plans` or `superpowers:subagent-driven-development` to implement this plan task by task.

**Goal:** Deploy a usable fake-search API and web application in `competition/us-west-2`, retain a production-ready zero-capacity GPU service, publish the approved runtime artifacts, and configure gated GitHub CD.

**Architecture:** A single API image serves deterministic fake search results backed by real Aurora job IDs and exposes job details from PostgreSQL. A small Fargate service runs that image now; a GPU ECS-on-EC2 service uses the same image and ALB target group but stays at zero until the account's G-family quota is raised. CloudFront serves the SvelteKit static site and forwards API paths to the private-origin ALB. Runtime artifacts live under one immutable, content-addressed S3 prefix.

**Stack:** Python 3.12, FastAPI, SQLAlchemy 2, psycopg 3, SvelteKit, TypeScript, AWS CDK, ECS/Fargate/ECS EC2 GPU, Aurora PostgreSQL, S3, CloudFront, WAF, GitHub Actions OIDC.

---

## Task 1: Publish the runtime artifact bundle

**Files:**

- Modify: `packages/contract/runtime-manifest.schema.json`
- Create: `scripts/promote_runtime_artifacts.py`
- Create: `tests/test_promote_runtime_artifacts.py`

1. Replace circular absolute S3 keys in the manifest with validated relative artifact paths.
2. Add one stdlib promotion script fixed to account `378849533305`, profile `competition`, region `us-west-2`, the approved source prefixes, and the formal data bucket.
3. Copy only the complete job embeddings, Qwen embedding model snapshot, and Tantivy index; exclude SQLite, behavior data, history, rerankers, and experiments.
4. Canonically serialize and hash the manifest, store everything below `runtime/<manifest-sha256>/`, and fail closed on account, checksum, size, or inventory drift.
5. Unit-test manifest canonicalization and path validation, then execute the promotion and read back the destination inventory.

## Task 2: Implement the production API runtime

**Files:**

- Modify: `packages/database/src/work_retrieval_database/`
- Modify: `packages/database/pyproject.toml`
- Modify: `apps/api/src/work_retrieval_api/`
- Modify: `apps/api/pyproject.toml`
- Modify: `apps/api/tests/test_app.py`
- Create: `apps/api/tests/test_runtime.py`
- Create: `apps/api/Dockerfile`
- Create: `.dockerignore`

1. Add the smallest SQLAlchemy repository needed to read jobs by ID and select the first ten real IDs in source order.
2. Add a production environment factory with explicit PostgreSQL settings and no fallback.
3. Make fake search deterministic and return only real Aurora job IDs; keep the existing `SearchEngine` boundary.
4. Add `GET /api/v1/jobs/{job_id}` returning all 39 source fields, with decimal strings, ISO timestamps, request IDs, and fail-closed 404/503 envelopes.
5. Add the ASGI entrypoint and a Python 3.12 container image, then test the runtime, API contract, and container build.

## Task 3: Update the committed contract and web UI

**Files:**

- Modify: `packages/contract/openapi.json`
- Modify: `packages/contract/types.d.ts`
- Modify: `apps/web/src/lib/search.ts`
- Modify: `apps/web/src/lib/search.test.ts`
- Modify: `apps/web/src/routes/+page.svelte`

1. Export the updated OpenAPI document and regenerate committed TypeScript types.
2. Add a typed job-detail client.
3. Show that search ranking is temporary, render result IDs, and allow loading complete job details.
4. Cover success, empty, and request-ID-bearing error states; run Svelte checks, tests, and a production build.

## Task 4: Make the application infrastructure deployable

**Files:**

- Modify: `infra/lib/data-stack.ts`
- Modify: `infra/lib/platform-stack.ts`
- Modify: `infra/bin/platform.ts`
- Modify: `infra/test/data-stack.test.ts`
- Modify: `infra/test/platform-stack.test.ts`

1. Put the persistent API ECR repository in the data stack to break the image/bootstrap cycle.
2. Add an always-on Fargate fake-API service and retain the GPU ECS-on-EC2 service at zero capacity.
3. Use the current ECS-optimized Amazon Linux 2023 GPU image, inject Aurora credentials through ECS secrets, add startup grace, rate limiting, alarms, and useful deployment outputs.
4. Create GitHub OIDC trust with the immutable repository subject and grant only deployment plus ECR publishing permissions.
5. Preserve CloudFront same-origin `/api/*`, private ALB origin restrictions, WAF, private data resources, and explicit capacity parameters; validate with CDK assertions and synth.

## Task 5: Configure CD and deploy production

**Files:**

- Modify: `.github/workflows/deploy.yml`
- Modify: `README.md`
- Modify: `CONTRIBUTING.md`
- Modify: `.env.example`

1. Make manual CD build and push the API image, verify its digest/scan result, build the web app, deploy both stacks, upload the site, invalidate CloudFront, and run smoke checks.
2. Require `DEPLOY_ENABLED=true`, explicit confirmation, the production GitHub environment, artifact SHA, and capacity inputs; default to Fargate `1` and GPU `0/0/0` while the quota is blocked.
3. Deploy the data-stack ECR bootstrap, build/push the image, deploy the platform stack, and upload the web application with profile `competition` in `us-west-2`.
4. Create/read back the GitHub production environment and variables, then exercise the manual workflow from `main` when permitted.
5. Verify CloudFormation, ECS tasks/targets, CloudFront `/healthz`, `/readyz`, fake search, job details, web rendering, runtime S3 inventory, and AWS account/region.

## Task 6: Final verification and integration

1. Run frozen installs, all Python/Node tests, lint, strict type checks, generated-contract checks, web production build, CDK tests, and synth.
2. Review the full diff for secrets, SQLite/fallback paths, legacy aliases, and accidental experiment/data inclusion.
3. Commit intentionally, push the feature branch, merge only after the checks pass, and verify the merged remote state.
4. Report local code, runtime artifacts, AWS stacks, image digest, ECS/ALB/CloudFront, GitHub CD state, API endpoint, web URL, and the remaining G-family quota blocker separately.
