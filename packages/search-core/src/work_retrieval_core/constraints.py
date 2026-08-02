from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, cast

EducationLevel = Literal["高中職", "專科", "大學", "碩士", "博士"]
JobAttribute = Literal["全職", "兼職", "工讀"]
WorkShift = Literal["日班", "中班", "晚班", "假日班", "輪班"]

EDUCATION_LEVELS: tuple[EducationLevel, ...] = (
    "高中職",
    "專科",
    "大學",
    "碩士",
    "博士",
)
EDUCATION_ALIASES: dict[str, EducationLevel] = {
    "高中": "高中職",
    **{level: level for level in EDUCATION_LEVELS},
}
ACCEPTED_EDUCATION_VALUES = {*EDUCATION_LEVELS, "國小/國中", "不拘"}
JOB_ATTRIBUTES: tuple[JobAttribute, ...] = ("全職", "兼職", "工讀")
WORK_SHIFTS: tuple[WorkShift, ...] = ("日班", "中班", "晚班", "假日班", "輪班")
NO_EXPERIENCE_VALUES = ("不拘", "無工作經驗")
MANAGEMENT_REQUIRED_VALUES = (
    "需管理人數10人以下",
    "需管理人數11-20人",
    "需管理人數21-50人",
    "需管理人數51-100人",
    "需管理人數101人以上",
)
MANAGEMENT_REQUIRED_TOKEN = "required"
MONTHLY_PERIOD = "月薪"
MINIMUM_MONTHLY_SALARY = 10_000
MAXIMUM_MONTHLY_SALARY = 2_000_000

_LEVEL = "|".join(sorted(EDUCATION_ALIASES, key=len, reverse=True))
_EDUCATION_CUE = re.compile(_LEVEL)
_EXPLICIT_EDUCATION = (
    re.compile(rf"學歷(?:需求|要求|限制)?[:\uff1a]?({_LEVEL})"),
    re.compile(rf"({_LEVEL})(?:學歷|畢業)"),
    re.compile(rf"(?:需|須|要求)(?:具備|有)?({_LEVEL})(?:以上|以下|學歷|畢業)?"),
)
_JOB_ATTRIBUTE_ALIASES: dict[str, JobAttribute] = {
    "全職": "全職",
    "正職": "全職",
    "兼職": "兼職",
    "工讀": "工讀",
}
_JOB_ATTRIBUTE_CUE = re.compile("|".join(sorted(_JOB_ATTRIBUTE_ALIASES, key=len, reverse=True)))
_WORK_SHIFT_CUE = re.compile("|".join(sorted(WORK_SHIFTS, key=len, reverse=True)))
_NO_EXPERIENCE_CUE = re.compile(r"(?:無工作經驗|無經驗)")
_MANAGEMENT_CUE = re.compile(r"(?:管理人數|帶領團隊|帶人)")
_LOCAL_NEGATION_PREFIX = re.compile(
    r"(?:不(?:接受|考慮|想找|想要|需要|需|用|要做|要|想|找|能)?|"
    r"非|免|無需|無)$"
)
_LOCAL_NEGATION_SUFFIX = re.compile(r"^(?:不拘|勿擾)")
_NON_MONTHLY_PERIOD = re.compile(r"(?:時薪|日薪|年薪|年收)")
_MONTHLY_CUE = re.compile(r"(?:月薪|月入|月領)")
_STRICT_MINIMUM_PREFIX = re.compile(r"(?:最低|至少|不低於|起薪)")
_STRICT_MINIMUM_SUFFIX = re.compile(r"^(?:以上|起(?:薪)?)")
_RANGE_SEPARATOR = re.compile(r"(?:-|~|\uff5e|至|到|\uff0d)")
_ARABIC_WAN = re.compile(r"(?P<value>\d+(?:\.\d+)?)萬")
_CHINESE_WAN = re.compile(r"(?P<value>[零〇一二兩三四五六七八九十百]+)萬")
_K_AMOUNT = re.compile(r"(?P<value>\d+(?:\.\d+)?)k\b", re.IGNORECASE)
_RAW_AMOUNT = re.compile(r"(?<!\d)(?P<value>\d{4,7})(?!\d)")


@dataclass(frozen=True, slots=True)
class EducationConstraint:
    degree: EducationLevel

    def __post_init__(self) -> None:
        if self.degree not in EDUCATION_LEVELS:
            raise ValueError("degree is outside the supported education vocabulary")

    def as_dict(self) -> dict[str, object]:
        return {
            "degree": self.degree,
            "policy": "accepted_set_contains_degree_or_不拘",
        }


