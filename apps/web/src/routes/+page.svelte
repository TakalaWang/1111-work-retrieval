<script>
  import { ApiError, searchJobs, serializeSearch } from '$lib/search';

  let query = $state('');
  let locationCodes = $state('');
  let dutyCodes = $state('');
  let timeoutSeconds = $state(10);
  let loading = $state(false);
  let searched = $state(false);
  /** @type {import('$lib/search').SearchResult[]} */
  let results = $state([]);
  /** @type {{ message: string; requestId?: string } | undefined} */
  let error = $state();
  /** @type {string | undefined} */
  let requestId = $state();

  async function submit() {
    loading = true;
    searched = false;
    error = undefined;
    results = [];
    requestId = undefined;

    try {
      const response = await searchJobs(
        serializeSearch({ query, locationCodes, dutyCodes }),
        fetch,
        AbortSignal.timeout(timeoutSeconds * 1_000)
      );
      results = response.result;
      requestId = response.request_id;
      searched = true;
    } catch (caught) {
      error =
        caught instanceof DOMException && caught.name === 'TimeoutError'
          ? {
              message: `搜尋超過 ${timeoutSeconds} 秒，請調高最長等待時間後重試。`
            }
          : caught instanceof ApiError
            ? { message: caught.message, requestId: caught.requestId }
            : { message: '目前無法連線到搜尋服務，請稍後再試。' };
    } finally {
      loading = false;
    }
  }
</script>

