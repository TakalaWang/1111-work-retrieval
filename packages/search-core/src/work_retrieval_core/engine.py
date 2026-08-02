from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from math import isfinite
from threading import BoundedSemaphore, Lock
from time import monotonic
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from work_retrieval_core.constraints import (
    QueryConstraints,
    compile_constraints,
    education_requirement_allows,
    job_attribute_allows,
    management_requirement_allows,
    monthly_salary_allows,
    no_experience_allows,
    work_shift_allows,
)
from work_retrieval_core.manifest import RuntimeManifest
from work_retrieval_core.serialization import canonical_code

CANDIDATE_LIMIT = 200
MAX_AGE_DAYS = 180
LANE_TIMEOUT_SECONDS = 5.0
SHADOW_COLLECTION_SECONDS = 1.0
METADATA_RESERVE_SECONDS = 1.0
MAX_INFLIGHT_SEARCHES = 8
RERANK_POOL_LIMIT = 50
RRF_K = 60
SEARCH_TIMEZONE = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """Validated API input passed to the production search engine."""

    text: str
    location_codes: tuple[str, ...] = ()
    duty_codes: tuple[str, ...] = ()
    search_date: date | None = None


@dataclass(frozen=True, slots=True)
class CandidateRequest:
    """A lane request whose filters must be applied before that lane's Top-K."""

    text: str
    location_codes: tuple[str, ...]
    duty_codes: tuple[str, ...]
    as_of: datetime
    minimum_updated_at: datetime
    lexical_texts: tuple[str, ...] = ()
    query_rewrites: tuple[QueryRewrite, ...] = ()
    constraints: QueryConstraints = field(default_factory=QueryConstraints)


@dataclass(frozen=True, slots=True)
class QueryRewrite:
    source: str
    target: str
    policy: str

    def as_dict(self) -> dict[str, str]:
        return {"source": self.source, "target": self.target, "policy": self.policy}


@dataclass(frozen=True, slots=True)
class CompiledQuery:
    lexical_texts: tuple[str, ...]
    rewrites: tuple[QueryRewrite, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    job_id: str
    score: float
    rank: int


@dataclass(frozen=True, slots=True)
class JobMetadata:
    job_id: str
    source_modified_at: datetime
    location_codes: tuple[str, ...]
    duty_codes: tuple[str, ...]
    education_requirement: str | None = None
    salary_period: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    job_attribute: str | None = None
    work_hours: str | None = None
    experience_requirement: str | None = None
    management_count: str | None = None


@runtime_checkable
class CandidateRetriever(Protocol):
    def retrieve(
        self, request: CandidateRequest, *, limit: int
    ) -> tuple[CandidateEvidence, ...]: ...

    def close(self) -> None: ...


@runtime_checkable
class JobMetadataLookup(Protocol):
    def get_many(self, job_ids: tuple[str, ...]) -> tuple[JobMetadata, ...]: ...

    def close(self) -> None: ...


@runtime_checkable
class QueryCompiler(Protocol):
    def compile(self, text: str) -> CompiledQuery: ...


@runtime_checkable
class CandidateReranker(Protocol):
    def rerank(self, query: str, job_ids: tuple[str, ...], limit: int) -> tuple[str, ...]: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RetrievalPorts:
    lexical_full_jd: CandidateRetriever
    dense_whole_jd: CandidateRetriever | None
    metadata: JobMetadataLookup
    dense_multiview_maxsim: CandidateRetriever | None = None
    query_compiler: QueryCompiler | None = None
    reranker: CandidateReranker | None = None


@dataclass(frozen=True, slots=True)
class LaneTrace:
    name: str
    status: str
    reason: str
    candidate_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "reason": self.reason,
            "candidate_count": self.candidate_count,
        }


@dataclass(frozen=True, slots=True)
class RankEvidence:
    lane: str
    rank: int
    raw_score: float
    ranking_contribution: float

    def as_dict(self) -> dict[str, object]:
        return {
            "lane": self.lane,
            "rank": self.rank,
            "raw_score": round(self.raw_score, 8),
            "ranking_contribution": round(self.ranking_contribution, 8),
        }


