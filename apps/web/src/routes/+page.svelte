<script>
  import {
    ApiError,
    getJobDetail,
    searchJobs,
    serializeSearch
  } from '$lib/search';

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

  let detailLoading = $state(false);
  /** @type {string | undefined} */
  let selectedJobId = $state();
  /** @type {import('$lib/search').JobDetail | undefined} */
  let jobDetail = $state();
  /** @type {{ title: string; message: string; requestId?: string } | undefined} */
  let detailError = $state();
  /** @type {string | undefined} */
  let detailRequestId = $state();
  let detailSequence = 0;

  /** @type {readonly { title: string; fields: readonly { key: keyof import('$lib/search').JobDetail; label: string }[] }[]} */
  const detailGroups = [
    {
      title: '待遇與分類',
      fields: [
        { key: 'salary_min', label: '待遇下限' },
        { key: 'salary_max', label: '待遇上限' },
        { key: 'duty_major', label: '職務大類' },
        { key: 'duty_middle', label: '職務中類' },
        { key: 'duty_minor', label: '職務小類' }
      ]
    },
    {
      title: '學經歷條件',
      fields: [
        { key: 'education_requirement', label: '學歷要求' },
        { key: 'major_requirement_1', label: '科系要求一' },
        { key: 'major_requirement_2', label: '科系要求二' },
        { key: 'major_requirement_3', label: '科系要求三' },
        { key: 'experience_requirement', label: '工作經驗' }
      ]
    },
    {
      title: '語言能力',
      fields: [
        { key: 'language_1', label: '語言一' },
        { key: 'language_1_listening', label: '語言一聽力' },
        { key: 'language_1_speaking', label: '語言一口說' },
        { key: 'language_1_reading', label: '語言一閱讀' },
        { key: 'language_1_writing', label: '語言一書寫' },
        { key: 'language_2', label: '語言二' },
        { key: 'language_2_listening', label: '語言二聽力' },
        { key: 'language_2_speaking', label: '語言二口說' },
        { key: 'language_2_reading', label: '語言二閱讀' },
        { key: 'language_2_writing', label: '語言二書寫' }
      ]
    },
    {
      title: '技能與其他條件',
      fields: [
        { key: 'computer_skills', label: '電腦技能' },
        { key: 'professional_certifications', label: '專業證照' },
        { key: 'work_skills', label: '工作技能' },
        { key: 'additional_conditions', label: '其他條件' },
        { key: 'management_count', label: '管理人數' },
        { key: 'requires_travel', label: '出差需求' }
      ]
    },
    {
      title: '產業資訊',
      fields: [
        { key: 'industry_major', label: '產業大類' },
        { key: 'industry_middle', label: '產業中類' },
        { key: 'industry_minor', label: '產業小類' }
      ]
    }
  ];

  /** @param {string | null} value */
  function displayValue(value) {
    return value?.trim() || '未提供';
  }

  /**
   * @param {import('$lib/search').JobDetail} job
   * @param {keyof import('$lib/search').JobDetail} key
   */
  function fieldValue(job, key) {
    return displayValue(job[key]);
  }

  async function submit() {
    detailSequence += 1;
    loading = true;
    searched = false;
    error = undefined;
    results = [];
    requestId = undefined;
    detailLoading = false;
    selectedJobId = undefined;
    jobDetail = undefined;
    detailError = undefined;
    detailRequestId = undefined;

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

  /** @param {string} jobId */
  async function selectJob(jobId) {
    const sequence = ++detailSequence;
    selectedJobId = jobId;
    detailLoading = true;
    jobDetail = undefined;
    detailError = undefined;
    detailRequestId = undefined;

    try {
      const response = await getJobDetail(jobId);
      if (sequence !== detailSequence) return;
      jobDetail = response.job;
      detailRequestId = response.request_id;
    } catch (caught) {
      if (sequence !== detailSequence) return;
      detailError =
        caught instanceof ApiError
          ? {
              title:
                caught.code === 'job_not_found' ? '找不到職缺' : '無法載入職缺',
              message: caught.message,
              requestId: caught.requestId
            }
          : {
              title: '無法連線到職缺服務',
              message: '目前無法連線到職缺服務，請稍後再試。'
            };
    } finally {
      if (sequence === detailSequence) detailLoading = false;
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
      <p class="product-note">資料連線展示</p>
    </div>
  </header>

  <section class="workspace" aria-labelledby="search-title">
    <div class="intro">
      <h1 id="search-title">搜尋合適的職缺</h1>
      <p>輸入關鍵字；需要縮小範圍時，再加入地區或職務代碼。</p>
      <aside class="demo-notice" role="note" aria-label="目前功能限制">
        <strong>目前為資料連線展示</strong>
        <p>搜尋條件尚未參與排序，所有搜尋會固定回傳相同 10 筆職缺。</p>
      </aside>
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

  <div class="results-layout">
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
              <button
                type="button"
                class="result-button"
                class:selected={selectedJobId === result.job_id}
                aria-pressed={selectedJobId === result.job_id}
                aria-label={`第 ${result.rank} 筆，查看職缺 ${result.job_id}`}
                onclick={() => void selectJob(result.job_id)}
              >
                <span class="rank" aria-hidden="true">{result.rank}</span>
                <span class="result-copy">
                  <span class="result-label">職缺 ID</span>
                  <strong>{result.job_id}</strong>
                </span>
                <span class="result-action" aria-hidden="true">查看</span>
              </button>
            </li>
          {/each}
        </ol>
      {:else}
        <div class="message">
          <strong>準備載入展示資料</strong>
          <p>送出任意有效關鍵字後，會顯示固定的 10 筆職缺。</p>
        </div>
      {/if}
    </section>

    <section
      class="detail"
      aria-labelledby="detail-title"
      aria-live="polite"
      aria-busy={detailLoading}
    >
      <div class="section-heading">
        <h2 id="detail-title">職缺詳細資訊</h2>
        {#if detailRequestId}<span class="request-id">{detailRequestId}</span
          >{/if}
      </div>

      {#if detailLoading}
        <div class="detail-skeleton skeleton" role="status">
          <span class="sr-only">正在載入職缺詳細資訊</span>
          <span></span><span></span><span></span>
        </div>
      {:else if detailError}
        <div class="message error" role="alert">
          <strong>{detailError.title}</strong>
          <p>{detailError.message}</p>
          {#if detailError.requestId}<code>{detailError.requestId}</code>{/if}
        </div>
      {:else if jobDetail}
        <article>
          <div class="detail-hero">
            <p class="eyebrow">{displayValue(jobDetail.job_attribute)}</p>
            <h3>{jobDetail.title}</h3>
            <p class="salary">{jobDetail.salary_text}</p>
            <dl class="summary-grid">
              <div>
                <dt>工作地點</dt>
                <dd>{displayValue(jobDetail.work_city)}</dd>
              </div>
              <div>
                <dt>工作時段</dt>
                <dd>{displayValue(jobDetail.work_hours)}</dd>
              </div>
              <div>
                <dt>時段說明</dt>
                <dd>{displayValue(jobDetail.work_hours_description)}</dd>
              </div>
              <div>
                <dt>職缺 ID</dt>
                <dd>{jobDetail.job_id}</dd>
              </div>
              <div>
                <dt>廠商 ID</dt>
                <dd>{jobDetail.vendor_id}</dd>
              </div>
              <div>
                <dt>來源更新時間</dt>
                <dd>{jobDetail.source_modified_at.replace('T', ' ')}</dd>
              </div>
            </dl>
          </div>

          <section class="description" aria-labelledby="description-title">
            <h4 id="description-title">工作內容</h4>
            <p>{displayValue(jobDetail.description)}</p>
          </section>

          {#each detailGroups as group (group.title)}
            <section
              class="detail-group"
              aria-labelledby={`group-${group.fields[0].key}`}
            >
              <h4 id={`group-${group.fields[0].key}`}>{group.title}</h4>
              <dl>
                {#each group.fields as field (field.key)}
                  <div>
                    <dt>{field.label}</dt>
                    <dd>{fieldValue(jobDetail, field.key)}</dd>
                  </div>
                {/each}
              </dl>
            </section>
          {/each}
        </article>
      {:else}
        <div class="message detail-empty">
          <strong>尚未選取職缺</strong>
          <p>先載入展示資料，再從搜尋結果選擇一筆職缺查看完整內容。</p>
        </div>
      {/if}
    </section>
  </div>
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
    width: min(100% - 2rem, 76rem);
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
  .product-note,
  .demo-notice p,
  .eyebrow {
    margin: 0;
  }

  .product-name {
    font-weight: 700;
  }

  .product-note,
  .intro > p,
  small,
  .result-label,
  .message p,
  dt {
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
  h3,
  h4,
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

  .intro > p {
    max-width: 34rem;
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

  .results-layout {
    display: grid;
    grid-template-columns: minmax(16rem, 0.72fr) minmax(0, 1.28fr);
    gap: clamp(2rem, 5vw, 4.5rem);
    padding-top: 2.5rem;
    align-items: start;
  }

  .section-heading {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 1rem;
    min-height: 2rem;
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

  .message strong,
  .message p {
    display: block;
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
    margin: 0;
    padding: 0;
    list-style: none;
    border-top: 1px solid oklch(0.9 0.01 340);
  }

  li {
    border-bottom: 1px solid oklch(0.9 0.01 340);
  }

  .result-button {
    display: grid;
    grid-template-columns: 2rem minmax(0, 1fr) auto;
    width: 100%;
    min-height: 4.5rem;
    align-items: center;
    gap: 1rem;
    border-radius: 0;
    padding: 0.75rem 0.6rem;
    background: transparent;
    color: inherit;
    text-align: left;
  }

  .result-button:hover,
  .result-button.selected {
    background: oklch(0.97 0.02 340);
  }

  .result-button.selected {
    box-shadow: inset 3px 0 oklch(0.55 0.21 340);
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

  .result-copy {
    min-width: 0;
  }

  .result-copy strong {
    display: block;
    overflow-wrap: anywhere;
  }

  .result-label {
    display: block;
    margin-bottom: 0.15rem;
    font-size: 0.75rem;
  }

  .result-action {
    color: oklch(0.42 0.12 340);
    font-size: 0.78rem;
    font-weight: 700;
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
    padding: 0;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }

  .detail-skeleton span:first-child {
    height: 8rem;
  }

  .detail-skeleton span:nth-child(2) {
    height: 12rem;
  }

  article {
    display: grid;
    gap: 2rem;
  }

  .detail-hero {
    padding-bottom: 2rem;
    border-bottom: 1px solid oklch(0.9 0.01 340);
  }

  .eyebrow {
    color: oklch(0.48 0.15 340);
    font-size: 0.78rem;
    font-weight: 800;
    letter-spacing: 0.04em;
  }

  h3 {
    margin: 0.45rem 0 0;
    font-size: clamp(1.55rem, 3vw, 2.15rem);
    line-height: 1.15;
    letter-spacing: -0.025em;
  }

  .salary {
    margin: 0.75rem 0 0;
    color: oklch(0.42 0.14 340);
    font-weight: 750;
  }

  dl,
  dd {
    margin: 0;
  }

  .summary-grid,
  .detail-group dl {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 1.15rem 1.75rem;
  }

  .summary-grid {
    margin-top: 1.75rem;
  }

  dt {
    margin-bottom: 0.3rem;
    font-size: 0.75rem;
    font-weight: 650;
  }

  dd {
    line-height: 1.5;
    overflow-wrap: anywhere;
  }

  h4 {
    margin: 0 0 1rem;
    font-size: 1rem;
  }

  .description,
  .detail-group {
    padding-bottom: 2rem;
    border-bottom: 1px solid oklch(0.9 0.01 340);
  }

  .description p {
    max-width: 72ch;
    margin: 0;
    line-height: 1.75;
    white-space: pre-wrap;
  }

  @keyframes pulse {
    to {
      background: oklch(0.98 0.005 340);
    }
  }

  @media (max-width: 58rem) {
    .results-layout {
      grid-template-columns: minmax(14rem, 0.85fr) minmax(0, 1.15fr);
      gap: 2rem;
    }

    .summary-grid,
    .detail-group dl {
      grid-template-columns: 1fr;
    }
  }

  @media (max-width: 46rem) {
    main {
      width: min(100% - 1.5rem, 76rem);
      padding-top: 1.25rem;
    }

    header {
      padding-bottom: 2.5rem;
    }

    .workspace,
    .results-layout {
      grid-template-columns: 1fr;
      gap: 2rem;
    }

    .workspace {
      padding-bottom: 3rem;
    }

    .filters {
      grid-template-columns: 1fr;
    }

    form > button {
      width: 100%;
    }

    .detail {
      padding-top: 1rem;
      border-top: 1px solid oklch(0.91 0.008 340);
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
