import type { components } from '@1111-work-retrieval/contract';

export type SearchRequest = components['schemas']['SearchRequest'];
export type SearchResponse = components['schemas']['SearchResponse'];
export type SearchResult = components['schemas']['SearchResultItem'];
export type JobDetail = components['schemas']['JobDetail'];
export type JobDetailResponse = components['schemas']['JobDetailResponse'];
type ErrorResponse = components['schemas']['ErrorResponse'];

export interface SearchForm {
  query: string;
  locationCodes: string;
  dutyCodes: string;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly requestId?: string,
    readonly code?: string
  ) {
    super(message);
    this.name = 'ApiError';
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

function hasExactKeys(
  value: Record<string, unknown>,
  expectedKeys: readonly string[]
): boolean {
  const keys = Object.keys(value);
  return (
    keys.length === expectedKeys.length &&
    keys.every((key) => expectedKeys.includes(key))
  );
}

const requiredJobStrings = [
  'job_id',
  'title',
  'salary_text',
  'vendor_id',
  'source_modified_at'
] as const satisfies readonly (keyof JobDetail)[];

const nullableJobStrings = [
  'description',
  'salary_min',
  'salary_max',
  'duty_major',
  'duty_middle',
  'duty_minor',
  'job_attribute',
  'work_hours',
  'work_hours_description',
  'work_city',
  'education_requirement',
  'major_requirement_1',
  'major_requirement_2',
  'major_requirement_3',
  'experience_requirement',
  'language_1',
  'language_1_listening',
  'language_1_speaking',
  'language_1_reading',
  'language_1_writing',
  'language_2',
  'language_2_listening',
  'language_2_speaking',
  'language_2_reading',
  'language_2_writing',
  'computer_skills',
  'professional_certifications',
  'work_skills',
  'additional_conditions',
  'management_count',
  'requires_travel',
  'industry_major',
  'industry_middle',
  'industry_minor'
] as const satisfies readonly (keyof JobDetail)[];

function isJobDetail(value: unknown): value is JobDetail {
  if (!isRecord(value)) return false;
  const expected = new Set<string>([
    ...requiredJobStrings,
    ...nullableJobStrings
  ]);
  if (Object.keys(value).length !== expected.size) return false;
  if (Object.keys(value).some((key) => !expected.has(key))) return false;
  if (requiredJobStrings.some((key) => typeof value[key] !== 'string'))
    return false;
  return nullableJobStrings.every(
    (key) => value[key] === null || typeof value[key] === 'string'
  );
}

function isJobDetailResponse(value: unknown): value is JobDetailResponse {
  return (
    isRecord(value) &&
    hasExactKeys(value, ['request_id', 'job']) &&
    typeof value.request_id === 'string' &&
    isJobDetail(value.job)
  );
}

function isSearchResponse(value: unknown): value is SearchResponse {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, ['request_id', 'result']) ||
    typeof value.request_id !== 'string' ||
    !Array.isArray(value.result) ||
    value.result.length > 10
  )
    return false;

  const jobIds = new Set<string>();
  return value.result.every((item, index) => {
    if (
      !isRecord(item) ||
      !hasExactKeys(item, ['job_id', 'rank']) ||
      typeof item.job_id !== 'string' ||
      item.job_id.trim().length === 0 ||
      item.rank !== index + 1 ||
      jobIds.has(item.job_id)
    )
      return false;
    jobIds.add(item.job_id);
    return true;
  });
}

function isErrorResponse(value: unknown): value is ErrorResponse {
  return (
    isRecord(value) &&
    hasExactKeys(value, ['request_id', 'error']) &&
    typeof value.request_id === 'string' &&
    isRecord(value.error) &&
    hasExactKeys(value.error, ['code', 'message', 'details']) &&
    typeof value.error.code === 'string' &&
    typeof value.error.message === 'string' &&
    Array.isArray(value.error.details) &&
    value.error.details.every(
      (detail) =>
        isRecord(detail) &&
        hasExactKeys(detail, ['field', 'code', 'message']) &&
        typeof detail.field === 'string' &&
        typeof detail.code === 'string' &&
        typeof detail.message === 'string'
    )
  );
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
      throw new ApiError(
        payload.error.message,
        payload.request_id,
        payload.error.code
      );
    throw new ApiError('搜尋服務回傳了無法辨識的錯誤。');
  }
  if (!isSearchResponse(payload))
    throw new ApiError('搜尋服務回傳了無法辨識的內容。');
  return payload;
}

export async function getJobDetail(
  jobId: string,
  fetcher: typeof fetch = fetch
): Promise<JobDetailResponse> {
  const response = await fetcher(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
  const payload: unknown = await response.json();

  if (!response.ok) {
    if (isErrorResponse(payload))
      throw new ApiError(
        payload.error.message,
        payload.request_id,
        payload.error.code
      );
    throw new ApiError('職缺服務回傳了無法辨識的錯誤。');
  }
  if (!isJobDetailResponse(payload))
    throw new ApiError('職缺服務回傳了不完整的內容。');
  return payload;
}
