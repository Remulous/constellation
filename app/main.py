from __future__ import annotations

import csv
import hashlib
import io
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

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
    ConnectionSuggestion,
    Employment,
    ExternalIdentity,
    FollowUpSuggestion,
    ImportBatch,
    Interaction,
    MergeCandidate,
    MergeHistory,
    Opportunity,
    Organization,
    Person,
    RelationshipSignal,
    SavedSegment,
    Tag,
    VetBizImportCandidate,
    VetBizImportRecord,
)
from app.security import csrf_token, safe_csv_cell, verify_csrf, verify_password
from app.services.followups import refresh_followup
from app.services.imports import import_csv
from app.services.normalize import normalize_linkedin_url
from app.services.vetbiz_imports import (
    EDITABLE_FIELDS,
    VetBizImportError,
    approve_safe_interactions,
    bulk_decide_candidates,
    commit_reviewed_import,
    create_reviewed_import,
    delete_pending_import,
    propose_connection_suggestion,
    propose_opportunity_from_signal,
    update_candidate,
)

BASE = Path(__file__).parent
ASSET_VERSION = hashlib.sha256(
    (BASE / "static" / "app.css").read_bytes()
    + (BASE / "static" / "app.js").read_bytes()
).hexdigest()[:12]
PEOPLE_BATCH_SIZE = 100
SEGMENT_FILTER_KEYS = ("q", "priority", "status", "tag", "followup", "sort")
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
    return {
        "request": request,
        "csrf_token": csrf_token(request.session),
        "asset_version": ASSET_VERSION,
        **values,
    }


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


@app.get("/today", response_class=HTMLResponse)
def today_page(request: Request, db: Session = Depends(get_db)):
    today = date.today()
    active = [Person.archived_at.is_(None), Person.cadence_paused.is_(False)]
    due = db.scalars(
        select(Person)
        .where(*active, Person.next_followup <= today)
        .options(selectinload(Person.identities))
        .order_by(Person.next_followup, Person.display_name)
    ).all()
    upcoming = db.scalars(
        select(Person)
        .where(
            *active,
            Person.next_followup > today,
            Person.next_followup <= today + timedelta(days=7),
        )
        .options(selectinload(Person.identities))
        .order_by(Person.next_followup, Person.display_name)
    ).all()
    recent_rows = db.execute(
        select(Interaction, Person)
        .join(Person, Interaction.person_id == Person.id)
        .where(
            Person.archived_at.is_(None),
            Interaction.interaction_date >= today - timedelta(days=7),
        )
        .order_by(Interaction.interaction_date.desc(), Interaction.id.desc())
        .limit(12)
    ).all()
    pending_merges = db.scalar(
        select(func.count()).select_from(MergeCandidate).where(MergeCandidate.status == "pending")
    ) or 0
    return templates.TemplateResponse("today.html", context(
        request,
        today=today,
        due=due,
        upcoming=upcoming,
        recent_rows=recent_rows,
        pending_merges=pending_merges,
        now_date=today,
    ))


@app.get("/people", response_class=HTMLResponse)
def people(
    request: Request, q: str = "", priority: str = "", status: str = "",
    tag: str = "", followup: str = "", sort: str = "name",
    db: Session = Depends(get_db),
):
    stmt, order = people_statement(q, priority, status, tag, followup, sort)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    rows = db.scalars(stmt.order_by(*order).limit(PEOPLE_BATCH_SIZE + 1)).all()
    has_next = len(rows) > PEOPLE_BATCH_SIZE
    next_url = people_rows_url(
        q, priority, status, tag, followup, sort, PEOPLE_BATCH_SIZE
    ) if has_next else ""
    all_tags = db.scalars(select(Tag).order_by(Tag.name)).all()
    return templates.TemplateResponse("people.html", context(
        request, people=rows[:PEOPLE_BATCH_SIZE], q=q, priority=priority, status=status,
        tag=tag, followup=followup, sort=sort, total=total, next_url=next_url,
        all_tags=all_tags, today=date.today(),
    ))


