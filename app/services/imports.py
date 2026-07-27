from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ContactMethod, Employment, ExternalIdentity, ImportBatch, MergeCandidate, Person, Tag
from app.services.normalize import (
    normalize_email,
    normalize_linkedin_url,
    normalize_name,
    normalize_org,
    normalize_phone,
)


@dataclass
class ImportedRecord:
    provider: str
    first_name: str = ""
    middle_name: str = ""
    last_name: str = ""
    display_name: str = ""
    emails: list[tuple[str, str]] = field(default_factory=list)
    phones: list[tuple[str, str]] = field(default_factory=list)
    profile_url: str = ""
    organization: str = ""
    title: str = ""
    connection_date: str = ""
    tags: list[str] = field(default_factory=list)
    payload: dict = field(default_factory=dict)

    @property
    def record_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.payload, sort_keys=True).encode()).hexdigest()


def decode_csv(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("Unsupported CSV encoding")


def _value(row: dict, *names: str) -> str:
    folded = {str(k).strip().casefold(): (v or "").strip() for k, v in row.items()}
    for name in names:
        if folded.get(name.casefold()):
            return folded[name.casefold()]
    return ""


def parse_google(data: bytes) -> list[ImportedRecord]:
    reader = csv.DictReader(io.StringIO(decode_csv(data), newline=""))
    records = []
    for row in reader:
        first = _value(row, "First Name", "Given Name")
        middle = _value(row, "Middle Name", "Additional Name")
        last = _value(row, "Last Name", "Family Name")
        display = _value(row, "Name", "Full Name") or " ".join(x for x in (first, middle, last) if x)
        emails, phones = [], []
        for key, val in row.items():
            if not val:
                continue
            k = str(key).casefold()
            if ("e-mail" in k or "email" in k) and "value" in k:
                label = key.rsplit(" ", 1)[0]
                emails.append((val.strip(), label))
            elif "phone" in k and "value" in k:
                label = key.rsplit(" ", 1)[0]
                phones.append((val.strip(), label))
        labels = _value(row, "Labels", "Group Membership", "Groups")
        records.append(ImportedRecord(
            provider="google_contacts", first_name=first, middle_name=middle, last_name=last,
            display_name=display, emails=emails, phones=phones,
            organization=_value(row, "Organization 1 - Name", "Organization Name"),
            title=_value(row, "Organization 1 - Title", "Organization Title"),
            profile_url=_value(row, "Website 1 - Value"),
            tags=[part.strip() for part in labels.replace(" ::: ", ";").split(";") if part.strip()],
            payload=row,
        ))
    return records


def parse_linkedin(data: bytes) -> list[ImportedRecord]:
    text = decode_csv(data)
    lines = text.splitlines()
    header_index = next((i for i, line in enumerate(lines) if "First Name" in line and "Last Name" in line), 0)
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_index:]), newline=""))
    return [
        ImportedRecord(
            provider="linkedin",
            first_name=_value(row, "First Name"),
            last_name=_value(row, "Last Name"),
            display_name=" ".join(x for x in (_value(row, "First Name"), _value(row, "Last Name")) if x),
            emails=[(_value(row, "Email Address"), "LinkedIn")] if _value(row, "Email Address") else [],
            profile_url=_value(row, "URL", "Profile URL"),
            organization=_value(row, "Company"),
            title=_value(row, "Position"),
            connection_date=_value(row, "Connected On"),
            payload=row,
        )
        for row in reader
    ]


def _match_person(db: Session, record: ImportedRecord) -> tuple[Person | None, str]:
    url = normalize_linkedin_url(record.profile_url)
    if url:
        identity = db.scalar(select(ExternalIdentity).where(ExternalIdentity.profile_url == url))
        if identity:
            return db.get(Person, identity.person_id), "exact LinkedIn URL"
    for email, _ in record.emails:
        norm = normalize_email(email)
        method = db.scalar(select(ContactMethod).where(
            ContactMethod.method_type == "email", ContactMethod.normalized_value == norm
        ))
        if method:
            return db.get(Person, method.person_id), "exact email"
    for phone, _ in record.phones:
        norm = normalize_phone(phone)
        method = db.scalar(select(ContactMethod).where(
            ContactMethod.method_type == "phone", ContactMethod.normalized_value == norm
        ))
        if method:
            return db.get(Person, method.person_id), "exact phone"
    name = normalize_name(record.display_name)
    org = normalize_org(record.organization)
    if name and org:
        candidates = db.scalars(select(Person).where(Person.archived_at.is_(None))).all()
        for person in candidates:
            if normalize_name(person.display_name) == name and normalize_org(person.current_organization) == org:
                return person, "exact name and organization"
    title = normalize_name(record.title)
    if name and title:
        candidates = db.scalars(select(Person).where(Person.archived_at.is_(None))).all()
        for person in candidates:
            if normalize_name(person.display_name) == name and normalize_name(person.current_title) == title:
                return person, "exact name and title"
    return None, ""


