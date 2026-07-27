from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit


def normalize_email(value: str | None) -> str:
    return (value or "").strip().casefold()


def normalize_phone(value: str | None) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 10:
        return f"1{digits}"
    return digits


def normalize_linkedin_url(value: str | None) -> str:
    if not value:
        return ""
    raw = value.strip()
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    host = parts.netloc.casefold().removeprefix("www.")
    path = re.sub(r"/+", "/", parts.path).rstrip("/")
    path = re.sub(r"^/[a-z]{2}-[a-z]{2}/(?=in/)", "/", path, flags=re.I)
    return urlunsplit(("https", host, path, "", ""))


def normalize_name(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def normalize_org(value: str | None) -> str:
    text = normalize_name(value)
    suffix = r"\b(incorporated|inc|llc|ltd|limited|corp|corporation|company|co)\b"
    return re.sub(r"\s+", " ", re.sub(suffix, "", text)).strip()

