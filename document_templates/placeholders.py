"""
placeholders.py

Scans a template's HTML content for {{ field }}-style merge-field
placeholders and checks them against the recognized field set for that
document_type, so an uploaded .docx (or a hand-typed CKEditor template) that
references a field the system doesn't actually know how to fill gets flagged
at save time instead of silently rendering blank at generation time.

RECOGNIZED_FIELDS is hand-derived from the guaranteed keys each
merge_context.py builder returns - PAYSLIP's pay_head_data-derived
allowances/deductions are deliberately excluded: they're lists consumed by
a template's {% for %} loop, not {{ field }} placeholders, so they're out of
scope for this check.
"""

import re

from .models import DocumentType

RECOGNIZED_FIELDS = {
    DocumentType.OFFER_LETTER: {
        "candidate_name",
        "candidate_email",
        "candidate_mobile",
        "candidate_address",
        "job_position",
        "joining_date",
        "company_name",
        "today",
    },
    DocumentType.RELIEVING_LETTER: {
        "employee_name",
        "employee_email",
        "job_position",
        "date_joining",
        "company_name",
        "today",
    },
    DocumentType.EXPERIENCE_LETTER: {
        "employee_name",
        "employee_email",
        "job_position",
        "date_joining",
        "company_name",
        "today",
    },
    DocumentType.PAYSLIP: {
        "employee_name",
        "today",
        "basic_pay",
        "gross_pay",
        "deduction",
        "net_pay",
        "start_date",
        "end_date",
    },
    # CUSTOM has no merge_context builder at all (see merge_context.py's
    # build_context) - any {{ field }} typed into a CUSTOM template will
    # always render blank, so every placeholder found is unmapped.
    DocumentType.CUSTOM: set(),
}

PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def extract_placeholders(content: str) -> set:
    """Every distinct {{ field }} name referenced in content."""
    if not content:
        return set()
    return set(PLACEHOLDER_RE.findall(content))


def unmapped_placeholders(document_type: str, content: str) -> list:
    """
    Placeholders in content that aren't recognized for this document_type,
    sorted for a stable, readable display order.
    """
    recognized = RECOGNIZED_FIELDS.get(document_type, set())
    found = extract_placeholders(content)
    return sorted(found - recognized)
