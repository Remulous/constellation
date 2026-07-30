from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ConnectionSuggestion,
    ContactMethod,
    ExternalIdentity,
    FollowUpSuggestion,
    Interaction,
    Opportunity,
    Organization,
    Person,
    RelationshipSignal,
    VetBizImportCandidate,
    VetBizImportRecord,
)
from app.services.normalize import (
    normalize_email,
    normalize_linkedin_url,
    normalize_name,
    normalize_org,
    normalize_phone,
)


SOURCE_TYPE = "vetbiz_reviewed_minutes"
ALLOWED_EXTENSIONS = {".md", ".markdown", ".txt", ".rtf"}
MAX_EXTRACTED_TEXT = 2_000_000
MAX_RTF_DEPTH = 256
MAX_FIELD_LENGTH = 4_000

SKIP_RTF_DESTINATIONS = {
    "annotation",
    "atnid",
    "author",
    "colortbl",
    "comment",
    "creatim",
    "datastore",
    "doccomm",
    "filetbl",
    "fonttbl",
    "footer",
    "footerf",
    "footerl",
    "footerr",
    "footnote",
    "generator",
    "header",
    "headerf",
    "headerl",
    "headerr",
    "info",
    "keywords",
    "listoverridetable",
    "listtable",
    "manager",
    "mmathpr",
    "operator",
    "pict",
    "private",
    "revtim",
    "rsidtbl",
    "shp",
    "shpinst",
    "stylesheet",
    "subject",
    "themedata",
    "title",
    "userprops",
    "xmlnstbl",
}
REJECTED_RTF_CONTROLS = {
    "objdata",
    "object",
    "password",
    "passwordhash",
}

HEADER_ALIASES = {
    "name": {"name", "participant", "participant name", "member", "attendee"},
    "organization": {
        "organization",
        "business",
        "company",
        "business / organization",
        "business or organization",
    },
    "title": {"title", "position", "role"},
    "contact": {"contact", "contact information", "contact info"},
    "email": {"email", "email address"},
    "phone": {"phone", "telephone", "mobile"},
    "website": {"website", "url"},
    "affiliation": {
        "affiliation",
        "class year",
        "military affiliation",
        "academy / class",
    },
    "offer": {"offer", "offers", "offering", "can help with", "services offered"},
    "ask": {"ask", "asks", "need", "needs", "looking for"},
    "notes_ask": {"notes / ask", "notes and ask", "notes / needs"},
    "notes": {"notes", "summary", "update", "comments"},
    "follow_up": {
        "follow-up",
        "follow up",
        "follow-up notes",
        "next action",
        "action",
    },
}

EDITABLE_FIELDS = {
    "new_contact": {
        "display_name",
        "first_name",
        "last_name",
        "email",
        "phone",
        "organization",
        "title",
        "affiliation",
        "website",
    },
    "contact_update": {
        "display_name",
        "first_name",
        "last_name",
        "email",
        "phone",
        "organization",
        "title",
        "affiliation",
        "website",
    },
    "organization": {"name", "website", "notes"},
    "interaction": {"summary"},
    "signal": {"signal_type", "summary", "category"},
    "follow_up": {"summary", "due_date"},
    "opportunity": {"title", "product", "stage", "next_action", "notes"},
    "connection_suggestion": {"reason"},
}

OPPORTUNITY_PATTERNS = {
    "LayoffLens": (
        r"\bWARN\b",
        r"\blayoff notices?\b",
        r"\bplant closures?\b",
    ),
    "SponsorLens": (
        r"\bOFLC\b",
        r"\bH-?1B\b",
        r"\bH-?2[AB]\b",
        r"\bPERM\b",
        r"\blabor certification",
        r"\bsponsorship records?\b",
    ),
    "UCRLens": (
        r"\bUCR\b",
        r"\bcrime statistics?\b",
        r"\bcrime data\b",
    ),
}

CONNECTION_CATEGORIES = {
    "funding": {
        "ask": (r"\bfunding\b", r"\bfinancing\b", r"\bcapital\b", r"\blender\b"),
        "offer": (
            r"\bprovid(?:e|es|ing) financing\b",
            r"\bacquisition financing\b",
            r"\blending\b",
            r"\binvest(?:or|ment)\b",
        ),
    },
    "due_diligence": {
        "ask": (r"\bdue diligence\b", r"\bbuying a business\b", r"\bacquisition\b"),
        "offer": (r"\bdue diligence\b", r"\bacquisition advisory\b", r"\bbusiness broker\b"),
    },
    "recruiting": {
        "ask": (r"\bhiring\b", r"\brecruit(?:er|ing)\b", r"\btalent\b", r"\bstaffing\b"),
        "offer": (r"\brecruit(?:er|ing)\b", r"\btalent search\b", r"\bstaffing\b"),
    },
    "vendor": {
        "ask": (r"\bvendor\b", r"\bsupplier\b", r"\bsubcontractor\b"),
        "offer": (r"\bvendor\b", r"\bsupplier\b", r"\bsubcontract(?:or|ing)\b"),
    },
    "partner": {
        "ask": (r"\bpartner\b", r"\bteaming\b", r"\bjoint venture\b"),
        "offer": (r"\bpartner\b", r"\bteaming\b", r"\bprime work\b"),
    },
}


class VetBizImportError(ValueError):
    pass


@dataclass
class ParsedParticipant:
    display_name: str
    first_name: str = ""
    last_name: str = ""
    organization: str = ""
    title: str = ""
    email: str = ""
    phone: str = ""
    website: str = ""
    affiliation: str = ""
    offer: str = ""
    ask: str = ""
    notes: str = ""
    follow_up: str = ""
    source_excerpt: str = ""
    contact_key: str = ""


@dataclass
class ParsedMinutes:
    meeting_title: str
    meeting_date: date
    raw_text: str
    participants: list[ParsedParticipant] = field(default_factory=list)


@dataclass
class ImportCreation:
    record: VetBizImportRecord
    exact_duplicate: bool = False
    revision_warning: bool = False


def _clean_text(value: str, limit: int = MAX_FIELD_LENGTH) -> str:
    value = value.replace("\x00", "")
    value = re.sub(r"[\t ]+", " ", value)
    value = re.sub(r"\s*\n\s*", "\n", value)
    return value.strip()[:limit]