@dataclass(frozen=True, slots=True)
class MonthlySalaryConstraint:
    minimum: int
    strict: bool
    confidence: Literal["medium"] = "medium"

    def __post_init__(self) -> None:
        if (
            isinstance(self.minimum, bool)
            or not MINIMUM_MONTHLY_SALARY <= self.minimum <= MAXIMUM_MONTHLY_SALARY
        ):
            raise ValueError("monthly salary minimum is outside the supported range")
        if not isinstance(self.strict, bool) or self.confidence != "medium":
            raise ValueError("monthly salary policy is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "minimum": self.minimum,
            "strict": self.strict,
            "confidence": self.confidence,
            "policy": (
                "advertised_lower_reaches_minimum"
                if self.strict
                else "positive_upper_else_lower_reaches_minimum"
            ),
        }


@dataclass(frozen=True, slots=True)
class JobAttributeConstraint:
    value: JobAttribute

    def __post_init__(self) -> None:
        if self.value not in JOB_ATTRIBUTES:
            raise ValueError("job attribute is outside the supported vocabulary")

    def as_dict(self) -> dict[str, object]:
        return {"value": self.value, "policy": "exact_typed_job_attribute"}


@dataclass(frozen=True, slots=True)
class WorkShiftConstraint:
    value: WorkShift

    def __post_init__(self) -> None:
        if self.value not in WORK_SHIFTS:
            raise ValueError("work shift is outside the supported vocabulary")

    def as_dict(self) -> dict[str, object]:
        return {"value": self.value, "policy": "typed_shift_set_contains_value"}


@dataclass(frozen=True, slots=True)
class NoExperienceConstraint:
    def as_dict(self) -> dict[str, object]:
        return {
            "value": "no_experience_required",
            "policy": "experience_is_不拘_or_無工作經驗",
        }


@dataclass(frozen=True, slots=True)
class ManagementConstraint:
    def as_dict(self) -> dict[str, object]:
        return {"value": "management_required", "policy": "typed_management_count_required"}


@dataclass(frozen=True, slots=True)
class QueryConstraints:
    education: EducationConstraint | None = None
    monthly_salary: MonthlySalaryConstraint | None = None
    job_attribute: JobAttributeConstraint | None = None
    work_shift: WorkShiftConstraint | None = None
    no_experience: NoExperienceConstraint | None = None
    management: ManagementConstraint | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "education": self.education.as_dict() if self.education is not None else None,
            "monthly_salary": (
                self.monthly_salary.as_dict() if self.monthly_salary is not None else None
            ),
            "job_attribute": (
                self.job_attribute.as_dict() if self.job_attribute is not None else None
            ),
            "work_shift": self.work_shift.as_dict() if self.work_shift is not None else None,
            "no_experience": (
                self.no_experience.as_dict() if self.no_experience is not None else None
            ),
            "management": self.management.as_dict() if self.management is not None else None,
        }

    def requested(self) -> bool:
        return any(
            constraint is not None
            for constraint in (
                self.education,
                self.monthly_salary,
                self.job_attribute,
                self.work_shift,
                self.no_experience,
                self.management,
            )
        )


def compile_constraints(text: str) -> QueryConstraints:
    normalized = _normalized(text)
    return QueryConstraints(
        education=_compile_education(normalized),
        monthly_salary=_compile_monthly_salary(normalized),
        job_attribute=_compile_job_attribute(normalized),
        work_shift=_compile_work_shift(normalized),
        no_experience=_compile_no_experience(normalized),
        management=_compile_management(normalized),
    )


def job_attribute_filter_value(value: str | None) -> JobAttribute | None:
    normalized = _normalized(value or "")
    return normalized if normalized in JOB_ATTRIBUTES else None


def job_attribute_allows(value: str | None, constraint: JobAttributeConstraint) -> bool:
    return job_attribute_filter_value(value) == constraint.value


def work_shift_filter_values(value: str | None) -> tuple[WorkShift, ...]:
    normalized = _normalized(value or "")
    values = tuple(
        cast(WorkShift, part.strip())
        for part in normalized.split(",")
        if part.strip() in WORK_SHIFTS
    )
    return tuple(dict.fromkeys(values))


def work_shift_allows(value: str | None, constraint: WorkShiftConstraint) -> bool:
    return constraint.value in work_shift_filter_values(value)


def no_experience_filter_value(value: str | None) -> str | None:
    normalized = _normalized(value or "")
    return normalized if normalized in NO_EXPERIENCE_VALUES else None


def no_experience_allows(value: str | None) -> bool:
    return no_experience_filter_value(value) is not None