@dataclass(frozen=True, slots=True)
class ResultTrace:
    job_id: str
    ranking_score: float
    freshness_score: float
    source_modified_at: datetime
    future_updated_snapshot: bool
    evidence: tuple[RankEvidence, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "ranking_score": round(self.ranking_score, 8),
            "freshness_score": round(self.freshness_score, 8),
            "source_modified_at": _isoformat(self.source_modified_at),
            "future_updated_snapshot": self.future_updated_snapshot,
            "evidence": [item.as_dict() for item in self.evidence],
        }


@dataclass(frozen=True, slots=True)
class SearchAuditTrace:
    as_of: datetime
    eligible_from: datetime
    max_age_days: int
    future_rows: str
    location_filter: str
    duty_filter: str
    query_rewrites: tuple[QueryRewrite, ...]
    lanes: tuple[LaneTrace, ...]
    results: tuple[ResultTrace, ...]
    constraints: QueryConstraints = field(default_factory=QueryConstraints)
    constraint_filter: str = "not_requested"

    def as_dict(self) -> dict[str, object]:
        return {
            "as_of": _isoformat(self.as_of),
            "eligible_from": _isoformat(self.eligible_from),
            "max_age_days": self.max_age_days,
            "future_rows": self.future_rows,
            "hard_filters": {
                "location": self.location_filter,
                "duty": self.duty_filter,
                "source_modified_at_lower_bound": (
                    "verified_on_returned_candidates" if self.results else "no_returned_candidates"
                ),
                "source_modified_at_upper_bound": (
                    "not_applied_snapshot_timestamp_is_not_creation_time"
                ),
                "education": (
                    self.constraint_filter
                    if self.constraints.education is not None
                    else "not_requested"
                ),
                "monthly_salary": (
                    self.constraint_filter
                    if self.constraints.monthly_salary is not None
                    else "not_requested"
                ),
                "job_attribute": (
                    self.constraint_filter
                    if self.constraints.job_attribute is not None
                    else "not_requested"
                ),
                "work_shift": (
                    self.constraint_filter
                    if self.constraints.work_shift is not None
                    else "not_requested"
                ),
                "no_experience": (
                    self.constraint_filter
                    if self.constraints.no_experience is not None
                    else "not_requested"
                ),
                "management": (
                    self.constraint_filter
                    if self.constraints.management is not None
                    else "not_requested"
                ),
            },
            "constraints": self.constraints.as_dict(),
            "query_rewrites": [rewrite.as_dict() for rewrite in self.query_rewrites],
            "lanes": [lane.as_dict() for lane in self.lanes],
            "results": [result.as_dict() for result in self.results],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class SearchResult:
    job_ids: tuple[str, ...]
    trace: SearchAuditTrace


class SearchUnavailableError(RuntimeError):
    """The production engine cannot serve the request without violating its contract."""


@runtime_checkable
class SearchEngine(Protocol):
    def search(self, query: SearchQuery, *, limit: int) -> SearchResult: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class _RankedCandidate:
    metadata: JobMetadata
    evidence: list[RankEvidence]


class ProductionSearchEngine:
    """Fail-closed BM25 incumbent with optional bounded shadow evidence."""

    def __init__(
        self,
        manifest: RuntimeManifest,
        ports: RetrievalPorts,
        *,
        enable_dense_shadow: bool = False,
        enable_multiview_maxsim: bool = False,
        multiview_artifact_key: str | None = None,
        reranker_mode: str = "off",
        max_inflight_searches: int = MAX_INFLIGHT_SEARCHES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if isinstance(max_inflight_searches, bool) or max_inflight_searches < 1:
            raise ValueError("max_inflight_searches must be a positive integer")
        if not isinstance(ports.lexical_full_jd, CandidateRetriever):
            raise TypeError("lexical_full_jd port does not satisfy CandidateRetriever")
        if enable_dense_shadow and not isinstance(ports.dense_whole_jd, CandidateRetriever):
            raise TypeError("enabled dense shadow port does not satisfy CandidateRetriever")
        if not isinstance(ports.metadata, JobMetadataLookup):
            raise TypeError("metadata port does not satisfy JobMetadataLookup")
        artifact = manifest.artifact(multiview_artifact_key or "")
        if enable_multiview_maxsim and (
            artifact is None or artifact.kind not in {"embedding", "index"}
        ):
            raise RuntimeError("enabled multi-view MaxSim requires its manifest artifact")
        if enable_multiview_maxsim and not isinstance(
            ports.dense_multiview_maxsim, CandidateRetriever
        ):
            raise RuntimeError("enabled multi-view MaxSim requires its configured retrieval port")
        if reranker_mode not in {"off", "shadow", "active"}:
            raise ValueError("reranker_mode must be off, shadow, or active")
        if reranker_mode != "off" and not enable_dense_shadow:
            raise RuntimeError("reranker requires whole-Dense candidate evidence")
        if reranker_mode != "off" and not isinstance(ports.reranker, CandidateReranker):
            raise TypeError("enabled reranker port does not satisfy CandidateReranker")

        self._ports = ports
        self._enable_dense_shadow = enable_dense_shadow
        self._enable_multiview_maxsim = enable_multiview_maxsim
        self._reranker_mode = reranker_mode
        self._clock = clock or (lambda: datetime.now(UTC))
        lane_count = 1 + int(enable_dense_shadow) + int(enable_multiview_maxsim)
        self._executor = ThreadPoolExecutor(
            max_workers=lane_count * max_inflight_searches,
            thread_name_prefix="retrieval-lane",
        )
        self._inflight = BoundedSemaphore(max_inflight_searches)
        self._closed = False
        self._state_lock = Lock()

    def search(self, query: SearchQuery, *, limit: int) -> SearchResult:
        if isinstance(limit, bool) or not 1 <= limit <= 10:
            raise ValueError("limit must be an integer between 1 and 10")
        with self._state_lock:
            if self._closed:
                raise SearchUnavailableError("search engine is closed")
        if not self._inflight.acquire(blocking=False):
            raise SearchUnavailableError("search capacity is saturated")
        try:
            return self._search_once(query, limit=limit)
        finally:
            self._inflight.release()

    def _search_once(self, query: SearchQuery, *, limit: int) -> SearchResult:
        as_of = (
            _request_as_of(query.search_date)
            if query.search_date is not None
            else _aware(self._clock(), field="as_of")
        )
        eligible_from = as_of - timedelta(days=MAX_AGE_DAYS)
        compiled = self._compile_query(query.text)
        constraints = compile_constraints(query.text)
        request = CandidateRequest(
            text=query.text,
            location_codes=tuple(canonical_code(value) for value in query.location_codes),
            duty_codes=tuple(canonical_code(value) for value in query.duty_codes),
            as_of=as_of,
            minimum_updated_at=eligible_from,
            lexical_texts=compiled.lexical_texts,
            query_rewrites=compiled.rewrites,
            constraints=constraints,
        )
        validated_lane_results, lane_failures, deadline = self._retrieve_lanes(request)
        constraint_filter = (
            "tantivy_pre_topk_and_postgres_revalidated"
            if constraints.requested()
            else "not_requested"
        )
        if constraints.requested() and not validated_lane_results[0][1]:
            request = replace(request, constraints=QueryConstraints())
            validated_lane_results, lane_failures, deadline = self._retrieve_lanes(request)
            constraint_filter = "relaxed_query_text_constraints_after_zero"
        candidate_ids = tuple(
            dict.fromkeys(
                candidate.job_id
                for _, candidates in validated_lane_results
                for candidate in candidates
            )
        )
        try:
            metadata = self._executor.submit(
                self._ports.metadata.get_many,
                candidate_ids,
            ).result(timeout=_remaining(deadline))
        except Exception as error:
            raise SearchUnavailableError("job metadata lookup failed") from error
        required_ids = {candidate.job_id for candidate in validated_lane_results[0][1]}
        metadata_by_id = self._validate_metadata(
            candidate_ids,
            metadata,
            request=request,
            required_ids=required_ids,
        )

        ranked_candidates: dict[str, _RankedCandidate] = {}
        lane_traces: list[LaneTrace] = []
        for lane_name, validated in validated_lane_results:
            if failure := lane_failures.get(lane_name):
                lane_traces.append(LaneTrace(lane_name, "failed", failure, 0))
                continue
            incumbent = lane_name == "tantivy_bm25_full_jd"
            dense_ranking_evidence = (
                self._reranker_mode == "active" and lane_name == "qwen_dense_whole_jd"
            )
            lane_traces.append(
                LaneTrace(
                    lane_name,
                    "enabled",
                    (
                        "top10_incumbent"
                        if incumbent
                        else "reranker_candidate_evidence"
                        if dense_ranking_evidence
                        else "shadow_tail_only"
                    ),
                    len(validated),
                )
            )
            for rank, candidate in enumerate(validated, start=1):
                if candidate.job_id not in metadata_by_id:
                    continue
                contribution = candidate.score if incumbent else 0.0
                rank_evidence = RankEvidence(lane_name, rank, candidate.score, contribution)
                job_metadata = metadata_by_id[candidate.job_id]
                existing = ranked_candidates.get(candidate.job_id)
                if existing is None:
                    ranked_candidates[candidate.job_id] = _RankedCandidate(
                        metadata=job_metadata,
                        evidence=[rank_evidence],
                    )
                else:
                    existing.evidence.append(rank_evidence)

        if not self._enable_dense_shadow:
            lane_traces.append(
                LaneTrace(
                    "qwen_dense_whole_jd",
                    "disabled",
                    "production_latency_not_approved",
                    0,
                )
            )
        if not self._enable_multiview_maxsim:
            lane_traces.append(
                LaneTrace(
                    "qwen_dense_multiview_maxsim",
                    "disabled",
                    "feature_flag_disabled",
                    0,
                )
            )
        scored = [
            self._result_trace(job_id, candidate, as_of=as_of)
            for job_id, candidate in ranked_candidates.items()
        ]
        scored.sort(key=_serving_order)
        reranker_trace = LaneTrace("reranker", "disabled", "feature_flag_disabled", 0)
        if self._reranker_mode != "off" and "qwen_dense_whole_jd" not in lane_failures:
            assert self._ports.reranker is not None
            pool = _reranker_pool_ids(ranked_candidates)
            if pool:
                reranker_failed = False
                try:
                    reranked = self._ports.reranker.rerank(query.text, pool, len(pool))
                except Exception as error:
                    if self._reranker_mode == "active":
                        raise SearchUnavailableError("reranker failed") from error
                    reranker_trace = LaneTrace("reranker", "failed", "shadow_call_failed", 0)
                    reranker_failed = True
                    reranked = ()
                invalid = (
                    not isinstance(reranked, tuple)
                    or len(reranked) != len(pool)
                    or len(set(reranked)) != len(reranked)
                    or set(reranked) != set(pool)
                )
                if not reranker_failed and invalid:
                    if self._reranker_mode == "active":
                        raise SearchUnavailableError("reranker violated its serving contract")
                    reranker_trace = LaneTrace("reranker", "failed", "shadow_contract_violation", 0)
                    reranked = ()
            else:
                reranked = ()
            if reranked:
                if self._reranker_mode == "active":
                    pool_set = set(pool)
                    ordered_ids = (
                        *reranked,
                        *(item.job_id for item in scored if item.job_id not in pool_set),
                    )
                    by_id = {item.job_id: item for item in scored}
                    scored = [by_id[job_id] for job_id in ordered_ids]
                    reranker_trace = LaneTrace("reranker", "enabled", "ranking_active", len(pool))
                else:
                    reranker_trace = LaneTrace("reranker", "enabled", "shadow_scored", len(pool))
            elif not pool:
                reranker_trace = LaneTrace(
                    "reranker",
                    "enabled",
                    "ranking_active" if self._reranker_mode == "active" else "shadow_scored",
                    0,
                )
        elif self._reranker_mode == "shadow":
            reranker_trace = LaneTrace("reranker", "failed", "dense_shadow_failed", 0)
        lane_traces.extend(
            (
                LaneTrace("graph", "disabled", "ablation_not_approved", 0),
                reranker_trace,
                LaneTrace("ltr", "disabled", "calibration_not_approved", 0),
                LaneTrace("guardrail", "disabled", "calibration_not_approved", 0),
            )
        )
        selected = tuple(scored[:limit])
        return SearchResult(
            job_ids=tuple(item.job_id for item in selected),
            trace=SearchAuditTrace(
                as_of=as_of,
                eligible_from=eligible_from,
                max_age_days=MAX_AGE_DAYS,
                future_rows="retained_with_zero_freshness",
                location_filter=_filter_status(request.location_codes, len(ranked_candidates)),
                duty_filter=_filter_status(request.duty_codes, len(ranked_candidates)),
                query_rewrites=request.query_rewrites,
                lanes=tuple(lane_traces),
                results=selected,
                constraints=constraints,
                constraint_filter=constraint_filter,
            ),
        )

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)
        seen: set[int] = set()
        for retriever in (
            self._ports.lexical_full_jd,
            self._ports.dense_whole_jd,
            self._ports.metadata,
            self._ports.dense_multiview_maxsim,
            self._ports.reranker,
        ):
            if retriever is not None and id(retriever) not in seen:
                seen.add(id(retriever))
                retriever.close()

    def _retrieve_lanes(
        self, request: CandidateRequest
    ) -> tuple[
        list[tuple[str, tuple[CandidateEvidence, ...]]],
        dict[str, str],
        float,
    ]:
        lanes: list[tuple[str, CandidateRetriever]] = [
            ("tantivy_bm25_full_jd", self._ports.lexical_full_jd),
        ]
        if self._enable_dense_shadow:
            assert self._ports.dense_whole_jd is not None
            lanes.append(("qwen_dense_whole_jd", self._ports.dense_whole_jd))
        if self._enable_multiview_maxsim:
            assert self._ports.dense_multiview_maxsim is not None
            lanes.append(("qwen_dense_multiview_maxsim", self._ports.dense_multiview_maxsim))
        futures = {
            name: self._executor.submit(retriever.retrieve, request, limit=CANDIDATE_LIMIT)
            for name, retriever in lanes
        }
        lane_results: list[tuple[str, tuple[CandidateEvidence, ...]]] = []
        failures: dict[str, str] = {}
        deadline = monotonic() + LANE_TIMEOUT_SECONDS
        try:
            lane_results.append(
                (lanes[0][0], futures[lanes[0][0]].result(timeout=_remaining(deadline)))
            )
        except Exception as error:
            for future in futures.values():
                future.cancel()
            raise SearchUnavailableError("a required retrieval lane failed") from error
        shadow_deadline = min(
            monotonic() + SHADOW_COLLECTION_SECONDS,
            deadline - METADATA_RESERVE_SECONDS,
        )
        for name, _retriever in lanes[1:]:
            try:
                lane_results.append(
                    (name, futures[name].result(timeout=_remaining(shadow_deadline)))
                )
            except Exception as error:
                futures[name].cancel()
                if self._reranker_mode == "active" and name == "qwen_dense_whole_jd":
                    raise SearchUnavailableError("a required ranking lane failed") from error
                failures[name] = "shadow_lane_failed"
                lane_results.append((name, ()))
        validated = [
            (
                lane_results[0][0],
                self._validate_lane(lane_results[0][0], lane_results[0][1]),
            )
        ]
        for name, candidates in lane_results[1:]:
            try:
                validated.append((name, self._validate_lane(name, candidates)))
            except SearchUnavailableError as error:
                if self._reranker_mode == "active" and name == "qwen_dense_whole_jd":
                    raise SearchUnavailableError("a required ranking lane failed") from error
                failures[name] = "shadow_lane_failed"
                validated.append((name, ()))
        return validated, failures, deadline

    def _validate_lane(
        self,
        lane_name: str,
        candidates: object,
    ) -> tuple[CandidateEvidence, ...]:
        if not isinstance(candidates, tuple):
            raise SearchUnavailableError(f"{lane_name} returned a non-tuple candidate set")
        if len(candidates) > CANDIDATE_LIMIT:
            raise SearchUnavailableError(f"{lane_name} returned too many candidates")
        seen: set[str] = set()
        previous_score = float("inf")
        for expected_rank, candidate in enumerate(candidates, start=1):
            if not isinstance(candidate, CandidateEvidence):
                raise SearchUnavailableError(f"{lane_name} returned malformed evidence")
            if (
                not candidate.job_id.isascii()
                or not candidate.job_id.isdecimal()
                or candidate.job_id in seen
            ):
                raise SearchUnavailableError(f"{lane_name} returned an invalid job_id")
            if not isfinite(candidate.score):
                raise SearchUnavailableError(f"{lane_name} returned a non-finite score")
            if candidate.rank != expected_rank or candidate.score > previous_score:
                raise SearchUnavailableError(f"{lane_name} returned an unsorted candidate lane")
            previous_score = candidate.score
            seen.add(candidate.job_id)
        return candidates

    def _validate_metadata(
        self,
        candidate_ids: tuple[str, ...],
        metadata: object,
        *,
        request: CandidateRequest,
        required_ids: set[str],
    ) -> dict[str, JobMetadata]:
        if not isinstance(metadata, tuple) or len(metadata) > len(candidate_ids):
            raise SearchUnavailableError("job metadata lookup returned malformed metadata")
        by_id: dict[str, JobMetadata] = {}
        for item in metadata:
            if (
                not isinstance(item, JobMetadata)
                or item.job_id in by_id
                or item.job_id not in candidate_ids
            ):
                raise SearchUnavailableError("job metadata lookup returned malformed metadata")
            updated_at = _aware(item.source_modified_at, field="source_modified_at")
            if updated_at < request.minimum_updated_at:
                if item.job_id in required_ids:
                    raise SearchUnavailableError("candidate violated temporal eligibility")
                continue
            locations = {canonical_code(value) for value in item.location_codes}
            duties = {canonical_code(value) for value in item.duty_codes}
            if request.location_codes and not locations.intersection(request.location_codes):
                if item.job_id in required_ids:
                    raise SearchUnavailableError("candidate violated the location hard filter")
                continue
            if request.duty_codes and not duties.intersection(request.duty_codes):
                if item.job_id in required_ids:
                    raise SearchUnavailableError("candidate violated the duty hard filter")
                continue
            education = request.constraints.education
            if education is not None and not education_requirement_allows(
                item.education_requirement,
                education.degree,
            ):
                if item.job_id in required_ids:
                    raise SearchUnavailableError("candidate violated the education hard filter")
                continue
            salary = request.constraints.monthly_salary
            if salary is not None and not monthly_salary_allows(
                item.salary_period,
                item.salary_min,
                item.salary_max,
                salary,
            ):
                if item.job_id in required_ids:
                    raise SearchUnavailableError(
                        "candidate violated the monthly salary hard filter"
                    )
                continue
            job_attribute = request.constraints.job_attribute
            if job_attribute is not None and not job_attribute_allows(
                item.job_attribute,
                job_attribute,
            ):
                if item.job_id in required_ids:
                    raise SearchUnavailableError("candidate violated the job attribute hard filter")
                continue
            work_shift = request.constraints.work_shift
            if work_shift is not None and not work_shift_allows(item.work_hours, work_shift):
                if item.job_id in required_ids:
                    raise SearchUnavailableError("candidate violated the work shift hard filter")
                continue
            if request.constraints.no_experience is not None and not no_experience_allows(
                item.experience_requirement
            ):
                if item.job_id in required_ids:
                    raise SearchUnavailableError("candidate violated the experience hard filter")
                continue
            if request.constraints.management is not None and not management_requirement_allows(
                item.management_count
            ):
                if item.job_id in required_ids:
                    raise SearchUnavailableError("candidate violated the management hard filter")
                continue
            by_id[item.job_id] = item
        if not required_ids.issubset(by_id):
            raise SearchUnavailableError(
                "job metadata lookup returned an incomplete incumbent result"
            )
        return by_id

    def _compile_query(self, text: str) -> CompiledQuery:
        if self._ports.query_compiler is None:
            return CompiledQuery((text,))
        compiled = self._ports.query_compiler.compile(text)
        if (
            not isinstance(compiled, CompiledQuery)
            or not 1 <= len(compiled.lexical_texts) <= 4
            or compiled.lexical_texts[0] != text
            or any(
                not isinstance(value, str) or not value.strip() for value in compiled.lexical_texts
            )
            or any(not isinstance(value, QueryRewrite) for value in compiled.rewrites)
        ):
            raise SearchUnavailableError("query compiler violated its serving contract")
        return compiled

    def _result_trace(
        self,
        job_id: str,
        candidate: _RankedCandidate,
        *,
        as_of: datetime,
    ) -> ResultTrace:
        evidence = tuple(candidate.evidence)
        ranking_score = sum(item.ranking_contribution for item in evidence)
        source_modified_at = candidate.metadata.source_modified_at
        future_updated_snapshot = source_modified_at > as_of
        if future_updated_snapshot:
            freshness = 0.0
        else:
            age = as_of - source_modified_at
            freshness = max(
                0.0,
                1.0 - age.total_seconds() / timedelta(days=MAX_AGE_DAYS).total_seconds(),
            )
        return ResultTrace(
            job_id=job_id,
            ranking_score=ranking_score,
            freshness_score=freshness,
            source_modified_at=source_modified_at,
            future_updated_snapshot=future_updated_snapshot,
            evidence=evidence,
        )


def _aware(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SearchUnavailableError(f"{field} must be timezone-aware")
    return value


def _request_as_of(value: date) -> datetime:
    return datetime.combine(value, time(23, 59, 59, 999_000), SEARCH_TIMEZONE).astimezone(UTC)


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _filter_status(requested: tuple[str, ...], returned: int) -> str:
    if not requested:
        return "not_requested"
    return "verified_on_returned_candidates" if returned else "no_returned_candidates"


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - monotonic())


def _reranker_pool_ids(candidates: Mapping[str, _RankedCandidate]) -> tuple[str, ...]:
    indexed = tuple(candidates.items())
    lexical = sorted(
        (
            (evidence.rank, job_id)
            for job_id, candidate in indexed
            for evidence in candidate.evidence
            if evidence.lane == "tantivy_bm25_full_jd"
        ),
        key=lambda item: (item[0], item[1]),
    )
    protected = tuple(job_id for _rank, job_id in lexical[:10])
    protected_set = set(protected)
    remaining: list[tuple[float, int, str]] = []
    for original_position, (job_id, candidate) in enumerate(indexed):
        if job_id in protected_set:
            continue
        evidence = tuple(
            item
            for item in candidate.evidence
            if item.lane in {"tantivy_bm25_full_jd", "qwen_dense_whole_jd"}
        )
        if evidence:
            remaining.append(
                (sum(1.0 / (RRF_K + item.rank) for item in evidence), original_position, job_id)
            )
    remaining.sort(key=lambda item: (-item[0], item[1], item[2]))
    return (*protected, *(job_id for _score, _position, job_id in remaining))[:RERANK_POOL_LIMIT]


def _serving_order(item: ResultTrace) -> tuple[int, int, float, str]:
    lexical = [
        evidence.rank for evidence in item.evidence if evidence.lane == "tantivy_bm25_full_jd"
    ]
    if lexical:
        return (0, min(lexical), -item.freshness_score, item.job_id)
    shadow = [evidence.rank for evidence in item.evidence]
    return (1, min(shadow), -item.freshness_score, item.job_id)