def _normalize_header(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*_`:#]+", " ", value)).strip().casefold()


def _canonical_header(value: str) -> str:
    normalized = _normalize_header(value)
    for canonical, aliases in HEADER_ALIASES.items():
        if normalized in aliases:
            return canonical
    return normalized.replace(" ", "_")


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise VetBizImportError("The document text encoding is not supported.")


def extract_rtf_text(data: bytes) -> str:
    if not data.lstrip().startswith(b"{\\rtf"):
        raise VetBizImportError("The uploaded RTF file is malformed.")
    text = data.decode("latin-1")
    lowered = text.casefold()
    for control in REJECTED_RTF_CONTROLS:
        if re.search(rf"\\{control}\b", lowered):
            raise VetBizImportError(
                "The RTF contains an embedded or protected payload and was rejected."
            )

    stack: list[dict] = [
        {"skip": False, "starred": False, "uc": 1, "group_start": True}
    ]
    output: list[str] = []
    extracted_length = 0
    skip_unicode_fallback = 0
    i = 0

    def append(value: str) -> None:
        nonlocal extracted_length
        if stack[-1]["skip"] or not value:
            return
        output.append(value)
        extracted_length += len(value)
        if extracted_length > MAX_EXTRACTED_TEXT:
            raise VetBizImportError("The extracted RTF text is unexpectedly large.")

    while i < len(text):
        char = text[i]
        if char == "{":
            if len(stack) >= MAX_RTF_DEPTH:
                raise VetBizImportError("The RTF nesting depth is unsafe.")
            stack.append(
                {
                    "skip": stack[-1]["skip"],
                    "starred": False,
                    "uc": stack[-1]["uc"],
                    "group_start": True,
                }
            )
            i += 1
            continue
        if char == "}":
            if len(stack) == 1:
                raise VetBizImportError("The RTF has unbalanced groups.")
            stack.pop()
            i += 1
            continue
        if char != "\\":
            if skip_unicode_fallback:
                skip_unicode_fallback -= 1
            elif not stack[-1]["skip"]:
                append(char)
            if not char.isspace():
                stack[-1]["group_start"] = False
            i += 1
            continue

        i += 1
        if i >= len(text):
            raise VetBizImportError("The RTF ends with an incomplete control sequence.")
        symbol = text[i]
        if symbol in "\\{}":
            if skip_unicode_fallback:
                skip_unicode_fallback -= 1
            else:
                append(symbol)
            stack[-1]["group_start"] = False
            i += 1
            continue
        if symbol == "*":
            stack[-1]["starred"] = True
            i += 1
            continue
        if symbol == "'":
            if i + 2 >= len(text) or not re.fullmatch(r"[0-9a-fA-F]{2}", text[i + 1:i + 3]):
                raise VetBizImportError("The RTF contains an invalid hexadecimal escape.")
            if skip_unicode_fallback:
                skip_unicode_fallback -= 1
            elif not stack[-1]["skip"]:
                append(bytes.fromhex(text[i + 1:i + 3]).decode("cp1252", errors="replace"))
            stack[-1]["group_start"] = False
            i += 3
            continue
        if not symbol.isalpha():
            replacements = {"~": "\u00a0", "-": "\u00ad", "_": "\u2011"}
            append(replacements.get(symbol, ""))
            stack[-1]["group_start"] = False
            i += 1
            continue

        start = i
        while i < len(text) and text[i].isalpha():
            i += 1
        word = text[start:i].casefold()
        sign = 1
        if i < len(text) and text[i] == "-":
            sign = -1
            i += 1
        number_start = i
        while i < len(text) and text[i].isdigit():
            i += 1
        parameter = None
        if i > number_start:
            parameter = sign * int(text[number_start:i])
        if i < len(text) and text[i] == " ":
            i += 1

        if stack[-1]["group_start"] and (
            stack[-1]["starred"] or word in SKIP_RTF_DESTINATIONS
        ):
            stack[-1]["skip"] = True
        stack[-1]["group_start"] = False
        if stack[-1]["skip"]:
            continue
        if word == "bin" and parameter is not None:
            if parameter < 0 or i + parameter > len(text):
                raise VetBizImportError("The RTF contains an invalid binary payload.")
            i += parameter
            continue
        if word == "uc" and parameter is not None:
            stack[-1]["uc"] = max(0, min(parameter, 8))
            continue
        if word == "u" and parameter is not None:
            codepoint = parameter if parameter >= 0 else parameter + 65536
            append(chr(codepoint))
            skip_unicode_fallback = stack[-1]["uc"]
            continue
        replacements = {
            "cell": "\t",
            "emdash": "\u2014",
            "emspace": "\u2003",
            "endash": "\u2013",
            "enspace": "\u2002",
            "line": " ",
            "lquote": "\u2018",
            "par": "\n",
            "qmspace": "\u2005",
            "row": "\n",
            "rquote": "\u2019",
            "tab": "\t",
        }
        append(replacements.get(word, ""))

    if len(stack) != 1:
        raise VetBizImportError("The RTF has unbalanced groups.")
    extracted = "".join(output)
    extracted = re.sub(r"[ \u00a0]+\t", "\t", extracted)
    extracted = re.sub(r"\t[ \u00a0]+", "\t", extracted)
    extracted = re.sub(r"[ \u00a0]+", " ", extracted)
    extracted = re.sub(r"\n[ \t]+", "\n", extracted)
    extracted = re.sub(r"\n{3,}", "\n\n", extracted)
    return extracted.strip()


def _parse_date(value: str) -> date | None:
    cleaned = value.strip().replace(",", "")
    formats = (
        "%B %d %Y",
        "%b %d %Y",
        "%d %B %Y",
        "%d %b %Y",
        "%m/%d/%Y",
        "%Y-%m-%d",
    )
    for fmt in formats:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _find_meeting_date(text: str) -> date | None:
    patterns = (
        r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}\b",
        r"\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December),?\s+\d{4}\b",
        r"\b\d{1,2}/\d{1,2}/\d{4}\b",
        r"\b\d{4}-\d{2}-\d{2}\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match and (parsed := _parse_date(match.group(0))):
            return parsed
    return None


def _find_title(text: str) -> str:
    lines = [_clean_text(line, 300) for line in text.splitlines()]
    candidates = [
        line.lstrip("#").strip()
        for line in lines
        if line
        and "\t" not in line
        and len(line) <= 300
        and not re.fullmatch(r"[-|: ]+", line)
    ]
    for line in candidates:
        if any(term in line.casefold() for term in ("minutes", "vetbiz", "biznetwork", "meeting")):
            return line
    return candidates[0] if candidates else "VetBiz Reviewed Meeting Minutes"


def _markdown_rows(text: str) -> list[dict[str, str]]:
    lines = text.splitlines()
    for index in range(len(lines) - 2):
        header = lines[index].strip()
        separator = lines[index + 1].strip()
        if "|" not in header or not re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", separator):
            continue
        headers = [_canonical_header(cell) for cell in header.strip("|").split("|")]
        if "name" not in headers:
            continue
        rows: list[dict[str, str]] = []
        for line in lines[index + 2:]:
            if "|" not in line:
                break
            cells = [_clean_text(cell) for cell in line.strip().strip("|").split("|")]
            if not any(cells):
                continue
            cells += [""] * (len(headers) - len(cells))
            rows.append(dict(zip(headers, cells)))
        if rows:
            return rows
    return []


def _tabular_rows(text: str) -> list[dict[str, str]]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        cells = [_clean_text(cell) for cell in line.split("\t")]
        while cells and not cells[-1]:
            cells.pop()
        headers = [_canonical_header(cell) for cell in cells]
        if "name" not in headers or len(headers) < 2:
            continue
        if not any(header in headers for header in ("organization", "contact", "ask", "notes_ask", "offer")):
            continue
        rows: list[dict[str, str]] = []
        pending_lines: list[str] = []
        pending_tabs = 0
        required_tabs = len(headers) - 1

        def flush_pending() -> None:
            nonlocal pending_lines, pending_tabs
            if not pending_lines:
                return
            logical_row = " ".join(pending_lines)
            candidate_cells = [_clean_text(cell) for cell in logical_row.split("\t")]
            while candidate_cells and not candidate_cells[-1]:
                candidate_cells.pop()
            pending_lines = []
            pending_tabs = 0
            if len(candidate_cells) < 2:
                return
            candidate_headers = [_canonical_header(cell) for cell in candidate_cells]
            if candidate_headers == headers:
                return
            candidate_cells.extend([""] * (len(headers) - len(candidate_cells)))
            rows.append(dict(zip(headers, candidate_cells[:len(headers)])))

        for candidate_line in lines[index + 1:]:
            if not candidate_line.strip():
                if pending_lines and pending_tabs >= 2:
                    flush_pending()
                continue
            line_tabs = candidate_line.count("\t")
            if line_tabs == 0:
                if pending_lines:
                    pending_lines.append(candidate_line.strip(" \r\n"))
                elif rows:
                    break
                continue
            pending_lines.append(candidate_line.strip(" \r\n"))
            pending_tabs += line_tabs
            if (
                len(pending_lines) == 1
                and line_tabs >= required_tabs
                and not candidate_line.rstrip(" \r\n").endswith("\t")
            ):
                flush_pending()
        flush_pending()
        if rows:
            return rows
    return []


def _labeled_rows(text: str) -> list[dict[str, str]]:
    rows = []
    for block in re.split(r"\n\s*\n+", text):
        parsed: dict[str, str] = {}
        current_key = ""
        for line in block.splitlines():
            match = re.match(r"^\s*([A-Za-z][A-Za-z /_-]{1,40}):\s*(.*)$", line)
            if match:
                key = _canonical_header(match.group(1))
                if key in {alias for alias in HEADER_ALIASES}:
                    current_key = key
                    parsed[key] = _clean_text(match.group(2))
                else:
                    current_key = ""
            elif current_key and line.strip():
                parsed[current_key] = _clean_text(f"{parsed[current_key]} {line.strip()}")
        if parsed.get("name"):
            rows.append(parsed)
    return rows


def _extract_contact_fields(row: dict[str, str]) -> dict[str, str]:
    contact = " ".join(
        value for value in (row.get("contact", ""), row.get("email", ""), row.get("phone", ""), row.get("website", "")) if value
    )
    email_match = re.search(r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w.-]+\.[A-Za-z]{2,}", contact)
    phone_match = re.search(
        r"(?<!\d)(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)",
        contact,
    )
    url_match = re.search(
        r"\b(?:https?://)?(?:www\.)?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?:/[^\s,;]*)?",
        contact,
        flags=re.IGNORECASE,
    )
    website = url_match.group(0) if url_match else row.get("website", "")
    if website.casefold().rstrip("/") in {"linkedin.com", "www.linkedin.com"}:
        website = ""
    return {
        "email": email_match.group(0) if email_match else _clean_text(row.get("email", ""), 320),
        "phone": phone_match.group(0) if phone_match else _clean_text(row.get("phone", ""), 80),
        "website": _clean_text(website, 500),
    }


def _split_name_affiliation(value: str) -> tuple[str, str]:
    value = _clean_text(value, 300)
    match = re.match(
        r"^(.*?)(?:,\s*)?(?:\(\s*)?"
        r"((?:USNA|USMA|USMMA|USAFA|USCGA)\s*)?"
        r"(?:[‘’'`](\d{2})|(?:class\s+of\s+)?((?:19|20)\d{2}))"
        r"(?:\s*\))?$",
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        return value, ""
    name = match.group(1).rstrip(" ,")
    academy = (match.group(2) or "").strip().upper()
    class_year = f"'{match.group(3)}" if match.group(3) else match.group(4)
    affiliation = f"{academy} {class_year}".strip()
    return name, affiliation