<svelte:head>
  <title>職缺搜尋</title>
  <meta
    name="description"
    content="以關鍵字、地區代碼與職務代碼搜尋 1111 人力銀行職缺。"
  />
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
      <div class="primary-search">
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
      </div>

      <div class="filters">
        <label>
          <span>地區代碼 <small>選填</small></span>
          <textarea
            bind:value={locationCodes}
            name="location_code"
            rows="2"
            disabled={loading}
            placeholder="100100, 100200"
          ></textarea>
          <small>以逗號、空白或換行分隔</small>
        </label>

        <label>
          <span>職務代碼 <small>選填</small></span>
          <textarea
            bind:value={dutyCodes}
            name="duty_code"
            rows="2"
            disabled={loading}
            placeholder="140200, 140300"
          ></textarea>
          <small>重複代碼會自動合併</small>
        </label>

        <label class="timeout-control">
          <span>最長等待時間 <small>秒</small></span>
          <input
            bind:value={timeoutSeconds}
            name="timeout_seconds"
            type="number"
            min="1"
            max="60"
            step="1"
            required
            disabled={loading}
          />
          <small>可調整 1–60 秒</small>
        </label>
      </div>
    </form>
  </section>

  <section
    class="results"
    aria-labelledby="results-title"
    aria-live="polite"
    aria-busy={loading}
  >
    <div class="results-heading">
      <h2 id="results-title">搜尋結果</h2>
      {#if requestId}<span class="request-id">{requestId}</span>{/if}
    </div>

    {#if loading}
      <div class="skeleton" role="status">
        <span class="sr-only">正在搜尋職缺</span>
        <span></span><span></span><span></span>
      </div>
    {:else if error}
      <div class="status error" role="alert">
        <strong>無法完成搜尋</strong>
        <p>{error.message}</p>
        {#if error.requestId}<code>{error.requestId}</code>{/if}
      </div>
    {:else if searched && results.length === 0}
      <div class="status">
        <strong>找不到符合條件的職缺</strong>
        <p>試著改用其他關鍵字或減少篩選條件。</p>
      </div>
    {:else if results.length > 0}
      <p class="result-summary">找到 {results.length} 筆職缺</p>
      <ol>
        {#each results as result (result.job_id)}
          <li>
            <article>
              <span class="rank" aria-label={`搜尋排名第 ${result.rank} 名`}>
                {result.rank}
              </span>
              <div>
                <span class="result-label">職缺編號</span>
                <h3>{result.job_id}</h3>
              </div>
            </article>
          </li>
        {/each}
      </ol>
    {:else}
      <p class="empty">輸入關鍵字開始搜尋。</p>
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
  :global(input),
  :global(textarea) {
    font: inherit;
  }

  main {
    width: min(100% - 2rem, 52rem);
    margin: 0 auto;
    padding: 4rem 0 5rem;
  }

  h1 {
    margin: 0 0 1rem;
    font-size: 2rem;
    letter-spacing: -0.03em;
    text-wrap: balance;
  }

  form {
    display: grid;
    gap: 1rem;
  }

  .primary-search {
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

  .primary-search:focus-within {
    border-color: #d7195f;
    box-shadow:
      0 0 0 3px rgb(215 25 95 / 12%),
      0 8px 28px rgb(32 33 36 / 9%);
  }

  .primary-search input {
    min-width: 0;
    min-height: 3.25rem;
    flex: 1;
    border: 0;
    outline: 0;
    padding: 0.75rem 1rem;
    background: transparent;
    color: inherit;
  }

  .primary-search input::placeholder,
  textarea::placeholder {
    color: #62656a;
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
    color: #56595f;
    cursor: not-allowed;
  }

  .filters {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr)) minmax(8rem, 0.45fr);
    gap: 0.75rem;
  }

  label {
    display: grid;
    gap: 0.4rem;
    color: #3f4248;
    font-size: 0.82rem;
    font-weight: 650;
  }

  label small {
    color: #62656a;
    font-weight: 400;
  }

  textarea {
    width: 100%;
    min-height: 4.5rem;
    resize: vertical;
    border: 1px solid #d8d9dc;
    border-radius: 0.65rem;
    padding: 0.7rem 0.8rem;
    background: #fff;
    color: inherit;
    line-height: 1.45;
  }

  .timeout-control input {
    width: 100%;
    min-height: 2.75rem;
    border: 1px solid #d8d9dc;
    border-radius: 0.65rem;
    padding: 0.7rem 0.8rem;
    background: #fff;
    color: inherit;
  }

  textarea:focus-visible,
  .timeout-control input:focus-visible {
    outline: 3px solid rgb(215 25 95 / 12%);
    border-color: #d7195f;
  }

  .results {
    margin-top: 2rem;
  }

  .results-heading {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 0.75rem;
  }

  h2 {
    margin: 0;
    font-size: 1rem;
  }

  .request-id,
  code {
    color: #62656a;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.72rem;
    overflow-wrap: anywhere;
  }

  .result-summary,
  .empty,
  .result-label,
  .status p {
    color: #62656a;
  }

  .result-summary,
  .empty {
    margin: 0 0 0.75rem;
    font-size: 0.88rem;
  }

  .empty {
    text-align: center;
  }

  .status {
    padding: 1.25rem;
    border: 1px solid #dedfe2;
    border-radius: 0.7rem;
    background: #fff;
  }

  .status p {
    margin: 0.4rem 0 0;
  }

  .status.error {
    border-color: #e7a1b9;
    background: #fff7fa;
  }

  .status code {
    display: inline-block;
    margin-top: 0.8rem;
  }

  ol {
    display: grid;
    gap: 0.75rem;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  article {
    display: flex;
    align-items: flex-start;
    gap: 0.85rem;
    padding: 1.25rem;
    border: 1px solid #dedfe2;
    border-radius: 0.7rem;
    background: #fff;
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

  h3 {
    margin: 0;
    font-size: 1.08rem;
    line-height: 1.4;
    text-wrap: balance;
  }

  .result-label {
    display: inline-block;
    margin-top: 0.25rem;
    font-size: 0.74rem;
  }

  .skeleton {
    display: grid;
    gap: 0.75rem;
  }

  .skeleton span {
    height: 4.5rem;
    border-radius: 0.7rem;
    background: #e9eaec;
    animation: pulse 800ms ease-in-out infinite alternate;
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

  @keyframes pulse {
    to {
      background: #f2f2f3;
    }
  }

  @media (max-width: 36rem) {
    main {
      width: min(100% - 1.25rem, 52rem);
      padding-top: 2rem;
    }

    .primary-search,
    .filters {
      display: grid;
      grid-template-columns: 1fr;
    }

    button {
      min-height: 3rem;
    }

    .results-heading {
      display: grid;
      gap: 0.35rem;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .primary-search,
    button {
      transition: none;
    }

    .skeleton span {
      animation: none;
    }
  }
</style>
