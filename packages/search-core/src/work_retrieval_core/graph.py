from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any

from work_retrieval_core.engine import (
    CandidateEvidence,
    CandidateRequest,
    CandidateRetriever,
)
from work_retrieval_core.graph_policy import (
    ALLOWED_RELATION_TYPES,
    MAXIMUM_ANCHORS,
    MAXIMUM_BRIDGE_TERMS,
    MAXIMUM_DUTY_ANCHORS,
    MAXIMUM_EDGES_PER_ANCHOR,
    MAXIMUM_GRAPH_SEED_JOBS,
    MAXIMUM_JOBS_PER_BRIDGE_TERM,
    MAXIMUM_NOVEL_TOP_10,
    MINIMUM_ANCHOR_SEED_JOBS,
    MINIMUM_DUTY_ANCHOR_SUPPORT,
    PROTECTED_BASELINE_HEAD,
    RRF_K,
    SEED_SCAN_DEPTH,
)
from work_retrieval_core.serialization import canonical_code, lexical_tokens

GRAPH_RETRIEVAL_TIMEOUT_SECONDS = 4.0
ALLOWED_RELATION_TYPE_SET = frozenset(ALLOWED_RELATION_TYPES)
ASCII_TOKEN = re.compile(r"[a-z0-9][a-z0-9+#.\-]*")


@dataclass(frozen=True, slots=True)
class _WeightedSkill:
    skill: str
    support: int
    weight: float


@dataclass(frozen=True, slots=True)
class _Relation:
    source: str
    relation_type: str
    target: str
    support: int
    weight: float


@dataclass(frozen=True, slots=True)
class _BridgeTerm:
    text: str
    strength: float


@dataclass(frozen=True, slots=True)
class SkillGraphIndex:
    skills_by_job: dict[str, tuple[str, ...]]
    skills_by_alias: dict[str, tuple[str, ...]]
    skills_by_duty: dict[str, tuple[_WeightedSkill, ...]]
    relations_by_skill: dict[str, tuple[_Relation, ...]]

    @classmethod
    def from_paths(
        cls,
        job_skills_path: Path,
        duty_skills_path: Path,
        skill_relations_path: Path,
    ) -> SkillGraphIndex:
        job_skills = _read_jsonl(job_skills_path, "job skills")
        duty_skills = _read_jsonl(duty_skills_path, "duty skills")
        relations = _read_jsonl(skill_relations_path, "skill relations")
        if not job_skills or not relations:
            raise RuntimeError("enabled skill Graph cannot be empty")
        by_job: dict[str, set[str]] = defaultdict(set)
        by_alias: dict[str, set[str]] = defaultdict(set)
        for row in job_skills:
            _exact_keys(row, {"job_id", "skill", "surface", "evidence_span"}, "job skill")
            job_id = _job_id(row["job_id"])
            skill = _text(row["skill"], "job skill")
            surface = _text(row["surface"], "job skill surface")
            _text(row["evidence_span"], "job skill evidence")
            by_job[job_id].add(skill)
            by_alias[skill].add(skill)
            by_alias[surface].add(skill)

        by_duty: dict[str, list[_WeightedSkill]] = defaultdict(list)
        for row in duty_skills:
            _exact_keys(row, {"duty", "skill", "support", "weight"}, "duty skill")
            duty = _text(row["duty"], "duty")
            by_duty[duty].append(
                _WeightedSkill(
                    _text(row["skill"], "duty skill"),
                    _positive_int(row["support"], "duty support"),
                    _weight(row["weight"], "duty weight"),
                )
            )

        by_relation: dict[str, list[_Relation]] = defaultdict(list)
        for row in relations:
            _exact_keys(
                row,
                {"source", "type", "target", "support", "weight"},
                "skill relation",
            )
            relation_type = _text(row["type"], "relation type").upper()
            if relation_type not in ALLOWED_RELATION_TYPE_SET:
                raise RuntimeError("skill Graph contains an unsupported relation type")
            relation = _Relation(
                _text(row["source"], "relation source"),
                relation_type,
                _text(row["target"], "relation target"),
                _positive_int(row["support"], "relation support"),
                _weight(row["weight"], "relation weight"),
            )
            if relation.source == relation.target:
                raise RuntimeError("skill Graph relation cannot be self-referential")
            by_relation[relation.source].append(relation)
            by_relation[relation.target].append(relation)

        return cls(
            {key: tuple(sorted(values)) for key, values in by_job.items()},
            {key: tuple(sorted(values)) for key, values in by_alias.items()},
            {
                key: tuple(sorted(values, key=lambda value: (-value.weight, value.skill)))
                for key, values in by_duty.items()
            },
            {
                key: tuple(
                    sorted(
                        values,
                        key=lambda value: (
                            -value.weight,
                            value.source,
                            value.relation_type,
                            value.target,
                        ),
                    )
                )
                for key, values in by_relation.items()
            },
        )

    def bridge_terms(
        self,
        query: str,
        duty_terms: tuple[str, ...],
        baseline_job_ids: tuple[str, ...],
    ) -> tuple[_BridgeTerm, ...]:
        anchors: dict[str, tuple[int, float]] = {}

        def add(skill: str, priority: int, strength: float) -> None:
            current = anchors.get(skill)
            if current is None or (priority, -strength) < (current[0], -current[1]):
                anchors[skill] = (priority, strength)

        normalized_query = canonical_code(query)
        query_tokens = set(lexical_tokens(query))
        for alias, skills in self.skills_by_alias.items():
            if _alias_matches_query(alias, normalized_query, query_tokens):
                for skill in skills:
                    add(skill, 0, 1.0)

        duty_candidates = [
            item
            for duty in duty_terms
            for item in self.skills_by_duty.get(canonical_code(duty), ())
            if item.support >= MINIMUM_DUTY_ANCHOR_SUPPORT
        ]
        for item in sorted(
            duty_candidates,
            key=lambda value: (-value.weight, -value.support, value.skill),
        )[:MAXIMUM_DUTY_ANCHORS]:
            add(item.skill, 1, item.weight)

        seed_job_ids = tuple(job_id for job_id in baseline_job_ids if job_id in self.skills_by_job)[
            :MAXIMUM_GRAPH_SEED_JOBS
        ]
        seed_counts = Counter(
            skill for job_id in seed_job_ids for skill in self.skills_by_job.get(job_id, ())
        )
        seed_total = len(seed_job_ids)
        if seed_total:
            for skill, support in seed_counts.items():
                if support >= MINIMUM_ANCHOR_SEED_JOBS:
                    add(skill, 2, support / seed_total)

        selected = sorted(
            anchors.items(),
            key=lambda item: (item[1][0], -item[1][1], item[0]),
        )[:MAXIMUM_ANCHORS]
        bridge_scores: dict[str, float] = defaultdict(float)
        for anchor, (_priority, strength) in selected:
            for relation in self.relations_by_skill.get(anchor, ())[:MAXIMUM_EDGES_PER_ANCHOR]:
                related = relation.target if relation.source == anchor else relation.source
                bridge_scores[related] += strength * relation.weight
        return tuple(
            _BridgeTerm(term, bridge_scores[term])
            for term in sorted(
                bridge_scores,
                key=lambda value: (-bridge_scores[value], value),
            )[:MAXIMUM_BRIDGE_TERMS]
        )


