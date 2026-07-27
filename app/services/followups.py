from datetime import date, timedelta

from app.models import Person


def calculate_next_followup(person: Person) -> date | None:
    if person.cadence_paused or person.archived_at:
        return None
    if person.followup_snoozed_until and person.followup_snoozed_until >= date.today():
        return person.followup_snoozed_until
    if person.followup_override:
        return person.followup_override
    if person.last_meaningful_interaction and person.followup_interval_days:
        return person.last_meaningful_interaction + timedelta(days=person.followup_interval_days)
    return None


def refresh_followup(person: Person) -> None:
    person.next_followup = calculate_next_followup(person)

