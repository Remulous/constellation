from pathlib import Path
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.database import get_db
from app.main import app
from app.models import (
    ConnectionSuggestion,
    ContactMethod,
    Interaction,
    Opportunity,
    Organization,
    Person,
    RelationshipSignal,
    VetBizImportCandidate,
    VetBizImportRecord,
)
from app.services.vetbiz_imports import (
    VetBizImportError,
    commit_reviewed_import,
    create_reviewed_import,
    extract_rtf_text,
    parse_reviewed_minutes,
    propose_connection_suggestion,
    propose_opportunity_from_signal,
    update_candidate,
)


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def candidate(record: VetBizImportRecord, candidate_type: str, name: str = ""):
    return next(
        item
        for item in record.candidates
        if item.candidate_type == candidate_type
        and (not name or item.extracted_data.get("display_name") == name)
    )


def test_markdown_plain_text_and_missing_fields_parse():
    markdown = parse_reviewed_minutes(
        "minutes.md", fixture("vetbiz_reviewed_minutes.md")
    )
    assert markdown.meeting_date.isoformat() == "2026-07-29"
    assert markdown.meeting_title == "Fictional Service Academy VetBiz Reviewed Minutes"
    assert len(markdown.participants) == 4
    assert markdown.participants[0].email == "alex.carter@example.test"
    assert "WARN" in markdown.participants[0].ask

    plain = parse_reviewed_minutes(
        "minutes.txt", fixture("vetbiz_reviewed_minutes.txt")
    )
    assert len(plain.participants) == 2
    assert plain.participants[1].organization == ""
    assert plain.participants[1].email == ""


def test_rtf_parser_preserves_tables_and_ignores_unsupported_formatting():
    parsed = parse_reviewed_minutes(
        "minutes.rtf", fixture("vetbiz_reviewed_minutes.rtf")
    )
    assert parsed.meeting_date.isoformat() == "2026-07-29"
    assert len(parsed.participants) == 2
    assert parsed.participants[0].display_name == "Riley Chen"
    assert parsed.participants[0].organization == "Harbor Fabrication"
    assert parsed.participants[0].email == "riley.chen@example.test"
    assert "89504e" not in parsed.raw_text
    assert "manufacturing consulting" in parsed.participants[0].offer


@pytest.mark.parametrize(
    "payload",
    [
        b"{\\rtf1\\ansi unbalanced",
        b"{\\rtf1\\ansi {\\object malicious}}",
        b"{\\rtf1\\ansi {\\objdata 414141}}",
        b"{\\rtf1\\ansi \\'zz}",
    ],
)
def test_malformed_or_suspicious_rtf_is_rejected(payload):
    with pytest.raises(VetBizImportError):
        extract_rtf_text(payload)


def test_review_confirmation_and_file_limits_are_required(db):
    data = fixture("vetbiz_reviewed_minutes.md")
    with pytest.raises(VetBizImportError, match="human review"):
        create_reviewed_import(db, "minutes.md", data, review_confirmed=False)
    with pytest.raises(VetBizImportError, match="upload limit"):
        create_reviewed_import(
            db, "minutes.md", data, review_confirmed=True, max_bytes=10
        )
    assert db.scalar(select(func.count()).select_from(VetBizImportRecord)) == 0


def test_import_is_staged_matches_exact_email_and_detects_duplicates(db):
    existing = Person(
        display_name="Alex Carter",
        current_organization="Harbor Analytics",
        primary_email="alex.carter@example.test",
    )
    existing.methods.append(
        ContactMethod(
            method_type="email",
            value="alex.carter@example.test",
            normalized_value="alex.carter@example.test",
            source="manual",
        )
    )
    db.add(existing)
    db.add_all(
        [
            Person(display_name="Jordan Smith", current_organization="Other One"),
            Person(display_name="Jordan Smith", current_organization="Other Two"),
        ]
    )
    db.commit()

    data = fixture("vetbiz_reviewed_minutes.md")
    creation = create_reviewed_import(
        db,
        "../unsafe/minutes.md",
        data,
        review_confirmed=True,
        review_notes="Final review complete",
    )
    record = creation.record
    assert record.filename == "minutes.md"
    assert record.review_confirmed is True
    assert record.source_type == "vetbiz_reviewed_minutes"
    assert candidate(record, "contact_update", "Alex Carter").match_reason == "exact email"
    ambiguous = candidate(record, "new_contact", "Jordan Smith")
    assert ambiguous.match_reason == "ambiguous exact full name"
    assert len(ambiguous.extracted_data["match_options"]) == 2

    # Parsing creates only audit records and proposals.
    assert db.scalar(select(func.count()).select_from(Person)) == 3
    assert db.scalar(select(func.count()).select_from(Interaction)) == 0
    assert db.scalar(select(func.count()).select_from(Opportunity)) == 0
    assert db.scalar(select(func.count()).select_from(ConnectionSuggestion)) == 0

    repeated = create_reviewed_import(
        db, "renamed.md", data, review_confirmed=True
    )
    assert repeated.exact_duplicate is True
    assert repeated.record.id == record.id
    assert db.scalar(select(func.count()).select_from(VetBizImportRecord)) == 1


