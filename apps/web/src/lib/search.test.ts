import { describe, expect, it, vi } from 'vitest';

import { ApiError, getJobDetail, searchJobs, serializeSearch } from './search';

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

const completeJob = {
  job_id: 'job/1',
  title: '後端工程師',
  description: '打造穩定 API',
  salary_text: '月薪 70,000 元',
  salary_min: '70000.00',
  salary_max: '90000.50',
  duty_major: '資訊軟體系統類',
  duty_middle: null,
  duty_minor: null,
  job_attribute: '全職',
  work_hours: '日班',
  work_hours_description: null,
  work_city: '台北市',
  education_requirement: '大學',
  major_requirement_1: null,
  major_requirement_2: null,
  major_requirement_3: null,
  experience_requirement: '2 年以上',
  language_1: '英文',
  language_1_listening: '中等',
  language_1_speaking: '中等',
  language_1_reading: '中等',
  language_1_writing: '中等',
  language_2: null,
  language_2_listening: null,
  language_2_speaking: null,
  language_2_reading: null,
  language_2_writing: null,
  computer_skills: 'Python、PostgreSQL',
  professional_certifications: null,
  work_skills: 'API 設計',
  additional_conditions: null,
  management_count: '無',
  requires_travel: '否',
  vendor_id: 'vendor-1',
  industry_major: '軟體業',
  industry_middle: null,
  industry_minor: null,
  source_modified_at: '2026-08-01T12:30:45.123000'
};

describe('job detail API boundary', () => {
  it('loads a complete generated job shape from the encoded relative URL', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({ request_id: 'req_job', job: completeJob }),
        {
          status: 200,
          headers: { 'content-type': 'application/json' }
        }
      )
    );

    await expect(getJobDetail('job/1', fetcher)).resolves.toEqual({
      request_id: 'req_job',
      job: completeJob
    });
    expect(fetcher).toHaveBeenCalledWith('/api/v1/jobs/job%2F1');
  });

  it('preserves request ID for not-found and service errors', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          request_id: 'req_missing',
          error: {
            code: 'job_not_found',
            message: '找不到這筆職缺。',
            details: []
          }
        }),
        { status: 404, headers: { 'content-type': 'application/json' } }
      )
    );

    await expect(getJobDetail('missing', fetcher)).rejects.toEqual(
      new ApiError('找不到這筆職缺。', 'req_missing', 'job_not_found')
    );
  });

  it('rejects missing fields and unexpected fields in a successful response', async () => {
    const { description: _description, ...missingField } = completeJob;
    void _description;
    const malformed = [missingField, { ...completeJob, source_row: 0 }];

    for (const job of malformed) {
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
        new Response(JSON.stringify({ request_id: 'req_invalid', job }), {
          status: 200,
          headers: { 'content-type': 'application/json' }
        })
      );
      await expect(getJobDetail('job-1', fetcher)).rejects.toEqual(
        new ApiError('職缺服務回傳了不完整的內容。')
      );
    }
  });

  it('rejects extra keys in the job-detail response envelope', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          request_id: 'req_invalid',
          job: completeJob,
          unexpected: true
        }),
        { status: 200, headers: { 'content-type': 'application/json' } }
      )
    );

    await expect(getJobDetail('job-1', fetcher)).rejects.toEqual(
      new ApiError('職缺服務回傳了不完整的內容。')
    );
  });
});