def people_statement(
    q: str,
    priority: str,
    status: str,
    tag: str,
    followup: str,
    sort: str,
):
    today = date.today()
    stmt = select(Person).where(Person.archived_at.is_(None)).options(
        selectinload(Person.tags), selectinload(Person.identities)
    )
    if q:
        term = f"%{q.strip()}%"
        stmt = stmt.where(or_(
            Person.display_name.ilike(term), Person.current_organization.ilike(term),
            Person.current_title.ilike(term), Person.primary_email.ilike(term),
            Person.location.ilike(term),
        ))
    if priority:
        stmt = stmt.where(Person.priority == priority)
    if status:
        stmt = stmt.where(Person.relationship_status == status)
    if tag:
        stmt = stmt.join(Person.tags).where(Tag.name == tag)
    if followup == "overdue":
        stmt = stmt.where(Person.next_followup < today)
    elif followup == "today":
        stmt = stmt.where(Person.next_followup == today)
    elif followup == "upcoming":
        stmt = stmt.where(
            Person.next_followup > today,
            Person.next_followup <= today + timedelta(days=30),
        )
    elif followup == "no_cadence":
        stmt = stmt.where(Person.followup_interval_days.is_(None))
    elif followup == "never_contacted":
        stmt = stmt.where(Person.last_meaningful_interaction.is_(None))
    order = {
        "name": (Person.display_name, Person.id),
        "organization": (Person.current_organization.asc().nullslast(), Person.display_name, Person.id),
        "followup": (Person.next_followup.asc().nullslast(), Person.display_name, Person.id),
        "recent": (Person.updated_at.desc(), Person.display_name, Person.id),
    }.get(sort, (Person.display_name, Person.id))
    return stmt, order


def people_rows_url(
    q: str,
    priority: str,
    status: str,
    tag: str,
    followup: str,
    sort: str,
    offset: int,
) -> str:
    return "/people/rows?" + urlencode({
        "q": q, "priority": priority, "status": status, "tag": tag,
        "followup": followup, "sort": sort, "offset": offset,
    })


