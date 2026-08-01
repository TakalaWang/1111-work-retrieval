import { describe, expect, it, vi } from 'vitest';

import { SearchApiError, searchJobs, serializeSearch } from './search';

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

  it.each([
    {
      request_id: 'req_invalid',
      result: [{ job_id: 'job-1', rank: 1 }]
    },
    {
      request_id: 'req_invalid',
      result: [
        { job_id: '1', rank: 1 },
        { job_id: '1', rank: 2 }
      ]
    },
    {
      request_id: 'req_invalid',
      result: [{ job_id: '1', rank: 2 }]
    },
    {
      request_id: 'req_invalid',
      result: Array.from({ length: 11 }, (_, index) => ({
        job_id: String(index),
        rank: index + 1
      }))
    }
  ])('rejects search results that violate ranking invariants', async (body) => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'content-type': 'application/json' }
      })
    );

    await expect(
      searchJobs({ query: '工程師', location_code: [], duty_code: [] }, fetcher)
    ).rejects.toEqual(new SearchApiError('搜尋服務回傳了無法辨識的內容。'));
  });

  it('maps invalid JSON to the API boundary error', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(new Response('{', { status: 200 }));

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
});
