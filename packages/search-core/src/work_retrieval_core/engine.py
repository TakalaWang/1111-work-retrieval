from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from threading import Lock
from typing import Protocol, runtime_checkable

from work_retrieval_core.manifest import RuntimeManifest

CANDIDATE_LIMIT = 200
MAX_AGE_DAYS = 180
RRF_K = 60


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """Validated API input passed to the production search engine."""

    text: str
    location_codes: tuple[str, ...] = ()
    duty_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidateRequest:
    """A lane request whose filters must be applied before that lane's Top-K."""

    text: str
    location_codes: tuple[str, ...]
    duty_codes: tuple[str, ...]
    as_of: datetime
    minimum_updated_at: datetime


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    job_id: str
    score: float
    source_modified_at: datetime


@runtime_checkable
class CandidateRetriever(Protocol):
    def retrieve(
        self, request: CandidateRequest, *, limit: int
    ) -> tuple[CandidateEvidence, ...]: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RetrievalPorts:
    lexical_full_jd: CandidateRetriever
    dense_whole_jd: CandidateRetriever
    dense_multiview_maxsim: CandidateRetriever | None = None


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
    rrf_contribution: float

    def as_dict(self) -> dict[str, object]:
        return {
            "lane": self.lane,
            "rank": self.rank,
            "raw_score": round(self.raw_score, 8),
            "rrf_contribution": round(self.rrf_contribution, 8),
        }


@dataclass(frozen=True, slots=True)
class ResultTrace:
    job_id: str
    fused_score: float
    freshness_score: float
    source_modified_at: datetime
    evidence: tuple[RankEvidence, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "fused_score": round(self.fused_score, 8),
            "freshness_score": round(self.freshness_score, 8),
            "source_modified_at": _isoformat(self.source_modified_at),
            "evidence": [item.as_dict() for item in self.evidence],
        }


