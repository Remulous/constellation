from __future__ import annotations

import csv
import io
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.database import Base, engine, get_db
from app.models import (
    ContactMethod,
    Employment,
    ExternalIdentity,
    ImportBatch,
    Interaction,
    MergeCandidate,
    MergeHistory,
    Person,
    Tag,
)
from app.security import csrf_token, safe_csv_cell, verify_csrf, verify_password
from app.services.followups import refresh_followup
from app.services.imports import import_csv

BASE = Path(__file__).parent
PEOPLE_BATCH_SIZE = 100
MERGE_FIELDS = (
    "display_name", "primary_email", "primary_phone", "current_organization",
    "current_title", "location", "priority", "relationship_status",
    "followup_interval_days", "general_note", "obsidian_uri",
)
PERSON_SNAPSHOT_FIELDS = (
    "display_name", "first_name", "middle_name", "last_name", "preferred_name",
    "primary_email", "primary_phone", "current_organization", "current_title",
    "location", "relationship_status", "priority", "followup_interval_days",
    "last_meaningful_interaction", "next_followup", "followup_override",
    "followup_snoozed_until", "cadence_paused", "obsidian_uri", "general_note",
    "created_at", "updated_at", "archived_at",
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="Constellation", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    public = request.url.path in {"/login", "/health"}
    if settings.app_password_hash and not request.session.get("authenticated") and not public:
        return RedirectResponse("/login", status_code=303)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; img-src 'self' data:; "
        "script-src 'self'; form-action 'self'; frame-ancestors 'none'"
    )
    return response


app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    https_only=settings.secure_cookies,
    same_site="lax",
    max_age=86400 * 30,
)


def context(request: Request, **values):
    return {"request": request, "csrf_token": csrf_token(request.session), **values}


def require_csrf(request: Request, supplied: str = Form(..., alias="csrf_token")) -> None:
    if not verify_csrf(request.session, supplied):
        raise HTTPException(403, "Invalid CSRF token")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if not settings.app_password_hash:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", context(request))


@app.post("/login")
def login(request: Request, password: str = Form("")):
    if not verify_password(password, settings.app_password_hash):
        return templates.TemplateResponse("login.html", context(request, error="Incorrect password"), status_code=401)
    request.session["authenticated"] = True
    csrf_token(request.session)
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request, _csrf: None = Depends(require_csrf)):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    today = date.today()
    active = [Person.archived_at.is_(None), Person.cadence_paused.is_(False)]
    overdue = db.scalars(select(Person).where(*active, Person.next_followup < today).order_by(Person.next_followup)).all()
    due_today = db.scalars(select(Person).where(*active, Person.next_followup == today)).all()
    due_week = db.scalars(select(Person).where(
        *active, Person.next_followup > today, Person.next_followup <= today + timedelta(days=7)
    ).order_by(Person.next_followup)).all()
    no_cadence = db.scalar(select(func.count()).select_from(Person).where(
        Person.archived_at.is_(None), Person.followup_interval_days.is_(None)
    ))
    pending = db.scalar(select(func.count()).select_from(MergeCandidate).where(MergeCandidate.status == "pending"))
    recent = db.scalars(select(Person).where(Person.archived_at.is_(None)).order_by(
        Person.last_meaningful_interaction.desc().nullslast()
    ).limit(8)).all()
    return templates.TemplateResponse("dashboard.html", context(
        request, overdue=overdue, due_today=due_today, due_week=due_week,
        no_cadence=no_cadence, pending=pending, recent=recent, today=today,
    ))


@app.get("/people", response_class=HTMLResponse)
def people(
    request: Request, q: str = "", priority: str = "", status: str = "",
    sort: str = "name", db: Session = Depends(get_db),
):
    stmt, order = people_statement(q, priority, status, sort)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    rows = db.scalars(stmt.order_by(*order).limit(PEOPLE_BATCH_SIZE + 1)).all()
    has_next = len(rows) > PEOPLE_BATCH_SIZE
    next_url = people_rows_url(q, priority, status, sort, PEOPLE_BATCH_SIZE) if has_next else ""
    all_tags = db.scalars(select(Tag).order_by(Tag.name)).all()
    return templates.TemplateResponse("people.html", context(
        request, people=rows[:PEOPLE_BATCH_SIZE], q=q, priority=priority, status=status,
        sort=sort, total=total, next_url=next_url, all_tags=all_tags, today=date.today(),
    ))


