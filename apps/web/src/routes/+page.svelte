<script>
  import { SearchApiError, searchJobs, serializeSearch } from '$lib/search';

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
        caught instanceof SearchApiError
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
      <p class="product-note">正式搜尋介面</p>
    </div>
  </header>

  <section class="workspace" aria-labelledby="search-title">
    <div class="intro">
      <h1 id="search-title">搜尋合適的職缺</h1>
      <p>輸入關鍵字；需要縮小範圍時，再加入地區或職務代碼。</p>
    </div>

    <form
      onsubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <label class="query-field">
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
    <div class="results-heading">
      <h2 id="results-title">搜尋結果</h2>
      {#if requestId}<span class="request-id">{requestId}</span>{/if}
    </div>

    {#if loading}
      <div class="skeleton" aria-label="正在載入搜尋結果">
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
        <strong>沒有符合條件的職缺</strong>
        <p>試著減少篩選代碼，或改用範圍較廣的關鍵字。</p>
      </div>
    {:else if results.length > 0}
      <ol>
        {#each results as result (result.job_id)}
          <li>
            <span class="rank">{result.rank}</span>
            <div>
              <span class="result-label">職缺 ID</span>
              <strong>{result.job_id}</strong>
            </div>
          </li>
        {/each}
      </ol>
    {:else}
      <div class="message">
        <strong>準備開始搜尋</strong>
        <p>搜尋結果會依相關性排序，最多顯示 10 筆。</p>
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
    width: min(100% - 2rem, 68rem);
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
    color: oklch(1 0 0);
    font-weight: 800;
  }

  .product-name,
  .product-note {
    margin: 0;
  }

  .product-name {
    font-weight: 700;
  }

  .product-note,
  .intro p,
  small,
  .result-label,
  .message p {
    color: oklch(0.46 0.02 340);
  }

  .product-note {
    margin-top: 0.1rem;
    font-size: 0.82rem;
  }

  .workspace {
    display: grid;
    grid-template-columns: minmax(14rem, 0.7fr) minmax(20rem, 1.3fr);
    gap: clamp(2rem, 6vw, 5rem);
    padding-bottom: 4rem;
    border-bottom: 1px solid oklch(0.91 0.008 340);
  }

  h1,
  h2,
  p {
    text-wrap: pretty;
  }

  h1 {
    margin: 0;
    max-width: 12ch;
    font-size: 2.25rem;
    line-height: 1.1;
    letter-spacing: -0.035em;
    text-wrap: balance;
  }

  .intro p {
    max-width: 34rem;
    margin: 1rem 0 0;
    line-height: 1.65;
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

  label > span small {
    margin-left: 0.25rem;
    font-weight: 500;
  }

  input,
  textarea {
    width: 100%;
    border: 1px solid oklch(0.83 0.015 340);
    border-radius: 0.65rem;
    background: oklch(1 0 0);
    color: inherit;
    transition:
      border-color 180ms ease-out,
      box-shadow 180ms ease-out;
  }

  input {
    min-height: 3.2rem;
    padding: 0.75rem 0.9rem;
  }

  textarea {
    min-height: 5.4rem;
    padding: 0.8rem 0.9rem;
    line-height: 1.5;
    resize: vertical;
  }

  input::placeholder,
  textarea::placeholder {
    color: oklch(0.48 0.015 340);
  }

  input:hover,
  textarea:hover {
    border-color: oklch(0.69 0.04 340);
  }

  input:focus-visible,
  textarea:focus-visible,
  button:focus-visible {
    outline: none;
    border-color: oklch(0.55 0.21 340);
    box-shadow: 0 0 0 3px oklch(0.91 0.05 340);
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
    color: oklch(1 0 0);
    font-weight: 750;
    cursor: pointer;
    transition:
      background 180ms ease-out,
      transform 180ms ease-out;
  }

  button:hover:not(:disabled) {
    background: oklch(0.48 0.2 340);
  }

  button:active:not(:disabled) {
    transform: translateY(1px);
  }

  button:disabled {
    background: oklch(0.77 0.03 340);
    color: oklch(0.42 0.015 340);
    cursor: not-allowed;
  }

  .results {
    padding-top: 2.5rem;
  }

  .results-heading {
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
    font-size: 0.78rem;
  }

  .message {
    padding: 2rem;
    border: 1px solid oklch(0.9 0.01 340);
    border-radius: 0.75rem;
    background: oklch(0.98 0.004 340);
  }

  .message strong,
  .message p {
    display: block;
  }

  .message p {
    margin: 0.5rem 0 0;
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
    display: flex;
    flex-direction: column;
    gap: 0;
    margin: 0;
    padding: 0;
    list-style: none;
    border-top: 1px solid oklch(0.9 0.01 340);
  }

  li {
    display: flex;
    align-items: center;
    gap: 1rem;
    padding: 1rem 0;
    border-bottom: 1px solid oklch(0.9 0.01 340);
  }

  .rank {
    display: grid;
    width: 2rem;
    height: 2rem;
    flex: 0 0 auto;
    place-items: center;
    border-radius: 999px;
    background: oklch(0.93 0.04 340);
    color: oklch(0.38 0.14 340);
    font-size: 0.8rem;
    font-weight: 800;
  }

  .result-label {
    display: block;
    margin-bottom: 0.15rem;
    font-size: 0.75rem;
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

  @keyframes pulse {
    to {
      background: oklch(0.98 0.005 340);
    }
  }

  @media (max-width: 46rem) {
    main {
      width: min(100% - 1.5rem, 68rem);
      padding-top: 1.25rem;
    }

    header {
      padding-bottom: 2.5rem;
    }

    .workspace {
      grid-template-columns: 1fr;
      gap: 2rem;
    }

    .filters {
      grid-template-columns: 1fr;
    }

    button {
      width: 100%;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    *,
    *::before,
    *::after {
      scroll-behavior: auto !important;
      transition-duration: 0.01ms !important;
      animation-duration: 0.01ms !important;
      animation-iteration-count: 1 !important;
    }
  }
</style>
