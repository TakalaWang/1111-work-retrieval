#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 13 ]]; then
  echo "usage: $0 SPLIT GRAPH EVIDENCE EXTRACTION_MANIFEST QRELS CANONICAL_QUERIES JOBS_CSV TANTIVY_OUTPUT EVALUATOR EVALUATOR_ID EVALUATOR_KIND MIN_NDCG_DELTA OUTPUT_DIR" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${13}"
if [[ -e "$OUTPUT_DIR" ]]; then
  echo "output directory already exists: $OUTPUT_DIR" >&2
  exit 1
fi

uv run python "$SCRIPT_DIR/tantivy_graph_off_runner.py" \
  --split-manifest "$1" \
  --queries "$6" \
  --jobs-csv "$7" \
  --tantivy-output "$8" \
  --output "$OUTPUT_DIR/baseline"

uv run python "$SCRIPT_DIR/graph_candidate_runner.py" \
  --split-manifest "$1" \
  --graph-output "$2" \
  --evidence "$3" \
  --extraction-manifest "$4" \
  --graph-off-run "$OUTPUT_DIR/baseline/graph-off.run" \
  --graph-off-manifest "$OUTPUT_DIR/baseline/graph-off.manifest.json" \
  --queries "$6" \
  --jobs-csv "$7" \
  --tantivy-output "$8" \
  --output "$OUTPUT_DIR/generated"

uv run python "$SCRIPT_DIR/graph_ablation_runner.py" \
  --split-manifest "$1" \
  --graph-output "$2" \
  --evidence "$3" \
  --extraction-manifest "$4" \
  --qrels "$5" \
  --queries "$6" \
  --jobs-csv "$7" \
  --tantivy-output "$8" \
  --graph-off-run "$OUTPUT_DIR/baseline/graph-off.run" \
  --graph-off-manifest "$OUTPUT_DIR/baseline/graph-off.manifest.json" \
  --graph-on-run "$OUTPUT_DIR/generated/graph-on.run" \
  --graph-on-manifest "$OUTPUT_DIR/generated/graph-on.manifest.json" \
  --evaluator-command "$9" \
  --evaluator-id "${10}" \
  --evaluator-kind "${11}" \
  --minimum-ndcg-delta "${12}" \
  --output "$OUTPUT_DIR/graph-ablation.json"
