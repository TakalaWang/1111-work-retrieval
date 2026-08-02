# Big Fruit Tree Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reshape the existing SvelteKit job-search page into the approved 「大果樹／新鮮市集」 experience while preserving keyword, date, location, duty, search, detail loading, and error behavior.

**Architecture:** Keep all API and state logic in the current page and `search.ts`; this is a presentation-only change. `+page.svelte` owns brand, search-shell composition, result states, and responsive layout, while `MultiSelectChecklist.svelte` retains its selection behavior and adopts the shared market-style control surface.

**Tech Stack:** Svelte 5, SvelteKit 2, TypeScript 5, Vitest 4, CSS scoped to Svelte components.

## Global Constraints

- Product name is 「大果樹」 and the hero must use 「找工作，今天就有好結果。」.
- Keep `SearchRequest` unchanged: `query`, optional `search_date`, `location_code`, and `duty_code`.
- Desktop order is 工作、區域、搜尋日期、關鍵字、搜尋職缺.
- Support 320px without horizontal scrolling.
- Team and competition language may appear only in the footer at low visual weight.
- Preserve semantic form controls, keyboard focus, status/alert roles, and WCAG AA contrast.
- Do not add dependencies, API changes, illustration assets, account features, or navigation sections.

---

## File Map

- Modify `apps/web/src/routes/+page.svelte`: brand copy, integrated search shell, footer, loading/error/result presentation, responsive CSS.
- Modify `apps/web/src/lib/MultiSelectChecklist.svelte`: compact two-line trigger and market-style panel visuals; no selection-logic changes.
- Create `apps/web/src/lib/branding.test.ts`: guard approved product copy and prevent competition identity from returning to the hero.
- Preserve `apps/web/src/lib/search.ts`, `apps/web/src/lib/search.test.ts`, and `apps/web/src/lib/filter-options.json` unchanged.

### Task 1: Lock the approved brand contract

**Files:**
- Create: `apps/web/src/lib/branding.test.ts`
- Modify: `apps/web/src/routes/+page.svelte`

**Interfaces:**
- Consumes: the existing Svelte page source and Vitest Node environment.
- Produces: source-level brand assertions that later visual tasks must keep passing.

- [ ] **Step 1: Write the failing brand test**

Create `apps/web/src/lib/branding.test.ts`:

```ts
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const page = readFileSync(
  new URL('../routes/+page.svelte', import.meta.url),
  'utf8'
);

describe('Big Fruit Tree page identity', () => {
  it('uses the approved product name and search copy', () => {
    expect(page).toContain('<title>大果樹｜職缺搜尋</title>');
    expect(page).toContain('找工作，今天就有好結果。');
    expect(page).toContain('選好條件、輸入關鍵字，把適合的職缺摘回家。');
    expect(page).toContain('搜尋職缺');
  });

  it('keeps the competition identity out of the hero', () => {
    const hero = page.slice(page.indexOf('<section class="search"'), page.indexOf('</section>'));
    expect(hero).not.toContain('蝦咪係Ai');
    expect(hero).not.toContain('雲湧智生');
    expect(hero).not.toContain('1111 智慧求職');
  });
});
```

- [ ] **Step 2: Run the test and verify it fails on the old identity**

Run: `pnpm --dir apps/web test -- branding.test.ts`

Expected: FAIL because the current title and hero still contain the old AI/team/competition copy.

- [ ] **Step 3: Replace hero identity and metadata with the approved copy**

In `+page.svelte`, replace the existing `<svelte:head>` and `.intro` header with:

```svelte
<svelte:head>
  <title>大果樹｜職缺搜尋</title>
  <meta
    name="description"
    content="選好條件、輸入關鍵字，在大果樹找到適合你的職缺。"
  />
</svelte:head>

<header class="intro">
  <div class="brand" aria-label="大果樹">
    <span class="brand-mark" aria-hidden="true">果</span>
    <span>大果樹</span>
  </div>
  <p class="brand-promise">讓每一次搜尋，都更接近好結果</p>
  <h1 id="search-title">找工作，今天就有好結果。</h1>
  <p class="intro-copy">選好條件、輸入關鍵字，把適合的職缺摘回家。</p>
</header>
```

Add a footer after the results section:

```svelte
<footer>
  <span>大果樹職缺搜尋</span>
  <span>2026 雲湧智生 · 1111 智慧求職</span>
</footer>
```

- [ ] **Step 4: Run the focused test**

Run: `pnpm --dir apps/web test -- branding.test.ts`

Expected: PASS, 2 tests passed.

- [ ] **Step 5: Commit the brand contract**

```bash
git add apps/web/src/lib/branding.test.ts apps/web/src/routes/+page.svelte
git commit -m "feat(web): introduce Big Fruit Tree identity"
```

### Task 2: Build the integrated search shell

**Files:**
- Modify: `apps/web/src/routes/+page.svelte`
- Modify: `apps/web/src/lib/MultiSelectChecklist.svelte`
- Test: `apps/web/src/lib/search.test.ts`

