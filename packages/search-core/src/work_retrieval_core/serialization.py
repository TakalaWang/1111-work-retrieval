from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from collections.abc import Mapping

DOCUMENT_POLICY_VERSION = "2026-07-24-clean-v1"
FULL_JOB_FIELDS = (
    ("職務名稱", "title"),
    ("職務小類", "duty_minor"),
    ("職務中類", "duty_middle"),
    ("職務大類", "duty_major"),
    ("電腦技能資料", "computer_skills"),
    ("工作技能", "work_skills"),
    ("專業證照", "professional_certifications"),
    ("工作經驗需求", "experience_requirement"),
    ("學歷需求", "education_requirement"),
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
