from pathlib import Path

from sqlalchemy import func, select

from app.models import ExternalIdentity, ImportBatch, MergeCandidate, Person
from app.services.imports import decode_csv, import_csv, parse_google, parse_linkedin

FIXTURES = Path(__file__).parent / "fixtures"


def test_bom_and_google_parsing():
    data = b"\xef\xbb\xbf" + (FIXTURES / "google_contacts.csv").read_bytes()
    assert decode_csv(data).startswith("Name")
    rows = parse_google(data)
    assert len(rows) == 2
    assert rows[0].emails[0][0] == "jerry@example.com"
    assert rows[0].phones[0][0] == "(757) 555-0101"
    assert rows[1].display_name == "Zoë, Smith"


def test_linkedin_preamble_and_url():
    rows = parse_linkedin((FIXTURES / "linkedin_connections.csv").read_bytes())
    assert len(rows) == 2
    assert rows[0].organization == "Example Partners"
    assert rows[1].profile_url == "linkedin.com/in/alex-johnson"


def test_google_labels_are_extracted():
    data = b"Name,Given Name,Family Name,Labels\nPat Doe,Pat,Doe,USNA alumni ::: VetBiz\n"
    assert parse_google(data)[0].tags == ["USNA alumni", "VetBiz"]


def test_exact_email_matching_and_import_idempotency(db):
    google = (FIXTURES / "google_contacts.csv").read_bytes()
    linkedin = (FIXTURES / "linkedin_connections.csv").read_bytes()
    first = import_csv(db, "google_contacts", "google.csv", google)
    assert first.created_contacts == 2
    second = import_csv(db, "linkedin", "linkedin.csv", linkedin)
    assert second.exact_matches == 1
    assert db.scalar(select(func.count()).select_from(Person)) == 3
    repeated = import_csv(db, "linkedin", "linkedin.csv", linkedin)
    assert repeated.id == second.id
    assert db.scalar(select(func.count()).select_from(ImportBatch)) == 2


def test_name_only_match_is_never_automatic(db):
    one = b"First Name,Last Name,URL,Company,Position\nSam,Lee,linkedin.com/in/sam-one,One Co,Founder\n"
    two = b"First Name,Last Name,URL,Company,Position\nSam,Lee,linkedin.com/in/sam-two,Two Co,Engineer\n"
    import_csv(db, "linkedin", "one.csv", one)
    import_csv(db, "linkedin", "two.csv", two)
    assert db.scalar(select(func.count()).select_from(Person)) == 2
    assert db.scalar(select(func.count()).select_from(MergeCandidate)) == 1


def test_linkedin_snapshot_marks_missing_identity_inactive(db):
    first = b"First Name,Last Name,URL,Company\nA,One,linkedin.com/in/a-one,X\nB,Two,linkedin.com/in/b-two,Y\n"
    second = b"First Name,Last Name,URL,Company\nA,One,linkedin.com/in/a-one,X\n"
    import_csv(db, "linkedin", "first.csv", first)
    import_csv(db, "linkedin", "second.csv", second)
    states = {
        identity.profile_url: identity.active
        for identity in db.scalars(select(ExternalIdentity))
    }
    assert states["https://linkedin.com/in/a-one"] is True
    assert states["https://linkedin.com/in/b-two"] is False
