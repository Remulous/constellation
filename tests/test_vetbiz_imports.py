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
    ExternalIdentity,
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
    bulk_decide_candidates,
    commit_reviewed_import,
    create_reviewed_import,
    delete_pending_import,
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
    assert markdown.participants[0].display_name == "Alex Carter"
    assert markdown.participants[0].last_name == "Carter"
    assert markdown.participants[0].affiliation == "'08"
    assert markdown.participants[0].email == "alex.carter@example.test"
    assert markdown.participants[0].website == ""
    assert (
        markdown.participants[0].linkedin_url
        == "https://linkedin.com/in/alex-carter-vetbiz"
    )
    assert "WARN" in markdown.participants[0].ask

    plain = parse_reviewed_minutes(
        "minutes.txt", fixture("vetbiz_reviewed_minutes.txt")
    )
    assert len(plain.participants) == 2
    assert plain.participants[1].organization == ""
    assert plain.participants[1].email == ""


@pytest.mark.parametrize(
    ("contact_lines", "expected_website"),
    [
        ("Email: avery@harbor.example", ""),
        (
            "Contact: avery@harbor.example; https://stone-works.example/team",
            "https://stone-works.example/team",
        ),
        ("Website: www.stone-works.example", "www.stone-works.example"),
    ],
)
def test_website_requires_an_explicit_url(contact_lines, expected_website):
    parsed = parse_reviewed_minutes(
        "minutes.txt",
        (
            "Fictional VetBiz Reviewed Minutes\n"
            "July 29, 2026\n\n"
            "Name: Avery Stone\n"
            "Organization: Stone Works\n"
            f"{contact_lines}\n"
        ).encode(),
    )
    assert parsed.participants[0].website == expected_website


def test_linkedin_profile_is_separate_and_matches_existing_identity(db):
    person = Person(display_name="Avery Stone")
    db.add(person)
    db.flush()
    db.add(
        ExternalIdentity(
            person_id=person.id,
            provider="linkedin",
            provider_record_id="https://linkedin.com/in/avery-stone",
            profile_url="https://linkedin.com/in/avery-stone",
            source_payload={},
            record_hash="linkedin-existing",
        )
    )
    db.commit()

    record = create_reviewed_import(
        db,
        "minutes.txt",
        (
            "Fictional VetBiz Reviewed Minutes\n"
            "July 29, 2026\n\n"
            "Name: Avery Stone\n"
            "Organization: Different Organization\n"
            "LinkedIn: www.linkedin.com/in/avery-stone/\n"
        ).encode(),
        review_confirmed=True,
    ).record
    contact = candidate(record, "contact_update", "Avery Stone")
    assert contact.match_reason == "exact external identifier"
    assert contact.matched_entity_id == person.id
    assert (
        contact.extracted_data["linkedin_url"]
        == "https://linkedin.com/in/avery-stone"
    )
    assert contact.extracted_data["website"] == ""


@pytest.mark.parametrize(
    ("reviewed_name", "expected_affiliation"),
    [
        ("Avery Stone ‘09", "'09"),
        ("Avery Stone ’09", "'09"),
        ("Avery Stone '09", "'09"),
        ("Avery Stone, USNA ‘09", "USNA '09"),
        ("Avery Stone (USMA '09)", "USMA '09"),
        ("Avery Stone Class of 2009", "2009"),
    ],
)
def test_class_year_is_affiliation_not_last_name(
    reviewed_name, expected_affiliation
):
    parsed = parse_reviewed_minutes(
        "minutes.txt",
        (
            "Fictional VetBiz Reviewed Minutes\n"
            "July 29, 2026\n\n"
            f"Name: {reviewed_name}\n"
            "Organization: Stone Works\n"
        ).encode(),
    )
    participant = parsed.participants[0]
    assert participant.display_name == "Avery Stone"
    assert participant.first_name == "Avery"
    assert participant.last_name == "Stone"
    assert participant.affiliation == expected_affiliation


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