def test_candidate_approval_rejection_and_provenance_commit(db):
    existing = Person(
        display_name="Alex Carter",
        current_organization="Harbor Analytics",
        primary_email="alex.carter@example.test",
    )
    existing.methods.append(
        ContactMethod(
            method_type="email",
            value="alex.carter@example.test",
            normalized_value="alex.carter@example.test",
            source="manual",
        )
    )
    db.add(existing)
    db.commit()
    record = create_reviewed_import(
        db,
        "minutes.md",
        fixture("vetbiz_reviewed_minutes.md"),
        review_confirmed=True,
    ).record

    alex_contact = candidate(record, "contact_update", "Alex Carter")
    alex_interaction = next(
        item
        for item in record.candidates
        if item.candidate_type == "interaction"
        and item.extracted_data["contact_key"]
        == alex_contact.extracted_data["contact_key"]
    )
    alex_signal = next(
        item
        for item in record.candidates
        if item.candidate_type == "signal"
        and "WARN" in item.extracted_data["summary"]
    )
    alex_opportunity = next(
        item
        for item in record.candidates
        if item.candidate_type == "opportunity"
        and item.extracted_data["product"] == "LayoffLens"
    )
    organization = next(
        item
        for item in record.candidates
        if item.candidate_type == "organization"
        and item.extracted_data["name"] == "Harbor Analytics"
    )

    update_candidate(
        alex_contact,
        "approve",
        {"title": "Founder"},
        matched_entity_id=existing.id,
    )
    update_candidate(
        alex_interaction,
        "approve",
        {"summary": "Discussed a reviewed public-data workflow."},
        matched_entity_id=existing.id,
    )
    update_candidate(alex_signal, "approve", {})
    update_candidate(alex_opportunity, "approve", {"stage": "potential_fit"})
    update_candidate(organization, "approve", {})
    rejected = next(
        item for item in record.candidates if item.status == "pending"
    )
    update_candidate(rejected, "reject", {}, resolution_notes="Not useful")
    db.commit()

    counts = commit_reviewed_import(db, record)
    assert counts["contacts"] == 1
    assert counts["interactions"] == 1
    assert counts["signals"] == 1
    assert counts["opportunities"] == 1
    assert counts["organizations"] == 1
    assert record.import_status == "committed"

    interaction = db.scalar(select(Interaction))
    signal = db.scalar(select(RelationshipSignal))
    opportunity = db.scalar(select(Opportunity))
    assert interaction.source_import_id == record.id
    assert interaction.source_candidate_id == alex_interaction.id
    assert interaction.source_excerpt
    assert signal.source_import_id == record.id
    assert opportunity.source_import_id == record.id
    assert opportunity.product == "LayoffLens"
    assert opportunity.stage == "potential_fit"
    assert rejected.status == "rejected"
    assert rejected.committed_entity_id is None


def test_connection_suggestion_requires_approval_and_never_introduces(db):
    record = create_reviewed_import(
        db,
        "minutes.rtf",
        fixture("vetbiz_reviewed_minutes.rtf"),
        review_confirmed=True,
    ).record
    connection = candidate(record, "connection_suggestion")
    assert db.scalar(select(func.count()).select_from(ConnectionSuggestion)) == 0

    related_keys = {
        connection.extracted_data["source_contact_key"],
        connection.extracted_data["target_contact_key"],
    }
    for item in record.candidates:
        if (
            item.candidate_type in {"new_contact", "contact_update", "signal"}
            and item.extracted_data.get("contact_key") in related_keys
        ):
            update_candidate(item, "approve", {})
    update_candidate(connection, "approve", {})
    db.commit()
    commit_reviewed_import(db, record)

    created = db.scalar(select(ConnectionSuggestion))
    assert created is not None
    assert created.status == "suggested"
    assert "Review both statements before introducing" in created.reason