def _participant_from_row(row: dict[str, str]) -> ParsedParticipant | None:
    display_name, inferred_affiliation = _split_name_affiliation(row.get("name", ""))
    if not display_name or _normalize_header(display_name) == "name":
        return None
    name_parts = display_name.split()
    contact = _extract_contact_fields(row)
    notes_ask = row.get("notes_ask", "")
    participant = ParsedParticipant(
        display_name=display_name,
        first_name=name_parts[0] if name_parts else "",
        last_name=name_parts[-1] if len(name_parts) > 1 else "",
        organization=_clean_text(row.get("organization", ""), 250),
        title=_clean_text(row.get("title", ""), 250),
        email=contact["email"],
        phone=contact["phone"],
        website=contact["website"],
        affiliation=_clean_text(row.get("affiliation", "") or inferred_affiliation, 120),
        offer=_clean_text(row.get("offer", "")),
        ask=_clean_text(row.get("ask", "") or notes_ask),
        notes=_clean_text(row.get("notes", "") or notes_ask),
        follow_up=_clean_text(row.get("follow_up", "")),
    )
    participant.source_excerpt = _clean_text(
        " | ".join(value for value in row.values() if value), 2_000
    )
    key_source = "|".join(
        (
            normalize_name(participant.display_name),
            normalize_email(participant.email),
            normalize_org(participant.organization),
        )
    )
    participant.contact_key = hashlib.sha256(key_source.encode()).hexdigest()[:20]
    return participant


