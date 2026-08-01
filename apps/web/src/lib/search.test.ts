import { describe, expect, it, vi } from 'vitest';

import { ApiError, searchJobs, serializeSearch } from './search';

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

  it('forwards a caller-provided request timeout signal', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ request_id: 'req_1', result: [] }), {
        status: 200
      })
    );
    const signal = AbortSignal.timeout(1_000);
    const request = { query: '工程師', location_code: [], duty_code: [] };

    await searchJobs(request, fetcher, signal);

    expect(fetcher).toHaveBeenCalledWith('/api/v1/jobs/search', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(request),
      signal
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
    ).rejects.toEqual(
      new ApiError('暫時無法搜尋。', 'req_failed', 'search_unavailable')
    );
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
    ).rejects.toEqual(new ApiError('搜尋服務回傳了無法辨識的內容。'));
  });

  it('rejects duplicate IDs, non-contiguous ranks, blank IDs, and more than 10 results', async () => {
    const malformedResults = [
      [
        { job_id: '1', rank: 1 },
        { job_id: '1', rank: 2 }
      ],
      [
        { job_id: '1', rank: 1 },
        { job_id: '2', rank: 3 }
      ],
      [{ job_id: '   ', rank: 1 }],
      Array.from({ length: 11 }, (_, index) => ({
        job_id: String(index + 1),
        rank: index + 1
      }))
    ];

    for (const result of malformedResults) {
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
        new Response(JSON.stringify({ request_id: 'req_invalid', result }), {
          status: 200,
          headers: { 'content-type': 'application/json' }
        })
      );
      await expect(
        searchJobs(
          { query: '工程師', location_code: [], duty_code: [] },
          fetcher
        )
      ).rejects.toEqual(new ApiError('搜尋服務回傳了無法辨識的內容。'));
    }
  });

  it('rejects extra keys in search response and result items', async () => {
    const malformedPayloads = [
      { request_id: 'req_invalid', result: [], unexpected: true },
      {
        request_id: 'req_invalid',
        result: [{ job_id: '1', rank: 1, unexpected: true }]
      }
    ];

    for (const payload of malformedPayloads) {
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
        new Response(JSON.stringify(payload), {
          status: 200,
          headers: { 'content-type': 'application/json' }
        })
      );
      await expect(
        searchJobs(
          { query: '工程師', location_code: [], duty_code: [] },
          fetcher
        )
      ).rejects.toEqual(new ApiError('搜尋服務回傳了無法辨識的內容。'));
    }
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
    ).rejects.toEqual(new ApiError('搜尋服務回傳了無法辨識的內容。'));
  });

  it('maps invalid JSON to the API boundary error', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(new Response('{', { status: 200 }));

    await expect(
      searchJobs({ query: '工程師', location_code: [], duty_code: [] }, fetcher)
    ).rejects.toEqual(new ApiError('搜尋服務回傳了無法辨識的內容。'));
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
    ).rejects.toEqual(new ApiError('搜尋服務回傳了無法辨識的錯誤。'));
  });

  it('rejects extra keys at every error-envelope level', async () => {
    const baseError = {
      code: 'search_unavailable',
      message: '暫時無法搜尋。',
      details: [{ field: 'query', code: 'invalid', message: '無效。' }]
    };
    const malformedPayloads = [
      { request_id: 'req_invalid', error: baseError, unexpected: true },
      {
        request_id: 'req_invalid',
        error: { ...baseError, unexpected: true }
      },
      {
        request_id: 'req_invalid',
        error: {
          ...baseError,
          details: [{ ...baseError.details[0], unexpected: true }]
        }
      }
    ];

    for (const payload of malformedPayloads) {
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
        new Response(JSON.stringify(payload), {
          status: 503,
          headers: { 'content-type': 'application/json' }
        })
      );
      await expect(
        searchJobs(
          { query: '工程師', location_code: [], duty_code: [] },
          fetcher
        )
      ).rejects.toEqual(new ApiError('搜尋服務回傳了無法辨識的錯誤。'));
    }
  });
});
