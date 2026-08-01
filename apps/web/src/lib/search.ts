import type { components } from '@1111-work-retrieval/contract';

export type SearchRequest = components['schemas']['SearchRequest'];
export type SearchResponse = components['schemas']['SearchResponse'];
export type SearchResult = components['schemas']['SearchResultItem'];
export type PullJobRequest = components['schemas']['PullJobRequest'];
export type JobResponse = components['schemas']['JobResponse'];
type ErrorResponse = components['schemas']['ErrorResponse'];

export interface SearchForm {
  query: string;
  locationCodes: string;
  dutyCodes: string;
}

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

export class SearchApiError extends Error {
  constructor(
    message: string,
    readonly requestId?: string
  ) {
    super(message);
    this.name = 'SearchApiError';
  }
}

function parseCodes(value: string): string[] {
  return [
    ...new Set(
      value
        .split(/[\s,，]+/u)
        .map((code) => code.trim())
        .filter(Boolean)
    )
  ];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isSearchResponse(value: unknown): value is SearchResponse {
  return (
    isRecord(value) &&
    typeof value.request_id === 'string' &&
    Array.isArray(value.result) &&
    value.result.every(
      (item) =>
        isRecord(item) &&
        typeof item.job_id === 'string' &&
        typeof item.rank === 'number' &&
        Number.isInteger(item.rank)
    )
  );
}

function isJobResponse(value: unknown): value is JobResponse {
  return (
    isRecord(value) &&
    typeof value.job_id === 'string' &&
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
  const value = details[key]?.replace(/<br\s*\/?\s*>/giu, ' ').trim();
  return value ? value.replace(/\s+/gu, ' ') : undefined;
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

export function serializeSearch(form: SearchForm): SearchRequest {
  return {
    query: form.query.trim(),
    location_code: parseCodes(form.locationCodes),
    duty_code: parseCodes(form.dutyCodes)
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
  const payload: unknown = await response.json();

  if (!response.ok) {
    if (isErrorResponse(payload))
      throw new SearchApiError(payload.error.message, payload.request_id);
    throw new SearchApiError('搜尋服務回傳了無法辨識的錯誤。');
  }
  if (!isSearchResponse(payload))
    throw new SearchApiError('搜尋服務回傳了無法辨識的內容。');
  return payload;
}

export async function pullJob(
  jobId: string,
  fetcher: typeof fetch = fetch
): Promise<JobResponse> {
  const request = { job_id: jobId } satisfies PullJobRequest;
  const response = await fetcher('/api/v1/jobs/pull', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(request)
  });
  const payload: unknown = await response.json();

  if (!response.ok) {
    if (isErrorResponse(payload))
      throw new SearchApiError(payload.error.message, payload.request_id);
    throw new SearchApiError('職缺資料服務回傳了無法辨識的錯誤。');
  }
  if (!isJobResponse(payload))
    throw new SearchApiError('職缺資料服務回傳了無法辨識的內容。');
  return payload;
}