def parse_reviewed_minutes(filename: str, data: bytes) -> ParsedMinutes:
    extension = Path(filename).suffix.casefold()
    if extension not in ALLOWED_EXTENSIONS:
        raise VetBizImportError(
            "Version 1 accepts Markdown, plain text, and RTF reviewed minutes."
        )
    raw_text = extract_rtf_text(data) if extension == ".rtf" else _decode_text(data)
    raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw_text:
        raise VetBizImportError("The reviewed-minutes document contains no readable text.")
    if len(raw_text) > MAX_EXTRACTED_TEXT:
        raise VetBizImportError("The extracted document text is unexpectedly large.")
    meeting_date = _find_meeting_date(raw_text)
    if not meeting_date:
        raise VetBizImportError(
            "A meeting date could not be found. Add a reviewed meeting date to the document."
        )
    rows = _markdown_rows(raw_text) or _tabular_rows(raw_text) or _labeled_rows(raw_text)
    participants = [
        participant
        for row in rows
        if (participant := _participant_from_row(row)) is not None
    ]
    return ParsedMinutes(
        meeting_title=_find_title(raw_text),
        meeting_date=meeting_date,
        raw_text=raw_text,
        participants=participants,
    )


def _match_person(
    db: Session, participant: ParsedParticipant
) -> tuple[Person | None, str, float, list[dict]]:
    if participant.email:
        norm = normalize_email(participant.email)
        people = db.scalars(
            select(Person)
            .outerjoin(ContactMethod, ContactMethod.person_id == Person.id)
            .where(
                Person.archived_at.is_(None),
                (
                    (func.lower(Person.primary_email) == norm)
                    | (
                        (ContactMethod.method_type == "email")
                        & (ContactMethod.normalized_value == norm)
                    )
                ),
            )
        ).unique().all()
        if len(people) == 1:
            return people[0], "exact email", 1.0, []
        if len(people) > 1:
            return None, "ambiguous exact email", 0.65, _person_options(people, "exact email")

    linkedin_url = normalize_linkedin_url(participant.website)
    if linkedin_url and "linkedin.com/in/" in linkedin_url.casefold():
        identities = db.scalars(
            select(ExternalIdentity).where(
                ExternalIdentity.profile_url == linkedin_url
            )
        ).all()
        people = [db.get(Person, identity.person_id) for identity in identities]
        people = [person for person in people if person and person.archived_at is None]
        if len(people) == 1:
            return people[0], "exact external identifier", 1.0, []
        if len(people) > 1:
            return None, "ambiguous external identifier", 0.65, _person_options(
                people, "exact external identifier"
            )

    active_people = db.scalars(
        select(Person).where(Person.archived_at.is_(None))
    ).all()
    name = normalize_name(participant.display_name)
    organization = normalize_org(participant.organization)
    name_org = [
        person
        for person in active_people
        if normalize_name(person.display_name) == name
        and organization
        and normalize_org(person.current_organization) == organization
    ]
    if len(name_org) == 1:
        return name_org[0], "exact name and organization", 0.98, []
    if len(name_org) > 1:
        return None, "ambiguous exact name and organization", 0.65, _person_options(
            name_org, "exact name and organization"
        )
    name_matches = [
        person for person in active_people if normalize_name(person.display_name) == name
    ]
    if len(name_matches) == 1:
        return name_matches[0], "exact full name", 0.9, []
    if len(name_matches) > 1:
        return None, "ambiguous exact full name", 0.6, _person_options(
            name_matches, "exact full name"
        )

    fuzzy: list[tuple[float, Person]] = []
    for person in active_people:
        name_score = SequenceMatcher(
            None, name, normalize_name(person.display_name)
        ).ratio()
        org_score = (
            SequenceMatcher(
                None, organization, normalize_org(person.current_organization)
            ).ratio()
            if organization and person.current_organization
            else 0.0
        )
        score = (name_score * 0.75) + (org_score * 0.25)
        if name_score >= 0.82 and score >= 0.76:
            fuzzy.append((score, person))
    fuzzy.sort(key=lambda item: item[0], reverse=True)
    options = [
        {
            "id": person.id,
            "display_name": person.display_name,
            "organization": person.current_organization or "",
            "reason": f"fuzzy name and organization ({score:.0%})",
        }
        for score, person in fuzzy[:5]
    ]
    return None, ("fuzzy suggestions only" if options else "no deterministic match"), (
        fuzzy[0][0] if fuzzy else 0.5
    ), options


def _person_options(people: Iterable[Person], reason: str) -> list[dict]:
    return [
        {
            "id": person.id,
            "display_name": person.display_name,
            "organization": person.current_organization or "",
            "reason": reason,
        }
        for person in people
    ]


def _signal_category(text: str, direction: str) -> str:
    for category, patterns in CONNECTION_CATEGORIES.items():
        if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns[direction]):
            return category
    return "other"


