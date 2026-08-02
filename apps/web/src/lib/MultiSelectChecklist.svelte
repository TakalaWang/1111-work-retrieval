<script lang="ts">
  type Option = { code: string; path: string[] };

  let {
    id,
    label,
    searchPlaceholder,
    options,
    selected = $bindable([]),
    disabled = false,
    align = 'start'
  }: {
    id: string;
    label: string;
    searchPlaceholder: string;
    options: readonly Option[];
    selected?: string[];
    disabled?: boolean;
    align?: 'start' | 'end';
  } = $props();

  let root: HTMLDivElement;
  let trigger: HTMLButtonElement;
  let open = $state(false);
  let keyword = $state('');
  let normalizedKeyword = $derived(keyword.trim().toLocaleLowerCase('zh-TW'));
  let matchedOptions = $derived(
    normalizedKeyword
      ? options.filter(
          (option) =>
            option.code.includes(normalizedKeyword) ||
            option.path.some((part) =>
              part.toLocaleLowerCase('zh-TW').includes(normalizedKeyword)
            )
        )
      : options.filter((option) => selected.includes(option.code))
  );
  let visibleOptions = $derived(
    normalizedKeyword ? matchedOptions.slice(0, 100) : matchedOptions
  );
  let firstSelection = $derived(
    options.find((option) => option.code === selected[0])
  );
  let summary = $derived(
    selected.length === 0
      ? '不限'
      : selected.length === 1
        ? (firstSelection?.path.at(-1) ?? selected[0])
        : `已選 ${selected.length} 項`
  );

  function toggle(code: string, checked: boolean) {
    selected = checked
      ? [...selected, code]
      : selected.filter((value) => value !== code);
  }

  function close(returnFocus = true) {
    open = false;
    keyword = '';
    if (returnFocus) trigger?.focus();
  }

  function togglePanel() {
    if (open) close(false);
    else open = true;
  }

  function handleOutside(event: PointerEvent) {
    if (open && !root.contains(event.target as Node)) close(false);
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape' && open) {
      event.preventDefault();
      close();
    }
  }

  $effect(() => {
    if (disabled && open) close(false);
  });
</script>

<svelte:window onpointerdown={handleOutside} onkeydown={handleKeydown} />

