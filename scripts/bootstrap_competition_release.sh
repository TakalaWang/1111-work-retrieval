#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 || $# -gt 4 || $3 != DEPLOY ]]; then
  echo "usage: $0 COMPETITION_ZIP NEW_WORK_ROOT DEPLOY [ALARM_EMAIL]" >&2
  exit 64
fi

competition_zip=$1
work_root=$2
alarm_email=${4:-}
source_manifest_sha=f762cc4d676e16aa04789e1573713ef30d66e72f3a7f96c5bcd7e7e6133a2adb
source_root="s3://jobbank-data-bucket/one111-search/runtime/$source_manifest_sha"

[[ -f $competition_zip ]] || { echo "competition ZIP is missing" >&2; exit 66; }
[[ ! -e $work_root ]] || { echo "work root must not exist" >&2; exit 73; }
[[ -z $alarm_email || $alarm_email =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || {
  echo "alarm email is invalid" >&2
  exit 64
}
[[ $(git branch --show-current) == main ]] || { echo "deployment must run from main" >&2; exit 69; }
[[ -z $(git status --porcelain) ]] || { echo "deployment requires a clean worktree" >&2; exit 69; }
git fetch --quiet origin main
[[ $(git rev-parse HEAD) == $(git rev-parse origin/main) ]] || { echo "main must equal origin/main" >&2; exit 69; }
expected_commit=$(git rev-parse HEAD)
[[ $(aws sts get-caller-identity --profile competition --region us-west-2 --query Account --output text) == 378849533305 ]] || {
  echo "AWS competition account differs" >&2
  exit 77
}
uv sync --frozen --all-packages

mkdir -p "$work_root"
uv run --all-packages python scripts/prepare_competition_zip.py \
  "$competition_zip" \
  --output "$work_root/dataset"

aws s3 cp \
  "$source_root/manifest.json" \
  "$work_root/source-inventory.json" \
  --profile competition \
  --region us-west-2 \
  --only-show-errors
test "$(uv run python -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$work_root/source-inventory.json")" = "$source_manifest_sha"

aws s3 sync \
  "$source_root/artifacts/experiments/qwen3-8b/full/" \
  "$work_root/whole-qwen/" \
  --profile competition \
  --region us-west-2 \
  --only-show-errors

uv run --all-packages python scripts/import_jobs_to_aws.py \
  "$work_root/dataset/職缺.csv"
uv run --all-packages python scripts/tantivy_index_pipeline.py build \
  --jobs-csv "$work_root/dataset/職缺.csv" \
  --location-taxonomy-csv "$work_root/dataset/城市對照表.csv" \
  --duty-taxonomy-csv "$work_root/dataset/職務對照表.csv" \
  --output "$work_root/tantivy"

component_sha=$(uv run python -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$work_root/tantivy/manifest.json")
build_sha=$(uv run python -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$work_root/tantivy/build-manifest.json")
index_sha=$(uv run python -c 'import json,sys; print(json.load(open(sys.argv[1]))["index_sha256"])' "$work_root/tantivy/manifest.json")

scripts/reproduce_runtime_release.sh \
  "$work_root/whole-qwen" \
  "$work_root/source-inventory.json" \
  "$work_root/tantivy" \
  "$work_root/runtime" \
  "$component_sha" \
  "$build_sha" \
  "$index_sha"

promotion_result=$(uv run --all-packages python scripts/promote_runtime_artifacts.py \
  --release-spec "$work_root/runtime/runtime-release-spec.json" \
  --source-manifest-file "$work_root/runtime/manifest.json" \
  --source-root "$work_root/runtime" \
  --approved-tantivy-build-sha256 "$build_sha" \
  --approved-tantivy-index-sha256 "$index_sha" \
  --stage-source \
  --execute)
printf '%s\n' "$promotion_result"
runtime_sha=$(uv run python -c 'import json,sys; print(json.loads(sys.argv[1])["manifest_sha256"])' "$promotion_result")

git fetch --quiet origin main
[[ -z $(git status --porcelain) ]] || { echo "deployment requires a clean worktree" >&2; exit 69; }
[[ $(git rev-parse HEAD) == "$expected_commit" && $(git rev-parse origin/main) == "$expected_commit" ]] || {
  echo "main changed while release artifacts were being built; restart from the new main" >&2
  exit 69
}
deployment_id=$(uv run python -c 'import uuid; print(uuid.uuid4())')
run_title="Deploy $runtime_sha $deployment_id"
before_run_ids=" $(gh run list \
  --workflow deploy.yml \
  --branch main \
  --event workflow_dispatch \
  --limit 100 \
  --json databaseId,headSha,displayTitle \
  --jq ".[] | select(.headSha == \"$expected_commit\" and .displayTitle == \"$run_title\") | .databaseId" \
  | tr '\n' ' ') "
gh workflow run deploy.yml \
  --ref main \
  -f confirm=DEPLOY \
  -f artifact_manifest_sha="$runtime_sha" \
  -f alarm_email="$alarm_email" \
  -f deployment_id="$deployment_id" \
  -f compute_profile=cpu-incumbent \
  -f gpu_instance_type=g5.xlarge

run_id=''
for _ in {1..30}; do
  candidates=$(gh run list \
    --workflow deploy.yml \
    --branch main \
    --event workflow_dispatch \
    --limit 100 \
    --json databaseId,headSha,displayTitle \
    --jq ".[] | select(.headSha == \"$expected_commit\" and .displayTitle == \"$run_title\") | .databaseId")
  while IFS= read -r candidate; do
    [[ $candidate =~ ^[0-9]+$ ]] || continue
    if [[ $before_run_ids != *" $candidate "* ]]; then
      run_id=$candidate
      break
    fi
  done <<<"$candidates"
  [[ -z $run_id ]] || break
  sleep 2
done
[[ $run_id =~ ^[0-9]+$ ]] || { echo "deployed workflow run was not found" >&2; exit 70; }
gh run watch "$run_id" --exit-status

printf 'runtime_manifest_sha=%s\ndeploy_run_id=%s\n' "$runtime_sha" "$run_id"