def _split_explicit_signals(participant: ParsedParticipant) -> list[dict]:
    signals: list[dict] = []
    if participant.offer:
        signals.append(
            {
                "signal_type": "offer",
                "direction": "offer",
                "summary": participant.offer,
                "category": _signal_category(participant.offer, "offer"),
            }
        )
    if participant.ask and participant.ask != participant.notes:
        signals.append(
            {
                "signal_type": "ask",
                "direction": "ask",
                "summary": participant.ask,
                "category": _signal_category(participant.ask, "ask"),
            }
        )

    combined = participant.notes or participant.ask
    for sentence in re.split(r"(?<=[.!?;])\s+", combined):
        sentence = _clean_text(sentence)
        if not sentence:
            continue
        is_ask = bool(
            re.search(
                r"\b(seeking|looking for|needs?|wants?|hiring|interested in finding)\b",
                sentence,
                flags=re.IGNORECASE,
            )
        )
        is_offer = bool(
            re.search(
                r"\b(offers?|provides?|specializes in|supports?|consulting|advises?|recruiting)\b",
                sentence,
                flags=re.IGNORECASE,
            )
        )
        for direction, matched in (("ask", is_ask), ("offer", is_offer)):
            if not matched:
                continue
            signal = {
                "signal_type": _specific_signal_type(sentence, direction),
                "direction": direction,
                "summary": sentence,
                "category": _signal_category(sentence, direction),
            }
            if not any(
                item["direction"] == signal["direction"]
                and item["summary"].casefold() == signal["summary"].casefold()
                for item in signals
            ):
                signals.append(signal)
    return signals


def _specific_signal_type(text: str, direction: str) -> str:
    lowered = text.casefold()
    if "hiring" in lowered:
        return "hiring"
    if "funding" in lowered or "financing" in lowered or "capital" in lowered:
        return "seeking_funding" if direction == "ask" else "offering_funding"
    if "customer" in lowered:
        return "seeking_customer"
    if "vendor" in lowered or "supplier" in lowered:
        return "seeking_vendor" if direction == "ask" else "offer"
    if "partner" in lowered or "teaming" in lowered:
        return "seeking_partner" if direction == "ask" else "offer"
    if "introduction" in lowered or "introduc" in lowered:
        return "seeking_introduction" if direction == "ask" else "offer"
    return direction


def _opportunity_products(text: str) -> list[str]:
    return [
        product
        for product, patterns in OPPORTUNITY_PATTERNS.items()
        if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)
    ]


def _add_candidate(
    record: VetBizImportRecord,
    candidate_type: str,
    data: dict,
    excerpt: str,
    confidence: float,
    matched_entity_id: str | None = None,
    match_reason: str | None = None,
) -> VetBizImportCandidate:
    candidate = VetBizImportCandidate(
        candidate_type=candidate_type,
        extracted_data=data,
        source_excerpt=_clean_text(excerpt, 2_000),
        confidence=max(0.0, min(confidence, 1.0)),
        status="pending",
        matched_entity_id=matched_entity_id,
        match_reason=match_reason,
    )
    record.candidates.append(candidate)
    return candidate


def create_reviewed_import(
    db: Session,
    filename: str,
    data: bytes,
    review_confirmed: bool,
    review_notes: str = "",
    max_bytes: int = 16 * 1024 * 1024,
) -> ImportCreation:
    if not review_confirmed:
        raise VetBizImportError(
            "Confirm that the minutes have completed human review before importing."
        )
    if not data:
        raise VetBizImportError("Upload or paste reviewed meeting minutes.")
    if len(data) > max_bytes:
        raise VetBizImportError("The reviewed-minutes document exceeds the upload limit.")
    safe_filename = Path(filename or "pasted-minutes.txt").name[:255]
    checksum = hashlib.sha256(data).hexdigest()
    exact = db.scalar(
        select(VetBizImportRecord).where(VetBizImportRecord.checksum == checksum)
    )
    if exact:
        return ImportCreation(record=exact, exact_duplicate=True)

    parsed = parse_reviewed_minutes(safe_filename, data)
    prior = db.scalar(
        select(VetBizImportRecord)
        .where(
            VetBizImportRecord.meeting_date == parsed.meeting_date,
            func.lower(VetBizImportRecord.meeting_title)
            == parsed.meeting_title.casefold(),
        )
        .order_by(VetBizImportRecord.imported_at.desc())
    )
    record = VetBizImportRecord(
        filename=safe_filename,
        meeting_title=parsed.meeting_title,
        meeting_date=parsed.meeting_date,
        review_confirmed=True,
        review_notes=_clean_text(review_notes, 2_000) or None,
        raw_text=parsed.raw_text,
        checksum=checksum,
        revision_of_id=prior.id if prior else None,
    )
    db.add(record)
    db.flush()

    organization_candidates: dict[str, VetBizImportCandidate] = {}
    signal_candidates: list[tuple[ParsedParticipant, dict, VetBizImportCandidate]] = []
    for participant in parsed.participants:
        matched, reason, confidence, match_options = _match_person(db, participant)
        contact_data = {
            "contact_key": participant.contact_key,
            "display_name": participant.display_name,
            "first_name": participant.first_name,
            "last_name": participant.last_name,
            "email": participant.email,
            "phone": participant.phone,
            "organization": participant.organization,
            "title": participant.title,
            "website": participant.website,
            "affiliation": participant.affiliation,
            "match_options": match_options,
        }
        contact_candidate = _add_candidate(
            record,
            "contact_update" if matched else "new_contact",
            contact_data,
            participant.source_excerpt,
            confidence,
            matched.id if matched else None,
            reason,
        )

        organization_key = normalize_org(participant.organization)
        if organization_key and organization_key not in organization_candidates:
            existing_org = db.scalar(
                select(Organization).where(
                    Organization.normalized_name == organization_key
                )
            )
            organization_candidates[organization_key] = _add_candidate(
                record,
                "organization",
                {
                    "organization_key": organization_key,
                    "name": participant.organization,
                    "website": participant.website,
                    "notes": "",
                },
                participant.source_excerpt,
                1.0 if existing_org else 0.9,
                str(existing_org.id) if existing_org else None,
                "exact normalized organization" if existing_org else "new organization",
            )

        interaction_summary = participant.notes or participant.ask or "Attended the reviewed VetBiz meeting."
        _add_candidate(
            record,
            "interaction",
            {
                "contact_key": participant.contact_key,
                "summary": interaction_summary,
                "meeting_title": parsed.meeting_title,
            },
            participant.source_excerpt,
            confidence,
            matched.id if matched else None,
            f"{reason}; meeting participation" if reason else "meeting participation",
        )

        for signal in _split_explicit_signals(participant):
            signal_key = hashlib.sha256(
                (
                    participant.contact_key
                    + "|"
                    + signal["direction"]
                    + "|"
                    + signal["summary"].casefold()
                ).encode()
            ).hexdigest()[:20]
            signal_data = {
                **signal,
                "signal_key": signal_key,
                "contact_key": participant.contact_key,
                "organization_key": organization_key,
            }
            signal_candidate = _add_candidate(
                record,
                "signal",
                signal_data,
                signal["summary"],
                0.82,
                matched.id if matched else None,
                "explicit reviewed-minutes statement",
            )
            signal_candidates.append((participant, signal, signal_candidate))
            if signal["direction"] == "ask":
                _add_candidate(
                    record,
                    "follow_up",
                    {
                        "contact_key": participant.contact_key,
                        "summary": f"Follow up about: {signal['summary']}",
                        "due_date": "",
                    },
                    signal["summary"],
                    0.65,
                    matched.id if matched else None,
                    "explicit ask; date requires user review",
                )
            for product in _opportunity_products(signal["summary"]):
                _add_candidate(
                    record,
                    "opportunity",
                    {
                        "contact_key": participant.contact_key,
                        "organization_key": organization_key,
                        "source_signal_key": signal_key,
                        "title": f"Possible {product} fit — {participant.display_name}",
                        "product": product,
                        "stage": "needs_clarification",
                        "next_action": "Review the stated data need before deciding whether to reach out.",
                        "notes": "Possible fit requiring review; no opportunity has been inferred from industry overlap alone.",
                    },
                    signal["summary"],
                    0.78,
                    matched.id if matched else None,
                    f"explicit data-related need matched {product} keywords",
                )

        if participant.follow_up:
            _add_candidate(
                record,
                "follow_up",
                {
                    "contact_key": participant.contact_key,
                    "summary": participant.follow_up,
                    "due_date": "",
                },
                participant.follow_up,
                0.85,
                matched.id if matched else None,
                "explicit follow-up text",
            )

    for ask_participant, ask, ask_candidate in signal_candidates:
        if ask["direction"] != "ask" or ask["category"] == "other":
            continue
        for offer_participant, offer, offer_candidate in signal_candidates:
            if (
                offer["direction"] != "offer"
                or offer["category"] != ask["category"]
                or offer_participant.contact_key == ask_participant.contact_key
            ):
                continue
            _add_candidate(
                record,
                "connection_suggestion",
                {
                    "source_contact_key": ask_participant.contact_key,
                    "target_contact_key": offer_participant.contact_key,
                    "ask_signal_key": ask_candidate.extracted_data.get("signal_key"),
                    "offer_signal_key": offer_candidate.extracted_data.get("signal_key"),
                    "reason": (
                        f"{ask_participant.display_name} stated an ask related to "
                        f"{ask['category']}; {offer_participant.display_name} stated a "
                        f"corresponding offer. Review both statements before introducing anyone."
                    ),
                },
                f"Ask: {ask['summary']}\nOffer: {offer['summary']}",
                0.72,
                match_reason="reviewed ask/offer category alignment",
            )

    db.commit()
    return ImportCreation(
        record=record,
        revision_warning=record.revision_of_id is not None,
    )