@dataclass(frozen=True, slots=True)
class SearchAuditTrace:
    as_of: datetime
    eligible_from: datetime
    max_age_days: int
    future_rows: str
    location_filter_applied: bool
    duty_filter_applied: bool
    lanes: tuple[LaneTrace, ...]
    results: tuple[ResultTrace, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "as_of": _isoformat(self.as_of),
            "eligible_from": _isoformat(self.eligible_from),
            "max_age_days": self.max_age_days,
            "future_rows": self.future_rows,
            "hard_filters": {
                "location": self.location_filter_applied,
                "duty": self.duty_filter_applied,
                "source_modified_at_lower_bound": True,
            },
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
class _FusedCandidate:
    source_modified_at: datetime
    evidence: list[RankEvidence]


class ProductionSearchEngine:
    """Fail-closed BM25 + Qwen retrieval with bounded, auditable RRF fusion."""

    def __init__(
        self,
        manifest: RuntimeManifest,
        ports: RetrievalPorts,
        *,
        enable_multiview_maxsim: bool = False,
        multiview_artifact_key: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(ports.lexical_full_jd, CandidateRetriever):
            raise TypeError("lexical_full_jd port does not satisfy CandidateRetriever")
        if not isinstance(ports.dense_whole_jd, CandidateRetriever):
            raise TypeError("dense_whole_jd port does not satisfy CandidateRetriever")
        artifact = manifest.artifact(multiview_artifact_key or "")
        if enable_multiview_maxsim and (
            artifact is None or artifact.kind not in {"embedding", "index"}
        ):
            raise RuntimeError("enabled multi-view MaxSim requires its manifest artifact")
        if enable_multiview_maxsim and not isinstance(
            ports.dense_multiview_maxsim, CandidateRetriever
        ):
            raise RuntimeError("enabled multi-view MaxSim requires its configured retrieval port")

        self._ports = ports
        self._enable_multiview_maxsim = enable_multiview_maxsim
        self._clock = clock or (lambda: datetime.now(UTC))
        lane_count = 3 if enable_multiview_maxsim else 2
        self._executor = ThreadPoolExecutor(
            max_workers=lane_count,
            thread_name_prefix="retrieval-lane",
        )
        self._closed = False
        self._state_lock = Lock()

    def search(self, query: SearchQuery, *, limit: int) -> SearchResult:
        if isinstance(limit, bool) or not 1 <= limit <= 10:
            raise ValueError("limit must be an integer between 1 and 10")
        with self._state_lock:
            if self._closed:
                raise SearchUnavailableError("search engine is closed")

        as_of = _aware(self._clock(), field="as_of")
        eligible_from = as_of - timedelta(days=MAX_AGE_DAYS)
        request = CandidateRequest(
            text=query.text,
            location_codes=query.location_codes,
            duty_codes=query.duty_codes,
            as_of=as_of,
            minimum_updated_at=eligible_from,
        )
        lanes: list[tuple[str, CandidateRetriever]] = [
            ("tantivy_bm25_full_jd", self._ports.lexical_full_jd),
            ("qwen_dense_whole_jd", self._ports.dense_whole_jd),
        ]
        if self._enable_multiview_maxsim:
            assert self._ports.dense_multiview_maxsim is not None
            lanes.append(("qwen_dense_multiview_maxsim", self._ports.dense_multiview_maxsim))

        futures = {
            name: self._executor.submit(
                retriever.retrieve,
                request,
                limit=CANDIDATE_LIMIT,
            )
            for name, retriever in lanes
        }
        lane_results: list[tuple[str, tuple[CandidateEvidence, ...]]] = []
        try:
            for name, _retriever in lanes:
                lane_results.append((name, futures[name].result()))
        except Exception as error:
            for future in futures.values():
                future.cancel()
            raise SearchUnavailableError("a required retrieval lane failed") from error

        fused: dict[str, _FusedCandidate] = {}
        lane_traces: list[LaneTrace] = []
        for lane_name, candidates in lane_results:
            validated = self._validate_lane(
                lane_name,
                candidates,
                eligible_from=eligible_from,
            )
            lane_traces.append(
                LaneTrace(lane_name, "enabled", "required_production_lane", len(validated))
            )
            for rank, candidate in enumerate(validated, start=1):
                contribution = 1.0 / (RRF_K + rank)
                rank_evidence = RankEvidence(lane_name, rank, candidate.score, contribution)
                existing = fused.get(candidate.job_id)
                if existing is None:
                    fused[candidate.job_id] = _FusedCandidate(
                        source_modified_at=candidate.source_modified_at,
                        evidence=[rank_evidence],
                    )
                else:
                    if existing.source_modified_at != candidate.source_modified_at:
                        raise SearchUnavailableError(
                            "retrieval lanes disagree on source_modified_at"
                        )
                    existing.evidence.append(rank_evidence)

        if not self._enable_multiview_maxsim:
            lane_traces.append(
                LaneTrace(
                    "qwen_dense_multiview_maxsim",
                    "disabled",
                    "feature_flag_disabled",
                    0,
                )
            )
        lane_traces.extend(
            LaneTrace(name, "disabled", reason, 0)
            for name, reason in (
                ("graph", "ablation_not_approved"),
                ("reranker", "calibration_not_approved"),
                ("ltr", "calibration_not_approved"),
                ("guardrail", "calibration_not_approved"),
            )
        )

        scored = [
            self._result_trace(job_id, candidate, as_of=as_of)
            for job_id, candidate in fused.items()
        ]
        scored.sort(
            key=lambda item: (
                -item.fused_score,
                -item.freshness_score,
                item.job_id,
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
                location_filter_applied=bool(query.location_codes),
                duty_filter_applied=bool(query.duty_codes),
                lanes=tuple(lane_traces),
                results=selected,
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
            self._ports.dense_multiview_maxsim,
        ):
            if retriever is not None and id(retriever) not in seen:
                seen.add(id(retriever))
                retriever.close()

    def _validate_lane(
        self,
        lane_name: str,
        candidates: object,
        *,
        eligible_from: datetime,
    ) -> tuple[CandidateEvidence, ...]:
        if not isinstance(candidates, tuple):
            raise SearchUnavailableError(f"{lane_name} returned a non-tuple candidate set")
        if len(candidates) > CANDIDATE_LIMIT:
            raise SearchUnavailableError(f"{lane_name} returned too many candidates")
        seen: set[str] = set()
        for candidate in candidates:
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
            updated_at = _aware(candidate.source_modified_at, field="source_modified_at")
            if updated_at < eligible_from:
                raise SearchUnavailableError(
                    f"{lane_name} violated the pre-Top-K temporal eligibility contract"
                )
            seen.add(candidate.job_id)
        return candidates

    def _result_trace(
        self,
        job_id: str,
        candidate: _FusedCandidate,
        *,
        as_of: datetime,
    ) -> ResultTrace:
        evidence = tuple(candidate.evidence)
        fused_score = sum(item.rrf_contribution for item in evidence)
        if candidate.source_modified_at > as_of:
            freshness = 0.0
        else:
            age = as_of - candidate.source_modified_at
            freshness = max(
                0.0,
                1.0 - age.total_seconds() / timedelta(days=MAX_AGE_DAYS).total_seconds(),
            )
        return ResultTrace(
            job_id=job_id,
            fused_score=fused_score,
            freshness_score=freshness,
            source_modified_at=candidate.source_modified_at,
            evidence=evidence,
        )


def _aware(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SearchUnavailableError(f"{field} must be timezone-aware")
    return value


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
