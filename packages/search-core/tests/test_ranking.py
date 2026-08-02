from __future__ import annotations

import pytest
from work_retrieval_core.ranking import (
    BusinessEvidence,
    CandidateEvidenceGate,
    evidence_preserving_rank,
    weighted_rrf_pool,
)


def _gate(
    job_id: str,
    *,
    suitability: float = 0.95,
    occupation_match: bool = True,
    bm25: bool = True,
    dense: bool = True,
    constraints: bool = True,
    popularity: float = 0.0,
    completeness: float = 0.0,
) -> CandidateEvidenceGate:
    return CandidateEvidenceGate(
        job_id=job_id,
        llm_suitability=suitability,
        hard_constraints_match=constraints,
        occupation_match=occupation_match,
        bm25_supported=bm25,
        dense_supported=dense,
        business=BusinessEvidence(popularity, completeness),
    )


def test_weighted_rrf_pool_uses_all_evidence_lanes_and_caps_at_50() -> None:
    bm25 = tuple(str(index) for index in range(60))
    ranked = weighted_rrf_pool(
        {
            "bm25": bm25,
            "whole_dense": ("2", "60"),
            "graph": ("61",),
            "multiview": ("62",),
        },
        {"bm25": 4.0, "whole_dense": 1.0, "graph": 0.25, "multiview": 0.5},
        limit=50,
    )

    assert len(ranked) == 50
    assert ranked[0] == "2"
    assert len(set(ranked)) == len(ranked)


def test_unrelated_or_constraint_conflicting_job_cannot_be_promoted() -> None:
    prior = ("a", "b", "c", "d")
    reranker = ("d", "c", "b", "a")
    evidence = {
        "a": _gate("a"),
        "b": _gate("b"),
        "c": _gate("c", constraints=False, popularity=1.0),
        "d": _gate(
            "d",
            occupation_match=False,
            bm25=False,
            dense=False,
            popularity=1.0,
            completeness=1.0,
        ),
    }

    ranked = evidence_preserving_rank(
        prior,
        reranker,
        evidence,
        protected_prefix=1,
        reranker_weight=0.5,
    )

    assert ranked[0] == "a"
    assert ranked.index("c") < ranked.index("d")
    assert ranked[-1] == "d"


def test_cross_modal_support_can_replace_literal_occupation_match() -> None:
    prior = ("a", "b", "c", "d")
    evidence = {job_id: _gate(job_id, occupation_match=job_id != "d") for job_id in prior}

    ranked = evidence_preserving_rank(
        prior,
        ("d", "c", "b", "a"),
        evidence,
        protected_prefix=1,
        reranker_weight=2.0,
    )

    assert ranked[0] == "a"
    assert ranked.index("d") < prior.index("d")


def test_named_bm25_protection_preserves_original_prior_for_fusion() -> None:
    prior = ("dense-both", "bm25-top", "other")
    evidence = {job_id: _gate(job_id) for job_id in prior}

    ranked = evidence_preserving_rank(
        prior,
        ("other", "dense-both", "bm25-top"),
        evidence,
        protected_prefix=0,
        protected_job_ids=("bm25-top",),
        reranker_weight=0.5,
    )

    assert ranked[0] == "bm25-top"
    assert set(ranked) == set(prior)


def test_business_evidence_is_bounded_below_relevance_top_three() -> None:
    prior = ("a", "b", "c", "d", "e", "f", "g")
    evidence = {
        job_id: _gate(
            job_id,
            popularity=1.0 if job_id in {"d", "g"} else 0.0,
            completeness=1.0 if job_id in {"d", "g"} else 0.0,
        )
        for job_id in prior
    }

    ranked = evidence_preserving_rank(
        prior,
        prior,
        evidence,
        protected_prefix=1,
        reranker_weight=0.0,
        business_weight=0.2,
        max_business_displacement=2,
    )

    assert ranked[:3] == prior[:3]
    assert ranked.index("g") >= 4
    assert abs(ranked.index("g") - prior.index("g")) <= 2


@pytest.mark.parametrize(
    "evidence",
    [
        BusinessEvidence(float("nan"), 0.0),
        BusinessEvidence(0.0, 1.1),
        BusinessEvidence(-0.1, 0.0),
    ],
)
def test_business_evidence_rejects_unbounded_values(evidence: BusinessEvidence) -> None:
    with pytest.raises(ValueError):
        evidence.validate()


def test_ranker_fails_closed_on_membership_or_evidence_mismatch() -> None:
    evidence = {"a": _gate("a"), "b": _gate("b")}
    with pytest.raises(ValueError):
        evidence_preserving_rank(("a", "b"), ("a", "c"), evidence)
    with pytest.raises(ValueError):
        evidence_preserving_rank(("a", "b"), ("a", "b"), {"a": evidence["a"]})
