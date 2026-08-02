<script lang="ts">
  import { tick } from 'svelte';

  import { childRows, type TaxonomyOption } from './taxonomy';

  let {
    id,
    label,
    options,
    selected = $bindable([]),
    disabled = false,
    align = 'start'
  }: {
    id: string;
    label: string;
    options: readonly TaxonomyOption[];
    selected?: string[];
    disabled?: boolean;
    align?: 'start' | 'end';
  } = $props();

  let root: HTMLDivElement;
  let trigger: HTMLButtonElement;
  let navigationHeading = $state<HTMLHeadingElement>();
  let open = $state(false);
  let currentPath = $state<string[]>([]);
  let visibleRows = $derived(childRows(options, currentPath));
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
    currentPath = [];
    if (returnFocus) trigger?.focus();
  }

  async function navigate(path: string[]) {
    currentPath = path;
    await tick();
    navigationHeading?.focus();
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
    <svg class="chevron" aria-hidden="true" viewBox="0 0 20 20">
      <path d="m5 7.5 5 5 5-5" />
    </svg>
  </button>

  {#if open}
    <div class="panel" id={`${id}-panel`}>
      <div class="navigation">
        {#if currentPath.length > 0}
          <button
            type="button"
            class="back"
            onclick={() => void navigate(currentPath.slice(0, -1))}
          >
            <svg aria-hidden="true" viewBox="0 0 20 20">
              <path d="m12.5 5-5 5 5 5" />
            </svg>
            上一層
          </button>
        {/if}
        <h3 bind:this={navigationHeading} tabindex="-1">
          {currentPath.length > 0 ? currentPath.join(' / ') : '全部分類'}
        </h3>
      </div>

      <fieldset>
        <legend class="sr-only">{label}</legend>
        <div class="options">
          {#each visibleRows as row (row.key)}
            <div class="option">
              {#if row.option}
                <label class="selection">
                  <input
                    type="checkbox"
                    checked={selected.includes(row.option.code)}
                    onchange={(event) => {
                      if (row.option)
                        toggle(row.option.code, event.currentTarget.checked);
                    }}
                  />
                  <span>
                    <span class="option-name">{row.path.at(-1)}</span>
                    <code>{row.option.code}</code>
                  </span>
                </label>
              {:else}
                <div class="group-label">
                  <span class="option-name">{row.path.at(-1)}</span>
                  <span class="group-kind">分類</span>
                </div>
              {/if}
              {#if row.hasChildren}
                <button
                  type="button"
                  class="drill"
                  aria-label={`查看 ${row.path.at(-1)} 子分類`}
                  onclick={() => void navigate(row.path)}
                >
                  <svg aria-hidden="true" viewBox="0 0 20 20">
                    <path d="m7.5 5 5 5-5 5" />
                  </svg>
                </button>
              {/if}
            </div>
          {:else}
            <p class="empty">此分類沒有下一層選項</p>
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
    gap: 0.4rem;
  }

  .field-label {
    font-weight: 700;
  }

  button,
  input {
    font: inherit;
  }

  .trigger {
    display: flex;
    width: 100%;
    min-width: 0;
    min-height: 2.75rem;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    border: 1px solid #cfd1d5;
    border-radius: 0.5rem;
    padding: 0.35rem 0.7rem;
    background: #fff;
    color: #4b4e54;
    cursor: pointer;
    text-align: left;
  }

  .trigger:hover:not(:disabled) {
    border-color: #aeb1b7;
  }

  .trigger:focus-visible,
  .selection input:focus-visible,
  .navigation button:focus-visible,
  .drill:focus-visible,
  .actions button:focus-visible {
    outline: 3px solid rgb(215 25 95 / 18%);
    outline-offset: 1px;
  }

  .trigger:disabled {
    background: #f1f1f2;
    color: #777a80;
    cursor: not-allowed;
  }

  .chevron {
    width: 1.1rem;
    height: 1.1rem;
    color: #767980;
    fill: none;
    stroke: currentcolor;
    stroke-linecap: round;
    stroke-linejoin: round;
    stroke-width: 1.8;
  }

  .panel {
    position: absolute;
    z-index: 10;
    top: calc(100% + 0.4rem);
    left: 0;
    width: min(22rem, calc(100vw - 2rem));
    border: 1px solid #d6d7da;
    border-radius: 0.65rem;
    padding: 0.65rem;
    background: #fff;
    box-shadow: 0 12px 32px rgb(32 33 36 / 16%);
  }

  .align-end .panel {
    right: 0;
    left: auto;
  }

  .navigation {
    display: grid;
    gap: 0.35rem;
    min-height: 2.75rem;
    align-content: center;
    padding: 0 0.35rem 0.55rem;
    border-bottom: 1px solid #ececef;
  }

  .navigation h3 {
    margin: 0;
    color: #34363b;
    font-size: 0.85rem;
    line-height: 1.4;
  }

  .navigation h3:focus-visible {
    outline: 3px solid rgb(215 25 95 / 18%);
    outline-offset: 2px;
  }

  .back {
    display: inline-flex;
    width: fit-content;
    min-height: 2.75rem;
    align-items: center;
    gap: 0.2rem;
    border: 0;
    padding: 0 0.25rem;
    background: transparent;
    color: #686b72;
    font-weight: 700;
    cursor: pointer;
  }

  .back svg,
  .drill svg {
    width: 1.1rem;
    height: 1.1rem;
    fill: none;
    stroke: currentcolor;
    stroke-linecap: round;
    stroke-linejoin: round;
    stroke-width: 1.8;
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

  .option {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    min-height: 2.75rem;
    align-items: center;
    border-radius: 0.4rem;
    color: #34363b;
  }

  .option:hover {
    background: #f7f1f4;
  }

  .selection {
    display: grid;
    min-width: 0;
    min-height: 2.75rem;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: center;
    gap: 0.65rem;
    padding: 0.45rem 0.5rem;
    cursor: pointer;
  }

  .group-label {
    display: grid;
    min-width: 0;
    min-height: 2.75rem;
    align-content: center;
    gap: 0.1rem;
    padding: 0.45rem 0.5rem;
    font-weight: 700;
  }

  .group-kind {
    color: #767980;
    font-size: 0.72rem;
    font-weight: 400;
  }

  .selection input {
    width: 1.1rem;
    height: 1.1rem;
    accent-color: #d7195f;
  }

  .option-name {
    display: block;
    line-height: 1.35;
    overflow-wrap: anywhere;
  }

  .drill {
    display: grid;
    width: 2.75rem;
    height: 2.75rem;
    place-items: center;
    border: 0;
    border-radius: 0.4rem;
    padding: 0;
    background: transparent;
    color: #686b72;
    cursor: pointer;
  }

  .drill:hover {
    background: #efe7eb;
    color: #b81250;
  }

  code {
    display: block;
    margin-top: 0.15rem;
    color: #767980;
    font-size: 0.72rem;
  }

  .empty {
    margin: 0;
    padding: 1rem 0.5rem;
    color: #767980;
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
    color: #686b72;
  }

  .clear:disabled {
    color: #abadb2;
    cursor: not-allowed;
  }

  .done {
    background: #d7195f;
    color: #fff;
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
    .panel,
    .align-end .panel {
      position: static;
      width: 100%;
    }
  }
</style>