<div class="field" class:align-end={align === 'end'} bind:this={root}>
  <span class="field-label" id={`${id}-label`}>{label}</span>
  <button
    class="trigger"
    type="button"
    bind:this={trigger}
    aria-labelledby={`${id}-label ${id}-summary`}
    aria-expanded={open}
    aria-controls={`${id}-panel`}
    {disabled}
    onclick={togglePanel}
  >
    <span id={`${id}-summary`}>{summary}</span>
    <span class="chevron" aria-hidden="true">⌄</span>
  </button>

  {#if open}
    <div class="panel" id={`${id}-panel`}>
      <label class="search-label" for={`${id}-search`}>
        <span class="sr-only">{searchPlaceholder}</span>
        <input
          id={`${id}-search`}
          bind:value={keyword}
          type="search"
          autocomplete="off"
          placeholder={searchPlaceholder}
          onkeydown={(event) => {
            if (event.key === 'Enter') event.preventDefault();
          }}
        />
      </label>

      {#if normalizedKeyword}
        <p class="match-count">
          找到 {matchedOptions.length} 項{matchedOptions.length > 100
            ? '，顯示前 100 項'
            : ''}
        </p>
      {/if}

      <fieldset>
        <legend class="sr-only">{label}</legend>
        <div class="options">
          {#each visibleOptions as option (option.code)}
            <label class="option">
              <input
                type="checkbox"
                checked={selected.includes(option.code)}
                onchange={(event) =>
                  toggle(option.code, event.currentTarget.checked)}
              />
              <span>
                <span class="option-path">{option.path.join(' / ')}</span>
                <code>{option.code}</code>
              </span>
            </label>
          {:else}
            <p class="empty">
              {normalizedKeyword
                ? '找不到符合的選項'
                : '輸入名稱或代碼開始搜尋'}
            </p>
          {/each}
        </div>
      </fieldset>

      <div class="actions">
        <button
          type="button"
          class="clear"
          disabled={selected.length === 0}
          onclick={() => (selected = [])}
        >
          清除
        </button>
        <button type="button" class="done" onclick={() => close()}>完成</button>
      </div>
    </div>
  {/if}
</div>

<style>
  .field {
    position: relative;
    display: grid;
    min-width: 0;
    align-content: center;
    gap: 0.15rem;
    padding: 0.7rem 0.9rem;
    border-right: 1px solid #d7dfd8;
  }

  .field-label {
    color: #748078;
    font-size: 0.68rem;
    font-weight: 850;
    letter-spacing: 0.06em;
  }

  button,
  input {
    font: inherit;
  }

  .trigger {
    display: flex;
    width: 100%;
    min-width: 0;
    min-height: 1.5rem;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    border: 0;
    padding: 0;
    background: transparent;
    color: #263b2c;
    font-weight: 800;
    cursor: pointer;
    text-align: left;
  }

  .trigger:hover:not(:disabled) {
    color: #3f754d;
  }

  .trigger:focus-visible,
  .panel input:focus-visible,
  .actions button:focus-visible {
    outline: 3px solid rgb(36 74 48 / 22%);
    outline-offset: 4px;
    border-radius: 0.25rem;
  }

  .trigger:disabled {
    color: #89938c;
    cursor: not-allowed;
  }

  .chevron {
    color: #617267;
    font-size: 1.1rem;
  }

  .panel {
    position: absolute;
    z-index: 10;
    top: calc(100% + 0.4rem);
    left: 0;
    width: min(22rem, calc(100vw - 2rem));
    border: 2px solid #244a30;
    border-radius: 0.85rem;
    padding: 0.65rem;
    background: #fff;
    box-shadow: 6px 7px 0 #b9d7b4;
  }

  .align-end .panel {
    right: 0;
    left: auto;
  }

  .search-label input {
    width: 100%;
    min-height: 2.75rem;
    border: 1px solid #aebcaf;
    border-radius: 0.45rem;
    padding: 0.5rem 0.65rem;
    color: #203b29;
  }

  fieldset {
    min-width: 0;
    margin: 0;
    border: 0;
    padding: 0;
  }

  .options {
    max-height: min(20rem, 52vh);
    overflow-y: auto;
    margin-top: 0.5rem;
  }

  .match-count {
    margin: 0.45rem 0 0;
    color: #6d796f;
    font-size: 0.75rem;
  }

  .option {
    display: grid;
    grid-template-columns: auto 1fr;
    min-height: 2.75rem;
    align-items: center;
    gap: 0.65rem;
    border-radius: 0.4rem;
    padding: 0.45rem 0.5rem;
    color: #263b2c;
    cursor: pointer;
  }

  .option:hover {
    background: #edf5e8;
  }

  .option input {
    width: 1.1rem;
    height: 1.1rem;
    accent-color: #f1a52a;
  }

  .option-path {
    display: block;
    line-height: 1.35;
  }

  code {
    display: block;
    margin-top: 0.15rem;
    color: #6d796f;
    font-size: 0.72rem;
  }

  .empty {
    margin: 0;
    padding: 1rem 0.5rem;
    color: #6d796f;
    text-align: center;
  }

  .actions {
    display: flex;
    justify-content: space-between;
    gap: 0.5rem;
    margin-top: 0.55rem;
    padding-top: 0.55rem;
    border-top: 1px solid #ececef;
  }

  .actions button {
    min-height: 2.75rem;
    border: 0;
    border-radius: 0.45rem;
    padding: 0.45rem 0.85rem;
    font-weight: 700;
    cursor: pointer;
  }

  .clear {
    background: transparent;
    color: #4f6255;
  }

  .clear:disabled {
    color: #abadb2;
    cursor: not-allowed;
  }

  .done {
    border: 2px solid #244a30 !important;
    background: #f1a52a;
    color: #203b29;
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

  @media (max-width: 36rem) {
    .field {
      border-right: 0;
      border-bottom: 1px solid #d7dfd8;
    }

    .panel,
    .align-end .panel {
      position: static;
      width: 100%;
    }
  }
</style>
