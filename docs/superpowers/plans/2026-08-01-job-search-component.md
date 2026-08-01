# Job Search Component Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a compact SvelteKit search component that resolves ranked search IDs through ten concurrent pull requests and renders useful CSV-backed job details.

**Architecture:** Keep network contracts and orchestration in `search.ts`, with generated OpenAPI types and runtime guards at both API boundaries. Keep `+page.svelte` responsible only for query state, user-visible progress, errors, partial-failure notices, and accessible result rendering.

**Tech Stack:** SvelteKit 2, Svelte 5 runes, TypeScript via JSDoc, Vitest 4, generated OpenAPI TypeScript types, pnpm 10.28.0, Node.js 24.

## Global Constraints

- The interface is one conventional search field plus results, not a complete site or dashboard.
- `POST /api/v1/jobs/search` receives a trimmed natural-language `query` with empty filter arrays.
- `POST /api/v1/jobs/pull` is called once per result with `{ "job_id": "..." }`.
- Pull calls run concurrently with `Promise.allSettled`; successful rows retain search rank order.
- Render only populated, user-relevant CSV fields and never invent a company name.
- Add no production mock server or automatic API fallback.
- Preserve accessible loading, error, empty, keyboard-focus, mobile, and reduced-motion behavior.

---

### Task 1: Typed pull boundary and job presentation model

**Files:**

- Modify: `apps/web/src/lib/search.ts:1-99`
- Test: `apps/web/src/lib/search.test.ts:1-91`

**Interfaces:**

- Consumes: `components['schemas']['PullJobRequest']`, `components['schemas']['JobResponse']`, and the existing `SearchApiError`.
- Produces: `pullJob(jobId: string, fetcher?: typeof fetch): Promise<JobResponse>`, `presentJob(response: JobResponse, rank: number): PresentedJob`, and exported `PresentedJob`.

- [ ] **Step 1: Add failing tests for the single-job pull request and response guard**

```ts
it('posts one job id to the pull endpoint', async () => {
  const fetcher = vi
    .fn<typeof fetch>()
    .mockResolvedValue(
      jsonResponse({ job_id: '53256270', details: { 職務名稱: '口譯人員' } })
    );
  await expect(pullJob('53256270', fetcher)).resolves.toMatchObject({
    job_id: '53256270'
  });
  expect(fetcher).toHaveBeenCalledWith('/api/v1/jobs/pull', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ job_id: '53256270' })
  });
});

it('rejects malformed pull details', async () => {
  const fetcher = vi
    .fn<typeof fetch>()
    .mockResolvedValue(
      jsonResponse({ job_id: '53256270', details: { 職務名稱: 7 } })
    );
  await expect(pullJob('53256270', fetcher)).rejects.toEqual(
    new SearchApiError('職缺資料服務回傳了無法辨識的內容。')
  );
});
```

- [ ] **Step 2: Run the focused suite and verify RED**

Run: `pnpm --dir apps/web test -- src/lib/search.test.ts`
Expected: FAIL because `pullJob` is not exported.

- [ ] **Step 3: Implement the typed pull boundary and shared JSON error parsing**

```ts
export type PullJobRequest = components['schemas']['PullJobRequest'];
export type JobResponse = components['schemas']['JobResponse'];

export async function pullJob(
  jobId: string,
  fetcher: typeof fetch = fetch
): Promise<JobResponse> {
  const response = await fetcher('/api/v1/jobs/pull', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ job_id: jobId } satisfies PullJobRequest)
  });
  const payload: unknown = await response.json();
  if (!response.ok) throw apiError(payload, '職缺資料暫時無法載入。');
  if (!isJobResponse(payload))
    throw new SearchApiError('職缺資料服務回傳了無法辨識的內容。');
  return payload;
}
```

- [ ] **Step 4: Run the focused suite and verify GREEN**

Run: `pnpm --dir apps/web test -- src/lib/search.test.ts`
Expected: all existing and new tests PASS.

- [ ] **Step 5: Add a failing test for useful-field selection and empty-field omission**