def test_atomic_commit_rolls_back_when_dependency_is_not_approved(db):
    record = create_reviewed_import(
        db,
        "minutes.txt",
        fixture("vetbiz_reviewed_minutes.txt"),
        review_confirmed=True,
    ).record
    interaction = candidate(record, "interaction")
    update_candidate(interaction, "approve", {})
    db.commit()

    with pytest.raises(VetBizImportError, match="related contact"):
        commit_reviewed_import(db, record)
    assert db.scalar(select(func.count()).select_from(Person)) == 0
    assert db.scalar(select(func.count()).select_from(Interaction)) == 0
    refreshed = db.get(VetBizImportRecord, record.id)
    assert refreshed.import_status == "pending_review"
    assert db.get(VetBizImportCandidate, interaction.id).status == "approved"


def test_same_meeting_revision_reuses_sourced_interactions(db):
    original = b"""# Fictional VetBiz Minutes

July 29, 2026

| Name | Organization | Notes | Contact |
| --- | --- | --- | --- |
| Avery Stone | Stone Works | Shared a reviewed update. | avery@example.test |
"""
    revision = original.replace(b"reviewed update", b"reviewed update and an extra note")
    first = create_reviewed_import(
        db, "minutes.md", original, review_confirmed=True
    ).record
    first_contact = candidate(first, "new_contact")
    first_interaction = candidate(first, "interaction")
    update_candidate(first_contact, "approve", {})
    update_candidate(first_interaction, "approve", {})
    db.commit()
    commit_reviewed_import(db, first)

    second_creation = create_reviewed_import(
        db, "minutes-v2.md", revision, review_confirmed=True
    )
    assert second_creation.revision_warning is True
    assert second_creation.record.revision_of_id == first.id


def test_user_can_create_opportunity_and_connection_proposals(db):
    record = create_reviewed_import(
        db,
        "minutes.rtf",
        fixture("vetbiz_reviewed_minutes.rtf"),
        review_confirmed=True,
    ).record
    signal = candidate(record, "signal")
    opportunity = propose_opportunity_from_signal(record, signal)
    assert opportunity.candidate_type == "opportunity"
    assert opportunity.status == "pending"
    assert opportunity.extracted_data["source_signal_key"] == signal.extracted_data["signal_key"]

    contacts = [
        item
        for item in record.candidates
        if item.candidate_type in {"new_contact", "contact_update"}
    ]
    connection = propose_connection_suggestion(
        record, contacts[0], contacts[1], "One stated a need the other may address."
    )
    assert connection.candidate_type == "connection_suggestion"
    assert connection.status == "pending"
    assert db.scalar(select(func.count()).select_from(Opportunity)) == 0
    assert db.scalar(select(func.count()).select_from(ConnectionSuggestion)) == 0


def test_http_review_workflow_requires_confirmation_and_escapes_source(db):
    app.dependency_overrides[get_db] = lambda: db
    malicious = b"""# Fictional VetBiz Reviewed Minutes

July 29, 2026

| Name | Organization | Notes | Contact |
| --- | --- | --- | --- |
| Avery Stone | Stone Works | <script>alert('x')</script> | avery@example.test |
"""
    try:
        with TestClient(app, base_url="https://testserver") as client:
            page = client.get("/vetbiz-imports")
            token = re.search(
                r'name="csrf_token" value="([^"]+)"', page.text
            ).group(1)
            rejected = client.post(
                "/vetbiz-imports",
                data={"csrf_token": token, "minutes_text": malicious.decode()},
            )
            assert rejected.status_code == 400
            assert "human review" in rejected.text
            assert db.scalar(select(func.count()).select_from(VetBizImportRecord)) == 0

            accepted = client.post(
                "/vetbiz-imports",
                data={
                    "csrf_token": token,
                    "minutes_text": malicious.decode(),
                    "review_confirmed": "true",
                    "review_notes": "Final review",
                },
                follow_redirects=False,
            )
            assert accepted.status_code == 303
            review = client.get(accepted.headers["location"])
            assert review.status_code == 200
            assert "&lt;script&gt;" in review.text
            assert "<script>alert" not in review.text
            assert "Final review" in review.text
            assert "Commit 0 approved changes" in review.text
    finally:
        app.dependency_overrides.clear()
