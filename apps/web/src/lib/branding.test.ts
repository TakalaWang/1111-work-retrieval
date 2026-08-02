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
    expect(page).toContain('選好條件、輸入關鍵字，把適合的職缺「摘」回家。');
    expect(page).toContain('搜尋職缺');
    expect(page).not.toContain('讓每一次搜尋，都更接近好結果');
  });

  it('keeps the competition identity out of the hero', () => {
    const hero = page.slice(
      page.indexOf('<section class="search"'),
      page.indexOf('</section>')
    );
    expect(hero).not.toContain('蝦咪係Ai');
    expect(hero).not.toContain('雲湧智生');
    expect(hero).not.toContain('1111 智慧求職');
  });

  it('orders the integrated controls by duty, location, date, query, and action', () => {
    const searchShellStart = page.indexOf('<div class="search-shell">');
    const searchButtonStart = page.indexOf(
      'class="search-button"',
      searchShellStart
    );
    const searchShell = page.slice(
      searchShellStart,
      page.indexOf('</div>', searchButtonStart)
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

  it('allows filter panels to extend beyond the hero', () => {
    const searchStart = page.indexOf('  .search {');
    const searchStyles = page.slice(
      searchStart,
      page.indexOf('\n  }', searchStart) + 4
    );

    expect(searchStyles).toContain('overflow: visible;');
    expect(searchStyles).not.toContain('overflow: hidden;');
  });

  it('uses compact desktop spacing between the brand and headline', () => {
    expect(page).toContain(
      'padding: clamp(1rem, 2vw, 1.5rem) clamp(1rem, 5vw, 4rem)'
    );
    expect(page).toContain('margin: clamp(1.5rem, 3vw, 2.5rem) auto 0;');
  });

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
});
