import { describe, expect, it, vi } from 'vitest';

import {
  presentJob,
  pullJob,
  SearchApiError,
  searchJobDetails,
  searchJobs,
  serializeSearch
} from './search';

describe('search API boundary', () => {
  it('trims the query and serializes deduplicated code lists', () => {
    expect(
      serializeSearch({
        query: '  後端工程師  ',
        locationCodes: '100100, 100200\n100100',
        dutyCodes: '140200，140300'
      })
    ).toEqual({
      query: '後端工程師',
      location_code: ['100100', '100200'],
      duty_code: ['140200', '140300']
    });
  });

  it('posts the committed request shape to the relative API path', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ request_id: 'req_1', result: [] }), {
        status: 200,
        headers: { 'content-type': 'application/json' }
      })
    );
    const request = { query: '工程師', location_code: [], duty_code: [] };

    await expect(searchJobs(request, fetcher)).resolves.toEqual({
      request_id: 'req_1',
      result: []
    });
    expect(fetcher).toHaveBeenCalledWith('/api/v1/jobs/search', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(request)
    });
  });

  it('preserves the request id from API errors', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          request_id: 'req_failed',
          error: {
            code: 'search_unavailable',
            message: '暫時無法搜尋。',
            details: []
          }
        }),
        { status: 503, headers: { 'content-type': 'application/json' } }
      )
    );

    await expect(
      searchJobs({ query: '工程師', location_code: [], duty_code: [] }, fetcher)
    ).rejects.toEqual(new SearchApiError('暫時無法搜尋。', 'req_failed'));
  });

  it('rejects a malformed successful response', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({ request_id: 'req_invalid', result: null }),
        {
          status: 200,
          headers: { 'content-type': 'application/json' }
        }
      )
    );

    await expect(
      searchJobs({ query: '工程師', location_code: [], duty_code: [] }, fetcher)
    ).rejects.toEqual(new SearchApiError('搜尋服務回傳了無法辨識的內容。'));
  });

  it('rejects a malformed error envelope', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          request_id: 'req_invalid',
          error: { code: 'search_unavailable', message: 503, details: [] }
        }),
        { status: 503, headers: { 'content-type': 'application/json' } }
      )
    );

    await expect(
      searchJobs({ query: '工程師', location_code: [], duty_code: [] }, fetcher)
    ).rejects.toEqual(new SearchApiError('搜尋服務回傳了無法辨識的錯誤。'));
  });

  it('posts one job id to the pull endpoint', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          job_id: '53256270',
          details: { 職務名稱: '口譯人員' }
        }),
        { status: 200, headers: { 'content-type': 'application/json' } }
      )
    );

    await expect(pullJob('53256270', fetcher)).resolves.toEqual({
      job_id: '53256270',
      details: { 職務名稱: '口譯人員' }
    });
    expect(fetcher).toHaveBeenCalledWith('/api/v1/jobs/pull', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ job_id: '53256270' })
    });
  });

  it('rejects malformed pull details', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          job_id: '53256270',
          details: { 職務名稱: 7 }
        }),
        { status: 200, headers: { 'content-type': 'application/json' } }
      )
    );

    await expect(pullJob('53256270', fetcher)).rejects.toEqual(
      new SearchApiError('職缺資料服務回傳了無法辨識的內容。')
    );
  });

  it('selects useful populated CSV fields for presentation', () => {
    expect(
      presentJob(
        {
          job_id: '53256270',
          details: {
            職務名稱: '後端工程師',
            工作城市: '台北市',
            薪資: '月薪 55,000 元',
            職務小類: '後端開發',
            職務中類: '軟體工程',
            職務內容: '開發 API<br>維護服務',
            工作技能: null,
            廠商編號: '123'
          }
        },
        1
      )
    ).toEqual({
      jobId: '53256270',
      rank: 1,
      title: '後端工程師',
      city: '台北市',
      salary: '月薪 55,000 元',
      category: '後端開發',
      description: '開發 API 維護服務',
      experience: undefined,
      education: undefined,
      skills: undefined,
      updatedAt: undefined
    });
  });

  it('keeps ranked jobs ordered when one pull request fails', async () => {
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      if (input === '/api/v1/jobs/search') {
        return new Response(
          JSON.stringify({
            request_id: 'req_1',
            result: [
              { job_id: '20', rank: 1 },
              { job_id: '10', rank: 2 },
              { job_id: '30', rank: 3 }
            ]
          }),
          { status: 200, headers: { 'content-type': 'application/json' } }
        );
      }

      const request = JSON.parse(String(init?.body)) as { job_id: string };
      if (request.job_id === '10') {
        return new Response(
          JSON.stringify({
            request_id: 'req_pull_failed',
            error: {
              code: 'job_not_found',
              message: '找不到職缺。',
              details: []
            }
          }),
          { status: 404, headers: { 'content-type': 'application/json' } }
        );
      }
      return new Response(
        JSON.stringify({
          job_id: request.job_id,
          details: { 職務名稱: request.job_id === '20' ? '第一筆' : '第三筆' }
        }),
        { status: 200, headers: { 'content-type': 'application/json' } }
      );
    });

    await expect(searchJobDetails(' 工程師 ', fetcher)).resolves.toEqual({
      requestId: 'req_1',
      failedCount: 1,
      jobs: [
        expect.objectContaining({ jobId: '20', rank: 1, title: '第一筆' }),
        expect.objectContaining({ jobId: '30', rank: 3, title: '第三筆' })
      ]
    });
  });

  it('reports an error when every job detail request fails', async () => {
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      if (input === '/api/v1/jobs/search') {
        return new Response(
          JSON.stringify({
            request_id: 'req_all_failed',
            result: [
              { job_id: '20', rank: 1 },
              { job_id: '10', rank: 2 }
            ]
          }),
          { status: 200, headers: { 'content-type': 'application/json' } }
        );
      }
      return new Response(
        JSON.stringify({
          request_id: 'req_pull_failed',
          error: {
            code: 'job_not_found',
            message: '找不到職缺。',
            details: []
          }
        }),
        { status: 404, headers: { 'content-type': 'application/json' } }
      );
    });

    await expect(searchJobDetails('工程師', fetcher)).rejects.toEqual(
      new SearchApiError('找到職缺，但詳細資料目前無法載入。', 'req_all_failed')
    );
  });
});
