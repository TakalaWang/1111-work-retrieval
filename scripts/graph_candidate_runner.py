#!/usr/bin/env python3
"""Generate a traceable Graph challenger from a frozen baseline TREC run."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from pipeline_contract import (
    atomic_json,
    exact_keys,
    read_json_object,
    require_sha256,
    sha256_file,
)
from skill_graph_pipeline import GRAPH_FILES, _read_jsonl, validate_graph
from tantivy_graph_off_runner import CanonicalQuery, read_canonical_queries
from tantivy_index_pipeline import DEFAULT_ARTIFACT_PREFIX, _local, validate_tantivy
from work_retrieval_core.adapters import (
    FilterTaxonomy,
    TantivyBm25Retriever,
    lexical_tokens,
    load_job_ids,
)
from work_retrieval_core.engine import MAX_AGE_DAYS, CandidateEvidence, CandidateRequest
from work_retrieval_core.graph_policy import (
    GRAPH_SERVING_ALGORITHM,
    GRAPH_SERVING_IMPLEMENTATION_SHA256,
    GRAPH_SERVING_POLICY,
)

RUN_MANIFEST_KEYS = {
    "schema_version",
    "complete",
    "variant",
    "split_manifest_sha256",
    "graph_manifest_sha256",
    "canonical_qids",
    "zero_result_qids",
    "non_graph_inputs",
    "run_sha256",
}
PARAMETERS: dict[str, object] = {
    **GRAPH_SERVING_POLICY,
    "maximum_relation_evidence_per_path": 3,
    "maximum_paths_per_candidate": 5,
    "maximum_results": 1000,
    "candidate_universe": "temporal_v2_tantivy_eligible_universe",
    "trec_score_policy": "strict_rank_descending_integer_v1",
}
ALGORITHM = GRAPH_SERVING_ALGORITHM
EVALUATION_IMPLEMENTATION_SHA256 = hashlib.sha256(
    GRAPH_SERVING_IMPLEMENTATION_SHA256.encode() + b"\0" + Path(__file__).read_bytes()
).hexdigest()
TAG = "graph-consensus-v1"
SAFE_QID = re.compile(r"[A-Za-z0-9_.:-]+")


@dataclass(frozen=True)
class RunRow:
    qid: str
    job_id: str
    rank: int
    score: float


@dataclass(frozen=True)
class GraphIndexes:
    skills_by_job: dict[str, list[dict[str, object]]]
    aliases: dict[str, list[dict[str, object]]]
    skills_by_duty: dict[str, list[dict[str, object]]]
    relations_by_skill: dict[str, list[dict[str, object]]]
    evidence_by_relation: dict[tuple[str, str, str], list[dict[str, object]]]


def read_trec_run(path: Path) -> dict[str, list[RunRow]]:
    queries: dict[str, list[RunRow]] = {}
    seen_qids: set[str] = set()
    active_qid: str | None = None
    active_job_ids: set[str] = set()
    try:
        with path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                parts = line.split()
                if len(parts) != 6:
                    raise RuntimeError(f"TREC line {line_number} must contain six fields")
                qid, q0, job_id, raw_rank, raw_score, tag = parts
                if not qid.isascii() or SAFE_QID.fullmatch(qid) is None:
                    raise RuntimeError(f"TREC line {line_number} has an unsafe qid")
                if (
                    not job_id.isascii()
                    or not job_id.isdecimal()
                    or int(job_id) < 1
                    or job_id != str(int(job_id))
                ):
                    raise RuntimeError(f"TREC line {line_number} has an unsafe job_id")
                if q0 != "Q0" or not tag.isascii() or SAFE_QID.fullmatch(tag) is None:
                    raise RuntimeError(f"TREC line {line_number} has an invalid Q0 or tag")
                try:
                    rank = int(raw_rank)
                    score = float(raw_score)
                except ValueError as error:
                    raise RuntimeError(
                        f"TREC line {line_number} has an invalid rank or score"
                    ) from error
                if rank < 1 or not math.isfinite(score):
                    raise RuntimeError(f"TREC line {line_number} has an invalid rank or score")
                if qid != active_qid:
                    if qid in seen_qids:
                        raise RuntimeError("TREC qid blocks must be contiguous")
                    seen_qids.add(qid)
                    active_qid = qid
                    active_job_ids = set()
                    queries[qid] = []
                rows = queries[qid]
                if len(rows) >= int(PARAMETERS["maximum_results"]):
                    raise RuntimeError(f"TREC qid {qid} exceeds fixed maximum_results")
                if rank != len(rows) + 1 or job_id in active_job_ids:
                    raise RuntimeError(f"TREC ranks or job IDs differ for qid {qid}")
                rows.append(RunRow(qid=qid, job_id=job_id, rank=rank, score=score))
                active_job_ids.add(job_id)
    except (OSError, UnicodeError) as error:
        raise RuntimeError("TREC run cannot be read as UTF-8") from error
    return queries


def _manifest_qids(manifest: dict[str, object]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    canonical = manifest["canonical_qids"]
    zero = manifest["zero_result_qids"]
    if (
        not isinstance(canonical, list)
        or not canonical
        or any(not isinstance(qid, str) or SAFE_QID.fullmatch(qid) is None for qid in canonical)
        or canonical != list(dict.fromkeys(canonical))
        or not isinstance(zero, list)
        or any(not isinstance(qid, str) for qid in zero)
        or zero != [qid for qid in canonical if qid in set(zero)]
    ):
        raise RuntimeError("run manifest canonical or zero-result qids differ")
    return tuple(canonical), tuple(zero)


def _graph_off_inputs(
    path: Path, run_path: Path, split_sha256: str
) -> tuple[dict[str, str], tuple[str, ...], tuple[str, ...]]:
    manifest = read_json_object(path, "graph_off run manifest")
    exact_keys(manifest, RUN_MANIFEST_KEYS, "graph_off run manifest")
    if (
        manifest["schema_version"] != 1
        or manifest["complete"] is not True
        or manifest["variant"] != "graph_off"
        or manifest["split_manifest_sha256"] != split_sha256
        or manifest["graph_manifest_sha256"] is not None
        or manifest["run_sha256"] != sha256_file(run_path)
    ):
        raise RuntimeError("graph_off run lineage differs")
    require_sha256(manifest["run_sha256"], "graph_off run SHA-256")
    canonical_qids, zero_result_qids = _manifest_qids(manifest)
    raw_inputs = manifest["non_graph_inputs"]
    if not isinstance(raw_inputs, dict) or not raw_inputs:
        raise RuntimeError("graph_off non_graph_inputs must be a non-empty object")
    inputs: dict[str, str] = {}
    for name, digest in raw_inputs.items():
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError("graph_off non-graph input name must be non-empty")
        inputs[name] = require_sha256(digest, f"graph_off non-graph input {name}")
    return inputs, canonical_qids, zero_result_qids


def _graph_indexes(graph_output: Path) -> GraphIndexes:
    job_skills = _read_jsonl(graph_output / GRAPH_FILES["job_skills"], "job_skills")
    duty_skills = _read_jsonl(graph_output / GRAPH_FILES["duty_skills"], "duty_skills")
    relations = _read_jsonl(graph_output / GRAPH_FILES["skill_relations"], "skill_relations")
    relation_evidence = _read_jsonl(
        graph_output / GRAPH_FILES["relation_evidence"], "relation_evidence"
    )
    skills_by_job: dict[str, list[dict[str, object]]] = defaultdict(list)
    aliases: dict[str, list[dict[str, object]]] = defaultdict(list)
    skills_by_duty: dict[str, list[dict[str, object]]] = defaultdict(list)
    relations_by_skill: dict[str, list[dict[str, object]]] = defaultdict(list)
    evidence_by_relation: dict[tuple[str, str, str], list[dict[str, object]]] = defaultdict(list)
    for edge in job_skills:
        skills_by_job[str(edge["job_id"])].append(edge)
        aliases[str(edge["skill"])].append(edge)
        aliases[str(edge["surface"])].append(edge)
    for edge in duty_skills:
        skills_by_duty[str(edge["duty"])].append(edge)
    for edge in relations:
        relations_by_skill[str(edge["source"])].append(edge)
        relations_by_skill[str(edge["target"])].append(edge)
    for evidence in relation_evidence:
        key = (str(evidence["source"]), str(evidence["type"]), str(evidence["target"]))
        evidence_by_relation[key].append(evidence)
    return GraphIndexes(
        skills_by_job,
        aliases,
        skills_by_duty,
        relations_by_skill,
        evidence_by_relation,
    )


class TemporalBridgeRetriever:
    """Re-query the validated temporal-v2 eligible universe with Graph bridge terms."""

    def __init__(
        self,
        *,
        tantivy_output: Path,
        jobs_csv: Path,
        artifact_prefix: str,
        non_graph_inputs: dict[str, str],
        queries_path: Path,
    ) -> None:
        validate_tantivy(tantivy_output, jobs_csv=jobs_csv, artifact_prefix=artifact_prefix)
        manifest = read_json_object(tantivy_output / "manifest.json", "Tantivy component")
        job_ids_path = _local(tantivy_output, manifest["job_ids_path"], artifact_prefix)
        taxonomy_path = _local(tantivy_output, manifest["taxonomy_path"], artifact_prefix)
        index_directory = _local(tantivy_output, manifest["index_directory"], artifact_prefix)
        expected = {
            "canonical_queries": sha256_file(queries_path),
            "jobs_csv": sha256_file(jobs_csv),
            "tantivy_component_manifest": sha256_file(tantivy_output / "manifest.json"),
            "tantivy_index": require_sha256(manifest["index_sha256"], "Tantivy index"),
            "tantivy_job_ids": sha256_file(job_ids_path),
            "tantivy_filter_taxonomy": sha256_file(taxonomy_path),
        }
        if any(non_graph_inputs.get(name) != digest for name, digest in expected.items()):
            raise RuntimeError("Graph expansion Tantivy inputs differ from graph_off")
        self._taxonomy = FilterTaxonomy.from_path(taxonomy_path)
        self._retriever = TantivyBm25Retriever(
            index_directory,
            load_job_ids(job_ids_path),
            self._taxonomy,
        )

    def duty_terms(self, codes: tuple[str, ...]) -> tuple[str, ...]:
        return self._taxonomy.resolve_duties(codes) or ()

    def retrieve(
        self, query: CanonicalQuery, bridge_term: str, *, limit: int
    ) -> tuple[CandidateEvidence, ...]:
        return self._retriever.retrieve(
            CandidateRequest(
                text=bridge_term,
                location_codes=query.location_codes,
                duty_codes=query.duty_codes,
                as_of=query.as_of,
                minimum_updated_at=query.as_of - timedelta(days=MAX_AGE_DAYS),
                lexical_texts=(bridge_term,),
            ),
            limit=limit,
        )

    def close(self) -> None:
        self._retriever.close()


def _protected_order(ordered: list[str], baseline_ranks: dict[str, int]) -> list[str]:
    protected_count = int(PARAMETERS["protected_baseline_head"])
    protected = [
        job_id
        for job_id, rank in sorted(baseline_ranks.items(), key=lambda item: item[1])
        if rank <= protected_count
    ]
    protected_set = set(protected)
    remaining = [job_id for job_id in ordered if job_id not in protected_set]
    selected = list(protected)
    deferred: list[str] = []
    novel_top_ten = 0
    novel_limit = max(
        int(PARAMETERS["maximum_novel_top_10"]),
        10 - min(10, len(baseline_ranks)),
    )
    for job_id in remaining:
        if len(selected) >= 10:
            deferred.append(job_id)
            continue
        if job_id not in baseline_ranks:
            if novel_top_ten >= novel_limit:
                deferred.append(job_id)
                continue
            novel_top_ten += 1
        selected.append(job_id)
    return selected + deferred


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _alias_matches_query(alias: str, query_text: str, query_tokens: set[str]) -> bool:
    if len(alias) < 2:
        return False
    if alias.isascii():
        alias_tokens = lexical_tokens(alias)
        return (
            bool(alias_tokens)
            and all(token in query_tokens for token in alias_tokens)
            and (len(alias_tokens) == 1 or alias in query_text)
        )
    return alias in query_text


def _query_candidates(
    baseline: list[RunRow],
    indexes: GraphIndexes,
    query: CanonicalQuery,
    bridge_retriever: TemporalBridgeRetriever,
) -> tuple[list[tuple[str, float]], list[dict[str, object]], dict[str, int]]:
    seed_rows = [
        row
        for row in baseline[: int(PARAMETERS["seed_scan_depth"])]
        if row.job_id in indexes.skills_by_job
    ][: int(PARAMETERS["maximum_graph_seed_jobs"])]
    seed_jobs_by_skill: dict[str, set[str]] = defaultdict(set)
    for row in seed_rows:
        for edge in indexes.skills_by_job.get(row.job_id, []):
            seed_jobs_by_skill[str(edge["skill"])].add(row.job_id)
    anchors: dict[str, dict[str, Any]] = {}

    def add_anchor(
        skill: str,
        *,
        source: str,
        strength: float,
        evidence: list[dict[str, object]],
        seed_job_ids: set[str] | None = None,
    ) -> None:
        priority = {"query_exact": 0, "duty_filter": 1, "baseline_consensus": 2}[source]
        candidate: dict[str, Any] = {
            "skill": skill,
            "source": source,
            "priority": priority,
            "strength": strength,
            "evidence": evidence,
            "seed_job_ids": seed_job_ids or set(),
        }
        current = anchors.get(skill)
        if current is None or (priority, -strength) < (
            int(current["priority"]),
            -float(current["strength"]),
        ):
            anchors[skill] = candidate

    query_text = _normalized_text(query.query)
    query_tokens = set(lexical_tokens(query.query))
    for alias, edges in sorted(indexes.aliases.items()):
        if not _alias_matches_query(alias, query_text, query_tokens):
            continue
        by_skill: dict[str, list[dict[str, object]]] = defaultdict(list)
        for edge in edges:
            by_skill[str(edge["skill"])].append(edge)
        for skill, matching_edges in by_skill.items():
            add_anchor(
                skill,
                source="query_exact",
                strength=1.0,
                evidence=[
                    {
                        "matched_alias": alias,
                        "job_id": edge["job_id"],
                        "surface": edge["surface"],
                        "evidence_span": edge["evidence_span"],
                    }
                    for edge in sorted(matching_edges, key=lambda item: int(str(item["job_id"])))
                ][: int(PARAMETERS["maximum_relation_evidence_per_path"])],
            )

    duty_anchor_rows = [
        (term, edge)
        for term in bridge_retriever.duty_terms(query.duty_codes)
        for edge in indexes.skills_by_duty.get(_normalized_text(term), [])
        if int(edge["support"]) >= int(PARAMETERS["minimum_duty_anchor_support"])
    ]
    for duty, edge in sorted(
        duty_anchor_rows,
        key=lambda item: (
            -float(item[1]["weight"]),
            -int(item[1]["support"]),
            str(item[1]["skill"]),
        ),
    )[: int(PARAMETERS["maximum_duty_anchors"])]:
        add_anchor(
            str(edge["skill"]),
            source="duty_filter",
            strength=float(edge["weight"]),
            evidence=[{"duty": duty, "support": edge["support"], "weight": edge["weight"]}],
        )

    for skill, seed_job_ids in seed_jobs_by_skill.items():
        if len(seed_job_ids) < int(PARAMETERS["minimum_anchor_seed_jobs"]):
            continue
        add_anchor(
            skill,
            source="baseline_consensus",
            strength=len(seed_job_ids) / len(seed_rows),
            seed_job_ids=seed_job_ids,
            evidence=[
                {
                    "job_id": row.job_id,
                    "surface": edge["surface"],
                    "evidence_span": edge["evidence_span"],
                }
                for row in seed_rows
                for edge in indexes.skills_by_job[row.job_id]
                if edge["skill"] == skill
            ],
        )
    selected_anchors = sorted(
        anchors.values(),
        key=lambda anchor: (
            int(anchor["priority"]),
            -float(anchor["strength"]),
            str(anchor["skill"]),
        ),
    )[: int(PARAMETERS["maximum_anchors"])]
    baseline_ranks = {row.job_id: row.rank for row in baseline}
    paths_by_job: dict[str, list[dict[str, object]]] = defaultdict(list)
    graph_scores: dict[str, float] = defaultdict(float)
    graph_path_counts: Counter[str] = Counter()
    bridge_paths: dict[str, list[dict[str, object]]] = defaultdict(list)
    allowed = set(PARAMETERS["allowed_relation_types"])
    for anchor_record in selected_anchors:
        anchor = str(anchor_record["skill"])
        anchor_strength = float(anchor_record["strength"])
        seed_job_ids = set(anchor_record["seed_job_ids"])
        relations = sorted(
            (
                edge
                for edge in indexes.relations_by_skill.get(anchor, [])
                if str(edge["type"]) in allowed
            ),
            key=lambda edge: (
                -float(edge["weight"]),
                str(edge["source"]),
                str(edge["type"]),
                str(edge["target"]),
            ),
        )[: int(PARAMETERS["maximum_edges_per_anchor"])]
        for edge in relations:
            related = str(edge["target"] if edge["source"] == anchor else edge["source"])
            direction = "outgoing" if edge["source"] == anchor else "incoming"
            relation_key = (str(edge["source"]), str(edge["type"]), str(edge["target"]))
            path_score = anchor_strength * float(edge["weight"])
            bridge_paths[related].append(
                {
                    "anchor_skill": anchor,
                    "anchor_source": anchor_record["source"],
                    "anchor_seed_job_ids": sorted(seed_job_ids, key=int),
                    "anchor_evidence": anchor_record["evidence"],
                    "anchor_consensus": anchor_strength,
                    "edge": dict(edge),
                    "traversal_direction": direction,
                    "related_skill": related,
                    "relation_evidence": [
                        {
                            "job_id": evidence["job_id"],
                            "evidence_span": evidence["evidence_span"],
                        }
                        for evidence in indexes.evidence_by_relation[relation_key][
                            : int(PARAMETERS["maximum_relation_evidence_per_path"])
                        ]
                    ],
                    "path_score": path_score,
                }
            )
    bridge_terms = sorted(
        bridge_paths,
        key=lambda term: (-sum(float(path["path_score"]) for path in bridge_paths[term]), term),
    )[: int(PARAMETERS["maximum_bridge_terms"])]
    bridge_retrieval_rows = 0
    graph_path_count = 0
    for bridge_term in bridge_terms:
        candidates = bridge_retriever.retrieve(
            query,
            bridge_term,
            limit=int(PARAMETERS["maximum_jobs_per_bridge_term"]),
        )
        bridge_retrieval_rows += len(candidates)
        for candidate in candidates:
            for path in bridge_paths[bridge_term]:
                job_id = candidate.job_id
                candidate_path = {
                    **path,
                    "candidate_evidence": {
                        "retriever": "temporal_v2_tantivy",
                        "bridge_query": bridge_term,
                        "rank": candidate.rank,
                        "score": candidate.score,
                        "as_of": query.as_of.isoformat(),
                        "minimum_updated_at": (
                            query.as_of - timedelta(days=MAX_AGE_DAYS)
                        ).isoformat(),
                        "location_codes": list(query.location_codes),
                        "duty_codes": list(query.duty_codes),
                    },
                }
                graph_path_count += 1
                graph_path_counts[job_id] += 1
                kept_paths = paths_by_job[job_id]
                kept_paths.append(candidate_path)
                kept_paths.sort(
                    key=lambda item: (
                        -float(item["path_score"]),
                        int(dict(item["candidate_evidence"])["rank"]),
                        str(item["anchor_skill"]),
                        str(item["related_skill"]),
                    )
                )
                del kept_paths[int(PARAMETERS["maximum_paths_per_candidate"]) :]
                graph_scores[job_id] += float(path["path_score"]) / (
                    int(PARAMETERS["rrf_k"]) + candidate.rank
                )
    graph_order = sorted(graph_scores, key=lambda job_id: (-graph_scores[job_id], int(job_id)))
    graph_ranks = {job_id: rank for rank, job_id in enumerate(graph_order, start=1)}
    rrf_k = int(PARAMETERS["rrf_k"])
    scores = {
        job_id: (
            float(PARAMETERS["baseline_weight"]) / (rrf_k + baseline_ranks[job_id])
            if job_id in baseline_ranks
            else 0.0
        )
        + (
            float(PARAMETERS["graph_weight"]) / (rrf_k + graph_ranks[job_id])
            if job_id in graph_ranks
            else 0.0
        )
        for job_id in baseline_ranks.keys() | graph_ranks.keys()
    }
    raw_order = sorted(
        scores.items(),
        key=lambda item: (-item[1], baseline_ranks.get(item[0], 10**9), int(item[0])),
    )
    protected_order = _protected_order([job_id for job_id, _score in raw_order], baseline_ranks)
    score_by_job = dict(raw_order)
    ranked = [(job_id, score_by_job[job_id]) for job_id in protected_order][
        : int(PARAMETERS["maximum_results"])
    ]
    traces: list[dict[str, object]] = []
    for job_id, _score in ranked:
        if job_id not in graph_ranks:
            continue
        retained_paths = paths_by_job[job_id]
        retained_contribution = sum(
            float(path["path_score"])
            / (int(PARAMETERS["rrf_k"]) + int(dict(path["candidate_evidence"])["rank"]))
            for path in retained_paths
        )
        traces.append(
            {
                "candidate_job_id": job_id,
                "baseline_rank": baseline_ranks.get(job_id),
                "graph_rank": graph_ranks[job_id],
                "graph_evidence_score": graph_scores[job_id],
                "fusion_score": scores[job_id],
                "path_count_total": graph_path_counts[job_id],
                "path_count_retained": len(retained_paths),
                "path_count_omitted": graph_path_counts[job_id] - len(retained_paths),
                "retained_graph_evidence_score": retained_contribution,
                "omitted_graph_evidence_score": graph_scores[job_id] - retained_contribution,
                "paths": retained_paths,
            }
        )
    return (
        ranked,
        traces,
        {
            "baseline_rows": len(baseline),
            "graph_covered_seed_rows": len(seed_rows),
            "anchors": len(selected_anchors),
            "query_exact_anchors": sum(
                anchor["source"] == "query_exact" for anchor in selected_anchors
            ),
            "duty_filter_anchors": sum(
                anchor["source"] == "duty_filter" for anchor in selected_anchors
            ),
            "consensus_anchors": sum(
                anchor["source"] == "baseline_consensus" for anchor in selected_anchors
            ),
            "graph_candidate_rows": len(graph_ranks),
            "novel_graph_candidate_rows": sum(
                job_id not in baseline_ranks for job_id in graph_ranks
            ),
            "bridge_terms": len(bridge_terms),
            "bridge_retrieval_rows": bridge_retrieval_rows,
            "graph_paths": graph_path_count,
        },
    )


def generate_graph_on(
    *,
    split_manifest_path: Path,
    graph_output: Path,
    evidence_path: Path,
    extraction_manifest_path: Path,
    graph_off_run: Path,
    graph_off_manifest: Path,
    queries_path: Path,
    jobs_csv: Path,
    tantivy_output: Path,
    artifact_prefix: str,
    output: Path,
) -> dict[str, Any]:
    if output.exists():
        raise RuntimeError("Graph challenger output already exists; generation never overwrites")
    validate_graph(
        graph_output,
        split_manifest_path,
        evidence_path=evidence_path,
        extraction_manifest_path=extraction_manifest_path,
    )
    split_sha256 = sha256_file(split_manifest_path)
    graph_sha256 = sha256_file(graph_output / "manifest.json")
    non_graph_inputs, manifest_qids, zero_result_qids = _graph_off_inputs(
        graph_off_manifest, graph_off_run, split_sha256
    )
    baseline = read_trec_run(graph_off_run)
    queries = read_canonical_queries(queries_path, split_manifest_path)
    canonical_qids = tuple(record.qid for record in queries)
    expected_result_qids = tuple(qid for qid in canonical_qids if qid not in zero_result_qids)
    if (
        manifest_qids != canonical_qids
        or tuple(baseline) != expected_result_qids
        or set(zero_result_qids) != set(canonical_qids).difference(baseline)
    ):
        raise RuntimeError("canonical query and graph_off qid coverage differ")
    indexes = _graph_indexes(graph_output)
    build_root = output.with_name(f".{output.name}.{os.getpid()}.partial")
    if build_root.exists():
        raise RuntimeError(f"partial Graph challenger output already exists: {build_root}")
    build_root.mkdir(parents=True)
    run_path = build_root / "graph-on.run"
    trace_path = build_root / "graph-traces.jsonl"
    bridge_retriever: TemporalBridgeRetriever | None = None
    try:
        bridge_retriever = TemporalBridgeRetriever(
            tantivy_output=tantivy_output,
            jobs_csv=jobs_csv,
            artifact_prefix=artifact_prefix,
            non_graph_inputs=non_graph_inputs,
            queries_path=queries_path,
        )
        totals: Counter[str] = Counter()
        graph_zero_result_qids: list[str] = []
        with run_path.open("xb") as run_output, trace_path.open("xb") as trace_output:
            for query in queries:
                qid = query.qid
                ranked, traces, statistics = _query_candidates(
                    baseline.get(qid, []), indexes, query, bridge_retriever
                )
                if not ranked:
                    graph_zero_result_qids.append(qid)
                totals["query_count"] += 1
                for name in (
                    "baseline_rows",
                    "graph_covered_seed_rows",
                    "graph_candidate_rows",
                    "novel_graph_candidate_rows",
                    "bridge_terms",
                    "bridge_retrieval_rows",
                    "graph_paths",
                ):
                    totals[name] += statistics[name]
                for output_name, source_name in (
                    ("queries_with_any_anchors", "anchors"),
                    ("queries_with_query_exact_anchors", "query_exact_anchors"),
                    ("queries_with_duty_filter_anchors", "duty_filter_anchors"),
                    ("queries_with_consensus_anchors", "consensus_anchors"),
                    ("queries_with_graph_candidates", "graph_candidate_rows"),
                    ("queries_with_novel_graph_candidates", "novel_graph_candidate_rows"),
                ):
                    totals[output_name] += statistics[source_name] > 0
                for rank, (job_id, _score) in enumerate(ranked, start=1):
                    trec_score = len(ranked) - rank + 1
                    run_output.write(f"{qid} Q0 {job_id} {rank} {trec_score:.12f} {TAG}\n".encode())
                for trace in traces:
                    trace_output.write(
                        json.dumps(
                            {"qid": qid, **trace},
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ).encode()
                        + b"\n"
                    )
            for stream in (run_output, trace_output):
                stream.flush()
                os.fsync(stream.fileno())
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "complete": True,
            "variant": "graph_on",
            "split_manifest_sha256": split_sha256,
            "graph_manifest_sha256": graph_sha256,
            "canonical_qids": list(canonical_qids),
            "zero_result_qids": graph_zero_result_qids,
            "non_graph_inputs": non_graph_inputs,
            "run_sha256": sha256_file(run_path),
            "generation": {
                "algorithm": ALGORITHM,
                "graph_off_run_sha256": sha256_file(graph_off_run),
                "graph_off_manifest_sha256": sha256_file(graph_off_manifest),
                "trace_path": trace_path.name,
                "trace_sha256": sha256_file(trace_path),
                "parameters": PARAMETERS,
                "statistics": dict(totals),
            },
        }
        atomic_json(build_root / "graph-on.manifest.json", manifest)
        build_root.replace(output)
    except BaseException:
        shutil.rmtree(build_root, ignore_errors=True)
        raise
    finally:
        if bridge_retriever is not None:
            bridge_retriever.close()
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split-manifest", type=Path, required=True)
    parser.add_argument("--graph-output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--extraction-manifest", type=Path, required=True)
    parser.add_argument("--graph-off-run", type=Path, required=True)
    parser.add_argument("--graph-off-manifest", type=Path, required=True)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--jobs-csv", type=Path, required=True)
    parser.add_argument("--tantivy-output", type=Path, required=True)
    parser.add_argument("--artifact-prefix", default=DEFAULT_ARTIFACT_PREFIX)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = generate_graph_on(
        split_manifest_path=args.split_manifest,
        graph_output=args.graph_output,
        evidence_path=args.evidence,
        extraction_manifest_path=args.extraction_manifest,
        graph_off_run=args.graph_off_run,
        graph_off_manifest=args.graph_off_manifest,
        queries_path=args.queries,
        jobs_csv=args.jobs_csv,
        tantivy_output=args.tantivy_output,
        artifact_prefix=args.artifact_prefix,
        output=args.output,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