def update_candidate(
    candidate: VetBizImportCandidate,
    action: str,
    submitted_fields: dict[str, str],
    matched_entity_id: str = "",
    resolution_notes: str = "",
) -> None:
    if candidate.import_record.import_status == "committed":
        raise VetBizImportError("Committed imports cannot be edited.")
    if action not in {"approve", "reject", "save"}:
        raise VetBizImportError("Unsupported candidate action.")
    if action == "reject":
        candidate.status = "rejected"
        candidate.resolution_notes = _clean_text(resolution_notes, 2_000) or None
        candidate.resolved_at = datetime.now(timezone.utc)
        return

    allowed = EDITABLE_FIELDS.get(candidate.candidate_type, set())
    data = dict(candidate.extracted_data)
    for key in allowed:
        if key in submitted_fields:
            data[key] = _clean_text(submitted_fields[key])
    candidate.extracted_data = data
    candidate.matched_entity_id = matched_entity_id or None
    candidate.resolution_notes = _clean_text(resolution_notes, 2_000) or None
    candidate.status = "approved" if action == "approve" else "edited"
    candidate.resolved_at = datetime.now(timezone.utc)


def approve_safe_interactions(record: VetBizImportRecord) -> int:
    if record.import_status == "committed":
        raise VetBizImportError("Committed imports cannot be edited.")
    approved = 0
    for candidate in record.candidates:
        if (
            candidate.candidate_type == "interaction"
            and candidate.status in {"pending", "edited"}
            and candidate.matched_entity_id
            and candidate.match_reason
            and "exact email" in candidate.match_reason
        ):
            candidate.status = "approved"
            candidate.resolved_at = datetime.now(timezone.utc)
            approved += 1
    return approved


def propose_opportunity_from_signal(
    record: VetBizImportRecord, signal_candidate: VetBizImportCandidate
) -> VetBizImportCandidate:
    if record.import_status == "committed":
        raise VetBizImportError("Committed imports cannot be edited.")
    if (
        signal_candidate.import_record_id != record.id
        or signal_candidate.candidate_type != "signal"
    ):
        raise VetBizImportError("Only a signal from this import can become an opportunity proposal.")
    signal_key = signal_candidate.extracted_data.get("signal_key", "")
    existing = next(
        (
            candidate
            for candidate in record.candidates
            if candidate.candidate_type == "opportunity"
            and candidate.extracted_data.get("source_signal_key") == signal_key
        ),
        None,
    )
    if existing:
        return existing
    contact_key = signal_candidate.extracted_data.get("contact_key", "")
    contact = next(
        (
            candidate
            for candidate in record.candidates
            if candidate.candidate_type in {"new_contact", "contact_update"}
            and candidate.extracted_data.get("contact_key") == contact_key
        ),
        None,
    )
    display_name = (
        contact.extracted_data.get("display_name", "reviewed contact")
        if contact
        else "reviewed contact"
    )
    summary = signal_candidate.extracted_data.get("summary", "")
    products = _opportunity_products(summary)
    product = products[0] if len(products) == 1 else ""
    return _add_candidate(
        record,
        "opportunity",
        {
            "contact_key": contact_key,
            "organization_key": signal_candidate.extracted_data.get(
                "organization_key", ""
            ),
            "source_signal_key": signal_key,
            "title": f"Possible opportunity — {display_name}",
            "product": product,
            "stage": "needs_clarification",
            "next_action": "Clarify the reviewed need before deciding whether this is a commercial or partnership fit.",
            "notes": "User-created opportunity proposal from a sourced signal; approval is still required.",
        },
        signal_candidate.source_excerpt,
        0.7,
        signal_candidate.matched_entity_id,
        "user converted a reviewed signal into an opportunity proposal",
    )