**Interfaces:**
- Consumes: `MultiSelectChecklist` props (`id`, `label`, `searchPlaceholder`, `options`, bindable `selected`, `disabled`, `align`) and `searchJobDetails(SearchForm)`.
- Produces: one responsive `.search-shell` with unchanged bound variables `dutyCodes`, `locationCodes`, `searchDate`, and `query`.

- [ ] **Step 1: Add a failing source assertion for the approved control order**

Extend `branding.test.ts`:

```ts
it('orders the integrated controls by duty, location, date, query, and action', () => {
  const searchShell = page.slice(
    page.indexOf('<div class="search-shell">'),
    page.indexOf('</div>', page.indexOf('<button class="search-button"'))
  );
  const positions = [
    'id="duty-codes"',
    'id="location-codes"',
    'id="search-date"',
    'id="job-query"',
    'class="search-button"'
  ].map((token) => searchShell.indexOf(token));
  expect(positions.every((position) => position >= 0)).toBe(true);
  expect(positions).toEqual([...positions].sort((a, b) => a - b));
});
```

- [ ] **Step 2: Run the focused test and verify the order assertion fails**

Run: `pnpm --dir apps/web test -- branding.test.ts`

Expected: FAIL because `.search-shell` does not exist.

- [ ] **Step 3: Recompose the existing controls without changing bindings**

Replace `.query-row` and `.controls` with this structure:

```svelte
<div class="search-shell">
  <MultiSelectChecklist
    id="duty-codes"
    label="工作"
    searchPlaceholder="搜尋工作名稱或代碼"
    options={filterOptions.duties}
    bind:selected={dutyCodes}
    disabled={loading}
  />
  <MultiSelectChecklist
    id="location-codes"
    label="區域"
    searchPlaceholder="搜尋區域名稱或代碼"
    options={filterOptions.locations}
    bind:selected={locationCodes}
    disabled={loading}
  />
  <label class="date-field" for="search-date">
    <span>搜尋日期</span>
    <input id="search-date" bind:value={searchDate} type="date" name="search_date" disabled={loading} />
  </label>
  <label class="query-field" for="job-query">
    <span class="sr-only">搜尋職缺</span>
    <span class="search-icon" aria-hidden="true">⌕</span>
    <input id="job-query" bind:value={query} type="search" name="query" required maxlength="512" autocomplete="off" placeholder="職務、技能或工作地點" />
  </label>
  <button class="search-button" type="submit" disabled={loading || !query.trim()}>
    {loading ? '搜尋中…' : '搜尋職缺'}
  </button>
</div>
```

Keep the existing `submit()` function exactly wired to `query`, `searchDate`, `locationCodes`, and `dutyCodes`.

- [ ] **Step 4: Restyle the multi-select trigger as a search-shell segment**

In `MultiSelectChecklist.svelte`:

- Keep all script logic and panel markup unchanged.
- Render `.field-label` as the first, small line and `summary` as the second line.
- Remove the trigger's standalone border and radius; its parent segment supplies the boundary.
- Use `#244A30` for text/focus, `#F1A52A` for selected checkbox accent, white for the panel, and `#B9D7B4` for soft shadow.
- Keep `.panel` absolute on desktop and constrain it to `min(22rem, calc(100vw - 2rem))`.
- Preserve `aria-expanded`, `aria-controls`, Escape close, outside close, and return-focus behavior.

Apply these essential declarations:

```css
.field { position: relative; min-width: 0; padding: 0.7rem 0.9rem; border-right: 1px solid #d7dfd8; }
.field-label { color: #748078; font-size: 0.68rem; font-weight: 850; letter-spacing: 0.06em; }
.trigger { min-height: 1.5rem; border: 0; padding: 0; background: transparent; color: #263b2c; font-weight: 800; }
.trigger:focus-visible { outline: 3px solid rgb(36 74 48 / 22%); outline-offset: 4px; border-radius: 0.25rem; }
.panel { border: 2px solid #244a30; border-radius: 0.85rem; box-shadow: 6px 7px 0 #b9d7b4; }
.option input { accent-color: #f1a52a; }
```

- [ ] **Step 5: Run behavior and component checks**

Run:

```bash
pnpm --dir apps/web test
pnpm --dir apps/web check
```

Expected: all Vitest tests pass and `svelte-check` reports 0 errors and 0 warnings.

- [ ] **Step 6: Commit the integrated search shell**

```bash
git add apps/web/src/lib/branding.test.ts apps/web/src/lib/MultiSelectChecklist.svelte apps/web/src/routes/+page.svelte
git commit -m "feat(web): integrate Big Fruit Tree search controls"
```

### Task 3: Apply the Fresh Market visual system and responsive result states

**Files:**
- Modify: `apps/web/src/routes/+page.svelte`
- Modify: `apps/web/src/lib/MultiSelectChecklist.svelte`

**Interfaces:**
- Consumes: the Task 2 `.search-shell`, existing Svelte state flags (`loading`, `searched`, `jobs`, `failedCount`, `error`, `requestId`), and existing result data.
- Produces: responsive 320px-to-desktop presentation with unchanged state transitions.

