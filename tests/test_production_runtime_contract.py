import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
PROMOTED_TEMPORAL_FILTER_SEMANTICS = (
    "updated_at >= as_of - 180 days before Top-K; updated_at > as_of retained with freshness=0"
)


def test_runtime_contract_matches_promoted_temporal_policy() -> None:
    schema = json.loads(
        (ROOT / "packages/contract/runtime-manifest.schema.json").read_text(encoding="utf-8")
    )

    assert (
        schema["$defs"]["temporalTantivy"]["properties"]["temporal_filter_semantics"]["const"]
        == PROMOTED_TEMPORAL_FILTER_SEMANTICS
    )
