from __future__ import annotations

from decimal import Decimal

import pytest
from work_retrieval_core.constraints import (
    EducationConstraint,
    JobAttributeConstraint,
    ManagementConstraint,
    MonthlySalaryConstraint,
    NoExperienceConstraint,
    QueryConstraints,
    WorkShiftConstraint,
    compile_constraints,
    education_filter_values,
    education_requirement_allows,
    job_attribute_allows,
    management_requirement_allows,
    monthly_salary_allows,
    monthly_salary_filter_values,
    no_experience_allows,
    normalize_salary_bound,
    work_shift_allows,
)


@pytest.mark.parametrize(
    ("query", "degree"),
    [
        ("大學", "大學"),
        ("大學學歷", "大學"),
        ("學歷大學", "大學"),
        ("需大學", "大學"),
        ("碩士", "碩士"),
        ("後端工程師 學歷大學", "大學"),
        ("輔導員+大學", "大學"),
    ],
)
def test_compiler_accepts_only_explicit_education_intent(query: str, degree: str) -> None:
    assert compile_constraints(query).education == EducationConstraint(degree)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "query",
    [
        "台灣大學",
        "大學眼科",
        "大學生",
        "專科護理師",
        "博士後研究員",
        "輔導員+專科護理師",
        "月薪制營業助理",
    ],
)
def test_compiler_keeps_entity_and_job_title_terms_out_of_hard_constraints(
    query: str,
) -> None:
    assert compile_constraints(query) == QueryConstraints()


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("月薪五萬", MonthlySalaryConstraint(50_000, strict=False)),
        ("月薪50000以上", MonthlySalaryConstraint(50_000, strict=True)),
        ("月薪五萬以上", MonthlySalaryConstraint(50_000, strict=True)),
        ("月薪50000起", MonthlySalaryConstraint(50_000, strict=True)),
        ("月薪50000起薪", MonthlySalaryConstraint(50_000, strict=True)),
        ("月薪起薪50000", MonthlySalaryConstraint(50_000, strict=True)),
        ("月薪至少50000", MonthlySalaryConstraint(50_000, strict=True)),
        ("月薪最低五萬", MonthlySalaryConstraint(50_000, strict=True)),
        ("月薪50K", MonthlySalaryConstraint(50_000, strict=False)),
        ("月薪4.5萬", MonthlySalaryConstraint(45_000, strict=False)),
    ],
)
def test_compiler_parses_monthly_minimum_salary(
    query: str,
    expected: MonthlySalaryConstraint,
) -> None:
    assert compile_constraints(query).monthly_salary == expected


def test_compiler_preserves_both_constraints_in_a_mixed_occupation_query() -> None:
    assert compile_constraints("後端工程師 學歷大學 月薪五萬") == QueryConstraints(
        education=EducationConstraint("大學"),
        monthly_salary=MonthlySalaryConstraint(50_000, strict=False),
    )


def test_compiler_parses_only_unambiguous_typed_job_cues() -> None:
    assert compile_constraints("晚班兼職").job_attribute == JobAttributeConstraint("兼職")
    assert compile_constraints("晚班兼職").work_shift == WorkShiftConstraint("晚班")
    assert compile_constraints("正職人員").job_attribute == JobAttributeConstraint("全職")
    assert compile_constraints("無經驗可").no_experience == NoExperienceConstraint()
    assert compile_constraints("客服管理職").management is None
    assert compile_constraints("需管理人數的客服").management == ManagementConstraint()

    assert compile_constraints("全職或兼職").job_attribute is None
    assert compile_constraints("日班晚班").work_shift is None
    for query in ("不輪班", "免輪班", "無需輪班"):
        assert compile_constraints(query).work_shift is None
    for query in ("不接受管理職", "不需帶人", "不用帶人", "無需帶人"):
        assert compile_constraints(query).management is None
    assert compile_constraints("需外派").requested() is False
    assert compile_constraints("三年工作經驗").requested() is False


@pytest.mark.parametrize(
    "query",
    [
        "不要兼職",
        "非工讀",
        "不找晚班",
        "非管理職",
        "不接受無經驗",
        "不需帶人",
        "不用輪班",
        "非大學學歷",
        "管理人數不拘",
        "兼職勿擾",
    ],
)
def test_compiler_never_inverts_locally_negated_cues_into_positive_filters(
    query: str,
) -> None:
    assert compile_constraints(query) == QueryConstraints()


def test_typed_job_filter_values_share_the_revalidation_policy() -> None:
    assert job_attribute_allows("兼職", JobAttributeConstraint("兼職"))
    assert not job_attribute_allows("全職", JobAttributeConstraint("兼職"))
    assert work_shift_allows("日班,晚班,輪班", WorkShiftConstraint("晚班"))
    assert not work_shift_allows("日班", WorkShiftConstraint("晚班"))
    assert no_experience_allows("不拘")
    assert no_experience_allows("無工作經驗")
    assert not no_experience_allows("1年工作經驗")
    assert management_requirement_allows("需管理人數10人以下")
    assert not management_requirement_allows("無\uff0c不接受管理職")


@pytest.mark.parametrize("query", ["月薪500", "時薪50000", "年薪五萬", "月薪"])
def test_compiler_rejects_impossible_or_non_monthly_numeric_interpretations(
    query: str,
) -> None:
    assert compile_constraints(query).monthly_salary is None


@pytest.mark.parametrize(
    ("requirement", "degree", "allowed"),
    [
        ("專科,大學,碩士", "大學", True),
        ("不拘", "博士", True),
        ("碩士,博士", "大學", False),
        (None, "大學", False),
        ("NULL", "大學", False),
    ],
)
def test_education_filter_uses_the_jd_accepted_set(
    requirement: str | None,
    degree: str,
    allowed: bool,
) -> None:
    assert education_requirement_allows(requirement, degree) is allowed  # type: ignore[arg-type]


def test_education_index_values_share_the_revalidation_policy() -> None:
    assert education_filter_values("專科,大學,碩士") == ("專科", "大學", "碩士")
    assert education_filter_values("大學,大學") == ()
    assert education_filter_values("NULL") == ()


def test_salary_filter_separates_recall_gate_from_strict_guarantee() -> None:
    medium = MonthlySalaryConstraint(50_000, strict=False)
    strict = MonthlySalaryConstraint(50_000, strict=True)

    assert monthly_salary_allows("月薪", 40_000, 60_000, medium)
    assert not monthly_salary_allows("月薪", 40_000, 0, medium)
    assert not monthly_salary_allows("月薪", 40_000, 60_000, strict)
    assert monthly_salary_allows("月薪", 50_000, 0, strict)
    assert not monthly_salary_allows("時薪", 500, 600, medium)
    assert not monthly_salary_allows("月薪", None, None, medium)


def test_salary_index_values_share_the_revalidation_policy() -> None:
    assert normalize_salary_bound(Decimal("50000.00")) == 50_000
    assert normalize_salary_bound("NULL") is None
    assert monthly_salary_filter_values("月薪", 40_000, 60_000) == (40_000, 60_000)
    assert monthly_salary_filter_values("月薪", 40_000, 0) == (40_000, 40_000)
    assert monthly_salary_filter_values("時薪", 500, 600) == (None, None)
    with pytest.raises(ValueError, match="integral"):
        normalize_salary_bound("50000.50")
