# Result Card Overflow Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make production job descriptions fill the result card and keep uninterrupted source strings inside its boundary.

**Architecture:** Keep the existing Svelte markup and API data untouched. Protect the intended presentation with source-level Vitest assertions, then make the smallest CSS-only change to `.description`.

**Tech Stack:** SvelteKit, CSS, TypeScript, Vitest, Prettier, ESLint

## Global Constraints

- Preserve API response text exactly; do not decode, remove, or rewrite job content.
- Do not change search controls, result fields, card spacing, or mobile layout.
- Ordinary Chinese and Latin text must keep natural wrapping behavior.
- Continuous strings wider than the card may break where necessary.

---

### Task 1: Make result descriptions fill and stay inside the card

**Files:**

- Modify: `apps/web/src/lib/branding.test.ts`
- Modify: `apps/web/src/routes/+page.svelte:776-783`

**Interfaces:**

- Consumes: the existing `.description` element rendered from `job.description`.
- Produces: full-width description layout with emergency wrapping and unchanged source text.

- [ ] **Step 1: Write failing layout regression tests**

Add these assertions to `apps/web/src/lib/branding.test.ts`:

```ts
it('lets result descriptions use the card width', () => {
  const descriptionStart = page.lastIndexOf('  .description {');
  const descriptionStyles = page.slice(
    descriptionStart,
    page.indexOf('\n  }', descriptionStart) + 4
  );

  expect(descriptionStyles).not.toContain('max-width: 72ch;');
});

it('wraps uninterrupted result description text inside the card', () => {
  const descriptionStart = page.lastIndexOf('  .description {');
  const descriptionStyles = page.slice(
    descriptionStart,
    page.indexOf('\n  }', descriptionStart) + 4
  );

  expect(descriptionStyles).toContain('overflow-wrap: anywhere;');
});
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
pnpm --dir apps/web test -- branding.test.ts
```

Expected: both new tests fail because the final `.description` rule contains `max-width: 72ch` and does not contain `overflow-wrap: anywhere`.

- [ ] **Step 3: Apply the minimal CSS fix**

Change the final `.description` rule in `apps/web/src/routes/+page.svelte` from:

```css
.description {
  display: block;
  max-width: 72ch;
  overflow: visible;
  color: #49584e;
  line-clamp: unset;
  -webkit-line-clamp: unset;
}
```

to:

```css
.description {
  display: block;
  overflow: visible;
  overflow-wrap: anywhere;
  color: #49584e;
  line-clamp: unset;
  -webkit-line-clamp: unset;
}
```

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```bash
pnpm --dir apps/web test -- branding.test.ts
```

Expected: all branding tests pass.

- [ ] **Step 5: Run the complete verification gate**

Run:

```bash
pnpm format:check
pnpm lint
pnpm --dir apps/web check
pnpm --dir apps/web test
pnpm --dir apps/web build
```

Expected: formatting and lint exit 0, Svelte reports 0 errors and 0 warnings, all Web tests pass, and Vite builds the static site.

- [ ] **Step 6: Commit the production fix**

```bash
git add apps/web/src/lib/branding.test.ts apps/web/src/routes/+page.svelte
git commit -m "fix(web): contain result card descriptions"
```
