import type { components } from '@1111-work-retrieval/contract';

export type SearchRequest = components['schemas']['SearchRequest'];
export type SearchResponse = components['schemas']['SearchResponse'];
export type SearchResult = components['schemas']['SearchResultItem'];
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
      !/^[0-9]+$/u.test(item.job_id) ||
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

async function jsonPayload(
  response: Response,
  invalidMessage: string
): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw new ApiError(invalidMessage);
  }
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
