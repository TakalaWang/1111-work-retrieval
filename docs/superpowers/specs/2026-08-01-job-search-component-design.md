# Job Search Component Design

## Goal

Replace the current broad search page with a compact, embeddable job-search component. The
component presents one conventional search field, retrieves up to ten ranked job IDs, resolves
each ID to its CSV-backed job details, and renders the useful fields beneath the search field.

This is a component, not a complete marketing site or dashboard. It has no navigation, hero,
brand shell, analytics panel, or advanced visual concept.

## API flow

1. The user enters a non-empty natural-language query and submits the form.
2. The client posts `{ "query": "...", "location_code": [], "duty_code": [] }` to
   `POST /api/v1/jobs/search`.
3. The successful response supplies zero to ten ordered `{ job_id, rank }` items.
4. The client posts `{ "job_id": "..." }` once per result to `POST /api/v1/jobs/pull`.
5. The ten pull requests run concurrently with `Promise.allSettled` so one missing CSV record does
   not discard the other successful jobs.
6. Successful job details retain the search ranking order and render below the form.

The frontend consumes the committed generated OpenAPI types. Response guards reject malformed
success and error payloads at the browser boundary.

## Interface

The component contains:

- one search input with a plain-language example;
- one submit button;
- a compact progress message while search and detail retrieval run;
- a result count and an optional partial-failure notice;
- up to ten job result cards;
- empty and error states directly beneath the form.

The search form remains visible in every state. The visual style is a conventional white search
surface with restrained borders, spacing, and one 1111 pink action color. It must remain readable
on workshop projection and collapse to one column on mobile.

Each result shows only populated, user-relevant CSV fields:

- `職務名稱` as the primary heading;
- `工作城市`, `薪資`, and the most specific available job category;
- `工作經驗需求` and `學歷需求`;
- a shortened `職務內容` preview;
- `工作技能` when present;
- `職缺最後修改時間`;
- the job ID as low-emphasis reference information.

Company name is not displayed because the current CSV has only `廠商編號`, not a human-readable
company name. Empty fields and empty labels are omitted.

## States and failures

- Before search: show a short prompt beneath the field.
- Searching: disable duplicate submission and announce that matching jobs are being found.
- Pulling details: keep the same loading state and announce that job details are loading.
- Empty search result: explain that no matching jobs were found.
- Search request failure: show the API message and request ID when available.
- All pull requests fail: show a detail-loading failure rather than an empty result.
- Some pull requests fail: show successful jobs in rank order and state how many could not load.
- A new submission clears stale results and errors before starting.

No production mock server or automatic API fallback is added. Browser-level request interception
may supply workshop data only while visually verifying the component locally.

## Code boundaries

- `apps/web/src/lib/search.ts` owns request serialization, runtime response validation, the search
  call, the single-job pull call, field selection, and the concurrent orchestration function.
- `apps/web/src/routes/+page.svelte` owns form state, status messaging, and accessible rendering.
- `apps/web/src/lib/search.test.ts` verifies both API calls, ordering, partial failures, malformed
  responses, and field selection.

## Verification

- Follow red-green-refactor for every new behavior in the TypeScript boundary.
- Run the focused Vitest suite after each red/green cycle.
- Run Prettier, ESLint, Svelte check, the full web test suite, and the production build.
- Launch the local SvelteKit app, intercept the two APIs with representative contract-shaped data,
  submit the form, inspect desktop and mobile layouts, and capture the final result view.
