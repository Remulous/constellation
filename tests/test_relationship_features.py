import csv
import io
import re
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import get_db
from app.main import app
from app.models import ContactMethod, ExternalIdentity, Interaction, Person, SavedSegment, Tag


def csrf_token(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def test_google_export_and_linkedin_shortcuts(db):
    person = Person(
        display_name="Ada Lovelace",
        first_name="Ada",
        last_name="Lovelace",
        primary_email="ada@example.com",
        primary_phone="+15551234567",
        current_organization="Analytical Engines",
        current_title="Founder",
        location="London",
        general_note="Ask about the next machine.",
    )
    person.methods.append(ContactMethod(
        method_type="email",
        value="ada@work.example",
        normalized_value="ada@work.example",
        label="Work",
        source="manual",
    ))
    person.identities.append(ExternalIdentity(
        provider="linkedin",
        provider_record_id="ada-linkedin",
        profile_url="https://www.linkedin.com/in/ada-lovelace",
        source_payload={},
        record_hash="ada-hash",
        active=True,
    ))
    person.tags.append(Tag(name="Founder"))
    db.add(person)
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app, base_url="https://testserver") as client:
            people = client.get("/people")
            assert people.status_code == 200
            assert 'href="https://www.linkedin.com/in/ada-lovelace"' in people.text
            assert f'href="/people/{person.id}/brief"' in people.text

            response = client.get("/export/google.csv")
            assert response.status_code == 200
            assert "constellation-google-contacts.csv" in response.headers["content-disposition"]
            rows = list(csv.DictReader(io.StringIO(response.text)))
            assert len(rows) == 1
            row = rows[0]
            assert row["First Name"] == "Ada"
            assert row["Email 1 - Value"] == "ada@example.com"
            assert row["Email 2 - Value"] == "ada@work.example"
            assert row["Website 1 - Label"] == "LinkedIn"
            assert row["Website 1 - Value"] == "https://www.linkedin.com/in/ada-lovelace"
            assert row["Organization Name"] == "Analytical Engines"
            assert row["Labels"] == "Founder"
    finally:
        app.dependency_overrides.clear()


def test_person_linkedin_profile_can_be_added_and_removed(db):
    person = Person(display_name="Avery Stone")
    other = Person(display_name="Morgan Lee")
    db.add_all([person, other])
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app, base_url="https://testserver") as client:
            profile = client.get(f"/people/{person.id}")
            token = csrf_token(profile.text)
            assert "Add LinkedIn" in profile.text

            invalid = client.post(
                f"/people/{person.id}/linkedin",
                data={
                    "csrf_token": token,
                    "action": "save",
                    "linkedin_url": "https://example.com/avery",
                },
            )
            assert invalid.status_code == 400

            added = client.post(
                f"/people/{person.id}/linkedin",
                data={
                    "csrf_token": token,
                    "action": "save",
                    "linkedin_url": "www.linkedin.com/in/avery-stone/",
                },
                follow_redirects=False,
            )
            assert added.status_code == 303
            assert added.headers["location"].endswith(
                "?linkedin=saved#linkedin-profile"
            )
            db.expire_all()
            identity = db.scalar(
                select(ExternalIdentity).where(
                    ExternalIdentity.person_id == person.id,
                    ExternalIdentity.provider == "linkedin",
                )
            )
            assert identity.profile_url == "https://linkedin.com/in/avery-stone"
            assert identity.source_payload["source"] == "manual_profile_edit"
            assert identity.active is True

            duplicate = client.post(
                f"/people/{other.id}/linkedin",
                data={
                    "csrf_token": token,
                    "action": "save",
                    "linkedin_url": "https://linkedin.com/in/avery-stone",
                },
            )
            assert duplicate.status_code == 400

            removed = client.post(
                f"/people/{person.id}/linkedin",
                data={"csrf_token": token, "action": "remove"},
                follow_redirects=False,
            )
            assert removed.status_code == 303
            assert removed.headers["location"].endswith(
                "?linkedin=removed#linkedin-profile"
            )
            db.expire_all()
            assert db.get(ExternalIdentity, identity.id).active is False
    finally:
        app.dependency_overrides.clear()


def test_today_meeting_brief_and_data_quality(db):
    today = date.today()
    due = Person(
        display_name="Grace Hopper",
        current_organization="US Navy",
        current_title="Rear Admiral",
        priority="high",
        next_followup=today,
        last_meaningful_interaction=today - timedelta(days=3),
        general_note="Pioneered practical compiler work.",
    )
    due.interactions.append(Interaction(
        interaction_type="Meeting",
        interaction_date=today - timedelta(days=3),
        summary="Discussed making systems easier to use",
        meaningful=True,
    ))
    upcoming = Person(
        display_name="Katherine Johnson",
        next_followup=today + timedelta(days=4),
    )
    db.add_all([due, upcoming])
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app, base_url="https://testserver") as client:
            page = client.get("/today")
            assert page.status_code == 200
            assert "Grace Hopper" in page.text
            assert "Katherine Johnson" in page.text
            assert "Recent momentum" in page.text

            brief = client.get(f"/people/{due.id}/brief")
            assert brief.status_code == 200
            assert "Meeting preparation brief" in brief.text
            assert "What has changed at US Navy" in brief.text
            assert "Discussed making systems easier to use" in brief.text

            quality = client.get("/data-quality?issue=missing_email")
            assert quality.status_code == 200
            assert "Missing email" in quality.text
            assert "Grace Hopper" in quality.text
            assert "Katherine Johnson" in quality.text
    finally:
        app.dependency_overrides.clear()


def test_saved_segments_preserve_people_filters(db):
    db.add_all([
        Person(display_name="Priority Person", priority="high"),
        Person(display_name="Normal Person", priority="normal"),
    ])
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app, base_url="https://testserver") as client:
            people = client.get("/people?priority=high&followup=never_contacted")
            token = csrf_token(people.text)
            response = client.post(
                "/segments",
                data={
                    "csrf_token": token,
                    "name": "High priority, never contacted",
                    "priority": "high",
                    "followup": "never_contacted",
                    "sort": "name",
                },
                follow_redirects=False,
            )
            assert response.status_code == 303
            segment = db.scalar(select(SavedSegment))
            assert segment.filters == {
                "priority": "high",
                "followup": "never_contacted",
            }

            page = client.get("/segments")
            assert page.status_code == 200
            assert "High priority, never contacted" in page.text
            assert "<strong>1</strong> matching contact" in page.text
            assert 'href="/people?priority=high&amp;followup=never_contacted"' in page.text

            response = client.post(
                f"/segments/{segment.id}/delete",
                data={"csrf_token": csrf_token(page.text)},
                follow_redirects=False,
            )
            assert response.status_code == 303
            assert db.get(SavedSegment, segment.id) is None
    finally:
        app.dependency_overrides.clear()
