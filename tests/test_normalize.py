from app.services.normalize import (
    normalize_email,
    normalize_linkedin_url,
    normalize_name,
    normalize_org,
    normalize_phone,
)


def test_email_normalization():
    assert normalize_email("  USER@Example.COM ") == "user@example.com"


def test_phone_normalization():
    assert normalize_phone("(757) 555-0101") == "17575550101"
    assert normalize_phone("+44 20 7946 0958") == "442079460958"


def test_linkedin_normalization():
    assert normalize_linkedin_url("http://www.linkedin.com/in/Test/?trk=x") == "https://linkedin.com/in/Test"
    assert normalize_linkedin_url("linkedin.com/en-us/in/Test/") == "https://linkedin.com/in/Test"


def test_name_and_org_normalization():
    assert normalize_name("  José  O’Neil, Jr. ") == "jose o neil jr"
    assert normalize_org("Example Partners, LLC") == "example partners"