def people_statement(q: str, priority: str, status: str, sort: str):
    stmt = select(Person).where(Person.archived_at.is_(None)).options(selectinload(Person.tags))
    if q:
        term = f"%{q.strip()}%"
        stmt = stmt.where(or_(
            Person.display_name.ilike(term), Person.current_organization.ilike(term),
            Person.current_title.ilike(term), Person.primary_email.ilike(term),
        ))
    if priority:
        stmt = stmt.where(Person.priority == priority)
    if status:
        stmt = stmt.where(Person.relationship_status == status)
    order = {
        "name": (Person.display_name, Person.id),
        "organization": (Person.current_organization.asc().nullslast(), Person.display_name, Person.id),
        "followup": (Person.next_followup.asc().nullslast(), Person.display_name, Person.id),
        "recent": (Person.updated_at.desc(), Person.display_name, Person.id),
    }.get(sort, (Person.display_name, Person.id))
    return stmt, order


def people_rows_url(q: str, priority: str, status: str, sort: str, offset: int) -> str:
    return "/people/rows?" + urlencode({
        "q": q, "priority": priority, "status": status, "sort": sort, "offset": offset,
    })


@app.get("/people/rows", response_class=HTMLResponse)
def people_rows(
    request: Request, q: str = "", priority: str = "", status: str = "",
    sort: str = "name", offset: int = 0, db: Session = Depends(get_db),
):
    offset = max(0, offset)
    stmt, order = people_statement(q, priority, status, sort)
    rows = db.scalars(
        stmt.order_by(*order).offset(offset).limit(PEOPLE_BATCH_SIZE + 1)
    ).all()
    has_next = len(rows) > PEOPLE_BATCH_SIZE
    next_url = people_rows_url(
        q, priority, status, sort, offset + PEOPLE_BATCH_SIZE
    ) if has_next else ""
    return templates.TemplateResponse("people_rows.html", context(
        request, people=rows[:PEOPLE_BATCH_SIZE], next_url=next_url, today=date.today(),
    ))


