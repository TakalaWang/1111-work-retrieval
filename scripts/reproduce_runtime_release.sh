#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 8 ]]; then
  echo "usage: $0 WHOLE_CACHE_ROOT WHOLE_SOURCE_INVENTORY TANTIVY_BUILD_ROOT OUTPUT_ROOT SOURCE_MANIFEST_KEY TANTIVY_COMPONENT_SHA256 TANTIVY_BUILD_SHA256 TANTIVY_INDEX_SHA256" >&2
  exit 64
fi

whole_cache_root=$1
whole_source_inventory=$2
tantivy_build_root=$3
output_root=$4
source_manifest_key=$5
tantivy_component_sha256=$6
tantivy_build_sha256=$7
tantivy_index_sha256=$8

uv run --all-packages python scripts/materialize_runtime_components.py \
  --whole-build-root "$whole_cache_root" \
  --whole-source-inventory "$whole_source_inventory" \
  --tantivy-build-root "$tantivy_build_root" \
  --output-root "$output_root" \
  --source-manifest-key "$source_manifest_key" \
  --approved-tantivy-component-sha256 "$tantivy_component_sha256" \
  --approved-tantivy-build-sha256 "$tantivy_build_sha256" \
  --approved-tantivy-index-sha256 "$tantivy_index_sha256"

uv run --all-packages python scripts/promote_runtime_artifacts.py \
  --release-spec "$output_root/runtime-release-spec.json" \
  --source-manifest-file "$output_root/manifest.json" \
  --source-root "$output_root" \
  --approved-tantivy-build-sha256 "$tantivy_build_sha256" \
  --approved-tantivy-index-sha256 "$tantivy_index_sha256"
