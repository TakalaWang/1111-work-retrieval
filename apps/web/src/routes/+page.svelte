<script>
  import MultiSelectChecklist from '$lib/MultiSelectChecklist.svelte';
  import filterOptions from '$lib/filter-options.json';
  import { ApiError, searchJobDetails } from '$lib/search';

  let query = $state('');
  /** @type {string[]} */
  let locationCodes = $state([]);
  /** @type {string[]} */
  let dutyCodes = $state([]);
  let loading = $state(false);
  let searched = $state(false);
  /** @type {import('$lib/search').PresentedJob[]} */
  let jobs = $state([]);
  let failedCount = $state(0);
  /** @type {{ message: string; requestId?: string } | undefined} */
  let error = $state();
  /** @type {string | undefined} */
  let requestId = $state();

  async function submit() {
    loading = true;
    searched = false;
    jobs = [];
    failedCount = 0;
    error = undefined;
    requestId = undefined;

    try {
      const outcome = await searchJobDetails({
        query,
        locationCodes,
        dutyCodes
      });
      jobs = outcome.jobs;
      failedCount = outcome.failedCount;
      requestId = outcome.requestId;
      searched = true;
    } catch (caught) {
      error =
        caught instanceof ApiError
          ? { message: caught.message, requestId: caught.requestId }
          : { message: '目前無法連線到職缺服務，請稍後再試。' };
    } finally {
      loading = false;
    }
  }
</script>

<svelte:head>
  <title>大果樹｜職缺搜尋</title>
  <meta
    name="description"
    content="選好條件、輸入關鍵字，在大果樹找到適合你的職缺。"
  />
</svelte:head>

