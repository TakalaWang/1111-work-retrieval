import { describe, expect, it, vi } from 'vitest';

import {
  ApiError,
  getJob,
  presentJob,
  searchJobDetails,
  searchJobs,
  serializeSearch
} from './search';

const REQUEST = {
  query: '工程師',
  search_date: '2026-06-08',
  location_code: [],
  duty_code: []
};

describe('search API boundary', () => {
  it('trims the query and omits a blank search date', () => {
    expect(
      serializeSearch({
        query: '  後端工程師  ',
        searchDate: ''
      })
    ).toEqual({
      query: '後端工程師',
      location_code: [],
      duty_code: []
    });
  });

  it('serializes an explicitly selected search date', () => {
    expect(
      serializeSearch({ query: '工程師', searchDate: '2026-06-09' })
    ).toEqual({
      query: '工程師',
      search_date: '2026-06-09',
      location_code: [],
      duty_code: []
    });
  });

  it('posts the committed request shape to the relative API path', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ request_id: 'req_1', result: [] }), {
        status: 200,
        headers: { 'content-type': 'application/json' }
      })
    );
    const request = REQUEST;

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

    await expect(searchJobs(REQUEST, fetcher)).rejects.toEqual(
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

    await expect(searchJobs(REQUEST, fetcher)).rejects.toEqual(
      new ApiError('搜尋服務回傳了無法辨識的內容。')
    );
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
      await expect(searchJobs(REQUEST, fetcher)).rejects.toEqual(
        new ApiError('搜尋服務回傳了無法辨識的內容。')
      );
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
      await expect(searchJobs(REQUEST, fetcher)).rejects.toEqual(
        new ApiError('搜尋服務回傳了無法辨識的內容。')
      );
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

    await expect(searchJobs(REQUEST, fetcher)).rejects.toEqual(
      new ApiError('搜尋服務回傳了無法辨識的內容。')
    );
  });

  it('maps invalid JSON to the API boundary error', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(new Response('{', { status: 200 }));

    await expect(searchJobs(REQUEST, fetcher)).rejects.toEqual(
      new ApiError('搜尋服務回傳了無法辨識的內容。')
    );
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

    await expect(searchJobs(REQUEST, fetcher)).rejects.toEqual(
      new ApiError('搜尋服務回傳了無法辨識的錯誤。')
    );
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
      await expect(searchJobs(REQUEST, fetcher)).rejects.toEqual(
        new ApiError('搜尋服務回傳了無法辨識的錯誤。')
      );
    }
  });

  it('reads and presents one persisted job', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          job_id: '53256270',
          details: {
            職務名稱: '後端工程師',
            工作城市: '台北市',
            薪資: '月薪 55,000 元',
            職務小類: '後端開發',
            職務內容: '開發 API<br />維護服務',
            工作經驗需求: '2 年',
            學歷需求: '大學',
            工作技能: 'Python',
            職缺最後修改時間: '2026-06-08T12:34:56.789'
          }
        }),
        { status: 200, headers: { 'content-type': 'application/json' } }
      )
    );

    const response = await getJob('53256270', fetcher);
    expect(fetcher).toHaveBeenCalledWith('/api/v1/job-details/53256270');
    expect(presentJob(response, 1)).toEqual({
      jobId: '53256270',
      rank: 1,
      title: '後端工程師',
      city: '台北市',
      salary: '月薪 55,000 元',
      category: '後端開發',
      description: '開發 API 維護服務',
      experience: '2 年',
      education: '大學',
      skills: 'Python',
      updatedAt: '2026-06-08T12:34:56.789'
    });
  });

  it('keeps successful detail cards when another detail fails', async () => {
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      if (input === '/api/v1/jobs/search') {
        return new Response(
          JSON.stringify({
            request_id: 'req_1',
            result: [
              { job_id: '20', rank: 1 },
              { job_id: '10', rank: 2 }
            ]
          }),
          { status: 200 }
        );
      }
      const jobId = String(input).replace('/api/v1/job-details/', '');
      if (jobId === '10')
        return new Response(
          JSON.stringify({
            request_id: 'req_detail',
            error: {
              code: 'job_not_found',
              message: '找不到職缺。',
              details: []
            }
          }),
          { status: 404 }
        );
      return new Response(
        JSON.stringify({ job_id: jobId, details: { 職務名稱: '第一筆' } }),
        { status: 200 }
      );
    });

    await expect(
      searchJobDetails({ query: ' 工程師 ', searchDate: '2026-06-08' }, fetcher)
    ).resolves.toEqual({
      requestId: 'req_1',
      failedCount: 1,
      jobs: [expect.objectContaining({ jobId: '20', rank: 1, title: '第一筆' })]
    });
  });

  it('fails when every detail request fails', async () => {
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      if (input === '/api/v1/jobs/search')
        return new Response(
          JSON.stringify({
            request_id: 'req_all_failed',
            result: [{ job_id: '20', rank: 1 }]
          }),
          { status: 200 }
        );
      return new Response(
        JSON.stringify({
          request_id: 'req_detail',
          error: { code: 'job_not_found', message: '找不到職缺。', details: [] }
        }),
        { status: 404 }
      );
    });

    await expect(
      searchJobDetails({ query: '工程師', searchDate: '2026-06-08' }, fetcher)
    ).rejects.toEqual(
      new ApiError('找到職缺，但詳細資料目前無法載入。', 'req_all_failed')
    );
  });
});
