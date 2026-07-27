from datetime import date, timedelta

from argon2 import PasswordHasher

from app.models import Person
from app.security import safe_csv_cell, verify_password
from app.services.followups import calculate_next_followup, refresh_followup


def person(**values):
    return Person(display_name="Test Person", **values)


def test_followup_calculation_and_manual_override():
    p = person(last_meaningful_interaction=date(2026, 7, 1), followup_interval_days=30)
    assert calculate_next_followup(p) == date(2026, 7, 31)
    p.followup_override = date(2026, 8, 15)
    assert calculate_next_followup(p) == date(2026, 8, 15)


def test_snooze_takes_precedence_when_active():
    p = person(
        last_meaningful_interaction=date.today() - timedelta(days=60),
        followup_interval_days=30,
        followup_snoozed_until=date.today() + timedelta(days=7),
    )
    refresh_followup(p)
    assert p.next_followup == date.today() + timedelta(days=7)


def test_paused_cadence():
    p = person(last_meaningful_interaction=None, followup_interval_days=30, cadence_paused=True)
    assert calculate_next_followup(p) is None


def test_csv_formula_injection_is_neutralized():
    assert safe_csv_cell("=HYPERLINK(\"bad\")").startswith("'")
    assert safe_csv_cell("@cmd").startswith("'")
    assert safe_csv_cell("ordinary") == "ordinary"


def test_auth_enabled_and_disabled():
    password_hash = PasswordHasher().hash("correct horse")
    assert verify_password("anything", "") is True
    assert verify_password("correct horse", password_hash) is True
    assert verify_password("wrong", password_hash) is False
