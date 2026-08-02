from __future__ import annotations

import csv
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import tantivy_index_pipeline as pipeline
from work_retrieval_core.adapters import (
    FilterTaxonomy,
    TantivyBm25Retriever,
    load_job_ids,
)
from work_retrieval_core.constraints import (
    EducationConstraint,
    JobAttributeConstraint,
    ManagementConstraint,
    MonthlySalaryConstraint,
    NoExperienceConstraint,
    QueryConstraints,
    WorkShiftConstraint,
)
from work_retrieval_core.engine import CandidateRequest
from work_retrieval_core.serialization import FULL_JOB_FIELDS


def test_official_csv_builds_with_taxonomies_and_numeric_filters(tmp_path: Path) -> None:
    source = tmp_path / "職缺.csv"
    fields = [
        *(label for label, _field in FULL_JOB_FIELDS),
        "職缺編號",
        "職缺最後修改時間",
        pipeline.SALARY_LOWER_SOURCE_FIELD,
        pipeline.SALARY_UPPER_SOURCE_FIELD,
    ]
    with source.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        row = dict.fromkeys(fields, "")
        row.update(
            {
                "職缺編號": "101",
                "職務名稱": "後端工程師",
                "職務大類": "資訊科技",
                "職務中類": "軟體工程",
                "職務小類": "後端工程師",
                "工作城市": "台北市",
                "薪資": "月薪",
                "薪資下限": "50000",
                "薪資上限": "70000",
                "學歷需求": "大學,碩士",
                "職缺屬性": "兼職",
                "工時": "晚班,輪班",
                "工作經驗需求": "不拘",
                "管理人數": "需管理人數10人以下",
                "職務內容": "使用 Python 建置 API",
                "職缺最後修改時間": "2026-06-07 10:00:00.000",
            }
        )
        writer.writerow(row)
        row.update(
            {
                "職缺編號": "102",
                "職務名稱": "未分類職缺",
                "職務大類": "NULL",
                "職務中類": "NULL",
                "職務小類": "NULL",
                "工作城市": "NULL",
                "職務內容": "待分類",
            }
        )
        writer.writerow(row)

    location_taxonomy = tmp_path / "城市對照表.csv"
    duty_taxonomy = tmp_path / "職務對照表.csv"
    for path, row in (
        (
            location_taxonomy,
            {"CodeNo": "100100", "CodeNameA": "台北市", "CodeNameB": "台北市", "CodeNameC": "台灣"},
        ),
        (
            duty_taxonomy,
            {
                "CodeNo": "140201",
                "CodeNameA": "後端工程師",
                "CodeNameB": "軟體工程",
                "CodeNameC": "資訊科技",
            },
        ),
    ):
        with path.open("w", encoding="utf-8", newline="") as target:
            writer = csv.DictWriter(
                target, fieldnames=("CodeNo", "CodeNameA", "CodeNameB", "CodeNameC")
            )
            writer.writeheader()
            writer.writerow(row)

    output = tmp_path / "tantivy"
    pipeline.build_tantivy(
        jobs_csv=source,
        output=output,
        artifact_prefix=pipeline.DEFAULT_ARTIFACT_PREFIX,
        location_code_field=None,
        location_term_field="工作城市",
        location_taxonomy_csv=location_taxonomy,
        duty_code_field=None,
        duty_term_field="職務小類",
        duty_taxonomy_csv=duty_taxonomy,
        visibility_field=None,
        modified_at_field="職缺最後修改時間",
        correction_candidate_path=None,
        correction_attestation_path=None,
    )

    taxonomy = FilterTaxonomy.from_path(output / "filter-taxonomy.json")
    retriever = TantivyBm25Retriever(
        output / "index", load_job_ids(output / "job-ids.json"), taxonomy
    )
    candidates = retriever.retrieve(
        CandidateRequest(
            text="Python",
            location_codes=("100100",),
            duty_codes=("140201",),
            as_of=datetime(2026, 6, 8, tzinfo=UTC),
            minimum_updated_at=datetime(2025, 12, 10, tzinfo=UTC),
            lexical_texts=("Python",),
            constraints=QueryConstraints(
                education=EducationConstraint("大學"),
                monthly_salary=MonthlySalaryConstraint(50_000, strict=True),
                job_attribute=JobAttributeConstraint("兼職"),
                work_shift=WorkShiftConstraint("晚班"),
                no_experience=NoExperienceConstraint(),
                management=ManagementConstraint(),
            ),
        ),
        limit=10,
    )

    assert [candidate.job_id for candidate in candidates] == ["101"]
    assert load_job_ids(output / "job-ids.json") == ("101", "102")
    assert taxonomy.location_code_to_terms["100100"] == ("台北市", "台灣")
    assert taxonomy.duty_code_to_terms["140201"] == ("後端工程師", "資訊科技", "軟體工程")
