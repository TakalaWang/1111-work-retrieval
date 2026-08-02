# Production Domain CDK Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Keep `1111.takalawang.dev` and its existing `us-east-1` ACM certificate attached to the production CloudFront distribution across CDK deployments.

**Architecture:** Import the issued certificate by ARN in the existing single-environment platform stack, configure the CloudFront distribution with the production domain and TLS 1.2 policy, and make deployment outputs use the public hostname. Do not create a second certificate or add runtime parameters for fixed production identity.

**Tech Stack:** AWS CDK v2, TypeScript, Vitest, CloudFront, ACM.

---

### Task 1: Lock the CloudFront contract with a failing assertion

**Files:**

- Modify: `infra/test/platform-stack.test.ts`

**Step 1: Write the failing test**

Assert that the synthesized distribution contains:

```ts
Aliases: ['1111.takalawang.dev'],
ViewerCertificate: {
  AcmCertificateArn:
    'arn:aws:acm:us-east-1:378849533305:certificate/c76499fc-2946-41f4-bc40-3cec2859fffe',
  MinimumProtocolVersion: 'TLSv1.2_2021',
  SslSupportMethod: 'sni-only'
}
```

Also assert `ApiBaseUrl` and `WebUrl` use the production hostname.

**Step 2: Run the test to verify it fails**

Run: `pnpm --dir infra test -- platform-stack.test.ts`

Expected: FAIL because the current template uses the CloudFront default certificate and distribution hostname outputs.

### Task 2: Import the existing certificate and configure CloudFront

**Files:**

- Modify: `infra/lib/platform-stack.ts`

**Step 1: Implement the minimum CDK change**

Import `aws-certificatemanager`, define the fixed production domain and certificate ARN, pass `certificate`, `domainNames`, and `minimumProtocolVersion` to the existing distribution, and emit public URLs from the production hostname.

**Step 2: Run the focused test**

Run: `pnpm --dir infra test -- platform-stack.test.ts`

Expected: PASS.

### Task 3: Verify the infrastructure artifact

**Files:**

- Verify: `infra/lib/platform-stack.ts`
- Verify: `infra/test/platform-stack.test.ts`

**Step 1: Run all infrastructure checks**

Run: `pnpm --dir infra test && pnpm --dir infra build && pnpm --dir infra synth`

Expected: all commands exit 0.

**Step 2: Compare the synthesized distribution with AWS**

Confirm the template and live distribution both contain the same alias, ACM ARN, and TLS policy. Do not deploy if the CDK diff contains unrelated infrastructure changes.
