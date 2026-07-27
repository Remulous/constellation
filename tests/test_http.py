import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import get_db
from app.main import app
from app.models import Interaction, Person, Tag


def test_mutation_requires_valid_csrf(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app, base_url="https://testserver") as client:
            assert client.post("/tags", data={"name": "Nope"}).status_code == 422
            page = client.get("/tags")
            assert 'href="/static/app.css"' in page.text
            assert 'href="/static/icons.svg#tag"' in page.text
            token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
            response = client.post("/tags", data={"name": "VetBiz", "csrf_token": token}, follow_redirects=False)
            assert response.status_code == 303
            assert db.scalar(select(Tag).where(Tag.name == "VetBiz"))
    finally:
        app.dependency_overrides.clear()


def test_people_continuous_scroll_and_quick_interaction(db):
    db.add_all([Person(display_name=f"Contact {index:03d}") for index in range(125)])
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app, base_url="https://testserver") as client:
            page = client.get("/people")
            assert page.status_code == 200
            assert page.text.count("data-person-row") == 100
            assert "data-infinite-sentinel" in page.text
            assert "all 125 contacts" in page.text

            more = client.get("/people/rows?offset=100")
            assert more.status_code == 200
            assert more.text.count("data-person-row") == 25
            assert "data-infinite-sentinel" not in more.text

            token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
            person = db.scalar(select(Person).where(Person.display_name == "Contact 000"))
            response = client.post(
                f"/people/{person.id}/interactions",
                data={
                    "csrf_token": token,
                    "interaction_type": "Meeting",
                    "interaction_date": "2026-07-27",
                    "summary": "Caught up",
                    "meaningful": "true",
                    "next_followup": "2026-08-15",
                    "return_to": "/people?sort=name",
                },
                follow_redirects=False,
            )
            assert response.status_code == 303
            assert response.headers["location"] == "/people?sort=name"
            assert db.scalar(select(Interaction).where(Interaction.person_id == person.id))
            assert person.last_meaningful_interaction.isoformat() == "2026-07-27"
            assert person.next_followup.isoformat() == "2026-08-15"
    finally:
        app.dependency_overrides.clear()
