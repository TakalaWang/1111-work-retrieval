<script>
  import { SearchApiError, searchJobDetails } from '$lib/search';

  let query = $state('');
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
      const outcome = await searchJobDetails(query);
      jobs = outcome.jobs;
      failedCount = outcome.failedCount;
      requestId = outcome.requestId;
      searched = true;
    } catch (caught) {
      error =
        caught instanceof SearchApiError
          ? { message: caught.message, requestId: caught.requestId }
          : { message: '目前無法連線到職缺服務，請稍後再試。' };
    } finally {
      loading = false;
    }
  }
</script>

<svelte:head>
  <title>職缺搜尋</title>
  <meta name="description" content="輸入關鍵字，搜尋最相關的 1111 職缺。" />
</svelte:head>

<main>
  <section class="search" aria-labelledby="search-title">
    <h1 id="search-title">職缺搜尋</h1>
    <form
      role="search"
      onsubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <label class="sr-only" for="job-query">搜尋職缺</label>
      <input
        id="job-query"
        bind:value={query}
        type="search"
        name="query"
        required
        disabled={loading}
        maxlength="512"
        autocomplete="off"
        placeholder="輸入職務、技能或工作地點"
      />
      <button type="submit" disabled={loading || !query.trim()}>
        {loading ? '搜尋中…' : '搜尋'}
      </button>
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
    {:else}
      <p class="hint">輸入關鍵字，查看最相關的 10 筆職缺。</p>
    {/if}

    {#if requestId}
      <p class="request-id">Request ID：{requestId}</p>
    {/if}
  </section>
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

  h1 {
    margin: 0 0 1rem;
    font-size: clamp(1.5rem, 4vw, 2rem);
    letter-spacing: -0.03em;
  }

  form {
    display: flex;
    padding: 0.35rem;
    border: 1px solid #cfd1d5;
    border-radius: 0.8rem;
    background: #fff;
    box-shadow: 0 6px 24px rgb(32 33 36 / 7%);
    transition:
      border-color 160ms ease,
      box-shadow 160ms ease;
  }

  form:focus-within {
    border-color: #d7195f;
    box-shadow:
      0 0 0 3px rgb(215 25 95 / 12%),
      0 8px 28px rgb(32 33 36 / 9%);
  }

  input {
    min-width: 0;
    min-height: 3.25rem;
    flex: 1;
    border: 0;
    outline: 0;
    padding: 0.75rem 1rem;
    background: transparent;
    color: inherit;
  }

  input::placeholder {
    color: #767980;
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

  .hint,
  .request-id,
  .updated,
  .job-id,
  .result-summary p {
    color: #686b72;
  }

  .hint {
    margin: 0;
    text-align: center;
    font-size: 0.92rem;
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

    form {
      display: grid;
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

  @media (prefers-reduced-motion: reduce) {
    form,
    button {
      transition: none;
    }

    .spinner {
      animation: none;
    }
  }
</style>
