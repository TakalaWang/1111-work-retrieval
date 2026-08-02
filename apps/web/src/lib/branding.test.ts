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
    expect(page).toContain('選好條件、輸入關鍵字，把適合的職缺摘回家。');
    expect(page).toContain('搜尋職缺');
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
});