def propose_connection_suggestion(
    record: VetBizImportRecord,
    source_contact: VetBizImportCandidate,
    target_contact: VetBizImportCandidate,
    reason: str,
) -> VetBizImportCandidate:
    if record.import_status == "committed":
        raise VetBizImportError("Committed imports cannot be edited.")
    contact_types = {"new_contact", "contact_update"}
    if (
        source_contact.import_record_id != record.id
        or target_contact.import_record_id != record.id
        or source_contact.candidate_type not in contact_types
        or target_contact.candidate_type not in contact_types
    ):
        raise VetBizImportError("Select two contact proposals from this import.")
    source_key = source_contact.extracted_data.get("contact_key", "")
    target_key = target_contact.extracted_data.get("contact_key", "")
    if not source_key or not target_key or source_key == target_key:
        raise VetBizImportError("Select two different contacts.")
    clean_reason = _clean_text(reason, 2_000)
    if not clean_reason:
        raise VetBizImportError("Explain why the introduction may be useful.")
    return _add_candidate(
        record,
        "connection_suggestion",
        {
            "source_contact_key": source_key,
            "target_contact_key": target_key,
            "ask_signal_key": "",
            "offer_signal_key": "",
            "reason": clean_reason,
        },
        (
            f"{source_contact.extracted_data.get('display_name', 'Contact')} ↔ "
            f"{target_contact.extracted_data.get('display_name', 'Contact')}: "
            f"{clean_reason}"
        ),
        0.6,
        match_reason="user-created introduction proposal from reviewed participants",
    )


def _resolve_person(
    db: Session,
    candidate: VetBizImportCandidate,
    people_by_key: dict[str, Person],
    key_field: str = "contact_key",
) -> Person | None:
    data = candidate.extracted_data
    key = data.get(key_field, "")
    if key and key in people_by_key:
        return people_by_key[key]
    if candidate.matched_entity_id:
        return db.get(Person, candidate.matched_entity_id)
    return None


def _add_contact_method(
    person: Person, method_type: str, value: str, source: str = SOURCE_TYPE
) -> None:
    if not value:
        return
    normalizer = normalize_email if method_type == "email" else normalize_phone
    normalized = normalizer(value)
    if not normalized:
        return
    if any(
        method.method_type == method_type and method.normalized_value == normalized
        for method in person.methods
    ):
        return
    person.methods.append(
        ContactMethod(
            method_type=method_type,
            value=value,
            normalized_value=normalized,
            label="VetBiz",
            source=source,
            primary=not any(method.method_type == method_type for method in person.methods),
        )
    )


def _mark_committed(
    candidate: VetBizImportCandidate, entity_type: str, entity_id: str | int | None
) -> None:
    candidate.status = "committed"
    candidate.committed_entity_type = entity_type
    candidate.committed_entity_id = str(entity_id) if entity_id is not None else None
    candidate.resolved_at = datetime.now(timezone.utc)


def _parse_optional_date(value: str) -> date | None:
    return _parse_date(value) if value else None


