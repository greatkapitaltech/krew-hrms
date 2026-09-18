"""
base/model_fields.py

Custom model fields usable by any app -- moved here from
krew_company_onboarding/model_fields.py, which now just re-exports from
here to keep old migrations (that reference the old module path) working.
"""

from django.db import models

from base.encryption import decrypt_value, encrypt_value


class EncryptedCharField(models.CharField):
    """
    Use case: a model field that must be stored encrypted at rest (e.g.
    Company.pan, CompanyBankDetails.account_number). Drop-in replacement
    for models.CharField -- use it exactly like a normal CharField in a
    model, and it transparently encrypts on save and decrypts on read, so
    the rest of the code just sees the plain value.

    Ciphertext is significantly longer than the plaintext it holds
    (Fernet's fixed overhead + base64 encoding), so `max_length` here
    governs DB column width, not the real user-facing input limit -- pass
    `plain_max_length` for the limit actually enforced on forms (e.g.
    plain_max_length=10 for a PAN, even though the DB column is 500 chars
    wide to fit the ciphertext).
    """

    def __init__(self, *args, plain_max_length=100, **kwargs):
        # Store the real (plaintext) length limit on the instance, and
        # default the actual DB column (`max_length`, inherited from
        # CharField) to a generous 500 chars -- Fernet's fixed per-token
        # overhead (version byte, timestamp, IV, HMAC, base64 encoding)
        # means ciphertext is always longer than its plaintext, so the
        # column has to be sized well above plain_max_length.
        self.plain_max_length = plain_max_length
        kwargs.setdefault("max_length", 500)
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        # Django Field API hook: called by `makemigrations` to serialize
        # this field into migration code. Without overriding it,
        # plain_max_length (a custom __init__ kwarg, not a real CharField
        # kwarg) would be silently dropped from every migration -- adding
        # it to kwargs here makes migrations reconstruct the field with
        # the correct plaintext limit, not just the DB max_length.
        name, path, args, kwargs = super().deconstruct()
        kwargs["plain_max_length"] = self.plain_max_length
        return name, path, args, kwargs

    def from_db_value(self, value, expression, connection):
        # Django Field API hook: called by the ORM on every row fetched
        # from this column (SELECT). Runs decrypt_value() so the Python
        # side always sees plaintext -- e.g. `company.pan` reads as the
        # real PAN, never the Fernet ciphertext sitting in the DB row.
        if value is None:
            return value
        return decrypt_value(value)

    def get_prep_value(self, value):
        # Django Field API hook: called by the ORM just before this field
        # is sent to the DB (INSERT/UPDATE, and lookups like `.filter()`).
        # Runs encrypt_value() so the DB only ever stores ciphertext, never
        # the raw value.
        value = super().get_prep_value(value)
        if not value:
            return value
        return encrypt_value(value)

    def formfield(self, **kwargs):
        # Django Field API hook: called when building a form field for
        # this model field (ModelForm, admin). Overrides the form's
        # max_length to plain_max_length -- otherwise Django would default
        # it to the DB's max_length=500 and let users type a value far
        # longer than what's actually meaningful (e.g. a 500-char PAN).
        defaults = {"max_length": self.plain_max_length}
        defaults.update(kwargs)
        return super().formfield(**defaults)
