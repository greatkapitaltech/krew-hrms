"""
company_onboarding/encryption.py

Symmetric field-level encryption for sensitive data at rest (bank account
numbers). The key is derived deterministically from SECRET_KEY via SHA-256
rather than requiring a separate managed secret -- anyone who can read
settings already has DB access implications in this project's threat
model, so this adds no new key-management burden while still meaning a
raw DB dump/leak doesn't hand over plaintext account numbers.
"""

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _get_fernet() -> Fernet:
    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_value(value: str) -> str:
    """Encrypts a plaintext string. Falsy values pass through unchanged."""
    if not value:
        return value
    return _get_fernet().encrypt(value.encode()).decode()


def decrypt_value(value: str) -> str:
    """
    Decrypts a ciphertext string. Falsy values pass through unchanged. A
    value that isn't valid Fernet ciphertext (e.g. a pre-encryption
    plaintext row that hasn't been re-saved yet) is returned as-is rather
    than raising, so old rows still render instead of crashing the page.
    """
    if not value:
        return value
    try:
        return _get_fernet().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        return value


def hash_value(value: str) -> str:
    """
    Deterministic HMAC-SHA256 hex digest of a plaintext value -- for
    uniqueness/lookup on a field that's ALSO stored encrypted via
    encrypt_value(). Fernet ciphertext includes a random component, so the
    same plaintext encrypts to a different value every time; a DB-level
    UniqueConstraint (or a plain equality lookup) on the ciphertext column
    itself would never actually catch a real duplicate. This hash is
    deterministic (same input -> same output every time) specifically so
    it CAN be compared/indexed, while still not being reversible to the
    original plaintext -- a "blind index", not a second copy of the value.
    Keyed with a distinct HMAC key (not the raw SECRET_KEY digest
    encrypt_value() uses) so this hash can't be repurposed to help recover
    the Fernet key or vice versa.
    """
    if not value:
        return value
    key = hashlib.sha256(f"{settings.SECRET_KEY}:hash".encode()).digest()
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def mask_value(value: str, visible: int = 4) -> str:
    """Masks all but the last `visible` characters with 'X'."""
    if not value:
        return ""
    if len(value) <= visible:
        return value
    return ("X" * (len(value) - visible)) + value[-visible:]
