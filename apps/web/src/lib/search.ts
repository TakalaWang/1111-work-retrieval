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