```ts
it('selects useful populated CSV fields for presentation', () => {
  expect(
    presentJob(
      {
        job_id: '53256270',
        details: {
          職務名稱: '後端工程師',
          工作城市: '台北市',
          薪資: '月薪 55,000 元',
          職務小類: '後端開發',
          職務內容: '開發 API',
          工作技能: null,
          廠商編號: '123'
        }
      },
      1
    )
  ).toEqual({
    jobId: '53256270',
    rank: 1,
    title: '後端工程師',
    city: '台北市',
    salary: '月薪 55,000 元',
    category: '後端開發',
    description: '開發 API',
    experience: undefined,
    education: undefined,
    skills: undefined,
    updatedAt: undefined
  });
});
```

- [ ] **Step 6: Run the focused suite and verify RED**

Run: `pnpm --dir apps/web test -- src/lib/search.test.ts`
Expected: FAIL because `presentJob` is not exported.

- [ ] **Step 7: Implement `PresentedJob` and `presentJob`**

Use `職務小類 || 職務中類 || 職務大類` for category, normalize blank strings to `undefined`, strip CSV `<br>` tags to spaces for previews, and fall back to `職缺 ${job_id}` only when `職務名稱` is empty.

- [ ] **Step 8: Run the focused suite and commit**

Run: `pnpm --dir apps/web test -- src/lib/search.test.ts`
Expected: PASS.

```bash
git add apps/web/src/lib/search.ts apps/web/src/lib/search.test.ts
git commit -m "feat(web): add job detail API boundary"
```

---

### Task 2: Concurrent search-to-details orchestration

**Files:**

- Modify: `apps/web/src/lib/search.ts`
- Test: `apps/web/src/lib/search.test.ts`

**Interfaces:**

- Consumes: `searchJobs`, `pullJob`, `presentJob`, and `PresentedJob` from Task 1.
- Produces: `searchJobDetails(query: string, fetcher?: typeof fetch): Promise<JobSearchOutcome>` where `JobSearchOutcome` contains `requestId`, `jobs`, and `failedCount`.

- [ ] **Step 1: Add a failing ordering-and-partial-failure test**

```ts
it('pulls every ranked id concurrently and keeps successful jobs ordered', async () => {
  const fetcher = routeFetch({
    search: {
      request_id: 'req_1',
      result: [
        { job_id: '20', rank: 1 },
        { job_id: '10', rank: 2 },
        { job_id: '30', rank: 3 }
      ]
    },
    pulls: {
      '20': { job_id: '20', details: { 職務名稱: '第一筆' } },
      '10': new Response(JSON.stringify(errorEnvelope), { status: 404 }),
      '30': { job_id: '30', details: { 職務名稱: '第三筆' } }
    }
  });
  await expect(searchJobDetails('工程師', fetcher)).resolves.toMatchObject({
    requestId: 'req_1',
    failedCount: 1,
    jobs: [
      { jobId: '20', rank: 1 },
      { jobId: '30', rank: 3 }
    ]
  });
});
```

- [ ] **Step 2: Run the focused suite and verify RED**

Run: `pnpm --dir apps/web test -- src/lib/search.test.ts`
Expected: FAIL because `searchJobDetails` is not exported.

- [ ] **Step 3: Implement minimal orchestration**

```ts
export async function searchJobDetails(
  query: string,
  fetcher: typeof fetch = fetch
): Promise<JobSearchOutcome> {
  const search = await searchJobs(
    { query: query.trim(), location_code: [], duty_code: [] },
    fetcher
  );
  const pulled = await Promise.allSettled(
    search.result.map(async ({ job_id, rank }) =>
      presentJob(await pullJob(job_id, fetcher), rank)
    )
  );
  return {
    requestId: search.request_id,
    jobs: pulled.flatMap((result) =>
      result.status === 'fulfilled' ? [result.value] : []
    ),
    failedCount: pulled.filter((result) => result.status === 'rejected').length
  };
}
```

- [ ] **Step 4: Add and satisfy the all-pulls-fail behavior**

