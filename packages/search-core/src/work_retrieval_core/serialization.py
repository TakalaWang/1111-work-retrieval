from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from collections.abc import Mapping

DOCUMENT_POLICY_VERSION = "2026-08-01-full-jd-v2"
FULL_JOB_FIELDS = (
    ("職務名稱", "title"),
    ("職務小類", "duty_minor"),
    ("職務中類", "duty_middle"),
    ("職務大類", "duty_major"),
    ("薪資", "salary_text"),
    ("職缺屬性", "job_attribute"),
    ("工時", "work_hours"),
    ("工時說明", "work_hours_description"),
    ("電腦技能資料", "computer_skills"),
    ("工作技能", "work_skills"),
    ("專業證照", "professional_certifications"),
    ("工作經驗需求", "experience_requirement"),
    ("學歷需求", "education_requirement"),
    ("科系需求1", "major_requirement_1"),
    ("科系需求2", "major_requirement_2"),
    ("科系需求3", "major_requirement_3"),
    ("語言能力一", "language_1"),
    ("語言能力一聽", "language_1_listening"),
    ("語言能力一說", "language_1_speaking"),
    ("語言能力一讀", "language_1_reading"),
    ("語言能力一寫", "language_1_writing"),
    ("語言能力二", "language_2"),
    ("語言能力二聽", "language_2_listening"),
    ("語言能力二說", "language_2_speaking"),
    ("語言能力二讀", "language_2_reading"),
    ("語言能力二寫", "language_2_writing"),
    ("管理人數", "management_count"),
    ("是否需外派", "requires_travel"),
    ("工作城市", "work_city"),
    ("產業小類", "industry_minor"),
    ("產業中類", "industry_middle"),
    ("產業大類", "industry_major"),
    ("附加條件", "additional_conditions"),
    ("職務內容", "description"),
)
HTML_TAG = re.compile(r"<[^>]*>")
URL = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060\ufeff]")


def canonical_text(value: str | None) -> str:
    text = value or ""
    if text.strip().casefold() == "null":
        return ""
    text = html.unescape(text)
    text = HTML_TAG.sub(" ", text)
    text = ZERO_WIDTH.sub("", text)
    text = URL.sub(" ", text)
    return " ".join(text.split())


def canonical_code(value: str | None) -> str:
    return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())


def serialize_full_job(values: Mapping[str, str | None]) -> str:
    missing = [field for _, field in FULL_JOB_FIELDS if field not in values]
    if missing:
        raise ValueError(f"full-job serializer is missing fields: {missing}")
    lines: list[str] = []
    seen: set[str] = set()
    for label, field in FULL_JOB_FIELDS:
        value = canonical_text(values[field])
        identity = canonical_code(value)
        if not identity or identity in seen:
            continue
        seen.add(identity)
        lines.append(f"{label}: {value}")
    return "\n".join(lines)


def document_template_sha256() -> str:
    template = (
        DOCUMENT_POLICY_VERSION
        + "\n"
        + "\n".join(f"{label}: {{{label}}}" for label, _ in FULL_JOB_FIELDS)
    )
    return hashlib.sha256(template.encode()).hexdigest()
