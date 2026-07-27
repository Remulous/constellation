from __future__ import annotations

import hmac
from datetime import date, timedelta
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload
from starlette.responses import JSONResponse

from app.config import settings
from app.database import SessionLocal
from app.models import Interaction, MergeCandidate, Person, Tag


public_url = urlsplit(settings.public_url)
public_origin = f"{public_url.scheme}://{public_url.netloc}"
public_host = public_url.netloc
public_hostname = public_url.hostname
allowed_hosts = [public_host, "127.0.0.1:*", "localhost:*", "[::1]:*"]
if public_hostname:
    allowed_hosts.append(f"{public_hostname}:*")
mcp = FastMCP(
    "Constellation",
    instructions=(
        "Read-only access to the user's private relationship CRM. Use search_people "
        "before get_person when an ID is unknown. Cite each result's url in answers. "
        "Never imply that a message was sent or a record was changed."
    ),
    stateless_http=True,
    json_response=True,
    streamable_http_path="/mcp",
    host="0.0.0.0",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=[
            public_origin,
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
        ],
    ),
)


def _iso(value):
    return value.isoformat() if value else None


def _person_summary(person: Person) -> dict:
    return {
        "id": person.id,
        "name": person.display_name,
        "organization": person.current_organization,
        "title": person.current_title,
        "location": person.location,
        "priority": person.priority,
        "relationship_status": person.relationship_status,
        "last_meaningful_interaction": _iso(person.last_meaningful_interaction),
        "next_followup": _iso(person.next_followup),
        "tags": sorted(tag.name for tag in person.tags),
        "url": f"{settings.public_url}/people/{person.id}",
    }


@mcp.tool()
def search_people(
    query: str = "",
    organization: str = "",
    tag: str = "",
    relationship_status: str = "",
    priority: str = "",
    limit: int = 25,
) -> dict:
    """Find contacts by name, organization, title, email, tag, status, or priority."""
    limit = max(1, min(limit, 100))
    with SessionLocal() as db:
        stmt = (
            select(Person)
            .where(Person.archived_at.is_(None))
            .options(selectinload(Person.tags))
        )
        if query.strip():
            term = f"%{query.strip()}%"
            stmt = stmt.where(or_(
                Person.display_name.ilike(term),
                Person.current_organization.ilike(term),
                Person.current_title.ilike(term),
                Person.primary_email.ilike(term),
            ))
        if organization.strip():
            stmt = stmt.where(Person.current_organization.ilike(f"%{organization.strip()}%"))
        if relationship_status:
            stmt = stmt.where(Person.relationship_status == relationship_status)
        if priority:
            stmt = stmt.where(Person.priority == priority)
        if tag.strip():
            stmt = stmt.join(Person.tags).where(Tag.name.ilike(tag.strip()))
        people = db.scalars(stmt.order_by(Person.display_name, Person.id).limit(limit)).unique().all()
        return {"count": len(people), "people": [_person_summary(person) for person in people]}


@mcp.tool()
def get_person(person_id: str) -> dict:
    """Return a contact's CRM profile, contact methods, employment, and interaction history."""
    with SessionLocal() as db:
        person = db.scalar(
            select(Person)
            .where(Person.id == person_id, Person.archived_at.is_(None))
            .options(
                selectinload(Person.tags),
                selectinload(Person.methods),
                selectinload(Person.identities),
                selectinload(Person.employments),
                selectinload(Person.interactions),
            )
        )
        if not person:
            return {"error": "Contact not found"}
        result = _person_summary(person)
        result.update({
            "email": person.primary_email,
            "phone": person.primary_phone,
            "general_note": person.general_note,
            "cadence_days": person.followup_interval_days,
            "cadence_paused": person.cadence_paused,
            "contact_methods": [
                {"type": method.method_type, "label": method.label, "value": method.value}
                for method in person.methods
            ],
            "employment": [
                {
                    "organization": employment.organization,
                    "title": employment.title,
                    "current": employment.current,
                    "start_date": _iso(employment.start_date),
                    "end_date": _iso(employment.end_date),
                }
                for employment in person.employments
            ],
            "interactions": [
                {
                    "type": interaction.interaction_type,
                    "date": _iso(interaction.interaction_date),
                    "direction": interaction.direction,
                    "meaningful": interaction.meaningful,
                    "summary": interaction.summary,
                }
                for interaction in sorted(
                    person.interactions, key=lambda item: item.interaction_date, reverse=True
                )[:50]
            ],
        })
        return result


@mcp.tool()
def list_followups(
    timeframe: str = "all",
    days: int = 30,
    limit: int = 50,
) -> dict:
    """List overdue, today, upcoming, or all scheduled relationship follow-ups."""
    today = date.today()
    days = max(1, min(days, 365))
    limit = max(1, min(limit, 100))
    with SessionLocal() as db:
        stmt = (
            select(Person)
            .where(
                Person.archived_at.is_(None),
                Person.cadence_paused.is_(False),
                Person.next_followup.is_not(None),
            )
            .options(selectinload(Person.tags))
        )
        if timeframe == "overdue":
            stmt = stmt.where(Person.next_followup < today)
        elif timeframe == "today":
            stmt = stmt.where(Person.next_followup == today)
        elif timeframe == "upcoming":
            stmt = stmt.where(
                Person.next_followup > today,
                Person.next_followup <= today + timedelta(days=days),
            )
        people = db.scalars(
            stmt.order_by(Person.next_followup, Person.display_name).limit(limit)
        ).all()
        return {
            "timeframe": timeframe,
            "as_of": today.isoformat(),
            "count": len(people),
            "people": [_person_summary(person) for person in people],
        }


@mcp.tool()
def relationship_overview() -> dict:
    """Return aggregate counts for the relationship network without exposing raw records."""
    today = date.today()
    with SessionLocal() as db:
        active = Person.archived_at.is_(None)
        return {
            "as_of": today.isoformat(),
            "people": db.scalar(select(func.count()).select_from(Person).where(active)),
            "overdue": db.scalar(
                select(func.count()).select_from(Person).where(active, Person.next_followup < today)
            ),
            "due_today": db.scalar(
                select(func.count()).select_from(Person).where(active, Person.next_followup == today)
            ),
            "without_cadence": db.scalar(
                select(func.count()).select_from(Person).where(
                    active, Person.followup_interval_days.is_(None)
                )
            ),
            "interactions": db.scalar(select(func.count()).select_from(Interaction)),
            "pending_merge_reviews": db.scalar(
                select(func.count()).select_from(MergeCandidate).where(
                    MergeCandidate.status == "pending"
                )
            ),
        }


async def health(_):
    return JSONResponse({"status": "ok", "connector": "mcp"})


starlette_app = mcp.streamable_http_app()
starlette_app.add_route("/health", health, methods=["GET"])


class BearerAuth:
    """Small ASGI token boundary that leaves lifespan and health checks untouched."""

    def __init__(self, wrapped):
        self.wrapped = wrapped

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") == "/health":
            await self.wrapped(scope, receive, send)
            return
        if not settings.mcp_api_token:
            response = JSONResponse({"detail": "MCP connector is disabled"}, status_code=404)
            await response(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        supplied = headers.get(b"authorization", b"").decode("latin-1")
        expected = f"Bearer {settings.mcp_api_token}"
        if not hmac.compare_digest(supplied, expected):
            response = JSONResponse(
                {"detail": "Valid MCP bearer token required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.wrapped(scope, receive, send)


app = BearerAuth(starlette_app)
