import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import get_db
from app.main import app
from app.models import Tag


def test_mutation_requires_valid_csrf(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app, base_url="https://testserver") as client:
            assert client.post("/tags", data={"name": "Nope"}).status_code == 422
            page = client.get("/tags")
            token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
            response = client.post("/tags", data={"name": "VetBiz", "csrf_token": token}, follow_redirects=False)
            assert response.status_code == 303
            assert db.scalar(select(Tag).where(Tag.name == "VetBiz"))
    finally:
        app.dependency_overrides.clear()