Add a test expecting `SearchApiError('找到職缺，但詳細資料目前無法載入。', 'req_1')` when search returns IDs and every pull rejects. Implement that branch after counting failures.

- [ ] **Step 5: Run the full library suite and commit**

Run: `pnpm --dir apps/web test -- src/lib/search.test.ts`
Expected: PASS with ordering, partial failure, all-failed, malformed responses, and previous search tests covered.

```bash
git add apps/web/src/lib/search.ts apps/web/src/lib/search.test.ts
git commit -m "feat(web): resolve ranked search results"
```

---

### Task 3: Compact accessible Svelte search component

**Files:**

- Modify: `apps/web/src/routes/+page.svelte:1-EOF`

**Interfaces:**

- Consumes: `searchJobDetails`, `SearchApiError`, `PresentedJob`, and `JobSearchOutcome` from Tasks 1–2.
- Produces: a responsive page whose only primary interaction is the search form and whose results use the presentation model.

- [ ] **Step 1: Replace page state with query, phase, jobs, failure count, request ID, and error**

The submit handler must clear stale state, set phase to `searching`, await `searchJobDetails(query)`, then render `jobs`, `failedCount`, and `requestId`. Catch `SearchApiError` without leaking internal errors and always return phase to `idle`.

- [ ] **Step 2: Replace the page markup with the conventional search component**

Use one visually hidden `<label>`, one `type="search"` input, and one submit button in a single row. Below it, use `aria-live="polite"` for loading and outcomes. Render results as an ordered list with title, populated metadata chips, description preview, skills, update time, rank, and job ID.

- [ ] **Step 3: Add restrained responsive styling**

Use a white surface, neutral gray borders and text, and 1111 pink for the submit action and focus accent. Constrain content to a readable width, stack input and button below 36rem, keep visible keyboard focus, and disable nonessential transitions under `prefers-reduced-motion: reduce`.

- [ ] **Step 4: Run static checks and fix only reported issues**

Run: `pnpm format:check`
Expected: PASS after running `pnpm exec prettier --write apps/web/src/routes/+page.svelte apps/web/src/lib/search.ts apps/web/src/lib/search.test.ts` if needed.

Run: `pnpm lint`
Expected: PASS.

Run: `pnpm --dir apps/web check`
Expected: `0 errors and 0 warnings`.

- [ ] **Step 5: Run regression tests and production build**

Run: `pnpm --dir apps/web test`
Expected: all web tests PASS.

Run: `pnpm --dir apps/web build`
Expected: static adapter writes the site to `apps/web/build`.

- [ ] **Step 6: Verify the actual UI in a browser**

Run the Svelte dev server with Node 24. Intercept `/api/v1/jobs/search` and `/api/v1/jobs/pull` in the browser using contract-shaped sample responses, submit `台北後端工程師`, verify ten detail requests, inspect desktop and mobile screenshots, keyboard focus, loading, partial-failure, and empty states.

- [ ] **Step 7: Confirm a clean tracked diff and commit**

Run: `git diff --check && git status --short`
Expected: only the intended web files and ignored build outputs differ.

```bash
git add apps/web/src/routes/+page.svelte
git commit -m "feat(web): present searchable job details"
```

---

### Task 4: Final verification

**Files:**

- Verify: `apps/web/src/lib/search.ts`
- Verify: `apps/web/src/lib/search.test.ts`
- Verify: `apps/web/src/routes/+page.svelte`

**Interfaces:**

- Consumes: the completed component.
- Produces: fresh completion evidence and a final screenshot for the user.

- [ ] **Step 1: Run all web quality gates in the required Node 24 environment**

Run Prettier check, ESLint, contract generation check, Svelte check, Vitest, and production build. Every command must exit 0.

- [ ] **Step 2: Re-run the browser happy path and capture the final result view**

Confirm the search request body, ten pull request bodies, ranked rendering, and responsive layout from fresh browser evidence.

- [ ] **Step 3: Inspect repository state**

Run: `git status --short --branch && git log -5 --oneline`
Expected: tracked implementation is committed; `.superpowers/` may remain untracked as brainstorming state and must not be staged.
