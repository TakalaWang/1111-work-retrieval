<script>
  import { ApiError, searchJobs, serializeSearch } from '$lib/search';

  let query = $state('');
  let locationCodes = $state('');
  let dutyCodes = $state('');
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
        serializeSearch({ query, locationCodes, dutyCodes })
      );
      results = response.result;
      requestId = response.request_id;
      searched = true;
    } catch (caught) {
      error =
        caught instanceof ApiError
          ? { message: caught.message, requestId: caught.requestId }
          : { message: '目前無法連線到搜尋服務，請稍後再試。' };
    } finally {
      loading = false;
    }
  }
</script>

<svelte:head>
  <title>職缺搜尋 · 1111 Work Retrieval</title>
  <meta
    name="description"
    content="以關鍵字、地區代碼與職務代碼搜尋 1111 人力銀行職缺。"
  />
</svelte:head>

<main>
  <header>
    <div class="brand-mark" aria-hidden="true">11</div>
    <div>
      <p class="product-name">1111 Work Retrieval</p>
      <p class="product-note">搜尋服務展示</p>
    </div>
  </header>

  <section class="workspace" aria-labelledby="search-title">
    <div class="intro">
      <h1 id="search-title">搜尋合適的職缺</h1>
      <p>輸入關鍵字；需要縮小範圍時，再加入地區或職務代碼。</p>
      <aside class="demo-notice" role="note" aria-label="目前功能限制">
        <strong>目前為假搜尋資料</strong>
        <p>搜尋條件尚未參與排序，所有搜尋會固定回傳相同 10 筆職缺 ID。</p>
      </aside>
    </div>

    <form
      onsubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <label>
        <span>搜尋關鍵字</span>
        <input
          bind:value={query}
          type="search"
          name="query"
          required
          maxlength="512"
          autocomplete="off"
          placeholder="例如：後端工程師"
        />
      </label>

      <div class="filters">
        <label>
          <span>地區代碼 <small>選填</small></span>
          <textarea
            bind:value={locationCodes}
            name="location_code"
            rows="3"
            placeholder="100100, 100200"
          ></textarea>
          <small>以逗號、空白或換行分隔</small>
        </label>

        <label>
          <span>職務代碼 <small>選填</small></span>
          <textarea
            bind:value={dutyCodes}
            name="duty_code"
            rows="3"
            placeholder="140200, 140300"
          ></textarea>
          <small>重複代碼會自動合併</small>
        </label>
      </div>

      <button type="submit" disabled={loading || !query.trim()}>
        {loading ? '搜尋中…' : '搜尋職缺'}
      </button>
    </form>
  </section>

  <section
    class="results"
    aria-labelledby="results-title"
    aria-live="polite"
    aria-busy={loading}
  >
    <div class="section-heading">
      <h2 id="results-title">搜尋結果</h2>
      {#if requestId}<span class="request-id">{requestId}</span>{/if}
    </div>

    {#if loading}
      <div class="skeleton" role="status">
        <span class="sr-only">正在載入搜尋結果</span>
        <span></span><span></span><span></span>
      </div>
    {:else if error}
      <div class="message error" role="alert">
        <strong>搜尋失敗</strong>
        <p>{error.message}</p>
        {#if error.requestId}<code>{error.requestId}</code>{/if}
      </div>
    {:else if searched && results.length === 0}
      <div class="message">
        <strong>沒有可顯示的職缺</strong>
        <p>目前的固定展示資料沒有回傳任何結果。</p>
      </div>
    {:else if results.length > 0}
      <ol>
        {#each results as result (result.job_id)}
          <li>
            <span class="rank" aria-hidden="true">{result.rank}</span>
            <span>
              <span class="result-label">職缺 ID</span>
              <strong>{result.job_id}</strong>
            </span>
          </li>
        {/each}
      </ol>
    {:else}
      <div class="message">
        <strong>準備載入展示資料</strong>
        <p>送出任意有效關鍵字後，會顯示固定的 10 筆職缺 ID。</p>
      </div>
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
    background: oklch(1 0 0);
    color: oklch(0.2 0.015 340);
  }

  :global(body) {
    margin: 0;
    min-width: 320px;
  }

  :global(button),
  :global(input),
  :global(textarea) {
    font: inherit;
  }

  main {
    width: min(100% - 2rem, 64rem);
    margin: 0 auto;
    padding: 2rem 0 5rem;
  }

  header {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding-bottom: 3.5rem;
  }

  .brand-mark {
    display: grid;
    width: 2.5rem;
    height: 2.5rem;
    place-items: center;
    border-radius: 0.65rem;
    background: oklch(0.55 0.21 340);
    color: white;
    font-weight: 800;
  }

  .product-name,
  .product-note,
  .demo-notice p {
    margin: 0;
  }

  .product-name {
    font-weight: 700;
  }

  .product-note,
  .intro > p,
  small,
  .result-label,
  .message p {
    color: oklch(0.46 0.02 340);
  }

  .product-note,
  .result-label {
    font-size: 0.8rem;
  }

  .workspace {
    display: grid;
    grid-template-columns: minmax(14rem, 0.7fr) minmax(20rem, 1.3fr);
    gap: clamp(2rem, 6vw, 5rem);
    padding-bottom: 4rem;
    border-bottom: 1px solid oklch(0.91 0.008 340);
  }

  h1 {
    margin: 0;
    max-width: 12ch;
    font-size: 2.25rem;
    line-height: 1.1;
    letter-spacing: -0.035em;
    text-wrap: balance;
  }

  .intro > p {
    margin: 1rem 0 0;
    line-height: 1.65;
  }

  .demo-notice {
    margin-top: 1.5rem;
    padding: 1rem;
    border: 1px solid oklch(0.83 0.08 80);
    border-radius: 0.65rem;
    background: oklch(0.97 0.03 80);
    color: oklch(0.34 0.06 65);
    font-size: 0.88rem;
    line-height: 1.55;
  }

  .demo-notice p {
    margin-top: 0.25rem;
  }

  form,
  label {
    display: flex;
    flex-direction: column;
  }

  form {
    gap: 1.4rem;
  }

  label {
    gap: 0.55rem;
    font-size: 0.9rem;
    font-weight: 650;
  }

  input,
  textarea {
    width: 100%;
    border: 1px solid oklch(0.83 0.015 340);
    border-radius: 0.65rem;
    padding: 0.8rem 0.9rem;
    background: white;
    color: inherit;
  }

  input {
    min-height: 3.2rem;
  }

  textarea {
    min-height: 5.4rem;
    resize: vertical;
  }

  input:focus-visible,
  textarea:focus-visible,
  button:focus-visible {
    outline: 3px solid oklch(0.91 0.05 340);
    outline-offset: 1px;
  }

  .filters {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 1rem;
  }

  button {
    min-height: 3rem;
    align-self: flex-start;
    border: 0;
    border-radius: 0.65rem;
    padding: 0.7rem 1.2rem;
    background: oklch(0.55 0.21 340);
    color: white;
    font-weight: 750;
    cursor: pointer;
  }

  button:disabled {
    background: oklch(0.77 0.03 340);
    color: oklch(0.42 0.015 340);
    cursor: not-allowed;
  }

  .results {
    padding-top: 2.5rem;
  }

  .section-heading {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 1.25rem;
  }

  h2 {
    margin: 0;
    font-size: 1.2rem;
  }

  .request-id,
  code {
    color: oklch(0.43 0.025 340);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.72rem;
    overflow-wrap: anywhere;
  }

  .message {
    padding: 2rem;
    border: 1px solid oklch(0.9 0.01 340);
    border-radius: 0.75rem;
    background: oklch(0.98 0.004 340);
  }

  .message p {
    margin: 0.5rem 0 0;
    line-height: 1.55;
  }

  .message.error {
    border-color: oklch(0.72 0.12 25);
    background: oklch(0.97 0.025 25);
  }

  .message code {
    display: inline-block;
    margin-top: 1rem;
  }

  ol {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.75rem;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  li {
    display: grid;
    grid-template-columns: 2rem minmax(0, 1fr);
    align-items: center;
    gap: 1rem;
    min-height: 4.5rem;
    padding: 0.75rem;
    border: 1px solid oklch(0.9 0.01 340);
    border-radius: 0.65rem;
  }

  .rank {
    display: grid;
    width: 2rem;
    height: 2rem;
    place-items: center;
    border-radius: 999px;
    background: oklch(0.93 0.04 340);
    color: oklch(0.38 0.14 340);
    font-size: 0.8rem;
    font-weight: 800;
  }

  .result-label,
  li strong {
    display: block;
    overflow-wrap: anywhere;
  }

  .skeleton {
    display: grid;
    gap: 0.75rem;
  }

  .skeleton span {
    height: 4rem;
    border-radius: 0.65rem;
    background: oklch(0.94 0.01 340);
    animation: pulse 1.2s ease-in-out infinite alternate;
  }

  .sr-only {
    position: absolute;
    width: 1px !important;
    height: 1px !important;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  @keyframes pulse {
    to {
      background: oklch(0.98 0.005 340);
    }
  }

  @media (max-width: 46rem) {
    main {
      width: min(100% - 1.5rem, 64rem);
      padding-top: 1.25rem;
    }

    header {
      padding-bottom: 2.5rem;
    }

    .workspace,
    ol,
    .filters {
      grid-template-columns: 1fr;
    }

    form > button {
      width: 100%;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .skeleton span {
      animation: none;
    }
  }
</style>