def _add_methods(person: Person, record: ImportedRecord) -> None:
    existing = {(m.method_type, m.normalized_value) for m in person.methods}
    for method_type, values, normalizer in (
        ("email", record.emails, normalize_email),
        ("phone", record.phones, normalize_phone),
    ):
        for value, label in values:
            norm = normalizer(value)
            if norm and (method_type, norm) not in existing:
                person.methods.append(ContactMethod(
                    method_type=method_type, value=value, normalized_value=norm,
                    label=label, source=record.provider, primary=not any(m.method_type == method_type for m in person.methods),
                ))
                existing.add((method_type, norm))


def import_csv(db: Session, provider: str, filename: str, data: bytes) -> ImportBatch:
    digest = hashlib.sha256(data).hexdigest()
    prior = db.scalar(select(ImportBatch).where(
        ImportBatch.provider == provider, ImportBatch.file_hash == digest, ImportBatch.status == "complete"
    ))
    if prior:
        return prior
    batch = ImportBatch(provider=provider, original_filename=filename, file_hash=digest)
    db.add(batch)
    db.flush()
    try:
        records = parse_google(data) if provider == "google_contacts" else parse_linkedin(data)
        batch.row_count = len(records)
        if provider == "linkedin":
            for identity in db.scalars(select(ExternalIdentity).where(ExternalIdentity.provider == provider)):
                identity.active = False
        for record in records:
            if not record.display_name:
                batch.skipped_rows += 1
                continue
            person, reason = _match_person(db, record)
            if person:
                batch.exact_matches += 1
                batch.updated_contacts += 1
            else:
                person = Person(
                    display_name=record.display_name, first_name=record.first_name or None,
                    middle_name=record.middle_name or None, last_name=record.last_name or None,
                    primary_email=normalize_email(record.emails[0][0]) if record.emails else None,
                    primary_phone=normalize_phone(record.phones[0][0]) if record.phones else None,
                    current_organization=record.organization or None, current_title=record.title or None,
                )
                db.add(person)
                db.flush()
                batch.created_contacts += 1
                # Name-only similarities are review candidates, never automatic merges.
                same_name = db.scalars(select(Person).where(
                    Person.display_name == record.display_name, Person.id != person.id
                )).first()
                if same_name:
                    batch.possible_matches += 1
            _add_methods(person, record)
            for tag_name in record.tags:
                tag = db.scalar(select(Tag).where(func.lower(Tag.name) == tag_name.casefold()))
                if not tag:
                    tag = Tag(name=tag_name, description="Imported from Google Contacts")
                    db.add(tag)
                if tag not in person.tags:
                    person.tags.append(tag)
            if record.organization and not any(
                e.organization == record.organization and e.title == record.title for e in person.employments
            ):
                person.employments.append(Employment(
                    organization=record.organization, title=record.title or None,
                    current=True, source=provider,
                ))
            url = normalize_linkedin_url(record.profile_url)
            provider_id = url or f"{record.record_hash}:{batch.id}"
            identity = db.scalar(select(ExternalIdentity).where(
                ExternalIdentity.provider == provider,
                ExternalIdentity.provider_record_id == provider_id,
            ))
            if not identity:
                identity = ExternalIdentity(
                    person_id=person.id, provider=provider, provider_record_id=provider_id,
                    profile_url=url or None, source_payload=record.payload,
                    record_hash=record.record_hash, import_batch_id=batch.id,
                )
                db.add(identity)
                db.flush()
            else:
                identity.person_id = person.id
                identity.source_payload = record.payload
                identity.record_hash = record.record_hash
                identity.import_batch_id = batch.id
            identity.active = True
            identity.last_imported_at = datetime.now().astimezone()
            if not reason:
                candidate = db.scalars(select(Person).where(
                    Person.display_name == record.display_name, Person.id != person.id
                )).first()
                if candidate:
                    db.add(MergeCandidate(
                        source_identity_id=identity.id, candidate_person_id=candidate.id,
                        match_reason="same normalized display name; manual review required",
                        confidence_score=0.55,
                    ))
        batch.status = "complete"
        db.commit()
        return batch
    except Exception as exc:
        db.rollback()
        batch = ImportBatch(
            provider=provider, original_filename=filename, file_hash=digest,
            status="failed", error_log=str(exc),
        )
        db.add(batch)
        db.commit()
        raise