<main>
  <section
    class="search"
    class:has-results={loading || searched || error}
    aria-labelledby="search-title"
  >
    <header class="intro">
      <div class="brand" aria-label="大果樹">
        <span class="brand-mark" aria-hidden="true">果</span>
        <span>大果樹</span>
      </div>
      <h1 id="search-title">找工作，今天就有好結果。</h1>
      <p class="intro-copy">選好條件、輸入關鍵字，把適合的職缺「摘」回家。</p>
    </header>
    <form
      role="search"
      onsubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <div class="search-shell">
        <MultiSelectChecklist
          id="duty-codes"
          label="工作"
          options={filterOptions.duties}
          bind:selected={dutyCodes}
          disabled={loading}
        />
        <MultiSelectChecklist
          id="location-codes"
          label="區域"
          options={filterOptions.locations}
          bind:selected={locationCodes}
          disabled={loading}
        />
        <label class="query-field" for="job-query">
          <span class="sr-only">搜尋職缺</span>
          <span class="search-icon" aria-hidden="true">⌕</span>
          <input
            id="job-query"
            bind:value={query}
            type="search"
            name="query"
            required
            maxlength="512"
            autocomplete="off"
            placeholder="職務、技能或工作地點"
          />
        </label>
        <button
          class="search-button"
          type="submit"
          disabled={loading || !query.trim()}
        >
          {loading ? '搜尋中…' : '搜尋職缺'}
        </button>
      </div>
    </form>
  </section>

  <section class="results" aria-live="polite" aria-busy={loading}>
    {#if loading}
      <div class="status loading" role="status">
        <span class="spinner" aria-hidden="true"></span>
        <span>正在搜尋並載入職缺資料…</span>
      </div>
    {:else if error}
      <div class="status error" role="alert">
        <strong>無法完成搜尋</strong>
        <p>{error.message}</p>
        {#if error.requestId}<code>{error.requestId}</code>{/if}
      </div>
    {:else if searched && jobs.length === 0}
      <div class="status">
        <strong>找不到符合條件的職缺</strong>
        <p>試著改用其他職稱、技能或地點重新搜尋。</p>
      </div>
    {:else if jobs.length > 0}
      <div class="result-summary">
        <p>找到 {jobs.length} 筆相關職缺</p>
        {#if failedCount > 0}
          <p class="partial" role="status">
            另有 {failedCount} 筆職缺資料暫時無法載入
          </p>
        {/if}
      </div>

      <ol>
        {#each jobs as job (job.jobId)}
          <li>
            <article>
              <div class="job-heading">
                <span class="rank" aria-label={`搜尋排名第 ${job.rank} 名`}>
                  {job.rank}
                </span>
                <div>
                  <h2>{job.title}</h2>
                  <span class="job-id">職缺編號 {job.jobId}</span>
                </div>
              </div>

              {#if job.city || job.salary || job.category}
                <ul class="metadata" aria-label="職缺摘要">
                  {#if job.city}<li>{job.city}</li>{/if}
                  {#if job.salary}<li>{job.salary}</li>{/if}
                  {#if job.category}<li>{job.category}</li>{/if}
                </ul>
              {/if}

              {#if job.description}
                <p class="description">{job.description}</p>
              {/if}

              {#if job.experience || job.education || job.skills}
                <dl>
                  {#if job.experience}
                    <div>
                      <dt>工作經驗</dt>
                      <dd>{job.experience}</dd>
                    </div>
                  {/if}
                  {#if job.education}
                    <div>
                      <dt>學歷要求</dt>
                      <dd>{job.education}</dd>
                    </div>
                  {/if}
                  {#if job.skills}
                    <div>
                      <dt>工作技能</dt>
                      <dd>{job.skills}</dd>
                    </div>
                  {/if}
                </dl>
              {/if}

              {#if job.updatedAt}
                <p class="updated">更新時間 {job.updatedAt}</p>
              {/if}
            </article>
          </li>
        {/each}
      </ol>
    {/if}

    {#if requestId}
      <p class="request-id">Request ID：{requestId}</p>
    {/if}
  </section>

  <footer>
    <span>大果樹職缺搜尋</span>
    <span>2026 雲湧智生 · 1111 智慧求職</span>
  </footer>
</main>

<style>
  :global(*) {
    box-sizing: border-box;
  }

  :global(html) {
    color-scheme: light;
    font-family:
      Inter,
      ui-sans-serif,
      system-ui,
      -apple-system,
      BlinkMacSystemFont,
      'Segoe UI',
      sans-serif;
    background: #f7f7f8;
    color: #202124;
  }

  :global(body) {
    min-width: 320px;
    margin: 0;
  }

  :global(button),
  :global(input) {
    font: inherit;
  }

  main {
    width: min(100% - 2rem, 52rem);
    margin: 0 auto;
    padding: clamp(3rem, 10vh, 6rem) 0 5rem;
  }

  .intro {
    margin-bottom: 1.25rem;
  }

  h1 {
    margin: 0;
    font-size: clamp(1.5rem, 4vw, 2rem);
    letter-spacing: -0.03em;
    line-height: 1.25;
    text-wrap: balance;
  }

  .intro-copy {
    margin: 0.45rem 0 0;
    color: #686b72;
    font-size: 0.9rem;
    line-height: 1.55;
  }

  button {
    min-width: 5.5rem;
    border: 0;
    border-radius: 0.55rem;
    padding: 0.75rem 1.25rem;
    background: #d7195f;
    color: #fff;
    font-weight: 700;
    cursor: pointer;
    transition:
      background 160ms ease,
      transform 160ms ease;
  }

  button:hover:not(:disabled) {
    background: #b81250;
  }

  button:active:not(:disabled) {
    transform: translateY(1px);
  }

  button:focus-visible {
    outline: 3px solid rgb(215 25 95 / 25%);
    outline-offset: 2px;
  }

  button:disabled {
    background: #c8c9cc;
    color: #62656a;
    cursor: not-allowed;
  }

  .results {
    margin-top: 1.5rem;
  }

  .request-id,
  .updated,
  .job-id,
  .result-summary p {
    color: #686b72;
  }

  .status {
    padding: 1.25rem;
    border: 1px solid #dedfe2;
    border-radius: 0.7rem;
    background: #fff;
  }

  .status strong,
  .status p {
    display: block;
  }

  .status p {
    margin: 0.4rem 0 0;
    color: #686b72;
  }

  .status.loading {
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }

  .status.error {
    border-color: #e7a1b9;
    background: #fff7fa;
  }

  code {
    display: inline-block;
    margin-top: 0.8rem;
    color: #686b72;
    font-size: 0.78rem;
    overflow-wrap: anywhere;
  }

  .spinner {
    width: 1rem;
    height: 1rem;
    flex: 0 0 auto;
    border: 2px solid #e2a7bd;
    border-top-color: #d7195f;
    border-radius: 50%;
    animation: spin 700ms linear infinite;
  }

  .result-summary {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 0.75rem;
  }

  .result-summary p {
    margin: 0;
    font-size: 0.88rem;
  }

  .result-summary .partial {
    color: #9b4e00;
  }

  ol,
  .metadata {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  ol {
    display: grid;
    gap: 0.75rem;
  }

  article {
    padding: 1.25rem;
    border: 1px solid #dedfe2;
    border-radius: 0.7rem;
    background: #fff;
  }

  .job-heading {
    display: flex;
    align-items: flex-start;
    gap: 0.85rem;
  }

  .job-heading > div {
    min-width: 0;
  }

  .rank {
    display: grid;
    width: 1.8rem;
    height: 1.8rem;
    flex: 0 0 auto;
    place-items: center;
    border-radius: 50%;
    background: #fde8ef;
    color: #b81250;
    font-size: 0.78rem;
    font-weight: 800;
  }

  h2 {
    margin: 0;
    font-size: 1.08rem;
    line-height: 1.4;
    overflow-wrap: anywhere;
  }

  .job-id {
    display: inline-block;
    margin-top: 0.25rem;
    font-size: 0.74rem;
  }

  .metadata {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    margin-top: 0.9rem;
    padding-left: 2.65rem;
  }

  .metadata li {
    border-radius: 999px;
    padding: 0.3rem 0.65rem;
    background: #f1f2f4;
    color: #50535a;
    font-size: 0.78rem;
  }

  .description {
    display: -webkit-box;
    overflow: hidden;
    margin: 1rem 0 0 2.65rem;
    color: #4e5157;
    font-size: 0.9rem;
    line-height: 1.65;
    line-clamp: 3;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 3;
  }

  dl {
    display: grid;
    gap: 0.45rem;
    margin: 1rem 0 0 2.65rem;
    padding-top: 0.85rem;
    border-top: 1px solid #ececef;
    font-size: 0.82rem;
  }

  dl div {
    display: grid;
    grid-template-columns: 5rem 1fr;
    gap: 0.75rem;
  }

  dt {
    color: #767980;
  }

  dd {
    margin: 0;
    color: #3f4248;
    overflow-wrap: anywhere;
  }

  .updated {
    margin: 0.85rem 0 0 2.65rem;
    font-size: 0.75rem;
  }

  .request-id {
    margin: 1rem 0 0;
    text-align: right;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.7rem;
    overflow-wrap: anywhere;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    clip-path: inset(50%);
  }

  /* Big Fruit Tree — Fresh Market */
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

  main {
    width: min(100% - 2rem, 76rem);
    padding: 1.5rem 0 3rem;
  }

  .search {
    position: relative;
    z-index: 2;
    overflow: visible;
    border: 2px solid var(--leaf);
    border-top: 0.75rem solid var(--sun);
    border-radius: 1.5rem;
    padding: clamp(1rem, 2vw, 1.5rem) clamp(1rem, 5vw, 4rem)
      clamp(2rem, 5vw, 4rem);
    background:
      radial-gradient(circle at 100% 0, #e0efd5 0 8rem, transparent 8.05rem),
      radial-gradient(circle at 0 100%, #f8dfa0 0 6.5rem, transparent 6.55rem),
      var(--cream);
    box-shadow: 0.65rem 0.75rem 0 #dcebd6;
    transition: padding 180ms ease;
  }

  .search.has-results {
    padding-top: 1.5rem;
    padding-bottom: 2rem;
  }

  .intro,
  form {
    position: relative;
    z-index: 1;
  }

  .intro {
    margin-bottom: 1.8rem;
  }

  .brand {
    display: flex;
    align-items: center;
    gap: 0.8rem;
    color: var(--leaf);
    font-size: 1.15rem;
    font-weight: 950;
    letter-spacing: 0.07em;
  }

  .brand-mark {
    display: grid;
    width: 2.65rem;
    height: 2.65rem;
    place-items: center;
    border: 2px solid var(--leaf);
    border-radius: 52% 46% 50% 44%;
    background: var(--fruit);
    box-shadow: 0.3rem 0.3rem 0 #78a869;
    color: var(--leaf);
    font-size: 1rem;
    font-weight: 950;
  }

  h1 {
    max-width: 46rem;
    margin: clamp(1.5rem, 3vw, 2.5rem) auto 0;
    color: var(--ink);
    font-family:
      'Arial Rounded MT Bold', 'Noto Sans TC', ui-rounded, system-ui, sans-serif;
    font-size: clamp(2.05rem, 5vw, 3.6rem);
    font-weight: 950;
    letter-spacing: -0.05em;
    line-height: 1.08;
    text-align: center;
  }

  .has-results h1 {
    margin-top: 1.5rem;
    font-size: clamp(1.7rem, 3.5vw, 2.5rem);
  }

  .intro-copy {
    margin: 0.75rem auto 0;
    color: #617066;
    font-size: 0.95rem;
    line-height: 1.55;
    text-align: center;
  }

  .search-shell {
    display: grid;
    grid-template-columns: 9.5rem 9.5rem minmax(13rem, 1fr) auto;
    align-items: stretch;
    max-width: 64rem;
    min-width: 0;
    margin: 0 auto;
    border: 2px solid var(--leaf);
    border-radius: 1rem;
    background: #fff;
    box-shadow: 0.45rem 0.5rem 0 var(--sprout);
    transition: box-shadow 160ms ease;
  }

  .search-shell:focus-within {
    box-shadow:
      0.45rem 0.5rem 0 var(--sprout),
      0 0 0 4px rgb(36 74 48 / 12%);
  }

  .query-field {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: 0.6rem;
    padding: 0.65rem 0.9rem;
  }

  .search-icon {
    color: var(--leaf);
    font-size: 1.35rem;
    font-weight: 900;
  }

  .query-field input {
    width: 100%;
    min-width: 0;
    min-height: 2.5rem;
    border: 0;
    outline: 0;
    padding: 0;
    background: transparent;
    color: var(--ink);
  }

  .query-field input::placeholder {
    color: #7c8980;
  }

  .search-button {
    min-width: 7.5rem;
    margin: 0.4rem;
    border: 2px solid var(--leaf);
    border-radius: 0.7rem;
    padding: 0.7rem 1.2rem;
    background: var(--fruit);
    color: var(--ink);
    font-weight: 900;
    white-space: nowrap;
  }

  .search-button:hover:not(:disabled) {
    background: #f6b946;
    transform: translateY(-1px);
  }

  .search-button:focus-visible,
  .query-field input:focus-visible {
    outline: 3px solid rgb(36 74 48 / 22%);
    outline-offset: 2px;
  }

  .search-button:disabled {
    border-color: #748078;
    background: #e4e6d7;
    color: #606b63;
  }

  .results {
    position: relative;
    z-index: 1;
    width: min(100%, 64rem);
    margin: 2.25rem auto 0;
  }

  .result-summary p,
  .request-id,
  .updated,
  .job-id {
    color: #617066;
  }

  .status {
    border: 1px solid #cfdbd0;
    border-radius: 1rem;
    background: #fff;
  }

  .status.error {
    border-color: #d89162;
    background: #fff8ed;
  }

  .spinner {
    border-color: #d7e5d1;
    border-top-color: var(--fruit);
  }

  ol {
    gap: 0.9rem;
  }

  article {
    border: 1px solid #d9e2da;
    border-radius: 1rem;
    padding: 1.4rem;
    background: #fff;
  }

  .rank {
    background: var(--leaf);
    color: #fffdf4;
  }

  .metadata li {
    background: #edf5e8;
    color: #35533c;
  }

  .description {
    display: block;
    overflow: visible;
    overflow-wrap: anywhere;
    color: #49584e;
    line-clamp: unset;
    -webkit-line-clamp: unset;
  }

  dl {
    border-top-color: #e4ebe4;
  }

  footer {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    width: min(100%, 64rem);
    margin: 2.5rem auto 0;
    padding-top: 1rem;
    border-top: 1px solid #dce4dc;
    color: #748078;
    font-size: 0.7rem;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }

  @media (max-width: 36rem) {
    main {
      width: min(100% - 1.25rem, 52rem);
      padding-top: 2rem;
    }

    button {
      min-height: 3rem;
    }

    .result-summary {
      display: grid;
      gap: 0.25rem;
    }

    .metadata,
    .description,
    dl,
    .updated {
      margin-left: 0;
    }
  }

  @media (max-width: 50rem) {
    .search-shell {
      grid-template-columns: 1fr 1fr;
    }

    .query-field,
    .search-button {
      grid-column: 1 / -1;
    }

    .query-field {
      border-bottom: 1px solid #d7dfd8;
    }

    footer {
      padding-inline: 0.25rem;
    }
  }

  @media (max-width: 30rem) {
    main {
      width: min(100% - 1rem, 76rem);
      padding-top: 0.75rem;
    }

    .search {
      border-radius: 1.15rem;
      padding: 1.25rem 0.75rem 1.75rem;
      box-shadow: 0.35rem 0.45rem 0 #dcebd6;
    }

    h1 {
      margin-top: 2.5rem;
      font-size: 2rem;
    }

    .search-shell {
      grid-template-columns: 1fr;
      box-shadow: 0.3rem 0.4rem 0 var(--sprout);
    }

    .query-field,
    .search-button {
      grid-column: auto;
    }

    .query-field {
      min-width: 0;
      border-right: 0;
      border-bottom: 1px solid #d7dfd8;
    }

    .search-button {
      min-height: 3rem;
    }

    article {
      min-width: 0;
      padding: 1.1rem;
    }

    footer {
      display: grid;
      gap: 0.35rem;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    *,
    *::before,
    *::after {
      scroll-behavior: auto !important;
      animation-duration: 0.01ms !important;
      transition-duration: 0.01ms !important;
    }

    .spinner {
      animation: none;
    }
  }
</style>
