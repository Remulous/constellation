from contextlib import nullcontext
from datetime import date, timedelta

from app import mcp_server
from app.models import Interaction, Person, Tag


def test_mcp_tools_are_bounded_and_return_source_links(db, monkeypatch):
    person = Person(
        display_name="Alex Morgan",
        current_organization="Example Security",
        current_title="Founder",
        priority="high",
        next_followup=date.today() + timedelta(days=3),
    )
    person.tags.append(Tag(name="cybersecurity"))
    person.interactions.append(Interaction(
        interaction_type="Meeting",
        interaction_date=date.today(),
        summary="Discussed partnerships",
        meaningful=True,
    ))
    db.add(person)
    db.commit()
    monkeypatch.setattr(mcp_server, "SessionLocal", lambda: nullcontext(db))

    search_result = mcp_server.search_people(query="Alex", limit=500)
    assert search_result["count"] == 1
    assert search_result["people"][0]["url"].endswith(f"/people/{person.id}")
    assert search_result["people"][0]["tags"] == ["cybersecurity"]

    detail = mcp_server.get_person(person.id)
    assert detail["interactions"][0]["summary"] == "Discussed partnerships"

    followups = mcp_server.list_followups(timeframe="upcoming", days=7)
    assert followups["people"][0]["id"] == person.id