@app.get("/people/rows", response_class=HTMLResponse)
def people_rows(
    request: Request, q: str = "", priority: str = "", status: str = "",
    tag: str = "", followup: str = "", sort: str = "name",
    offset: int = 0, db: Session = Depends(get_db),
):
    offset = max(0, offset)
    stmt, order = people_statement(q, priority, status, tag, followup, sort)
    rows = db.scalars(
        stmt.order_by(*order).offset(offset).limit(PEOPLE_BATCH_SIZE + 1)
    ).all()
    has_next = len(rows) > PEOPLE_BATCH_SIZE
    next_url = people_rows_url(
        q, priority, status, tag, followup, sort, offset + PEOPLE_BATCH_SIZE
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


def meeting_brief_questions(person: Person, today: date) -> list[str]:
    questions = []
    if person.current_organization:
        questions.append(f"What has changed at {person.current_organization} since we last spoke?")
    if person.interactions:
        latest = max(person.interactions, key=lambda item: (item.interaction_date, item.id))
        if latest.summary:
            questions.append(f"Follow up on: {latest.summary}")
    if person.next_followup and person.next_followup <= today:
        questions.append("What would make this reconnection useful for them right now?")
    if not person.current_title:
        questions.append("What are they focused on professionally now?")
    if not person.general_note:
        questions.append("What context or personal milestone should I remember for next time?")
    return questions[:4] or ["What has changed since we last connected?", "How can I be useful?"]


@app.get("/people/{person_id}/brief", response_class=HTMLResponse)
def person_brief(request: Request, person_id: str, db: Session = Depends(get_db)):
    person = get_person(db, person_id)
    today = date.today()
    recent_interactions = sorted(
        person.interactions,
        key=lambda item: (item.interaction_date, item.id),
        reverse=True,
    )[:5]
    return templates.TemplateResponse("brief.html", context(
        request,
        person=person,
        today=today,
        recent_interactions=recent_interactions,
        questions=meeting_brief_questions(person, today),
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


@app.post("/people/{person_id}/linkedin")
def edit_person_linkedin(
    request: Request,
    person_id: str,
    action: str = Form("save"),
    linkedin_url: str = Form(""),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    person = get_person(db, person_id)
    identities = db.scalars(
        select(ExternalIdentity).where(ExternalIdentity.provider == "linkedin")
    ).all()
    person_identities = [
        identity for identity in identities if identity.person_id == person.id
    ]

    if action == "remove":
        for identity in person_identities:
            identity.active = False
        db.commit()
        return RedirectResponse(
            f"/people/{person.id}?linkedin=removed#linkedin-profile",
            status_code=303,
        )
    if action != "save":
        raise HTTPException(400, "Unsupported LinkedIn profile action")

    normalized = normalize_linkedin_url(linkedin_url)
    if not normalized.casefold().startswith("https://linkedin.com/in/"):
        raise HTTPException(
            400, "Enter a LinkedIn person profile URL using linkedin.com/in/."
        )

    matching = [
        identity
        for identity in identities
        if normalize_linkedin_url(
            identity.profile_url or identity.provider_record_id
        )
        == normalized
    ]
    if any(identity.person_id != person.id for identity in matching):
        raise HTTPException(
            400, "That LinkedIn profile is already attached to another person."
        )

    for identity in person_identities:
        identity.active = False
    identity = matching[0] if matching else None
    if identity is None:
        identity = ExternalIdentity(
            person_id=person.id,
            provider="linkedin",
            provider_record_id=normalized,
            profile_url=normalized,
            source_payload={"source": "manual_profile_edit"},
            record_hash=hashlib.sha256(
                f"manual_profile_edit|{normalized}".encode()
            ).hexdigest(),
        )
        db.add(identity)
    else:
        identity.profile_url = normalized
    identity.active = True
    identity.last_imported_at = datetime.now(timezone.utc)
    db.commit()
    return RedirectResponse(
        f"/people/{person.id}?linkedin=saved#linkedin-profile",
        status_code=303,
    )


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


VETBIZ_GROUPS = (
    ("contacts", "Contacts", {"new_contact", "contact_update"}),
    ("organizations", "Organizations", {"organization"}),
    ("interactions", "Meeting interactions", {"interaction"}),
    ("signals", "Offers and asks", {"signal"}),
    ("follow_ups", "Follow-up suggestions", {"follow_up"}),
    ("opportunities", "Possible Remulous Labs opportunities", {"opportunity"}),
    ("connections", "Possible introductions", {"connection_suggestion"}),
)
VETBIZ_GROUP_TYPES = {
    key: candidate_types for key, _label, candidate_types in VETBIZ_GROUPS
}
VETBIZ_GROUP_LABELS = {key: label for key, label, _types in VETBIZ_GROUPS}


def _vetbiz_import_context(
    request: Request,
    db: Session,
    record: VetBizImportRecord,
    error: str = "",
    notice: str = "",
):
    candidates = db.scalars(
        select(VetBizImportCandidate)
        .where(VetBizImportCandidate.import_record_id == record.id)
        .order_by(VetBizImportCandidate.id)
    ).all()
    grouped = []
    for key, label, candidate_types in VETBIZ_GROUPS:
        rows = [
            candidate
            for candidate in candidates
            if candidate.candidate_type in candidate_types
        ]
        if rows:
            grouped.append((key, label, rows))
    matched_people = {
        candidate.matched_entity_id: db.get(Person, candidate.matched_entity_id)
        for candidate in candidates
        if candidate.matched_entity_id
        and candidate.candidate_type != "organization"
    }
    prior = db.get(VetBizImportRecord, record.revision_of_id) if record.revision_of_id else None
    counts = {
        status: sum(candidate.status == status for candidate in candidates)
        for status in ("pending", "edited", "approved", "rejected", "committed")
    }
    contact_candidates = [
        candidate
        for candidate in candidates
        if candidate.candidate_type in {"new_contact", "contact_update"}
    ]
    return context(
        request,
        record=record,
        grouped=grouped,
        matched_people=matched_people,
        prior=prior,
        counts=counts,
        editable_fields=EDITABLE_FIELDS,
        contact_candidates=contact_candidates,
        error=error,
        notice=notice,
    )


@app.get("/vetbiz-imports", response_class=HTMLResponse)
def vetbiz_imports_page(request: Request, db: Session = Depends(get_db)):
    records = db.scalars(
        select(VetBizImportRecord)
        .order_by(VetBizImportRecord.imported_at.desc())
        .limit(50)
    ).all()
    return templates.TemplateResponse(
        "vetbiz_imports.html",
        context(
            request,
            records=records,
            max_minutes_upload_mb=settings.max_minutes_upload_mb,
            notice=(
                "The in-progress review and its staged proposals were deleted."
                if request.query_params.get("deleted")
                else ""
            ),
        ),
    )


@app.post("/vetbiz-imports")
async def upload_vetbiz_import(
    request: Request,
    review_confirmed: bool = Form(False),
    review_notes: str = Form(""),
    minutes_text: str = Form(""),
    minutes_file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    filename = "pasted-vetbiz-minutes.txt"
    data = minutes_text.encode("utf-8") if minutes_text.strip() else b""
    limit = settings.max_minutes_upload_mb * 1024 * 1024
    if minutes_file and minutes_file.filename:
        filename = Path(minutes_file.filename).name
        data = await minutes_file.read(limit + 1)
    try:
        creation = create_reviewed_import(
            db,
            filename,
            data,
            review_confirmed=review_confirmed,
            review_notes=review_notes,
            max_bytes=limit,
        )
    except VetBizImportError as exc:
        records = db.scalars(
            select(VetBizImportRecord)
            .order_by(VetBizImportRecord.imported_at.desc())
            .limit(50)
        ).all()
        return templates.TemplateResponse(
            "vetbiz_imports.html",
            context(
                request,
                records=records,
                max_minutes_upload_mb=settings.max_minutes_upload_mb,
                error=str(exc),
            ),
            status_code=400,
        )
    suffix = "?duplicate=1" if creation.exact_duplicate else (
        "?revision=1" if creation.revision_warning else ""
    )
    return RedirectResponse(
        f"/vetbiz-imports/{creation.record.id}{suffix}", status_code=303
    )


@app.get("/vetbiz-imports/{import_id}", response_class=HTMLResponse)
def vetbiz_import_review(
    request: Request, import_id: int, db: Session = Depends(get_db)
):
    record = db.get(VetBizImportRecord, import_id)
    if not record:
        raise HTTPException(404, "Reviewed-minutes import not found")
    notice = ""
    if request.query_params.get("duplicate"):
        notice = "This exact document was already imported. No duplicate candidates were created."
    elif request.query_params.get("revision"):
        notice = "The meeting metadata matches an earlier import. Review this document as a possible revision."
    elif request.query_params.get("saved"):
        notice = "Candidate decision saved."
    elif request.query_params.get("bulk"):
        notice = "Safe exact-email meeting interactions were approved."
    elif group_key := request.query_params.get("bulk_group"):
        action = request.query_params.get("bulk_action", "updated")
        count = request.query_params.get("bulk_count", "0")
        group_label = VETBIZ_GROUP_LABELS.get(group_key, "Candidate")
        past_action = {"approve": "approved", "reject": "rejected"}.get(
            action, "updated"
        )
        notice = (
            f"{count} unresolved {group_label.lower()} proposal(s) {past_action}."
        )
    elif request.query_params.get("committed"):
        notice = "Approved changes were committed atomically."
    return templates.TemplateResponse(
        "vetbiz_import_review.html",
        _vetbiz_import_context(request, db, record, notice=notice),
    )


@app.post("/vetbiz-imports/{import_id}/delete")
def delete_vetbiz_import(
    request: Request,
    import_id: int,
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    record = db.get(VetBizImportRecord, import_id)
    if not record:
        raise HTTPException(404, "Reviewed-minutes import not found")
    try:
        delete_pending_import(db, record)
        db.commit()
    except VetBizImportError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse("/vetbiz-imports?deleted=1", status_code=303)


@app.post("/vetbiz-imports/{import_id}/candidates/{candidate_id}")
async def decide_vetbiz_candidate(
    request: Request,
    import_id: int,
    candidate_id: int,
    action: str = Form(...),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    candidate = db.get(VetBizImportCandidate, candidate_id)
    if not candidate or candidate.import_record_id != import_id:
        raise HTTPException(404, "Import candidate not found")
    form = await request.form()
    submitted = {
        key: str(form.get(key, ""))
        for key in EDITABLE_FIELDS.get(candidate.candidate_type, set())
    }
    matched_entity_id = str(
        form.get("matched_entity_id", candidate.matched_entity_id or "")
    )
    try:
        update_candidate(
            candidate,
            action,
            submitted,
            matched_entity_id=matched_entity_id,
            resolution_notes=str(form.get("resolution_notes", "")),
        )
        db.commit()
    except VetBizImportError as exc:
        db.rollback()
        record = db.get(VetBizImportRecord, import_id)
        return templates.TemplateResponse(
            "vetbiz_import_review.html",
            _vetbiz_import_context(request, db, record, error=str(exc)),
            status_code=400,
        )
    return RedirectResponse(
        f"/vetbiz-imports/{import_id}?saved=1#candidate-{candidate_id}",
        status_code=303,
    )


@app.post("/vetbiz-imports/{import_id}/approve-safe")
def bulk_approve_vetbiz_interactions(
    request: Request,
    import_id: int,
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    record = db.get(VetBizImportRecord, import_id)
    if not record:
        raise HTTPException(404, "Reviewed-minutes import not found")
    approve_safe_interactions(record)
    db.commit()
    return RedirectResponse(f"/vetbiz-imports/{import_id}?bulk=1", status_code=303)


@app.post("/vetbiz-imports/{import_id}/groups/{group_key}")
def bulk_decide_vetbiz_group(
    request: Request,
    import_id: int,
    group_key: str,
    action: str = Form(...),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    record = db.get(VetBizImportRecord, import_id)
    candidate_types = VETBIZ_GROUP_TYPES.get(group_key)
    if not record:
        raise HTTPException(404, "Reviewed-minutes import not found")
    if not candidate_types:
        raise HTTPException(404, "Reviewed-minutes candidate group not found")
    try:
        count = bulk_decide_candidates(record, candidate_types, action)
        db.commit()
    except VetBizImportError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "vetbiz_import_review.html",
            _vetbiz_import_context(request, db, record, error=str(exc)),
            status_code=400,
        )
    return RedirectResponse(
        f"/vetbiz-imports/{import_id}?bulk_group={group_key}"
        f"&bulk_action={action}&bulk_count={count}#{group_key}",
        status_code=303,
    )


@app.post("/vetbiz-imports/{import_id}/candidates/{candidate_id}/opportunity")
def convert_vetbiz_signal_to_opportunity(
    request: Request,
    import_id: int,
    candidate_id: int,
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    record = db.get(VetBizImportRecord, import_id)
    signal_candidate = db.get(VetBizImportCandidate, candidate_id)
    if not record or not signal_candidate:
        raise HTTPException(404, "Reviewed-minutes import or signal not found")
    try:
        propose_opportunity_from_signal(record, signal_candidate)
        db.commit()
    except VetBizImportError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "vetbiz_import_review.html",
            _vetbiz_import_context(request, db, record, error=str(exc)),
            status_code=400,
        )
    return RedirectResponse(
        f"/vetbiz-imports/{import_id}?saved=1#opportunities", status_code=303
    )


@app.post("/vetbiz-imports/{import_id}/connection-suggestions")
def create_vetbiz_connection_suggestion(
    request: Request,
    import_id: int,
    source_candidate_id: int = Form(...),
    target_candidate_id: int = Form(...),
    reason: str = Form(...),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    record = db.get(VetBizImportRecord, import_id)
    source_contact = db.get(VetBizImportCandidate, source_candidate_id)
    target_contact = db.get(VetBizImportCandidate, target_candidate_id)
    if not record or not source_contact or not target_contact:
        raise HTTPException(404, "Reviewed-minutes import or contact proposal not found")
    try:
        propose_connection_suggestion(
            record, source_contact, target_contact, reason
        )
        db.commit()
    except VetBizImportError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "vetbiz_import_review.html",
            _vetbiz_import_context(request, db, record, error=str(exc)),
            status_code=400,
        )
    return RedirectResponse(
        f"/vetbiz-imports/{import_id}?saved=1#connections", status_code=303
    )


@app.post("/vetbiz-imports/{import_id}/metadata")
def update_vetbiz_import_metadata(
    request: Request,
    import_id: int,
    meeting_title: str = Form(...),
    meeting_date: date = Form(...),
    review_notes: str = Form(""),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    record = db.get(VetBizImportRecord, import_id)
    if not record:
        raise HTTPException(404, "Reviewed-minutes import not found")
    if record.import_status == "committed":
        raise HTTPException(400, "Committed import metadata cannot be changed")
    title = meeting_title.strip()
    if not title:
        raise HTTPException(400, "Meeting title is required")
    record.meeting_title = title[:300]
    record.meeting_date = meeting_date
    record.review_notes = review_notes.strip()[:2000] or None
    db.commit()
    return RedirectResponse(f"/vetbiz-imports/{import_id}?saved=1", status_code=303)


@app.post("/vetbiz-imports/{import_id}/commit")
def commit_vetbiz_import(
    request: Request,
    import_id: int,
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    record = db.get(VetBizImportRecord, import_id)
    if not record:
        raise HTTPException(404, "Reviewed-minutes import not found")
    try:
        commit_reviewed_import(db, record)
    except VetBizImportError as exc:
        record = db.get(VetBizImportRecord, import_id)
        return templates.TemplateResponse(
            "vetbiz_import_review.html",
            _vetbiz_import_context(request, db, record, error=str(exc)),
            status_code=400,
        )
    return RedirectResponse(
        f"/vetbiz-imports/{import_id}?committed=1", status_code=303
    )


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


def segment_filters(values: dict) -> dict:
    filters = {
        key: str(values.get(key, "")).strip()
        for key in SEGMENT_FILTER_KEYS
        if str(values.get(key, "")).strip()
    }
    if filters.get("sort") == "name":
        filters.pop("sort")
    return filters


def segment_url(filters: dict) -> str:
    return "/people" + (f"?{urlencode(filters)}" if filters else "")


@app.get("/segments", response_class=HTMLResponse)
def segments_page(request: Request, db: Session = Depends(get_db)):
    segments = db.scalars(select(SavedSegment).order_by(SavedSegment.name)).all()
    rows = []
    for segment in segments:
        filters = segment_filters(segment.filters or {})
        stmt, _ = people_statement(
            filters.get("q", ""),
            filters.get("priority", ""),
            filters.get("status", ""),
            filters.get("tag", ""),
            filters.get("followup", ""),
            filters.get("sort", "name"),
        )
        count = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
        rows.append((segment, count, segment_url(filters)))
    return templates.TemplateResponse("segments.html", context(request, rows=rows))


@app.post("/segments")
def save_segment(
    request: Request,
    name: str = Form(...),
    q: str = Form(""),
    priority: str = Form(""),
    status: str = Form(""),
    tag: str = Form(""),
    followup: str = Form(""),
    sort: str = Form("name"),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(400, "Segment name is required")
    filters = segment_filters({
        "q": q,
        "priority": priority,
        "status": status,
        "tag": tag,
        "followup": followup,
        "sort": sort,
    })
    existing = db.scalar(
        select(SavedSegment).where(func.lower(SavedSegment.name) == clean_name.casefold())
    )
    if existing:
        existing.filters = filters
    else:
        db.add(SavedSegment(name=clean_name, filters=filters))
    db.commit()
    return RedirectResponse("/segments", status_code=303)


@app.post("/segments/{segment_id}/delete")
def delete_segment(
    request: Request,
    segment_id: int,
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
):
    segment = db.get(SavedSegment, segment_id)
    if not segment:
        raise HTTPException(404)
    db.delete(segment)
    db.commit()
    return RedirectResponse("/segments", status_code=303)


def valid_linkedin_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and (
        host == "linkedin.com" or host.endswith(".linkedin.com")
    )


QUALITY_LABELS = {
    "missing_email": ("Missing email", "No primary or imported email address"),
    "missing_phone": ("Missing phone", "No primary or imported phone number"),
    "missing_organization": ("Missing organization", "No current organization"),
    "missing_linkedin": ("Missing LinkedIn", "No active LinkedIn profile URL"),
    "invalid_linkedin": ("Invalid LinkedIn", "Profile URL does not point to LinkedIn"),
    "never_contacted": ("Never contacted", "No meaningful interaction recorded"),
    "no_tags": ("No tags", "Not included in any relationship group"),
    "no_cadence": ("No cadence", "No recurring follow-up rhythm"),
}


def person_quality_issues(person: Person) -> list[str]:
    method_types = {method.method_type for method in person.methods if method.value}
    linkedin_identities = [
        identity for identity in person.identities
        if identity.provider == "linkedin" and identity.active
    ]
    issues = []
    if not person.primary_email and "email" not in method_types:
        issues.append("missing_email")
    if not person.primary_phone and "phone" not in method_types:
        issues.append("missing_phone")
    if not person.current_organization:
        issues.append("missing_organization")
    if not linkedin_identities:
        issues.append("missing_linkedin")
    elif not any(valid_linkedin_url(identity.profile_url) for identity in linkedin_identities):
        issues.append("invalid_linkedin")
    if not person.last_meaningful_interaction:
        issues.append("never_contacted")
    if not person.tags:
        issues.append("no_tags")
    if not person.followup_interval_days:
        issues.append("no_cadence")
    return issues


@app.get("/data-quality", response_class=HTMLResponse)
def data_quality_page(
    request: Request,
    issue: str = "",
    db: Session = Depends(get_db),
):
    people = db.scalars(
        select(Person)
        .where(Person.archived_at.is_(None))
        .options(
            selectinload(Person.methods),
            selectinload(Person.identities),
            selectinload(Person.tags),
        )
        .order_by(Person.display_name)
    ).all()
    issues_by_person = {person.id: person_quality_issues(person) for person in people}
    counts = {
        code: sum(code in issues for issues in issues_by_person.values())
        for code in QUALITY_LABELS
    }
    selected = issue if issue in QUALITY_LABELS else ""
    visible = [
        person for person in people
        if issues_by_person[person.id] and (
            not selected or selected in issues_by_person[person.id]
        )
    ]
    visible.sort(key=lambda person: (-len(issues_by_person[person.id]), person.display_name))
    return templates.TemplateResponse("data_quality.html", context(
        request,
        counts=counts,
        labels=QUALITY_LABELS,
        selected=selected,
        people=visible[:100],
        issues_by_person=issues_by_person,
        total_with_issues=sum(bool(issues) for issues in issues_by_person.values()),
    ))


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


def _contact_values(person: Person, method_type: str, primary_value: str | None) -> list[tuple[str, str]]:
    values = []
    seen = set()
    if primary_value:
        values.append(("Primary", primary_value))
        seen.add(primary_value.casefold())
    for method in person.methods:
        if method.method_type != method_type or not method.value:
            continue
        normalized = method.value.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        values.append((method.label or "Other", method.value))
    return values


def _profile_urls(person: Person) -> list[tuple[str, str]]:
    values = []
    seen = set()
    for identity in person.identities:
        if not identity.active or not identity.profile_url:
            continue
        normalized = identity.profile_url.rstrip("/").casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        label = "LinkedIn" if identity.provider == "linkedin" else identity.provider.replace("_", " ").title()
        values.append((label, identity.profile_url))
    return values


@app.get("/export/google.csv")
def export_google_contacts(db: Session = Depends(get_db)):
    people = db.scalars(
        select(Person)
        .where(Person.archived_at.is_(None))
        .options(
            selectinload(Person.tags),
            selectinload(Person.methods),
            selectinload(Person.identities),
        )
        .order_by(Person.display_name)
    ).all()
    records = [
        (
            person,
            _contact_values(person, "email", person.primary_email),
            _contact_values(person, "phone", person.primary_phone),
            _profile_urls(person),
        )
        for person in people
    ]
    max_emails = max((len(record[1]) for record in records), default=1)
    max_phones = max((len(record[2]) for record in records), default=1)
    max_websites = max((len(record[3]) for record in records), default=1)
    max_emails = max(1, max_emails)
    max_phones = max(1, max_phones)
    max_websites = max(1, max_websites)

    header = ["First Name", "Middle Name", "Last Name", "Nickname", "File as"]
    for index in range(1, max_emails + 1):
        header.extend([f"Email {index} - Label", f"Email {index} - Value"])
    for index in range(1, max_phones + 1):
        header.extend([f"Phone {index} - Label", f"Phone {index} - Value"])
    header.extend(["Organization Name", "Organization Title"])
    for index in range(1, max_websites + 1):
        header.extend([f"Website {index} - Label", f"Website {index} - Value"])
    custom_fields = (
        "Constellation ID",
        "Priority",
        "Relationship Status",
        "Last Contact",
        "Next Follow-up",
        "Location",
        "Obsidian URI",
    )
    for index in range(1, len(custom_fields) + 1):
        header.extend([f"Custom Field {index} - Label", f"Custom Field {index} - Value"])
    header.extend(["Notes", "Labels"])

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(header)
    for person, emails, phones, websites in records:
        first_name = person.first_name
        if not first_name and not person.last_name:
            first_name = person.display_name
        row = [
            first_name or "",
            person.middle_name or "",
            person.last_name or "",
            person.preferred_name or "",
            person.display_name,
        ]
        for values, maximum in ((emails, max_emails), (phones, max_phones)):
            for index in range(maximum):
                if index < len(values):
                    row.extend(values[index])
                else:
                    row.extend(["", ""])
        row.extend([person.current_organization or "", person.current_title or ""])
        for index in range(max_websites):
            if index < len(websites):
                row.extend(websites[index])
            else:
                row.extend(["", ""])
        custom_values = (
            person.id,
            person.priority,
            person.relationship_status,
            person.last_meaningful_interaction,
            person.next_followup,
            person.location,
            person.obsidian_uri,
        )
        for label, value in zip(custom_fields, custom_values, strict=True):
            row.extend([label, value or ""])
        row.extend([
            person.general_note or "",
            " ::: ".join(sorted(tag.name for tag in person.tags)),
        ])
        writer.writerow([safe_csv_cell(value) for value in row])
    headers = {
        "Content-Disposition": 'attachment; filename="constellation-google-contacts.csv"'
    }
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers=headers,
    )


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