def test_rtf_parser_handles_vertical_roster_cells_and_prefers_labeled_date():
    data = br"""{\rtf1\ansi
VetBiz Meeting Recap\par
USNA Hampton Roads | August 2026 | Working draft for human review\par
Next VetBiz meeting: September 2, 2026\par
Participant / Class or Affiliation\cell
Organization / Role\cell
Email\cell
LinkedIn / Website\cell
Other Relevant Contact\cell\row
David Duffie '75; submarine veteran\cell
Training Modernization Group; Alumni Association representative\cell
duffieda@example.test\cell
Not provided\cell
401-369-5823\cell\row
Tara \b  Feher\b0  '03; former helicopter pilot\cell
Psionic; Blue and Gold Officer\cell
Not provided\cell
https://www.linkedin.com/in/tara-feher/\cell
Not provided\cell\row
Date:\par
August 5, 2026 (verify before distribution)\par
}"""

    parsed = parse_reviewed_minutes("working-notes.rtf", data)

    assert parsed.meeting_date.isoformat() == "2026-08-05"
    assert [participant.display_name for participant in parsed.participants] == [
        "David Duffie",
        "Tara Feher",
    ]
    assert parsed.participants[0].affiliation == "'75; submarine veteran"
    assert parsed.participants[0].phone == "401-369-5823"
    assert parsed.participants[0].website == ""
    assert parsed.participants[1].last_name == "Feher"
    assert parsed.participants[1].affiliation == "'03; former helicopter pilot"
    assert parsed.participants[1].email == ""
    assert (
        parsed.participants[1].linkedin_url
        == "https://linkedin.com/in/tara-feher"
    )


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
    linkedin_identity = db.scalar(
        select(ExternalIdentity).where(
            ExternalIdentity.person_id == existing.id,
            ExternalIdentity.provider == "linkedin",
        )
    )
    signal = db.scalar(select(RelationshipSignal))
    opportunity = db.scalar(select(Opportunity))
    assert interaction.source_import_id == record.id
    assert interaction.source_candidate_id == alex_interaction.id
    assert interaction.source_excerpt
    assert (
        linkedin_identity.profile_url
        == "https://linkedin.com/in/alex-carter-vetbiz"
    )
    assert linkedin_identity.source_payload["source_import_id"] == record.id
    assert linkedin_identity.source_payload["source_candidate_id"] == alex_contact.id
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


def test_bulk_decision_updates_only_unresolved_candidates(db):
    record = create_reviewed_import(
        db,
        "minutes.md",
        fixture("vetbiz_reviewed_minutes.md"),
        review_confirmed=True,
    ).record
    signals = [
        item for item in record.candidates if item.candidate_type == "signal"
    ]
    update_candidate(signals[0], "reject", {})
    update_candidate(signals[1], "save", {"summary": "Reviewed wording"})

    decided = bulk_decide_candidates(record, {"signal"}, "approve")

    assert decided == len(signals) - 1
    assert signals[0].status == "rejected"
    assert all(item.status == "approved" for item in signals[1:])
    assert all(item.resolved_at is not None for item in signals)


def test_committed_review_cannot_be_deleted(db):
    record = create_reviewed_import(
        db,
        "minutes.md",
        fixture("vetbiz_reviewed_minutes.md"),
        review_confirmed=True,
    ).record
    record.import_status = "committed"

    with pytest.raises(VetBizImportError, match="audit trail"):
        delete_pending_import(db, record)

    assert db.get(VetBizImportRecord, record.id) is record


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

            record = db.scalar(select(VetBizImportRecord))
            review_candidate = db.scalar(
                select(VetBizImportCandidate)
                .where(VetBizImportCandidate.import_record_id == record.id)
                .order_by(VetBizImportCandidate.id)
            )
            anchor = f"candidate-{review_candidate.id}"
            assert f'id="{anchor}"' in review.text
            assert f"/vetbiz-imports/{record.id}/groups/contacts" in review.text

            bulk = client.post(
                f"/vetbiz-imports/{record.id}/groups/contacts",
                data={"csrf_token": token, "action": "approve"},
                follow_redirects=False,
            )
            assert bulk.status_code == 303
            assert bulk.headers["location"].endswith(
                "?bulk_group=contacts&bulk_action=approve&bulk_count=1#contacts"
            )

            decision = client.post(
                f"/vetbiz-imports/{record.id}/candidates/{review_candidate.id}",
                data={"csrf_token": token, "action": "reject"},
                follow_redirects=False,
            )
            assert decision.status_code == 303
            assert decision.headers["location"].endswith(f"?saved=1#{anchor}")

            deleted = client.post(
                f"/vetbiz-imports/{record.id}/delete",
                data={"csrf_token": token},
                follow_redirects=False,
            )
            assert deleted.status_code == 303
            assert deleted.headers["location"] == "/vetbiz-imports?deleted=1"
            assert db.get(VetBizImportRecord, record.id) is None
            assert (
                db.scalar(select(func.count()).select_from(VetBizImportCandidate))
                == 0
            )
            assert db.scalar(select(func.count()).select_from(Person)) == 0
    finally:
        app.dependency_overrides.clear()
