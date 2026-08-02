from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite

RRF_K = 60
MAX_RERANK_POOL = 50


@dataclass(frozen=True, slots=True)
class BusinessEvidence:
    """External, time-safe business signals; never inferred by the LLM."""

    popularity: float
    completeness: float

    def validate(self) -> None:
        if not all(
            isfinite(value) and 0.0 <= value <= 1.0
            for value in (self.popularity, self.completeness)
        ):
            raise ValueError("business evidence must be finite values between zero and one")


@dataclass(frozen=True, slots=True)
class CandidateEvidenceGate:
    job_id: str
    llm_suitability: float
    hard_constraints_match: bool
    occupation_match: bool
    bm25_supported: bool
    dense_supported: bool
    business: BusinessEvidence

    def __post_init__(self) -> None:
        if not self.job_id or self.job_id != self.job_id.strip():
            raise ValueError("candidate job ID must be a canonical non-empty string")
        if not isfinite(self.llm_suitability) or not 0.0 <= self.llm_suitability <= 1.0:
            raise ValueError("LLM suitability must be between zero and one")
        self.business.validate()

    def promotable(self, suitability_threshold: float) -> bool:
        return (
            self.hard_constraints_match
            and self.llm_suitability >= suitability_threshold
            and (self.occupation_match or (self.bm25_supported and self.dense_supported))
        )


def weighted_rrf_pool(
    lanes: Mapping[str, Sequence[str]],
    weights: Mapping[str, float],
    *,
    limit: int = MAX_RERANK_POOL,
) -> tuple[str, ...]:
    if not lanes or set(lanes) != set(weights) or not 1 <= limit <= MAX_RERANK_POOL:
        raise ValueError("RRF lanes, weights, or limit are invalid")
    scores: defaultdict[str, float] = defaultdict(float)
    for name, candidates in lanes.items():
        weight = weights[name]
        if not isfinite(weight) or weight <= 0:
            raise ValueError("RRF weights must be finite and positive")
        if len(set(candidates)) != len(candidates):
            raise ValueError("each RRF lane must contain unique candidates")
        for rank, job_id in enumerate(candidates, 1):
            if not job_id or job_id != job_id.strip():
                raise ValueError("RRF job IDs must be canonical non-empty strings")
            scores[job_id] += weight / (RRF_K + rank)
    return tuple(sorted(scores, key=lambda job_id: (-scores[job_id], job_id))[:limit])


def evidence_preserving_rank(
    prior: Sequence[str],
    reranker_order: Sequence[str],
    evidence: Mapping[str, CandidateEvidenceGate],
    *,
    protected_prefix: int = 1,
    suitability_threshold: float = 0.9,
    reranker_weight: float = 0.5,
    business_weight: float = 0.1,
    max_business_displacement: int = 2,
) -> tuple[str, ...]:
    """Fuse semantic and business evidence without allowing occupation drift."""
    prior = tuple(prior)
    reranker_order = tuple(reranker_order)
    if (
        not prior
        or len(set(prior)) != len(prior)
        or len(set(reranker_order)) != len(reranker_order)
        or set(prior) != set(reranker_order)
        or set(prior) != set(evidence)
        or not 0 <= protected_prefix <= len(prior)
        or not isfinite(suitability_threshold)
        or not 0.0 <= suitability_threshold <= 1.0
        or not isfinite(reranker_weight)
        or reranker_weight < 0.0
        or not isfinite(business_weight)
        or business_weight < 0.0
        or isinstance(max_business_displacement, bool)
        or max_business_displacement < 0
    ):
        raise ValueError("ranking contract is invalid")
    if any(item.job_id != job_id for job_id, item in evidence.items()):
        raise ValueError("ranking evidence key and job ID differ")

    prior_rank = {job_id: rank for rank, job_id in enumerate(prior, 1)}
    reranker_rank = {job_id: rank for rank, job_id in enumerate(reranker_order, 1)}
    protected = prior[:protected_prefix]
    movable = prior[protected_prefix:]
    relevance_order = tuple(
        sorted(
            movable,
            key=lambda job_id: (
                -(
                    1.0 / (RRF_K + prior_rank[job_id])
                    + (
                        reranker_weight / (RRF_K + reranker_rank[job_id])
                        if evidence[job_id].promotable(suitability_threshold)
                        else 0.0
                    )
                ),
                prior_rank[job_id],
                job_id,
            ),
        )
    )
    ranked = [*protected, *relevance_order]
    if business_weight == 0.0 or max_business_displacement == 0:
        return tuple(ranked)

    def reorder(start: int, stop: int) -> None:
        current = {job_id: position for position, job_id in enumerate(ranked)}

        def business_score(job_id: str) -> float:
            item = evidence[job_id]
            if not item.promotable(suitability_threshold):
                return 0.0
            return business_weight * (item.business.popularity + item.business.completeness) / 2.0

        ranked[start:stop] = sorted(
            ranked[start:stop],
            key=lambda job_id: (-business_score(job_id), current[job_id], job_id),
        )

    top_three_end = min(3, len(ranked))
    if protected_prefix < top_three_end:
        reorder(protected_prefix, top_three_end)
    band_size = max_business_displacement + 1
    for start in range(max(top_three_end, protected_prefix), len(ranked), band_size):
        reorder(start, min(start + band_size, len(ranked)))
    return tuple(ranked)