class GraphConditionedRetriever:
    def __init__(
        self,
        baseline: CandidateRetriever,
        graph: SkillGraphIndex,
        *,
        duty_terms: Callable[[tuple[str, ...]], tuple[str, ...]],
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if not isinstance(baseline, CandidateRetriever):
            raise TypeError("Graph baseline must satisfy CandidateRetriever")
        self._baseline = baseline
        self._graph = graph
        self._duty_terms = duty_terms
        self._clock = clock

    def retrieve(self, request: CandidateRequest, *, limit: int) -> tuple[CandidateEvidence, ...]:
        deadline = self._clock() + GRAPH_RETRIEVAL_TIMEOUT_SECONDS
        baseline_limit = max(limit, SEED_SCAN_DEPTH)
        baseline = _validated_candidates(
            self._baseline.retrieve(request, limit=baseline_limit),
            limit=baseline_limit,
        )
        self._require_time(deadline)
        baseline_ranks = {candidate.job_id: candidate.rank for candidate in baseline}
        bridge_terms = self._graph.bridge_terms(
            request.text,
            self._duty_terms(request.duty_codes),
            tuple(baseline_ranks),
        )
        graph_scores: dict[str, float] = defaultdict(float)
        for bridge in bridge_terms:
            self._require_time(deadline)
            bridge_request = CandidateRequest(
                text=bridge.text,
                location_codes=request.location_codes,
                duty_codes=request.duty_codes,
                as_of=request.as_of,
                minimum_updated_at=request.minimum_updated_at,
                lexical_texts=(bridge.text,),
                query_rewrites=request.query_rewrites,
            )
            candidates = _validated_candidates(
                self._baseline.retrieve(
                    bridge_request,
                    limit=MAXIMUM_JOBS_PER_BRIDGE_TERM,
                ),
                limit=MAXIMUM_JOBS_PER_BRIDGE_TERM,
            )
            for candidate in candidates:
                graph_scores[candidate.job_id] += bridge.strength / (RRF_K + candidate.rank)
            self._require_time(deadline)

        graph_order = sorted(graph_scores, key=lambda job_id: (-graph_scores[job_id], int(job_id)))
        graph_ranks = {job_id: rank for rank, job_id in enumerate(graph_order, start=1)}
        scores = {
            job_id: (1 / (RRF_K + baseline_ranks[job_id]) if job_id in baseline_ranks else 0)
            + (1 / (RRF_K + graph_ranks[job_id]) if job_id in graph_ranks else 0)
            for job_id in baseline_ranks.keys() | graph_ranks.keys()
        }
        raw_order = sorted(
            scores,
            key=lambda job_id: (
                -scores[job_id],
                baseline_ranks.get(job_id, 10**9),
                int(job_id),
            ),
        )
        ordered = _protected_order(raw_order, baseline_ranks)[:limit]
        return tuple(
            CandidateEvidence(job_id, float(len(ordered) - rank + 1), rank)
            for rank, job_id in enumerate(ordered, start=1)
        )

    def eligible_indices(self, request: CandidateRequest, *, max_rows: int) -> Any:
        eligible = getattr(self._baseline, "eligible_indices", None)
        if not callable(eligible):
            raise RuntimeError("Graph baseline does not expose eligible rows")
        return eligible(request, max_rows=max_rows)

    def close(self) -> None:
        pass

    def _require_time(self, deadline: float) -> None:
        if self._clock() >= deadline:
            raise RuntimeError("Graph retrieval exceeded its bounded deadline")


def _protected_order(ordered: list[str], baseline_ranks: dict[str, int]) -> list[str]:
    protected = [
        job_id
        for job_id, rank in sorted(baseline_ranks.items(), key=lambda item: item[1])
        if rank <= PROTECTED_BASELINE_HEAD
    ]
    protected_set = set(protected)
    selected = list(protected)
    deferred: list[str] = []
    novel_top_ten = 0
    novel_limit = max(MAXIMUM_NOVEL_TOP_10, 10 - min(10, len(baseline_ranks)))
    for job_id in (job_id for job_id in ordered if job_id not in protected_set):
        if len(selected) >= 10 or (job_id not in baseline_ranks and novel_top_ten >= novel_limit):
            deferred.append(job_id)
        else:
            novel_top_ten += job_id not in baseline_ranks
            selected.append(job_id)
    return selected + deferred


def _alias_matches_query(alias: str, query: str, query_tokens: set[str]) -> bool:
    if len(alias) < 2:
        return False
    if alias.isascii():
        alias_tokens = ASCII_TOKEN.findall(alias)
        return (
            bool(alias_tokens)
            and all(token in query_tokens for token in alias_tokens)
            and (len(alias_tokens) == 1 or alias in query)
        )
    return alias in query


def _validated_candidates(
    candidates: object,
    *,
    limit: int,
) -> tuple[CandidateEvidence, ...]:
    if not isinstance(candidates, tuple) or len(candidates) > limit:
        raise RuntimeError("Graph baseline returned a malformed candidate set")
    seen: set[str] = set()
    previous_score = float("inf")
    for rank, candidate in enumerate(candidates, start=1):
        if (
            not isinstance(candidate, CandidateEvidence)
            or candidate.rank != rank
            or candidate.job_id in seen
            or not candidate.job_id.isascii()
            or not candidate.job_id.isdecimal()
            or int(candidate.job_id) < 1
            or not math.isfinite(candidate.score)
            or candidate.score > previous_score
        ):
            raise RuntimeError("Graph baseline returned a malformed candidate set")
        seen.add(candidate.job_id)
        previous_score = candidate.score
    return candidates


def _read_jsonl(path: Path, label: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    try:
        with path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                value = json.loads(
                    line,
                    parse_constant=lambda value: _raise_nonfinite(label, value),
                )
                if not isinstance(value, dict):
                    raise RuntimeError(f"{label} row {line_number} must be an object")
                rows.append(value)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{label} cannot be read as JSONL") from error
    return rows


def _raise_nonfinite(label: str, value: str) -> Any:
    raise RuntimeError(f"{label} contains a non-finite value: {value}")


def _exact_keys(value: dict[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise RuntimeError(f"{label} contains missing or unknown fields")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not (normalized := canonical_code(value)):
        raise RuntimeError(f"{label} must be non-empty text")
    return normalized


def _job_id(value: object) -> str:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or int(value) < 1:
        raise RuntimeError("skill Graph contains an invalid job_id")
    return str(int(value))


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RuntimeError(f"{label} must be a positive integer")
    return value


def _weight(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RuntimeError(f"{label} must be finite")
    parsed = float(value)
    if not 0 < parsed <= 1:
        raise RuntimeError(f"{label} must be in (0, 1]")
    return parsed
