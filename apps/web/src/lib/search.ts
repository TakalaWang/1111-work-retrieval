import type { components } from '@1111-work-retrieval/contract';

export type SearchRequest = components['schemas']['SearchRequest'];
export type SearchResponse = components['schemas']['SearchResponse'];
export type JobResponse = components['schemas']['JobResponse'];
type ErrorResponse = components['schemas']['ErrorResponse'];

export interface PresentedJob {
  jobId: string;
  rank: number;
  title: string;
  city?: string;
  salary?: string;
  category?: string;
  description?: string;
  experience?: string;
  education?: string;
  skills?: string;
  updatedAt?: string;
}

export interface JobSearchOutcome {
  requestId: string;
  jobs: PresentedJob[];
  failedCount: number;
}

export class SearchApiError extends Error {
  constructor(
    message: string,
    readonly requestId?: string
  ) {
    super(message);
    this.name = 'SearchApiError';
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isSearchResponse(value: unknown): value is SearchResponse {
  if (
    !isRecord(value) ||
    typeof value.request_id !== 'string' ||
    !Array.isArray(value.result) ||
    value.result.length > 10
  )
    return false;

  const jobIds = new Set<string>();
  return value.result.every((item, index) => {
    if (
      !isRecord(item) ||
      typeof item.job_id !== 'string' ||
      !/^[0-9]+$/u.test(item.job_id) ||
      item.rank !== index + 1 ||
      jobIds.has(item.job_id)
    )
      return false;
    jobIds.add(item.job_id);
    return true;
  });
}

function isJobResponse(value: unknown): value is JobResponse {
  return (
    isRecord(value) &&
    typeof value.job_id === 'string' &&
    /^[0-9]+$/u.test(value.job_id) &&
    isRecord(value.details) &&
    Object.values(value.details).every(
      (detail) => typeof detail === 'string' || detail === null
    )
  );
}

function isErrorResponse(value: unknown): value is ErrorResponse {
  return (
    isRecord(value) &&
    typeof value.request_id === 'string' &&
    isRecord(value.error) &&
    typeof value.error.code === 'string' &&
    typeof value.error.message === 'string' &&
    Array.isArray(value.error.details) &&
    value.error.details.every(
      (detail) =>
        isRecord(detail) &&
        typeof detail.field === 'string' &&
        typeof detail.code === 'string' &&
        typeof detail.message === 'string'
    )
  );
}

function usefulDetail(details: JobResponse['details'], key: string) {
  const value = details[key]?.replace(/<br(?:\s*\/)?\s*>/giu, ' ').trim();
  return value ? value.replace(/\s+/gu, ' ') : undefined;
}

async function jsonPayload(
  response: Response,
  invalidMessage: string
): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw new SearchApiError(invalidMessage);
  }
}

export function presentJob(response: JobResponse, rank: number): PresentedJob {
  const { details } = response;
  return {
    jobId: response.job_id,
    rank,
    title: usefulDetail(details, '職務名稱') ?? `職缺 ${response.job_id}`,
    city: usefulDetail(details, '工作城市'),
    salary: usefulDetail(details, '薪資'),
    category:
      usefulDetail(details, '職務小類') ??
      usefulDetail(details, '職務中類') ??
      usefulDetail(details, '職務大類'),
    description: usefulDetail(details, '職務內容'),
    experience: usefulDetail(details, '工作經驗需求'),
    education: usefulDetail(details, '學歷需求'),
    skills: usefulDetail(details, '工作技能'),
    updatedAt: usefulDetail(details, '職缺最後修改時間')
  };
}

export async function searchJobs(
  request: SearchRequest,
  fetcher: typeof fetch = fetch
): Promise<SearchResponse> {
  const response = await fetcher('/api/v1/jobs/search', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(request)
  });
  const payload = await jsonPayload(
    response,
    response.ok
      ? '搜尋服務回傳了無法辨識的內容。'
      : '搜尋服務回傳了無法辨識的錯誤。'
  );

  if (!response.ok) {
    if (isErrorResponse(payload))
      throw new SearchApiError(payload.error.message, payload.request_id);
    throw new SearchApiError('搜尋服務回傳了無法辨識的錯誤。');
  }
  if (!isSearchResponse(payload))
    throw new SearchApiError('搜尋服務回傳了無法辨識的內容。');
  return payload;
}

export async function getJob(
  jobId: string,
  fetcher: typeof fetch = fetch
): Promise<JobResponse> {
  const response = await fetcher(
    `/api/v1/job-details/${encodeURIComponent(jobId)}`
  );
  const payload = await jsonPayload(
    response,
    response.ok
      ? '職缺資料服務回傳了無法辨識的內容。'
      : '職缺資料服務回傳了無法辨識的錯誤。'
  );

  if (!response.ok) {
    if (isErrorResponse(payload))
      throw new SearchApiError(payload.error.message, payload.request_id);
    throw new SearchApiError('職缺資料服務回傳了無法辨識的錯誤。');
  }
  if (!isJobResponse(payload) || payload.job_id !== jobId)
    throw new SearchApiError('職缺資料服務回傳了無法辨識的內容。');
  return payload;
}

export async function searchJobDetails(
  query: string,
  fetcher: typeof fetch = fetch
): Promise<JobSearchOutcome> {
  const search = await searchJobs(
    { query: query.trim(), location_code: [], duty_code: [] },
    fetcher
  );
  const settled = await Promise.allSettled(
    search.result.map(async ({ job_id, rank }) =>
      presentJob(await getJob(job_id, fetcher), rank)
    )
  );
  const jobs = settled.flatMap((result) =>
    result.status === 'fulfilled' ? [result.value] : []
  );
  const failedCount = settled.filter(
    (result) => result.status === 'rejected'
  ).length;

  if (search.result.length > 0 && jobs.length === 0) {
    throw new SearchApiError(
      '找到職缺，但詳細資料目前無法載入。',
      search.request_id
    );
  }

  return {
    requestId: search.request_id,
    jobs,
    failedCount
  };
}
