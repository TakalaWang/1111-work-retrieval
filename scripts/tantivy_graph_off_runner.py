#!/usr/bin/env python3
"""Generate the Graph-off TREC baseline from canonical queries and temporal-v2 Tantivy."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from pipeline_contract import (
    atomic_json,
    canonical_json,
    exact_keys,
    read_json_object,
    require_sha256,
    sha256_file,
)
from skill_graph_pipeline import _timestamp, load_split_manifest
from tantivy_index_pipeline import DEFAULT_ARTIFACT_PREFIX, _local, validate_tantivy
from work_retrieval_core.adapters import (
    CorpusQueryCompiler,
    FilterTaxonomy,
    TantivyBm25Retriever,
)
from work_retrieval_core.engine import MAX_AGE_DAYS, CandidateRequest

QUERY_KEYS = {"qid", "query", "as_of", "location_codes", "duty_codes"}
SAFE_QID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
MAXIMUM_RESULTS = 1000
ALGORITHM = "temporal-v2-tantivy-bm25-graph-off-v1"
TAG = "tantivy-temporal-v2"


@dataclass(frozen=True, slots=True)
class CanonicalQuery:
    qid: str
    query: str
    as_of: datetime
    location_codes: tuple[str, ...]
    duty_codes: tuple[str, ...]


def _codes(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(code, str)
        or not code.isascii()
        or not code.isdecimal()
        or (len(code) > 1 and code.startswith("0"))
        for code in value
    ):
        raise RuntimeError(f"canonical query {name} must contain canonical ASCII decimal codes")
    parsed = cast(list[str], value)
    if parsed != sorted(set(parsed), key=lambda code: (len(code), code)):
        raise RuntimeError(f"canonical query {name} must be unique and numerically sorted")
    return tuple(parsed)


def read_canonical_queries(path: Path, split_manifest_path: Path) -> tuple[CanonicalQuery, ...]:
    split, _ = load_split_manifest(split_manifest_path)
    evaluation_start = _timestamp(split["evaluation_start_inclusive"], "evaluation start")
    evaluation_end = _timestamp(split["evaluation_end_exclusive"], "evaluation end")
    records: list[CanonicalQuery] = []
    seen: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise RuntimeError("canonical queries cannot be read as UTF-8 JSONL") from error
    for line_number, line in enumerate(lines, start=1):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"canonical query line {line_number} is not valid JSON") from error
        if not isinstance(raw, dict):
            raise RuntimeError(f"canonical query line {line_number} must be an object")
        exact_keys(raw, QUERY_KEYS, f"canonical query line {line_number}")
        qid = raw["qid"]
        query = raw["query"]
        if not isinstance(qid, str) or SAFE_QID.fullmatch(qid) is None or qid in seen:
            raise RuntimeError(f"canonical query line {line_number} has an invalid qid")
        if not isinstance(query, str) or not query.strip() or query != query.strip():
            raise RuntimeError(f"canonical query line {line_number} has an invalid query")
        as_of = _timestamp(raw["as_of"], f"canonical query line {line_number} as_of")
        if not evaluation_start <= as_of < evaluation_end:
            raise RuntimeError(
                f"canonical query line {line_number} as_of is outside the evaluation window"
            )
        seen.add(qid)
        records.append(
            CanonicalQuery(
                qid=qid,
                query=query,
                as_of=as_of,
                location_codes=_codes(raw["location_codes"], "location_codes"),
                duty_codes=_codes(raw["duty_codes"], "duty_codes"),
            )
        )
    if not records:
        raise RuntimeError("canonical queries are empty")
    return tuple(records)


def _compiler(
    manifest: dict[str, Any], tantivy_output: Path, artifact_prefix: str
) -> CorpusQueryCompiler:
    correction = manifest["query_corrections"]
    if correction == {"enabled": False}:
        return CorpusQueryCompiler.identity()
    if not isinstance(correction, dict):
        raise RuntimeError("Tantivy query correction mode must be an object")
    return CorpusQueryCompiler.from_promoted_paths(
        _local(tantivy_output, correction["artifact_path"], artifact_prefix),
        _local(tantivy_output, correction["promotion_attestation_path"], artifact_prefix),
    )


def generate_graph_off(
    *,
    split_manifest_path: Path,
    queries_path: Path,
    tantivy_output: Path,
    jobs_csv: Path,
    artifact_prefix: str,
    output: Path,
) -> dict[str, Any]:
    if output.exists():
        raise RuntimeError("Graph-off output already exists; generation never overwrites")
    validate_tantivy(tantivy_output, jobs_csv=jobs_csv, artifact_prefix=artifact_prefix)
    manifest = read_json_object(tantivy_output / "manifest.json", "Tantivy component")
    queries = read_canonical_queries(queries_path, split_manifest_path)
    job_ids_path = _local(tantivy_output, manifest["job_ids_path"], artifact_prefix)
    taxonomy_path = _local(tantivy_output, manifest["taxonomy_path"], artifact_prefix)
    index_directory = _local(tantivy_output, manifest["index_directory"], artifact_prefix)
    try:
        raw_job_ids = json.loads(job_ids_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Tantivy job IDs cannot be read") from error
    if not isinstance(raw_job_ids, list) or any(
        not isinstance(value, str) for value in raw_job_ids
    ):
        raise RuntimeError("Tantivy job IDs are malformed")
    retriever = TantivyBm25Retriever(
        index_directory,
        tuple(cast(list[str], raw_job_ids)),
        FilterTaxonomy.from_path(taxonomy_path),
    )
    compiler = _compiler(manifest, tantivy_output, artifact_prefix)
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f".{output.name}.{os.getpid()}.partial")
    if partial.exists():
        raise RuntimeError(f"partial Graph-off output already exists: {partial}")
    partial.mkdir()
    run_path = partial / "graph-off.run"
    try:
        zero_result_qids: list[str] = []
        with run_path.open("xb") as run_output:
            for record in queries:
                compiled = compiler.compile(record.query)
                candidates = retriever.retrieve(
                    CandidateRequest(
                        text=record.query,
                        location_codes=record.location_codes,
                        duty_codes=record.duty_codes,
                        as_of=record.as_of,
                        minimum_updated_at=record.as_of - timedelta(days=MAX_AGE_DAYS),
                        lexical_texts=compiled.lexical_texts,
                        query_rewrites=compiled.rewrites,
                    ),
                    limit=MAXIMUM_RESULTS,
                )
                if not candidates:
                    zero_result_qids.append(record.qid)
                for rank, candidate in enumerate(candidates, start=1):
                    score = len(candidates) - rank + 1
                    run_output.write(
                        f"{record.qid} Q0 {candidate.job_id} {rank} {score:.12f} {TAG}\n".encode()
                    )
            run_output.flush()
            os.fsync(run_output.fileno())
        retrieval_config = {
            "algorithm": ALGORITHM,
            "maximum_results": MAXIMUM_RESULTS,
            "filter_semantics": manifest["filter_semantics"],
            "temporal_filter_semantics": manifest["temporal_filter_semantics"],
            "lexical_policy_sha256": manifest["lexical_policy_sha256"],
            "query_corrections": manifest["query_corrections"],
            "trec_score_policy": "strict_rank_descending_integer_v1",
        }
        result: dict[str, Any] = {
            "schema_version": 1,
            "complete": True,
            "variant": "graph_off",
            "split_manifest_sha256": sha256_file(split_manifest_path),
            "graph_manifest_sha256": None,
            "canonical_qids": [record.qid for record in queries],
            "zero_result_qids": zero_result_qids,
            "non_graph_inputs": {
                "canonical_queries": sha256_file(queries_path),
                "jobs_csv": sha256_file(jobs_csv),
                "tantivy_component_manifest": sha256_file(tantivy_output / "manifest.json"),
                "tantivy_index": require_sha256(manifest["index_sha256"], "Tantivy index"),
                "tantivy_job_ids": sha256_file(job_ids_path),
                "tantivy_filter_taxonomy": sha256_file(taxonomy_path),
                "retrieval_config": hashlib.sha256(canonical_json(retrieval_config)).hexdigest(),
            },
            "run_sha256": sha256_file(run_path),
        }
        atomic_json(partial / "graph-off.manifest.json", result)
        partial.replace(output)
        return result
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    finally:
        retriever.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split-manifest", type=Path, required=True)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--tantivy-output", type=Path, required=True)
    parser.add_argument("--jobs-csv", type=Path, required=True)
    parser.add_argument("--artifact-prefix", default=DEFAULT_ARTIFACT_PREFIX)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = generate_graph_off(
        split_manifest_path=args.split_manifest,
        queries_path=args.queries,
        tantivy_output=args.tantivy_output,
        jobs_csv=args.jobs_csv,
        artifact_prefix=args.artifact_prefix,
        output=args.output,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
