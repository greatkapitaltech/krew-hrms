"""
company_onboarding/wizard_utils.py

Shared helpers for the Company Setup wizard's step views.

Repeatable rows (Place of Work states, POC Contacts, Signatories) use
Django's built-in ModelForm `prefix=` support to get correct name/id
rendering and clean validation/cleaned_data for free — each row is
`RowForm(data, prefix=f"{prefix}{index}", instance=...)`. Since we're
deliberately not using a Django formset (no management-form bookkeeping),
the number of rows submitted has to be discovered by scanning POST keys
for the prefix pattern, rather than read off a TOTAL_FORMS field.
"""

import re


def discover_row_indices(post_data, prefix, anchor_field):
    """
    Scans POST keys for `{prefix}{index}-{anchor_field}` and returns the
    sorted list of indices present, e.g.
    discover_row_indices(request.POST, "state", "state") -> [1, 2, 3]

    anchor_field should be a plain text/select field guaranteed to be
    submitted even when empty (not a checkbox, which browsers omit from
    POST entirely when unchecked).
    """
    pattern = re.compile(rf"^{re.escape(prefix)}(\d+)-{re.escape(anchor_field)}$")
    indices = set()
    for key in post_data:
        match = pattern.match(key)
        if match:
            indices.add(int(match.group(1)))
    return sorted(indices)


def get_bank_details(company):
    """
    Explicit query for a company's CompanyBankDetails row — deliberately
    NOT `company.bank_details` / `hasattr(company, "bank_details")`.
    Constructing an unsaved `CompanyBankDetails(company=company)` anywhere
    earlier in the same request (e.g. as a form's fallback instance)
    populates Django's reverse-OneToOne cache on `company`, so the
    attribute accessor can silently return that unsaved, pk-less instance
    afterward instead of raising DoesNotExist — this sidesteps that cache
    entirely by always hitting the DB directly.
    """
    from company_onboarding.models import CompanyBankDetails

    return CompanyBankDetails.objects.filter(company=company).first()


CONTRACT_TERM_FIELDS = (
    "msa_reference_number",
    "start_date",
    "end_date",
    "billing_model",
    "billing_value",
)


def contract_terms_changed(old_values, cleaned_data, new_file_uploaded):
    """
    True if a CompanyContractForm submission actually differs from the
    contract's PRE-edit values -- used to decide whether a post-Active
    contract edit is a real amendment (worth preserving as history) or a
    no-op resave (the wizard resubmits the whole form on every Draft/Next,
    even when nothing in this section changed).

    `old_values` must be a plain dict snapshot (field -> value) taken
    BEFORE the form was constructed -- NOT read live off the model
    instance after the fact. A bound ModelForm mutates its `instance` in
    place (via `construct_instance()`) the moment `.is_valid()` /
    `.cleaned_data` / `.errors` is touched, even before `.save()` is ever
    called -- so if `instance` is the same live active_contract object,
    reading its attributes afterward would just compare the NEW submitted
    values against themselves.
    """
    if new_file_uploaded:
        return True
    return any(
        cleaned_data.get(field) != old_values.get(field)
        for field in CONTRACT_TERM_FIELDS
    )


def validate_for_active(company):
    """
    Independent, authoritative re-validation of every mandatory field
    across Steps 1-2, run when "Mark as Active" is submitted. Re-derives
    pass/fail from current DB state every time — never trusts that a prior
    "Next" pass is still valid. Returns a list of human-readable blockers
    (empty list = ready for Active), structured like
    Employee.get_archive_condition() (employee/models.py:412).

    Step 3 (branding/documents) is deliberately NOT checked here — it's
    explicitly deferrable per the PRD.
    """
    from django.utils.translation import gettext as _

    blockers = []

    if not company.legal_name:
        blockers.append(_("Legal Name is required."))
    if not company.tax_country:
        blockers.append(_("Tax & Registration Country is required."))
    elif company.tax_country == "INDIA" and not company.pan:
        blockers.append(_("PAN is required for domestic clients."))
    elif company.tax_country == "FOREIGN" and not company.foreign_tax_id:
        blockers.append(_("Foreign Tax ID is required for foreign clients."))

    if not company.state_registrations.exists():
        blockers.append(_("At least one Place of Work state is required."))

    if not company.poc_contacts.exists():
        blockers.append(_("At least one point of contact is required."))

    from company_onboarding.models import CompanyBankDetails

    bank_details = get_bank_details(company)
    bank_fields_complete = bank_details and all(
        [
            bank_details.account_number,
            bank_details.bank_name,
            bank_details.ifsc_swift,
            bank_details.account_holder_name,
            bank_details.contact_number,
        ]
    )
    if not bank_fields_complete:
        blockers.append(_("Bank details are required."))
    elif bank_details.verification_status != CompanyBankDetails.VerificationStatus.VERIFIED:
        # Filled-in details only prove the account was TYPED correctly, not
        # that it's real -- Mark-as-Active requires the penny-drop
        # verification to have actually succeeded (see
        # company_onboarding/services/bank_verification.py), not just that
        # the form was completed.
        blockers.append(
            _("Bank account must be verified before the company can be marked Active.")
        )

    active_contract = company.contracts.filter(status="ACTIVE").first()
    if not active_contract or not all(
        [
            active_contract.msa_reference_number,
            active_contract.start_date,
            active_contract.msa_document,
            active_contract.billing_model,
            active_contract.billing_value is not None,
        ]
    ):
        blockers.append(_("An active MSA/Contract with all its fields is required."))

    if not company.signatories.filter(is_enabled=True).exists():
        blockers.append(_("At least one enabled signatory is required."))

    if company.require_payroll_signoff and not company.poc_contacts.filter(
        is_payroll_approver=True
    ).exists():
        blockers.append(
            _(
                "Payroll Sign-off is enabled but no point of contact is "
                "set as the approver."
            )
        )

    return blockers
