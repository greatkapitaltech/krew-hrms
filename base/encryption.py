"""
base/encryption.py

Symmetric field-level encryption for sensitive data at rest (bank account
numbers, PAN). The key is derived deterministically from SECRET_KEY via
SHA-256 rather than requiring a separate managed secret -- anyone who can
read settings already has DB access implications in this project's threat
model, so this adds no new key-management burden while still meaning a
raw DB dump/leak doesn't hand over plaintext account numbers.

Lives in `base` (not scoped to one app) so any app's models can use it --
moved here from krew_company_onboarding/encryption.py, which now just
re-exports from here to keep old migrations (that reference the old
module path) working.
"""

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _get_fernet() -> Fernet:
    """
    Builds the Fernet cipher used by encrypt_value()/decrypt_value().
    Fernet = AES-128 in CBC mode for confidentiality + HMAC-SHA256 for
    integrity (authenticated encryption -- a tampered ciphertext fails to
    decrypt instead of silently returning garbage), wrapped in a
    versioned, timestamped token format. SHA-256 of SECRET_KEY gives a
    32-byte digest, which is then base64-urlsafe-encoded into the exact
    key format Fernet requires. Internal helper only -- not called
    directly outside this module.
    """
    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_value(value: str) -> str:
    """
    Use case: turn a plaintext value (e.g. an account number) into a
    Fernet token right before saving it to the DB. Called automatically by
    EncryptedCharField.get_prep_value() (see model_fields.py) on every
    save() -- you normally don't need to call this yourself. Falsy values
    (None, "") pass through unchanged.
    """
    if not value:
        return value
    return _get_fernet().encrypt(value.encode()).decode()


def decrypt_value(value: str) -> str:
    """
    Use case: turn a Fernet token back into the original plaintext when
    reading a field from the DB. Called automatically by
    EncryptedCharField.from_db_value() on every SELECT -- you normally
    don't need to call this yourself. Falsy values pass through unchanged.
    A value that fails Fernet's own signature check (InvalidToken) -- e.g.
    an old plaintext row saved before encryption was added, not yet
    re-saved -- is returned as-is instead of raising, so old rows still
    render instead of crashing the page.
    """
    if not value:
        return value
    try:
        return _get_fernet().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        return value


def hash_value(value: str) -> str:
    """
    Use case: you have a field that's stored encrypted (via
    encrypt_value()) but you STILL need to detect duplicates or look it up
    by exact value -- e.g. "has this PAN already been used by another
    company?" Encrypted values can't be compared directly, because Fernet
    adds randomness so the same plaintext produces different ciphertext
    every time it's encrypted.

    This function instead produces a fixed, repeatable 64-char hex digest
    (HMAC-SHA256) of the plaintext -- same input always gives the same
    output, so it CAN be compared/indexed/made UNIQUE in the DB (e.g. a
    UniqueConstraint on this hash column), but a one-way HMAC can't be
    reversed back into the original value. This is called a "blind
    index": store it alongside the encrypted field, and check uniqueness
    against the hash column instead of the (non-comparable) ciphertext
    column.

    Keyed with SHA-256(SECRET_KEY + ":hash") -- a distinct key from the
    one _get_fernet() derives -- so this hash can't be used to help
    recover the Fernet key, or vice versa.
    """
    if not value:
        return value
    key = hashlib.sha256(f"{settings.SECRET_KEY}:hash".encode()).digest()
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def mask_value(value: str, visible: int = 4) -> str:
    """
    Use case: display a sensitive value on screen (e.g. an account number)
    without showing it in full -- e.g. "XXXXXX1234". Replaces every
    character except the last `visible` ones with "X". This is a display
    helper only -- it doesn't touch what's stored in the DB.
    """
    if not value:
        return ""
    if len(value) <= visible:
        return value
    return ("X" * (len(value) - visible)) + value[-visible:]
