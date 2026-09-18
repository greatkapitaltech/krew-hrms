"""
krew_company_onboarding/model_fields.py

Compatibility shim only. EncryptedCharField now lives in base/model_fields.py
(a shared/global location, not scoped to this app) -- this re-export exists
solely because migrations/0006_alter_companybankdetails_account_number.py
has already frozen the class's old import path
(krew_company_onboarding.model_fields.EncryptedCharField) into its
generated code. New code should import from base.model_fields directly.
"""

from base.model_fields import EncryptedCharField

__all__ = ["EncryptedCharField"]
