from __future__ import annotations

import hashlib
import json
from pathlib import Path

GRAPH_SERVING_ALGORITHM = "graph-conditioned-temporal-bridge-retrieval-protected-rrf-v3"
SEED_SCAN_DEPTH = 1000
MAXIMUM_GRAPH_SEED_JOBS = 10
MINIMUM_ANCHOR_SEED_JOBS = 2
MAXIMUM_ANCHORS = 8
MAXIMUM_DUTY_ANCHORS = 4
MINIMUM_DUTY_ANCHOR_SUPPORT = 2
MAXIMUM_EDGES_PER_ANCHOR = 10
MAXIMUM_BRIDGE_TERMS = 16
MAXIMUM_JOBS_PER_BRIDGE_TERM = 50
ALLOWED_RELATION_TYPES = ["RELATED_TO", "SPECIALIZATION_OF", "USED_WITH"]
RRF_K = 60
PROTECTED_BASELINE_HEAD = 3
MAXIMUM_NOVEL_TOP_10 = 2
GRAPH_SERVING_POLICY: dict[str, object] = {
    "seed_scan_depth": SEED_SCAN_DEPTH,
    "maximum_graph_seed_jobs": MAXIMUM_GRAPH_SEED_JOBS,
    "minimum_anchor_seed_jobs": MINIMUM_ANCHOR_SEED_JOBS,
    "maximum_anchors": MAXIMUM_ANCHORS,
    "maximum_duty_anchors": MAXIMUM_DUTY_ANCHORS,
    "minimum_duty_anchor_support": MINIMUM_DUTY_ANCHOR_SUPPORT,
    "maximum_edges_per_anchor": MAXIMUM_EDGES_PER_ANCHOR,
    "maximum_bridge_terms": MAXIMUM_BRIDGE_TERMS,
    "maximum_jobs_per_bridge_term": MAXIMUM_JOBS_PER_BRIDGE_TERM,
    "allowed_relation_types": ALLOWED_RELATION_TYPES,
    "maximum_hops": 1,
    "rrf_k": RRF_K,
    "baseline_weight": 1.0,
    "graph_weight": 1.0,
    "protected_baseline_head": PROTECTED_BASELINE_HEAD,
    "maximum_novel_top_10": MAXIMUM_NOVEL_TOP_10,
    "novel_top_10_policy": "maximum_two_replacements_then_fill_missing_baseline_slots_v1",
}
GRAPH_SERVING_POLICY_SHA256 = hashlib.sha256(
    json.dumps(GRAPH_SERVING_POLICY, separators=(",", ":"), sort_keys=True).encode()
).hexdigest()


def _implementation_sha256() -> str:
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for name in ("graph.py", "graph_policy.py", "serialization.py"):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update((root / name).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


GRAPH_SERVING_IMPLEMENTATION_SHA256 = _implementation_sha256()
