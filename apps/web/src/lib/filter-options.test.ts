import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';

import { describe, expect, it } from 'vitest';

import filterOptions from './filter-options.json';
import { childRows } from './taxonomy';

describe('filter option asset', () => {
  it('keeps the fully verified taxonomy snapshot byte-exact', () => {
    const contents = readFileSync(
      new URL('./filter-options.json', import.meta.url)
    );
    expect(createHash('sha256').update(contents).digest('hex')).toBe(
      '3dd7c5f43a2cf856a3634da4975ab56350d6a92363a2d6e45f6661b74fa5c949'
    );
  });

  it('matches the committed source tables', () => {
    expect(filterOptions.schema_version).toBe(1);
    expect(filterOptions.sources).toEqual({
      locations: {
        file: '城市對照表.csv',
        sha256:
          '6fb964a02a5700df3e31235b1d9adf72f353a0c4885e52ab200e9bf0cf2bab4a',
        rows: 1077
      },
      duties: {
        file: '職務對照表.csv',
        sha256:
          '51654e460e17a49bde42a3a4e867a21656158799173a68330b7dcc8295a41619',
        rows: 691
      }
    });
    expect(filterOptions.locations).toHaveLength(1077);
    expect(filterOptions.duties).toHaveLength(691);
    expect(new Set(filterOptions.locations.map(({ code }) => code)).size).toBe(
      1077
    );
    expect(new Set(filterOptions.duties.map(({ code }) => code)).size).toBe(
      691
    );
  });

  it('preserves representative hierarchy paths and codes', () => {
    expect(
      filterOptions.locations.find(({ code }) => code === '100101')?.path
    ).toEqual(['台灣', '台北市', '中正區']);
    expect(
      filterOptions.duties.find(({ code }) => code === '140201')?.path
    ).toEqual(['電腦系統／資訊／軟硬體', '軟體工程', '軟體專案主管']);
  });

  it('keeps descendants reachable when a source parent has no code', () => {
    const topLevel = '電腦系統／資訊／軟硬體';
    const networkGroup = childRows(filterOptions.duties, [topLevel]).find(
      ({ path }) => path.at(-1) === '網路管理'
    );

    expect(networkGroup?.option).toBeUndefined();
    expect(networkGroup?.hasChildren).toBe(true);
    expect(
      childRows(filterOptions.duties, networkGroup?.path ?? []).map(
        ({ option }) => option?.code
      )
    ).toEqual(['140401', '140402', '140403', '140405']);
  });
});
