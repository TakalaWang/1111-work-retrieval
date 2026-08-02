from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from work_retrieval_core import CandidateEvidence, CandidateRequest
from work_retrieval_core.graph import GraphConditionedRetriever, SkillGraphIndex


class StubBaseline:
    def __init__(self) -> None:
        self.requests: list[CandidateRequest] = []
        self.limits: list[int] = []
        self.closed = False

    def retrieve(self, request: CandidateRequest, *, limit: int) -> tuple[CandidateEvidence, ...]:
        self.requests.append(request)
        self.limits.append(limit)
        if request.lexical_texts == ("sql",):
            rows = (("9", 8.0), ("2", 7.0))
        else:
            rows = tuple((str(index), 20.0 - index) for index in range(1, 11))
        return tuple(
            CandidateEvidence(job_id, score, rank)
            for rank, (job_id, score) in enumerate(rows[:limit], start=1)
        )

    def close(self) -> None:
        self.closed = True


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_graph_conditioned_retriever_uses_bounded_bridge_terms_and_protects_head(
    tmp_path: Path,
) -> None:
    job_skills = tmp_path / "job-skills.jsonl"
    duty_skills = tmp_path / "duty-skills.jsonl"
    relations = tmp_path / "skill-relations.jsonl"
    _write_jsonl(
        job_skills,
        [
            {"job_id": "1", "skill": "python", "surface": "Python", "evidence_span": "Python"},
            {"job_id": "2", "skill": "python", "surface": "Python", "evidence_span": "Python"},
        ],
    )
    _write_jsonl(
        duty_skills,
        [{"duty": "軟體工程師", "skill": "python", "support": 2, "weight": 0.8}],
    )
    _write_jsonl(
        relations,
        [
            {
                "source": "python",
                "type": "USED_WITH",
                "target": "sql",
                "support": 2,
                "weight": 0.9,
            }
        ],
    )
    baseline = StubBaseline()
    retriever = GraphConditionedRetriever(
        baseline,
        SkillGraphIndex.from_paths(job_skills, duty_skills, relations),
        duty_terms=lambda codes: ("軟體工程師",) if codes == ("140200",) else (),
    )
    as_of = datetime(2026, 6, 8, tzinfo=UTC)
    request = CandidateRequest(
        text="Python 工程師",
        location_codes=("100100",),
        duty_codes=("140200",),
        as_of=as_of,
        minimum_updated_at=as_of - timedelta(days=180),
        lexical_texts=("Python 工程師",),
    )

    result = retriever.retrieve(request, limit=10)

    assert [candidate.job_id for candidate in result[:3]] == ["1", "2", "3"]
    assert [candidate.rank for candidate in result] == list(range(1, 11))
    assert [candidate.score for candidate in result] == sorted(
        (candidate.score for candidate in result), reverse=True
    )
    assert any(call.lexical_texts == ("sql",) for call in baseline.requests)
    assert baseline.limits[0] == 1000
    assert baseline.limits[1:] == [50]
    bridge = next(call for call in baseline.requests if call.lexical_texts == ("sql",))
    assert bridge.location_codes == request.location_codes
    assert bridge.duty_codes == request.duty_codes
    assert bridge.as_of == request.as_of
    assert bridge.minimum_updated_at == request.minimum_updated_at

    retriever.close()
    assert not baseline.closed


def test_graph_index_rejects_nonfinite_relation_weight(tmp_path: Path) -> None:
    job_skills = tmp_path / "job-skills.jsonl"
    duty_skills = tmp_path / "duty-skills.jsonl"
    relations = tmp_path / "skill-relations.jsonl"
    _write_jsonl(job_skills, [])
    _write_jsonl(duty_skills, [])
    relations.write_text(
        '{"source":"python","type":"USED_WITH","target":"sql","support":2,"weight":NaN}\n',
        encoding="utf-8",
    )

    try:
        SkillGraphIndex.from_paths(job_skills, duty_skills, relations)
    except RuntimeError as error:
        assert "finite" in str(error)
    else:
        raise AssertionError("non-finite Graph weight was accepted")


def test_ascii_alias_requires_token_boundary(tmp_path: Path) -> None:
    job_skills = tmp_path / "job-skills.jsonl"
    duty_skills = tmp_path / "duty-skills.jsonl"
    relations = tmp_path / "skill-relations.jsonl"
    _write_jsonl(
        job_skills,
        [{"job_id": "1", "skill": "go", "surface": "go", "evidence_span": "go"}],
    )
    _write_jsonl(duty_skills, [])
    _write_jsonl(
        relations,
        [
            {
                "source": "go",
                "type": "USED_WITH",
                "target": "sql",
                "support": 1,
                "weight": 1.0,
            }
        ],
    )
    index = SkillGraphIndex.from_paths(job_skills, duty_skills, relations)

    assert index.bridge_terms("Google Cloud", (), ()) == ()
    assert len(index.bridge_terms("Go engineer", (), ())) == 1


def test_enabled_graph_cannot_be_empty(tmp_path: Path) -> None:
    paths = [
        tmp_path / name
        for name in ("job-skills.jsonl", "duty-skills.jsonl", "skill-relations.jsonl")
    ]
    for path in paths:
        _write_jsonl(path, [])

    try:
        SkillGraphIndex.from_paths(*paths)
    except RuntimeError as error:
        assert "cannot be empty" in str(error)
    else:
        raise AssertionError("empty Graph was accepted")


def test_graph_retrieval_fails_when_internal_deadline_is_exhausted(tmp_path: Path) -> None:
    job_skills = tmp_path / "job-skills.jsonl"
    duty_skills = tmp_path / "duty-skills.jsonl"
    relations = tmp_path / "skill-relations.jsonl"
    _write_jsonl(
        job_skills,
        [{"job_id": "1", "skill": "python", "surface": "Python", "evidence_span": "Python"}],
    )
    _write_jsonl(duty_skills, [])
    _write_jsonl(
        relations,
        [
            {
                "source": "python",
                "type": "USED_WITH",
                "target": "sql",
                "support": 1,
                "weight": 1.0,
            }
        ],
    )
    ticks = iter((0.0, 5.0))
    retriever = GraphConditionedRetriever(
        StubBaseline(),
        SkillGraphIndex.from_paths(job_skills, duty_skills, relations),
        duty_terms=lambda codes: (),
        clock=lambda: next(ticks),
    )
    as_of = datetime(2026, 6, 8, tzinfo=UTC)

    try:
        retriever.retrieve(
            CandidateRequest(
                "Python",
                (),
                (),
                as_of,
                as_of - timedelta(days=180),
                ("Python",),
            ),
            limit=10,
        )
    except RuntimeError as error:
        assert "deadline" in str(error)
    else:
        raise AssertionError("expired Graph retrieval continued")