@app.post("/people/bulk")
def bulk_people(
    request: Request, person_ids: list[str] = Form(default=[]),
    bulk_action: str = Form(...), tag_id: int | None = Form(None),
    cadence: int | None = Form(None), db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    people = db.scalars(select(Person).where(Person.id.in_(person_ids))).all() if person_ids else []
    if bulk_action == "tag" and tag_id:
        tag = db.get(Tag, tag_id)
        if tag:
            for person in people:
                if tag not in person.tags:
                    person.tags.append(tag)
    elif bulk_action == "cadence":
        for person in people:
            person.followup_interval_days = cadence
            refresh_followup(person)
    db.commit()
    return RedirectResponse("/people", status_code=303)


@app.get("/follow-ups", response_class=HTMLResponse)
def followups_page(request: Request, db: Session = Depends(get_db)):
    rows = db.scalars(select(Person).where(
        Person.archived_at.is_(None), Person.next_followup.is_not(None)
    ).order_by(Person.next_followup)).all()
    return templates.TemplateResponse("followups.html", context(request, people=rows, today=date.today()))


def get_person(db: Session, person_id: str) -> Person:
    person = db.scalar(select(Person).where(Person.id == person_id).options(
        selectinload(Person.tags), selectinload(Person.methods), selectinload(Person.identities),
        selectinload(Person.employments), selectinload(Person.interactions),
    ))
    if not person:
        raise HTTPException(404)
    return person


@app.get("/people/{person_id}", response_class=HTMLResponse)
def person_detail(request: Request, person_id: str, db: Session = Depends(get_db)):
    person = get_person(db, person_id)
    all_tags = db.scalars(select(Tag).order_by(Tag.name)).all()
    return templates.TemplateResponse("person.html", context(
        request, person=person, all_tags=all_tags, now_date=date.today(),
    ))


@app.post("/people/{person_id}/edit")
def edit_person(
    request: Request, person_id: str, display_name: str = Form(...),
    organization: str = Form(""), title: str = Form(""), priority: str = Form("normal"),
    relationship_status: str = Form("active"), cadence: str = Form(""),
    obsidian_uri: str = Form(""), general_note: str = Form(""),
    tag_ids: list[int] = Form(default=[]), db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    person = get_person(db, person_id)
    person.display_name = display_name.strip()
    person.current_organization = organization.strip() or None
    person.current_title = title.strip() or None
    person.priority = priority
    person.relationship_status = relationship_status
    person.followup_interval_days = int(cadence) if cadence else None
    person.obsidian_uri = obsidian_uri.strip() or None
    person.general_note = general_note.strip() or None
    person.tags = db.scalars(select(Tag).where(Tag.id.in_(tag_ids))).all() if tag_ids else []
    refresh_followup(person)
    db.commit()
    return RedirectResponse(f"/people/{person.id}", status_code=303)


@app.post("/people/{person_id}/interactions")
def add_interaction(
    request: Request, person_id: str, interaction_type: str = Form(...),
    interaction_date: date = Form(...), summary: str = Form(""),
    direction: str = Form(""), meaningful: bool = Form(False),
    next_followup: date | None = Form(None), return_to: str = Form(""),
    db: Session = Depends(get_db), _csrf: None = Depends(require_csrf),
):
    person = get_person(db, person_id)
    interaction = Interaction(
        person_id=person.id, interaction_type=interaction_type,
        interaction_date=interaction_date, summary=summary.strip() or None,
        direction=direction or None, meaningful=meaningful,
    )
    db.add(interaction)
    if meaningful and (not person.last_meaningful_interaction or interaction_date >= person.last_meaningful_interaction):
        person.last_meaningful_interaction = interaction_date
        person.followup_override = None
        person.followup_snoozed_until = None
        refresh_followup(person)
    if next_followup:
        person.followup_override = next_followup
        refresh_followup(person)
    db.commit()
    destination = return_to if return_to.startswith("/") and not return_to.startswith("//") else f"/people/{person.id}"
    return RedirectResponse(destination, status_code=303)


@app.post("/people/{person_id}/followup")
def schedule_followup(
    request: Request, person_id: str, action: str = Form(...),
    followup_date: date | None = Form(None), db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    person = get_person(db, person_id)
    if action == "schedule":
        person.followup_override = followup_date
    elif action == "snooze":
        person.followup_snoozed_until = followup_date
    elif action == "clear":
        person.followup_override = person.followup_snoozed_until = None
    elif action == "archive":
        person.archived_at = datetime.now(timezone.utc)
    refresh_followup(person)
    db.commit()
    return RedirectResponse(f"/people/{person.id}", status_code=303)


@app.get("/imports", response_class=HTMLResponse)
def imports_page(request: Request, db: Session = Depends(get_db)):
    batches = db.scalars(select(ImportBatch).order_by(ImportBatch.imported_at.desc()).limit(30)).all()
    return templates.TemplateResponse("imports.html", context(request, batches=batches))


@app.post("/imports")
async def upload_import(
    request: Request, provider: str = Form(...), csv_file: UploadFile = File(...),
    db: Session = Depends(get_db), _csrf: None = Depends(require_csrf),
):
    if provider not in {"google_contacts", "linkedin"}:
        raise HTTPException(400, "Unsupported provider")
    if not (csv_file.filename or "").lower().endswith(".csv"):
        raise HTTPException(400, "Only CSV files are accepted")
    limit = settings.max_upload_mb * 1024 * 1024
    data = await csv_file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, "Upload too large")
    import_csv(db, provider, Path(csv_file.filename or "upload.csv").name, data)
    return RedirectResponse("/imports", status_code=303)


@app.get("/merge-review", response_class=HTMLResponse)
def merge_review(request: Request, db: Session = Depends(get_db)):
    candidates = db.scalars(select(MergeCandidate).where(
        MergeCandidate.status == "pending"
    ).order_by(MergeCandidate.id)).all()
    rows = []
    for candidate in candidates:
        identity = db.get(ExternalIdentity, candidate.source_identity_id)
        rows.append((candidate, db.get(Person, identity.person_id), db.get(Person, candidate.candidate_person_id)))
    history = db.scalars(
        select(MergeHistory)
        .where(MergeHistory.undone_at.is_(None))
        .order_by(MergeHistory.merged_at.desc())
        .limit(10)
    ).all()
    return templates.TemplateResponse("merge.html", context(
        request, rows=rows, history=history, merge_fields=MERGE_FIELDS,
    ))


def _snapshot_value(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def _person_fields(person: Person) -> dict:
    return {field: _snapshot_value(getattr(person, field)) for field in PERSON_SNAPSHOT_FIELDS}


def _restore_person_fields(person: Person, values: dict) -> None:
    date_fields = {
        "last_meaningful_interaction", "next_followup",
        "followup_override", "followup_snoozed_until",
    }
    datetime_fields = {"created_at", "updated_at", "archived_at"}
    for field in PERSON_SNAPSHOT_FIELDS:
        value = values.get(field)
        if value and field in date_fields:
            value = date.fromisoformat(value)
        elif value and field in datetime_fields:
            value = datetime.fromisoformat(value)
        setattr(person, field, value)


def _method_snapshot(method: ContactMethod) -> dict:
    return {
        "id": method.id,
        "method_type": method.method_type,
        "value": method.value,
        "normalized_value": method.normalized_value,
        "label": method.label,
        "source": method.source,
        "primary": method.primary,
        "verified": method.verified,
    }


def merge_people(
    db: Session,
    survivor: Person,
    duplicate: Person,
    candidate: MergeCandidate | None = None,
    selected_values: dict | None = None,
) -> None:
    snapshot = {
        "duplicate": _person_fields(duplicate),
        "survivor": _person_fields(survivor),
        "methods": [_method_snapshot(method) for method in duplicate.methods],
        "identity_ids": [identity.id for identity in duplicate.identities],
        "employment_ids": [employment.id for employment in duplicate.employments],
        "interaction_ids": [interaction.id for interaction in duplicate.interactions],
        "duplicate_tag_ids": [tag.id for tag in duplicate.tags],
        "survivor_tag_ids": [tag.id for tag in survivor.tags],
        "candidate_person_id": candidate.candidate_person_id if candidate else None,
    }
    existing_methods = {(m.method_type, m.normalized_value) for m in survivor.methods}
    for method in list(duplicate.methods):
        duplicate.methods.remove(method)
        if (method.method_type, method.normalized_value) not in existing_methods:
            survivor.methods.append(method)
            existing_methods.add((method.method_type, method.normalized_value))
        else:
            db.delete(method)
    for source_collection, destination_collection in (
        (duplicate.identities, survivor.identities),
        (duplicate.employments, survivor.employments),
        (duplicate.interactions, survivor.interactions),
    ):
        for item in list(source_collection):
            source_collection.remove(item)
            destination_collection.append(item)
    for tag in duplicate.tags:
        if tag not in survivor.tags:
            survivor.tags.append(tag)
    for field in ("primary_email", "primary_phone", "current_organization", "current_title", "location", "obsidian_uri", "general_note"):
        if not getattr(survivor, field) and getattr(duplicate, field):
            setattr(survivor, field, getattr(duplicate, field))
    if duplicate.last_meaningful_interaction and (
        not survivor.last_meaningful_interaction or duplicate.last_meaningful_interaction > survivor.last_meaningful_interaction
    ):
        survivor.last_meaningful_interaction = duplicate.last_meaningful_interaction
    for field, value in (selected_values or {}).items():
        if field in MERGE_FIELDS:
            setattr(survivor, field, value)
    db.add(MergeHistory(
        survivor_person_id=survivor.id,
        merged_person_id=duplicate.id,
        candidate_id=candidate.id if candidate else None,
        snapshot=snapshot,
    ))
    db.delete(duplicate)
    refresh_followup(survivor)


@app.post("/merge-review/{candidate_id}")
async def resolve_merge(
    request: Request, candidate_id: int, action: str = Form(...),
    survivor_id: str = Form(""), db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    candidate = db.get(MergeCandidate, candidate_id)
    if not candidate or candidate.status != "pending":
        raise HTTPException(404)
    identity = db.get(ExternalIdentity, candidate.source_identity_id)
    source = get_person(db, identity.person_id)
    target = get_person(db, candidate.candidate_person_id)
    if action == "approve":
        survivor = source if survivor_id == source.id else target
        duplicate = target if survivor is source else source
        form = await request.form()
        selected_values = {}
        for field in MERGE_FIELDS:
            selected_person = source if form.get(f"field_{field}") == source.id else target
            selected_values[field] = getattr(selected_person, field)
        merge_people(db, survivor, duplicate, candidate, selected_values)
        candidate.status = "approved"
        candidate.resolved_at = datetime.now(timezone.utc)
        if candidate.candidate_person_id == duplicate.id:
            candidate.candidate_person_id = survivor.id
    elif action in {"rejected", "ignored"}:
        candidate.status = action
        candidate.resolved_at = datetime.now(timezone.utc)
    else:
        raise HTTPException(400)
    db.commit()
    return RedirectResponse("/merge-review", status_code=303)


@app.post("/merge-history/{history_id}/undo")
def undo_merge(
    request: Request, history_id: int, db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    history = db.get(MergeHistory, history_id)
    if not history or history.undone_at is not None:
        raise HTTPException(404)
    if "duplicate" not in history.snapshot:
        raise HTTPException(409, "This legacy merge predates undo support")
    if db.get(Person, history.merged_person_id):
        raise HTTPException(409, "Merged person already exists")
    survivor = get_person(db, history.survivor_person_id)
    snapshot = history.snapshot

    duplicate = Person(id=history.merged_person_id, display_name=snapshot["duplicate"]["display_name"])
    _restore_person_fields(duplicate, snapshot["duplicate"])
    db.add(duplicate)
    db.flush()

    for method_data in snapshot.get("methods", []):
        method = db.get(ContactMethod, method_data["id"])
        if method:
            method.person_id = duplicate.id
        else:
            db.add(ContactMethod(person_id=duplicate.id, **method_data))
    for model, key in (
        (ExternalIdentity, "identity_ids"),
        (Employment, "employment_ids"),
        (Interaction, "interaction_ids"),
    ):
        for item_id in snapshot.get(key, []):
            item = db.get(model, item_id)
            if item:
                item.person_id = duplicate.id

    duplicate.tags = [
        tag for tag_id in snapshot.get("duplicate_tag_ids", [])
        if (tag := db.get(Tag, tag_id))
    ]
    _restore_person_fields(survivor, snapshot["survivor"])
    survivor.tags = [
        tag for tag_id in snapshot.get("survivor_tag_ids", [])
        if (tag := db.get(Tag, tag_id))
    ]

    if history.candidate_id:
        candidate = db.get(MergeCandidate, history.candidate_id)
        if candidate:
            candidate.status = "pending"
            candidate.resolved_at = None
            if snapshot.get("candidate_person_id"):
                candidate.candidate_person_id = snapshot["candidate_person_id"]
    history.undone_at = datetime.now(timezone.utc)
    db.commit()
    return RedirectResponse("/merge-review", status_code=303)


@app.get("/tags", response_class=HTMLResponse)
def tags_page(request: Request, db: Session = Depends(get_db)):
    tags = db.scalars(select(Tag).order_by(Tag.name)).all()
    return templates.TemplateResponse("tags.html", context(request, tags=tags))


@app.post("/tags")
def add_tag(
    request: Request, name: str = Form(...), description: str = Form(""),
    db: Session = Depends(get_db), _csrf: None = Depends(require_csrf),
):
    clean = name.strip()
    if clean and not db.scalar(select(Tag).where(func.lower(Tag.name) == clean.casefold())):
        db.add(Tag(name=clean, description=description.strip() or None))
        db.commit()
    return RedirectResponse("/tags", status_code=303)


@app.get("/export.csv")
def export_people(db: Session = Depends(get_db)):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "name", "email", "phone", "organization", "title", "priority", "status", "last_contact", "next_followup", "tags", "obsidian_uri"])
    people = db.scalars(select(Person).options(selectinload(Person.tags)).order_by(Person.display_name)).all()
    for p in people:
        writer.writerow([safe_csv_cell(v) for v in (
            p.id, p.display_name, p.primary_email, p.primary_phone, p.current_organization,
            p.current_title, p.priority, p.relationship_status, p.last_meaningful_interaction,
            p.next_followup, "; ".join(t.name for t in p.tags), p.obsidian_uri,
        )])
    headers = {"Content-Disposition": 'attachment; filename="constellation-export.csv"'}
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv; charset=utf-8", headers=headers)


@app.get("/obsidian-uri")
def build_obsidian_uri(vault: str = "", path: str = ""):
    resolved_vault = vault or settings.obsidian_vault
    if not resolved_vault or not path:
        raise HTTPException(400, "Vault and path are required")
    return {"uri": f"obsidian://open?vault={quote(resolved_vault)}&file={quote(path)}"}


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", context(
        request, password_enabled=bool(settings.app_password_hash),
        secure_cookies=settings.secure_cookies, upload_limit=settings.max_upload_mb,
        obsidian_vault=settings.obsidian_vault, mcp_enabled=bool(settings.mcp_api_token),
        public_url=settings.public_url,
    ))
