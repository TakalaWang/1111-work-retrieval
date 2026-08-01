# CPU Incumbent Production Profile Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Serve the promoted temporal BM25 incumbent on an explicit CPU Fargate profile while retaining the GPU service as a separately selected shadow profile.

**Architecture:** A single `ComputeProfile` CloudFormation parameter derives mutually exclusive CPU and GPU capacities. The incumbent profile runs one 2-vCPU/16-GiB Fargate task with no GPU resource requirement; the GPU shadow profile retains the existing EC2 GPU task. Both services register with the same ALB target group, but only the selected profile has non-zero desired capacity. Dense and multi-view flags stay false and the immutable manifest keeps Graph, LTR, and reranker disabled.

**Tech Stack:** AWS CDK, ECS Fargate, ECS EC2, ALB, GitHub Actions, TypeScript, Vitest.

---

### Task 1: Make compute profiles explicit and mutually exclusive

**Files:**

- Modify: `infra/lib/platform-stack.ts`
- Test: `infra/test/platform-stack.test.ts`

1. Replace raw CPU/GPU capacity parameters with a `ComputeProfile` parameter allowing only `cpu-incumbent` and `gpu-shadow`.
2. Write synthesis assertions for profile-derived desired/min/max capacities and verify the test fails against the old stack.
3. Derive CPU desired `1` and GPU capacity `0/0/0` for the incumbent; invert to `0` and `1/2/1` for the shadow profile.
4. Size Fargate to 2 vCPU/16 GiB, give it the same startup grace, and register both services in the ALB target group.
5. Verify the CPU task has no GPU resource requirement and both tasks keep all unpromoted feature flags off.

### Task 2: Enforce the profile in deployment automation

**Files:**

- Modify: `.github/workflows/deploy.yml`
- Modify: `scripts/bootstrap_competition_release.sh`
- Test: `tests/test_promote_runtime_artifacts.py`

1. Replace the four caller-controlled capacity inputs with an exact `compute_profile` choice defaulting to `cpu-incumbent`.
2. Pass only `ComputeProfile` and the optional GPU instance type to CDK.
3. Make the competition bootstrap request `cpu-incumbent` explicitly.
4. Assert the workflow has no legacy count inputs and still requires immutable manifest validation, digest-pinned image scan, and exact readiness identity.

### Task 3: Document and release

**Files:**

- Modify: `README.md`
- Modify: `docs/architecture.md`

1. Document CPU incumbent as the default promoted hot path and GPU shadow as explicit opt-in only.
2. Run formatting, lint, TypeScript, infra, Python, contract, web, and shell checks.
3. Push a focused PR, wait for all CI jobs, merge, and deploy `cpu-incumbent` with the approved manifest SHA.
4. Read back main SHA, ECR digest and scan, CloudFormation, Fargate service/task, target health, `/healthz`, `/readyz`, UI, two distinct query rankings, and one job detail.