def commit_reviewed_import(db: Session, record: VetBizImportRecord) -> dict[str, int]:
    if record.import_status == "committed":
        return {"already_committed": 1}
    approved = [
        candidate for candidate in record.candidates if candidate.status == "approved"
    ]
    counts: dict[str, int] = {}
    people_by_key: dict[str, Person] = {}
    organizations_by_key: dict[str, Organization] = {}
    signals_by_key: dict[str, RelationshipSignal] = {}

    try:
        for candidate in approved:
            if candidate.candidate_type not in {"new_contact", "contact_update"}:
                continue
            data = candidate.extracted_data
            person = (
                db.get(Person, candidate.matched_entity_id)
                if candidate.matched_entity_id
                else None
            )
            if candidate.matched_entity_id and person is None:
                raise VetBizImportError("A selected contact match no longer exists.")
            if person is None:
                if not data.get("display_name"):
                    raise VetBizImportError("An approved new contact requires a name.")
                person = Person(
                    display_name=data["display_name"],
                    first_name=data.get("first_name") or None,
                    last_name=data.get("last_name") or None,
                    primary_email=normalize_email(data.get("email", "")) or None,
                    primary_phone=normalize_phone(data.get("phone", "")) or None,
                    current_organization=data.get("organization") or None,
                    current_title=data.get("title") or None,
                )
                db.add(person)
                db.flush()
            else:
                person.first_name = person.first_name or data.get("first_name") or None
                person.last_name = person.last_name or data.get("last_name") or None
                person.primary_email = (
                    person.primary_email or normalize_email(data.get("email", "")) or None
                )
                person.primary_phone = (
                    person.primary_phone or normalize_phone(data.get("phone", "")) or None
                )
                person.current_organization = (
                    person.current_organization or data.get("organization") or None
                )
                person.current_title = person.current_title or data.get("title") or None
            _add_contact_method(person, "email", data.get("email", ""))
            _add_contact_method(person, "phone", data.get("phone", ""))
            people_by_key[data.get("contact_key", "")] = person
            _mark_committed(candidate, "person", person.id)
            counts["contacts"] = counts.get("contacts", 0) + 1

        for candidate in approved:
            if candidate.candidate_type != "organization":
                continue
            data = candidate.extracted_data
            normalized = normalize_org(data.get("name", ""))
            if not normalized:
                raise VetBizImportError("An approved organization requires a name.")
            organization = (
                db.get(Organization, int(candidate.matched_entity_id))
                if candidate.matched_entity_id
                else db.scalar(
                    select(Organization).where(
                        Organization.normalized_name == normalized
                    )
                )
            )
            if organization is None:
                organization = Organization(
                    name=data["name"],
                    normalized_name=normalized,
                    website=data.get("website") or None,
                    notes=data.get("notes") or None,
                    source_import_id=record.id,
                    source_candidate_id=candidate.id,
                )
                db.add(organization)
                db.flush()
            organizations_by_key[data.get("organization_key", normalized)] = organization
            _mark_committed(candidate, "organization", organization.id)
            counts["organizations"] = counts.get("organizations", 0) + 1

        for candidate in approved:
            if candidate.candidate_type != "interaction":
                continue
            person = _resolve_person(db, candidate, people_by_key)
            if person is None:
                raise VetBizImportError(
                    "Approve or select the related contact before committing an interaction."
                )
            summary = candidate.extracted_data.get("summary") or "Attended the reviewed VetBiz meeting."
            interaction = db.scalar(
                select(Interaction).where(
                    Interaction.person_id == person.id,
                    Interaction.interaction_date == record.meeting_date,
                    Interaction.summary == summary,
                    Interaction.source_import_id.is_not(None),
                )
            )
            if interaction is None:
                interaction = Interaction(
                    person_id=person.id,
                    interaction_type="VetBiz meeting",
                    interaction_date=record.meeting_date,
                    direction="in-person",
                    summary=summary,
                    source=SOURCE_TYPE,
                    external_reference=f"vetbiz-import:{record.id}",
                    source_import_id=record.id,
                    source_candidate_id=candidate.id,
                    source_excerpt=candidate.source_excerpt,
                    meaningful=True,
                )
                db.add(interaction)
                db.flush()
                if (
                    person.last_meaningful_interaction is None
                    or record.meeting_date > person.last_meaningful_interaction
                ):
                    person.last_meaningful_interaction = record.meeting_date
            else:
                candidate.resolution_notes = (
                    "Reused an existing sourced interaction for this meeting."
                )
            _mark_committed(candidate, "interaction", interaction.id)
            counts["interactions"] = counts.get("interactions", 0) + 1

        for candidate in approved:
            if candidate.candidate_type != "signal":
                continue
            data = candidate.extracted_data
            person = _resolve_person(db, candidate, people_by_key)
            organization = organizations_by_key.get(data.get("organization_key", ""))
            summary = data.get("summary", "")
            if not summary:
                raise VetBizImportError("An approved signal requires a summary.")
            signal = db.scalar(
                select(RelationshipSignal).where(
                    RelationshipSignal.person_id == (person.id if person else None),
                    RelationshipSignal.meeting_date == record.meeting_date,
                    RelationshipSignal.signal_type == data.get("signal_type", "other"),
                    RelationshipSignal.summary == summary,
                )
            )
            if signal is None:
                signal = RelationshipSignal(
                    person_id=person.id if person else None,
                    organization_id=organization.id if organization else None,
                    signal_type=data.get("signal_type") or "other",
                    summary=summary,
                    meeting_date=record.meeting_date,
                    source_import_id=record.id,
                    source_candidate_id=candidate.id,
                    source_excerpt=candidate.source_excerpt,
                )
                db.add(signal)
                db.flush()
            else:
                candidate.resolution_notes = (
                    "Reused an existing sourced signal for this meeting."
                )
            if data.get("signal_key"):
                signals_by_key[data["signal_key"]] = signal
            _mark_committed(candidate, "relationship_signal", signal.id)
            counts["signals"] = counts.get("signals", 0) + 1

        for candidate in approved:
            if candidate.candidate_type != "follow_up":
                continue
            person = _resolve_person(db, candidate, people_by_key)
            if person is None:
                raise VetBizImportError(
                    "Approve or select the related contact before committing a follow-up."
                )
            data = candidate.extracted_data
            due_date = _parse_optional_date(data.get("due_date", ""))
            follow_up = FollowUpSuggestion(
                person_id=person.id,
                summary=data.get("summary") or "Follow up after the VetBiz meeting.",
                due_date=due_date,
                source_import_id=record.id,
                source_candidate_id=candidate.id,
                source_excerpt=candidate.source_excerpt,
            )
            db.add(follow_up)
            db.flush()
            if due_date and (person.next_followup is None or due_date < person.next_followup):
                person.next_followup = due_date
            _mark_committed(candidate, "follow_up_suggestion", follow_up.id)
            counts["follow_ups"] = counts.get("follow_ups", 0) + 1

        for candidate in approved:
            if candidate.candidate_type != "opportunity":
                continue
            data = candidate.extracted_data
            person = _resolve_person(db, candidate, people_by_key)
            organization = organizations_by_key.get(data.get("organization_key", ""))
            signal = signals_by_key.get(data.get("source_signal_key"))
            opportunity = Opportunity(
                title=data.get("title") or "Possible Remulous Labs fit",
                person_id=person.id if person else None,
                organization_id=organization.id if organization else None,
                product=data.get("product") or None,
                stage=data.get("stage") or "identified",
                next_action=data.get("next_action") or None,
                notes=data.get("notes") or None,
                source_signal_id=signal.id if signal else None,
                source_import_id=record.id,
                source_candidate_id=candidate.id,
                source_excerpt=candidate.source_excerpt,
            )
            db.add(opportunity)
            db.flush()
            _mark_committed(candidate, "opportunity", opportunity.id)
            counts["opportunities"] = counts.get("opportunities", 0) + 1

        for candidate in approved:
            if candidate.candidate_type != "connection_suggestion":
                continue
            data = candidate.extracted_data
            source_person = people_by_key.get(data.get("source_contact_key", ""))
            target_person = people_by_key.get(data.get("target_contact_key", ""))
            if source_person is None or target_person is None:
                raise VetBizImportError(
                    "Approve both related contacts before committing a connection suggestion."
                )
            if source_person.id == target_person.id:
                raise VetBizImportError("A person cannot be introduced to themselves.")
            signal_ids = [
                signal.id
                for signal_key in (
                    data.get("ask_signal_key"),
                    data.get("offer_signal_key"),
                )
                if (signal := signals_by_key.get(signal_key)) is not None
            ]
            suggestion = ConnectionSuggestion(
                source_person_id=source_person.id,
                target_person_id=target_person.id,
                reason=data.get("reason") or "Reviewed ask/offer alignment.",
                supporting_signal_ids=signal_ids,
                source_import_id=record.id,
                source_candidate_id=candidate.id,
                source_excerpt=candidate.source_excerpt,
            )
            db.add(suggestion)
            db.flush()
            _mark_committed(candidate, "connection_suggestion", suggestion.id)
            counts["connections"] = counts.get("connections", 0) + 1

        record.import_status = "committed"
        record.committed_at = datetime.now(timezone.utc)
        db.commit()
        return counts
    except Exception:
        db.rollback()
        raise
