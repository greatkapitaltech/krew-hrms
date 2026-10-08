"""
merge_context.py

Builds the flat, explicit dict of merge fields available to a DocumentTemplate
of a given document_type. Deliberately flat primitives (not raw model
instances) so a template's `{{ field }}` can't walk into attributes/methods
the author never intended to expose - see docs/TECH_OVERVIEW.md for how
HorillaCompanyManager-backed querysets already scope these to the request's
company before we ever get here.
"""

from datetime import date

from document_templates.models import DocumentType


def _fmt(value):
    if value is None:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%d %b %Y")
    return str(value)


def build_offer_letter_context(candidate) -> dict:
    recruitment = getattr(candidate, "recruitment_id", None)
    job_position = getattr(candidate, "job_position_id", None)
    company = getattr(recruitment, "company_id", None)
    return {
        "candidate_name": candidate.name or "",
        "candidate_email": candidate.email or "",
        "candidate_mobile": candidate.mobile or "",
        "candidate_address": candidate.address or "",
        "job_position": str(job_position) if job_position else "",
        "joining_date": _fmt(candidate.joining_date),
        "company_name": getattr(company, "company", ""),
        "today": _fmt(date.today()),
    }


def build_relieving_letter_context(employee) -> dict:
    work_info = getattr(employee, "employee_work_info", None)
    company = getattr(work_info, "company_id", None)
    job_position = getattr(work_info, "job_position_id", None)
    return {
        "employee_name": employee.get_full_name(),
        "employee_email": getattr(employee, "email", ""),
        "job_position": str(job_position) if job_position else "",
        "date_joining": _fmt(getattr(work_info, "date_joining", None)),
        "company_name": getattr(company, "company", ""),
        "today": _fmt(date.today()),
    }


def build_experience_letter_context(employee) -> dict:
    # Same base facts as a relieving letter; kept separate so each
    # document_type can grow its own fields independently later.
    return build_relieving_letter_context(employee)


def build_payslip_context(payslip) -> dict:
    # pay_head_data holds the computed allowances/deductions breakdown (see
    # payroll/filters.py's own reads of it), but the authoritative totals -
    # gross_pay, basic_pay, deduction, net_pay - live on the Payslip model's
    # own fields, not duplicated inside that JSON blob, so they're added
    # explicitly rather than assumed to already be in pay_head_data.
    data = dict(payslip.pay_head_data or {})
    data["employee_name"] = payslip.employee_id.get_full_name()
    data["today"] = _fmt(date.today())
    data["basic_pay"] = payslip.basic_pay
    data["gross_pay"] = payslip.gross_pay
    data["deduction"] = payslip.deduction
    data["net_pay"] = payslip.net_pay
    data["start_date"] = _fmt(payslip.start_date)
    data["end_date"] = _fmt(payslip.end_date)
    return data


BUILDERS = {
    DocumentType.OFFER_LETTER: build_offer_letter_context,
    DocumentType.RELIEVING_LETTER: build_relieving_letter_context,
    DocumentType.EXPERIENCE_LETTER: build_experience_letter_context,
    DocumentType.PAYSLIP: build_payslip_context,
}

SAMPLE_CONTEXT = {
    "candidate_name": "Jordan Example",
    "candidate_email": "jordan@example.com",
    "candidate_mobile": "+1 555 0100",
    "candidate_address": "123 Example Street",
    "employee_name": "Jordan Example",
    "employee_email": "jordan@example.com",
    "job_position": "Software Engineer",
    "joining_date": "01 Jan 2026",
    "date_joining": "01 Jan 2026",
    "company_name": "Example Company",
    "today": _fmt(date.today()),
}


def build_context(document_type: str, target) -> dict:
    """
    target: a Candidate for OFFER_LETTER, an Employee for RELIEVING_LETTER /
    EXPERIENCE_LETTER, a Payslip for PAYSLIP. CUSTOM has no builder - it
    renders with an empty context (static content only).
    """
    builder = BUILDERS.get(document_type)
    if builder is None or target is None:
        return {}
    return builder(target)