def management_filter_value(value: str | None) -> str | None:
    return (
        MANAGEMENT_REQUIRED_TOKEN
        if _normalized(value or "") in MANAGEMENT_REQUIRED_VALUES
        else None
    )


def management_requirement_allows(value: str | None) -> bool:
    return management_filter_value(value) == MANAGEMENT_REQUIRED_TOKEN


def education_requirement_allows(
    requirement: str | None,
    degree: EducationLevel,
) -> bool:
    if degree not in EDUCATION_LEVELS:
        return False
    values = education_filter_values(requirement)
    return degree in values or "不拘" in values


def education_filter_values(requirement: str | None) -> tuple[str, ...]:
    if requirement is None:
        return ()
    normalized = unicodedata.normalize("NFKC", requirement).strip()
    values = tuple(part.strip() for part in normalized.split(","))
    if (
        not values
        or any(not value or value not in ACCEPTED_EDUCATION_VALUES for value in values)
        or len(set(values)) != len(values)
    ):
        return ()
    return values


def normalize_salary_bound(value: Decimal | int | str | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("salary bound must be a non-negative integral amount")
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFKC", value).strip()
        if not normalized or normalized.casefold() == "null":
            return None
        try:
            numeric = Decimal(normalized)
        except InvalidOperation as error:
            raise ValueError("salary bound must be a non-negative integral amount") from error
    elif isinstance(value, Decimal):
        numeric = value
    elif isinstance(value, int):
        numeric = Decimal(value)
    else:
        raise ValueError("salary bound must be a non-negative integral amount")
    integral = numeric.to_integral_value()
    if numeric != integral or not 0 <= integral <= 100_000_000:
        raise ValueError("salary bound must be a non-negative integral amount")
    return int(integral)


def monthly_salary_filter_values(
    period: str | None,
    lower: int | None,
    upper: int | None,
) -> tuple[int | None, int | None]:
    if period != MONTHLY_PERIOD or not _valid_salary_bound(lower) or not _valid_salary_bound(upper):
        return None, None
    positive_lower = lower if lower is not None and lower > 0 else None
    positive_upper = upper if upper is not None and upper > 0 else None
    return positive_lower, positive_upper if positive_upper is not None else positive_lower


def monthly_salary_allows(
    period: str | None,
    lower: int | None,
    upper: int | None,
    constraint: MonthlySalaryConstraint,
) -> bool:
    positive_lower, maximum_possible = monthly_salary_filter_values(period, lower, upper)
    if constraint.strict:
        return positive_lower is not None and positive_lower >= constraint.minimum
    return maximum_possible is not None and maximum_possible >= constraint.minimum


def salary_period(salary_text: str | None) -> str | None:
    if salary_text is None:
        return None
    normalized = unicodedata.normalize("NFKC", salary_text).strip()
    if not normalized:
        return None
    return normalized.split("‧", 1)[0].strip() or None


def _compile_education(normalized: str) -> EducationConstraint | None:
    compact = re.sub(r"\s+", "", normalized)
    if any(
        _locally_negated(compact, match.start(), match.end())
        for match in _EDUCATION_CUE.finditer(compact)
    ):
        return None
    exact = EDUCATION_ALIASES.get(compact)
    if exact is not None:
        return EducationConstraint(exact)
    candidates = {
        EDUCATION_ALIASES[match.group(1)]
        for pattern in _EXPLICIT_EDUCATION
        for match in pattern.finditer(compact)
    }
    candidates.update(
        EDUCATION_ALIASES[segment]
        for segment in re.split(r"[\s+]+", normalized)
        if segment in EDUCATION_ALIASES
    )
    if len(candidates) != 1:
        return None
    return EducationConstraint(candidates.pop())


def _compile_job_attribute(normalized: str) -> JobAttributeConstraint | None:
    matches = tuple(_JOB_ATTRIBUTE_CUE.finditer(normalized))
    if any(_locally_negated(normalized, match.start(), match.end()) for match in matches):
        return None
    candidates = {_JOB_ATTRIBUTE_ALIASES[match.group(0)] for match in matches}
    if len(candidates) != 1:
        return None
    return JobAttributeConstraint(candidates.pop())


def _compile_work_shift(normalized: str) -> WorkShiftConstraint | None:
    matches = tuple(_WORK_SHIFT_CUE.finditer(normalized))
    if any(_locally_negated(normalized, match.start(), match.end()) for match in matches):
        return None
    candidates = {cast(WorkShift, match.group(0)) for match in matches}
    if len(candidates) != 1:
        return None
    return WorkShiftConstraint(candidates.pop())


def _compile_no_experience(normalized: str) -> NoExperienceConstraint | None:
    matches = tuple(_NO_EXPERIENCE_CUE.finditer(normalized))
    if not matches or any(
        _locally_negated(normalized, match.start(), match.end()) for match in matches
    ):
        return None
    return NoExperienceConstraint()


def _compile_management(normalized: str) -> ManagementConstraint | None:
    matches = tuple(_MANAGEMENT_CUE.finditer(normalized))
    if not matches or any(
        _locally_negated(normalized, match.start(), match.end()) for match in matches
    ):
        return None
    return ManagementConstraint()


def _locally_negated(text: str, cue_start: int, cue_end: int | None = None) -> bool:
    compact = re.sub(r"[\s,\uff0c\u3001;\uff1b:\uff1a]", "", text)
    removed_before = len(text[:cue_start]) - len(
        re.sub(r"[\s,\uff0c\u3001;\uff1b:\uff1a]", "", text[:cue_start])
    )
    compact_start = cue_start - removed_before
    raw_end = cue_end if cue_end is not None else cue_start
    removed_through_end = len(text[:raw_end]) - len(
        re.sub(r"[\s,\uff0c\u3001;\uff1b:\uff1a]", "", text[:raw_end])
    )
    compact_end = raw_end - removed_through_end
    prefix = compact[max(0, compact_start - 8) : compact_start]
    suffix = compact[compact_end : compact_end + 4]
    return (
        _LOCAL_NEGATION_PREFIX.search(prefix) is not None
        or _LOCAL_NEGATION_SUFFIX.match(suffix) is not None
    )


def _compile_monthly_salary(normalized: str) -> MonthlySalaryConstraint | None:
    compact = re.sub(r"[\s,\uff0c$\uff04]", "", normalized).casefold()
    if _NON_MONTHLY_PERIOD.search(compact):
        return None
    cue = _MONTHLY_CUE.search(compact)
    if cue is None:
        return None
    fragment = compact[cue.end() : cue.end() + 20]
    amount_match: re.Match[str] | None = None
    multiplier = 1
    parser: Literal["arabic", "chinese"] = "arabic"
    for pattern, candidate_multiplier, candidate_parser in (
        (_ARABIC_WAN, 10_000, "arabic"),
        (_CHINESE_WAN, 10_000, "chinese"),
        (_K_AMOUNT, 1_000, "arabic"),
        (_RAW_AMOUNT, 1, "arabic"),
    ):
        candidate = pattern.search(fragment)
        if candidate is not None and (
            amount_match is None or candidate.start() < amount_match.start()
        ):
            amount_match = candidate
            multiplier = candidate_multiplier
            parser = cast(Literal["arabic", "chinese"], candidate_parser)
    if amount_match is None:
        return None
    trailing = fragment[amount_match.end() :]
    if _RANGE_SEPARATOR.match(trailing):
        return None
    raw_value = amount_match.group("value")
    numeric: Decimal | int | None
    if parser == "chinese":
        numeric = _chinese_number(raw_value)
    else:
        try:
            numeric = Decimal(raw_value)
        except InvalidOperation:
            return None
    if numeric is None:
        return None
    amount = Decimal(numeric) * multiplier
    integral = amount.to_integral_value()
    if amount != integral:
        return None
    minimum = int(integral)
    if not MINIMUM_MONTHLY_SALARY <= minimum <= MAXIMUM_MONTHLY_SALARY:
        return None
    strict = (
        _STRICT_MINIMUM_PREFIX.search(fragment[: amount_match.end()]) is not None
        or _STRICT_MINIMUM_SUFFIX.match(trailing) is not None
    )
    return MonthlySalaryConstraint(minimum, strict=strict)


def _chinese_number(value: str) -> int | None:
    digits = {
        "零": 0,
        "\u3007": 0,
        "一": 1,
        "二": 2,
        "兩": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if value in digits:
        return digits[value]
    if value in {"十", "一十"}:
        return 10
    if "百" in value:
        hundreds, remainder = value.split("百", 1)
        result = digits.get(hundreds, 1) * 100
        if remainder.startswith("十"):
            return result + 10 + digits.get(remainder[1:], 0)
        return result + digits.get(remainder, 0)
    if "十" in value:
        tens, ones = value.split("十", 1)
        return digits.get(tens, 1) * 10 + digits.get(ones, 0)
    return None


def _valid_salary_bound(value: int | None) -> bool:
    return value is None or (
        not isinstance(value, bool) and isinstance(value, int) and 0 <= value <= 100_000_000
    )


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()
