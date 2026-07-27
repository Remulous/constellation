from __future__ import annotations

import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError


hasher = PasswordHasher()


def verify_password(password: str, expected_hash: str) -> bool:
    if not expected_hash:
        return True
    try:
        return hasher.verify(expected_hash, password)
    except VerificationError:
        return False


def csrf_token(session: dict) -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def verify_csrf(session: dict, supplied: str | None) -> bool:
    expected = session.get("csrf", "")
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))


def safe_csv_cell(value: object) -> str:
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text