- [ ] **Step 1: Add the approved page-level visual tokens and hero structure**

Define component CSS custom properties and use them throughout:

```css
:global(html) {
  --leaf: #244a30;
  --fruit: #f1a52a;
  --sun: #f4b937;
  --sprout: #b9d7b4;
  --cream: #fffdf4;
  --ink: #203b29;
  background: var(--cream);
  color: var(--ink);
}
```

Style `.search` with a `0.75rem` sun-yellow top border, cream background, `1.5rem` radius, and clipped decorative pseudo-elements. Style `.brand-mark` as a fruit-orange rounded mark with a dark border and offset sprout-green shadow. Center the headline and copy while keeping the brand row aligned to the page edge.

- [ ] **Step 2: Style the integrated shell and its state transitions**

Use this desktop grid and interaction contract:

```css
.search-shell {
  display: grid;
  grid-template-columns: 9.5rem 9.5rem 10rem minmax(13rem, 1fr) auto;
  align-items: stretch;
  border: 2px solid var(--leaf);
  border-radius: 1rem;
  background: #fff;
  box-shadow: 0.45rem 0.5rem 0 var(--sprout);
}
.search-shell:focus-within { box-shadow: 0.45rem 0.5rem 0 var(--sprout), 0 0 0 4px rgb(36 74 48 / 12%); }
.search-button { margin: 0.4rem; border: 2px solid var(--leaf); background: var(--fruit); color: var(--ink); font-weight: 900; }
```

Keep disabled buttons visibly disabled using a desaturated cream/green pair while maintaining readable text. Respect reduced motion:

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
}
```

- [ ] **Step 3: Restyle result, loading, empty, partial, and error states**

- Use white result cards with `1px solid #d9e2da`, `1rem` radius, and no strong drop shadow.
- Use leaf-green rank badges and pale sprout metadata chips.
- Keep the full description visible and constrain the card's text line length; do not truncate or hide job content.
- Use fruit-orange only for actionable or warning emphasis, not every card.
- Keep `.status` roles and current Chinese messages unchanged.
- Add `class:has-results={loading || searched || error}` to the search section and use `.search.has-results` to reduce hero padding after submission.

- [ ] **Step 4: Add exact responsive layouts**

At `max-width: 800px`, use two columns for filters and full-width query/action:

```css
@media (max-width: 800px) {
  .search-shell { grid-template-columns: 1fr 1fr; }
  .date-field { grid-column: 1 / -1; }
  .query-field, .search-button { grid-column: 1 / -1; }
}
```

At `max-width: 480px`, stack each segment, reduce outer padding, make panels fit `calc(100vw - 2rem)`, and remove all right borders in favor of bottom dividers. Ensure `main`, `.search`, `.search-shell`, `.field`, `.query-field`, and result cards have `min-width: 0` where needed.

- [ ] **Step 5: Run complete Web verification**

Run:

```bash
pnpm --dir apps/web test
pnpm --dir apps/web check
pnpm --dir apps/web build
```

Expected: all tests pass, `svelte-check` reports 0 errors and 0 warnings, and Vite exits 0 with a static build.

- [ ] **Step 6: Verify visually at desktop and 320px**

Start: `pnpm --dir apps/web dev --host 127.0.0.1`

In the browser, verify:

- Desktop: controls appear in 工作、區域、日期、關鍵字、搜尋順序 in one shell.
- 320px: no horizontal scroll; every control is reachable; the multi-select panel stays inside the viewport.
- Keyboard: Tab reaches every control in order, Enter submits, Escape closes a multi-select and returns focus.
- Loading, successful results, partial detail failure, empty results, and API error remain readable.
- Hero contains no team/competition language; footer is the only place it appears.

- [ ] **Step 7: Commit visual polish**

```bash
git add apps/web/src/lib/MultiSelectChecklist.svelte apps/web/src/routes/+page.svelte
git commit -m "feat(web): polish Big Fruit Tree search experience"
```

### Task 4: Final regression and branch handoff

**Files:**
- Verify only; no planned source changes.

**Interfaces:**
- Consumes: Tasks 1–3 commits.
- Produces: a review-ready branch with evidence for behavior, type safety, build, and visual responsiveness.

- [ ] **Step 1: Confirm scope**

Run:

```bash
git status --short
git diff origin/main...HEAD -- apps/web docs/superpowers
```

Expected: only the approved design/plan documents and Web presentation files are changed; `.superpowers/` remains untracked and is not staged.

- [ ] **Step 2: Run the final Web gate from a fresh command**

Run:

```bash
pnpm --dir apps/web test && pnpm --dir apps/web check && pnpm --dir apps/web build
```

Expected: exit 0 with all tests passing, 0 Svelte errors/warnings, and a successful Vite build.

- [ ] **Step 3: Review commits and working tree**

Run:

```bash
git log --oneline --max-count=5
git status --short --branch
```

Expected: branch `codex/big-fruit-tree-search` contains focused commits and no tracked working-tree changes.
