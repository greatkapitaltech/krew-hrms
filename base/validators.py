"""
base/validators.py

Shared, structured-ID / format RegexValidators usable by any app (not
scoped to one module) -- mirrors the module-level RegexValidator convention
already used for the looser general phone field (employee.models.
phone_validator, 7-20 chars, accepts international formats). Moved here
(from krew_company_onboarding/validators.py) so any app can import these
without depending on krew_company_onboarding.
"""

from django.core.validators import RegexValidator
from django.utils.translation import gettext_lazy as _

# Use case: Indian PAN (Permanent Account Number) fields, e.g. Company.pan.
# Format is fixed by law: 5 letters, 4 digits, 1 letter (e.g. ABCDE1234F).
pan_validator = RegexValidator(
    regex=r"^[A-Z]{5}[0-9]{4}[A-Z]$",
    message=_("Enter a valid PAN (format: AAAAA9999A)."),
)

# Use case: Indian GSTIN (GST registration number) fields, e.g. per-state
# CompanyStateRegistration.gstin. 15 characters: 2-digit state code, PAN,
# entity code, "Z", checksum.
gstin_validator = RegexValidator(
    regex=r"^\d{2}[A-Z]{5}\d{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$",
    message=_("Enter a valid 15-character GSTIN."),
)

# Use case: any field that must hold a plain 10-digit phone number with
# nothing else in it -- no "+91", no spaces, no dashes (e.g.
# CompanyPOCContact.mobile, CompanyBankDetails.contact_number).
# Deliberately stricter than employee.models.phone_validator, which stays
# loose (7-20 chars, optional "+") for general employee-facing phone
# fields -- don't reuse that one where an exact 10-digit number is required.
phone_number_validator = RegexValidator(
    regex=r"^\d{10}$",
    message=_("Enter a valid 10-digit phone number."),
)
