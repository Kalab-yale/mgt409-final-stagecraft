"""Stagecraft AI web server: JSON API + the Mission Control UI.

Run with `python main.py` (http://127.0.0.1:8000) or `uvicorn backend.app:app --reload`.
Endpoints are plain `def`, so FastAPI runs them in a worker thread - the agents' blocking LLM calls
never stall the server.
"""

from __future__ import annotations

import html
import re
from urllib.parse import quote

import httpx
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from backend import auth
from backend import database as db
from backend import changes, notify, planning, security, team, tokenomics, tools
from backend.agents.assistants import TABS as ASSISTANT_TABS
from backend.agents.assistants import run_assistant_turn
from backend.agents import team_writers
from backend.agents.brief_writer import get_or_create_brief
from backend.agents.inbox_watcher import add_pasted_email, process_email, process_inbox
from backend.agents.onboarding_agent import (
    generate_event_from_prompt,
    import_event_from_csv_custom_city,
    import_event_from_csv_or_template,
)
from backend.config import STATIC_DIR, TEMPLATES_DIR
from backend.benchmark import list_benchmarks, run_cost_quality_benchmark
from backend.evidence import SCALE_TIERS, build_evidence_bundle, system_status
from backend.agents.onboarding_agent import geocode_city
from backend.connectors import gmail
from backend.connectors import google_gmail
from backend.orchestrator import build_event_calendar, commit_resolution_plan, list_war_room_runs, run_crisis_war_room_pipeline


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()  # tables + seed (YBVS 2026 at Evans Hall, Tech & AI Summit at Pier 27) on first run
    tokenomics.load_cost_saver_mode()  # restore the Settings toggle
    yield


# No public API docs (/docs, /redoc, /openapi.json): they would hand anyone a map of every endpoint.
app = FastAPI(title="Stagecraft AI", version="0.16.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


PUBLIC_API = re.compile(r"^/api/(auth/(login|signup|logout|me)|events/[^/]+/calendar\.ics)$")  # everything else under /api needs a login


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Every /api call needs a valid session cookie, except logging in/out and the public attendee calendar.
    Pages and static files load for everyone; the app shows the login screen when /api/auth/me says 401."""
    path = request.url.path
    if path.startswith("/api/") and not PUBLIC_API.match(path):
        user = await run_in_threadpool(auth.user_for_token, request.cookies.get(auth.COOKIE))
        if user is None:
            return JSONResponse(status_code=401, content={"detail": "Please log in."})
        request.state.user = user
    return await call_next(request)


CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
       "font-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
CLASSIC_CSP = "frame-ancestors 'none'; object-src 'none'; base-uri 'self'"  # the first-build console loads CDN scripts inline


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Browser protections on every response: no framing (clickjacking), scripts only from this site, no MIME sniffing."""
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = CLASSIC_CSP if request.url.path == "/classic" else CSP
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


@app.middleware("http")
async def revalidate_ui_assets(request: Request, call_next):
    """UI modules import each other without version tags; make browsers revalidate them (cheap, via ETag)."""
    response = await call_next(request)
    if request.url.path.startswith("/static/ui/"):
        response.headers["Cache-Control"] = "no-cache"
    return response
templates = Jinja2Templates(directory=TEMPLATES_DIR)


# ---- Payload builders ---------------------------------------------------------------------------


def _event_payload(event_id: str) -> dict:
    """Full workspace state for one event, enriched for the UI (utilization, speaker chips, AV gaps, badges)."""
    try:
        state = db.get_event_full_state(event_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown event '{event_id}'.")
    rooms = {r.id: r for r in state.rooms}
    speakers = {sp.id: sp for sp in state.speakers}
    badges = db.get_session_update_badges(event_id)

    room_cards = []
    for r in state.rooms:
        in_room = [s for s in state.sessions if s.room_id == r.id]
        peak = max((s.expected_attendance for s in in_room), default=0)
        room_cards.append(r.model_dump() | {"session_count": len(in_room), "peak_attendance": peak,
                                            "utilization_pct": round(100 * peak / r.capacity, 1)})
    sessions = []
    for s in state.sessions:
        room = rooms.get(s.room_id)
        av = tools._match_av(s.required_av, room.av_specs if room else [])
        sessions.append(s.model_dump() | {
            "room": {"id": s.room_id, "name": room.name if room else "?", "capacity": room.capacity if room else 0,
                     "status": room.status if room else "Closed", "floor_or_zone": room.floor_or_zone if room else ""},
            "utilization_pct": round(100 * s.expected_attendance / room.capacity, 1) if room else None,
            "av_gaps": av["missing_av"] + av["portable_backup_needed"],
            "speakers": [{"id": sp.id, "name": sp.name, "vip_tier": sp.vip_tier, "status": sp.status, "travel_origin": sp.travel_origin}
                         for sp in (speakers.get(i) for i in s.speaker_ids) if sp],
            "war_room_update": badges.get(s.id),
        })
    overview = tools.get_event_overview_tool(event_id)
    return {
        "event": state.event.model_dump(mode="json"),
        "venue": state.venue.model_dump(),
        "rooms": room_cards,
        "sessions": sessions,
        "speakers": [sp.model_dump() for sp in state.speakers],
        "catering_slas": [c.model_dump() | {"room_name": rooms[c.room_id].name if c.room_id in rooms else c.room_id}
                          for c in state.catering_slas],
        "crisis_presets": [c.model_dump() for c in state.crisis_presets],
        "active_conflicts": overview["active_conflicts"],
        "tracks": sorted({s.track for s in state.sessions}),
    }


# ---- Accounts ------------------------------------------------------------------------------------


class SignupRequest(BaseModel):
    first_name: str = Field(max_length=60)
    last_name: str = Field(max_length=60)
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)
    confirm_password: Optional[str] = Field(default=None, max_length=128)


class LoginRequest(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


def _user_json(user: auth.User) -> dict:
    return {"user": user.model_dump() | {"display_name": user.display_name}}


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _set_session_cookie(request: Request, response: JSONResponse, user_id: str) -> None:
    response.set_cookie(auth.COOKIE, auth.create_session(user_id, request.headers.get("user-agent", "")), max_age=auth.SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", secure=request.url.scheme == "https", path="/")


def _session_response(request: Request, user: auth.User) -> JSONResponse:
    response = JSONResponse(_user_json(user))
    _set_session_cookie(request, response, user.id)
    return response


def _auth_error(exc: auth.AuthError, status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=429 if isinstance(exc, auth.RateLimited) else status, content={"error": str(exc)})


@app.post("/api/auth/signup")
def signup(request: Request, body: SignupRequest):
    """Create an account and log it in. The password is hashed with scrypt; it is never stored or logged."""
    try:
        auth.check_rate(_client(request), "signup")
        if body.confirm_password is not None and body.confirm_password != body.password:
            raise auth.AuthError("The two passwords don't match.")
        user = auth.create_user(body.first_name, body.last_name, body.email, body.password)
    except auth.AuthError as exc:
        return _auth_error(exc)
    return _session_response(request, user)


@app.post("/api/auth/login")
def login(request: Request, body: LoginRequest):
    try:
        auth.check_rate(_client(request), "login")
        user = auth.authenticate(body.email, body.password)
    except auth.AuthError as exc:
        return _auth_error(exc, 401 if "incorrect" in str(exc) else 429)
    return _session_response(request, user)


@app.post("/api/auth/logout")
def logout(request: Request):
    auth.end_session(request.cookies.get(auth.COOKIE))
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE, path="/")
    return response


@app.get("/api/auth/me")
def me(request: Request):
    user = auth.user_for_token(request.cookies.get(auth.COOKIE))
    if user is None:
        return JSONResponse(status_code=401, content={"detail": "Please log in."})
    return _user_json(user)


# ---- Account settings (all need a session; sensitive ones need the current password too) ----------


class ProfileUpdate(BaseModel):
    first_name: str = Field(max_length=60)
    last_name: str = Field(max_length=60)


class EmailChange(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=128)
    new_password: str = Field(max_length=128)
    confirm_password: Optional[str] = Field(default=None, max_length=128)


class Preferences(BaseModel):
    theme: Optional[str] = Field(default=None, max_length=10)
    accent: Optional[str] = Field(default=None, max_length=10)


class DeleteAccount(BaseModel):
    password: str = Field(max_length=128)
    confirm: str = Field(max_length=20)


@app.post("/api/account/profile")
def account_profile(request: Request, body: ProfileUpdate):
    try:
        return _user_json(auth.update_profile(request.state.user, body.first_name, body.last_name))
    except auth.AuthError as exc:
        return _auth_error(exc)


@app.post("/api/account/email")
def account_email(request: Request, body: EmailChange):
    try:
        auth.check_rate(_client(request), "sensitive")
        return _user_json(auth.change_email(request.state.user, body.email, body.password))
    except auth.AuthError as exc:
        return _auth_error(exc)


@app.post("/api/account/password")
def account_password(request: Request, body: PasswordChange):
    """New password; every other session is signed out, and this one gets a fresh token."""
    try:
        auth.check_rate(_client(request), "sensitive")
        if body.confirm_password is not None and body.confirm_password != body.new_password:
            raise auth.AuthError("The two new passwords don't match.")
        ended = auth.change_password(request.state.user, body.current_password, body.new_password, keep_token=None)
    except auth.AuthError as exc:
        return _auth_error(exc)
    response = JSONResponse({"ok": True, "other_sessions_ended": max(0, ended - 1)})
    _set_session_cookie(request, response, request.state.user.id)  # all old tokens are gone; issue a new one for this browser
    return response


@app.get("/api/account/sessions")
def account_sessions(request: Request):
    return {"sessions": auth.list_sessions(request.state.user, request.cookies.get(auth.COOKIE))}


@app.post("/api/account/sessions/revoke-others")
def account_revoke_others(request: Request):
    return {"ended": auth.end_other_sessions(request.state.user, request.cookies.get(auth.COOKIE))}


@app.delete("/api/account/sessions/{session_id}")
def account_revoke_one(request: Request, session_id: str):
    if not auth.end_session_by_id(request.state.user, session_id):
        raise HTTPException(status_code=404, detail="No such session.")
    return {"ok": True}


@app.post("/api/account/preferences")
def account_preferences(request: Request, body: Preferences):
    try:
        return _user_json(auth.set_preferences(request.state.user, body.theme, body.accent))
    except auth.AuthError as exc:
        return _auth_error(exc)


@app.post("/api/account/delete")
def account_delete(request: Request, body: DeleteAccount):
    try:
        auth.check_rate(_client(request), "sensitive")
        auth.delete_account(request.state.user, body.password, body.confirm)
    except auth.AuthError as exc:
        return _auth_error(exc)
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE, path="/")
    return response


# ---- Pages ---------------------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """The event-planner workspace (design A). Also where Google sends the browser back after "Connect Gmail"."""
    if google_gmail.is_callback(request.query_params):
        user = auth.user_for_token(request.cookies.get(auth.COOKIE))
        try:
            return_to, address = google_gmail.finish_connect(user, request.query_params)
            return RedirectResponse(f"/?gmail=connected{return_to}", status_code=303)
        except (google_gmail.GmailError, httpx.HTTPError) as exc:
            return RedirectResponse(f"/?gmail_error={quote(str(exc)[:200])}#/", status_code=303)
    return templates.TemplateResponse(request, "app.html", {"app_version": app.version})


@app.get("/classic", response_class=HTMLResponse)
def classic(request: Request):
    """The original dark mission-control console (crisis resolver + settings) until they move into the new UI."""
    return templates.TemplateResponse(request, "classic.html", {"app_version": app.version})


def _resolve_event(event_id_or_slug: str):
    return db.get_event(event_id_or_slug) or next((e for e in db.get_events() if e.slug == event_id_or_slug), None)


@app.get("/public/{event_id}")
def public_redirect(event_id: str):
    return RedirectResponse(url=f"/live/{event_id}", status_code=307)


@app.get("/live/{event_id}", response_class=HTMLResponse)
def public_live_page(request: Request, event_id: str):
    """Attendee-facing live schedule (mobile-first). Accepts the event id or its slug."""
    event = _resolve_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail=f"Unknown event '{event_id}'.")
    payload = _event_payload(event.id)
    history = db.get_resolution_history(event.id)
    latest = history[0] if history else None
    return templates.TemplateResponse(request, "public_live.html", {
        "event": payload["event"], "venue": payload["venue"], "sessions": payload["sessions"],
        "date_label": f"{event.date:%A, %B} {event.date.day}, {event.date.year}",
        "unavailable_rooms": [r | {"still_listed": [x["title"] for x in payload["sessions"] if x["room"]["id"] == r["id"]]}
                              for r in payload["rooms"] if r["status"] != "Operational"],
        "banner": (latest["public_banner"] or f"Schedule updated: {latest['option_label']}") if latest else None,
        "banner_time": f"{datetime.fromisoformat(latest['committed_at']):%b} {datetime.fromisoformat(latest['committed_at']).day}, "
                       f"{datetime.fromisoformat(latest['committed_at']):%H:%M} UTC" if latest else None,
        "update_count": len(history),
    })


# ---- Events API ----------------------------------------------------------------------------------


@app.get("/api/events")
def list_events():
    venues = db.get_venues()
    venue_names = {v.id: v for v in venues}
    return {
        "events": [e.model_dump(mode="json") | {"venue_name": venue_names[e.venue_id].name, "venue_city": venue_names[e.venue_id].city}
                   for e in db.get_events() if e.venue_id in venue_names],
        "venues": [v.model_dump() | {"rooms": [{"id": r.id, "name": r.name, "capacity": r.capacity, "is_catering_hub": r.is_catering_hub}
                                             for r in db.get_rooms(v.id)]} for v in venues],
        "cost_saver_mode": tokenomics.get_cost_saver_mode(),
    }


@app.get("/api/events/{event_id}")
def get_event(event_id: str):
    return _event_payload(event_id)


@app.get("/api/home")
def home():
    """Home screen: one card per event with its health (days to go, progress, red flags, next deadline)."""
    return {"today": planning.demo_today().isoformat(), "events": planning.home_cards()}


@app.get("/api/events/{event_id}/planning")
def event_planning(request: Request, event_id: str):
    """Deadlines, initiatives + tasks, attendees, budget, vendors, inbox, and red flags for one event, plus `me`:
    which team member the signed-in person is here and whether they may approve budget changes."""
    if db.get_event(event_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown event '{event_id}'.")
    return planning.get_planning_state(event_id) | {"me": notify.access_for(event_id, request.state.user)}


@app.get("/api/events/{event_id}/weather")
def get_event_weather(event_id: str):
    result = tools.check_live_venue_weather_and_transit_tool(event_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=10, max_length=4000)


def _creator_runs_it(request: Request, result: dict) -> dict:
    """The person who creates an event becomes its PMO and a Lead, so they can assign work and approve budget changes."""
    event_id = result.get("event_id")
    if event_id and db.get_event(event_id):
        m = team.add_creator(event_id, request.state.user)
        if m:
            changes.log_activity(event_id, _actor(request), "team", f"{m.name} created the event and is its PMO", m.id)
            result["you"] = {"member_id": m.id, "is_pmo": True, "access": "Lead"}
    return result


@app.post("/api/events/generate-ai")
def generate_event(request: Request, body: GenerateRequest):
    result = generate_event_from_prompt(body.prompt)
    if result.get("ok"):
        return _creator_runs_it(request, result)
    return JSONResponse(status_code=403 if result.get("blocked") else 422, content=result)


class ImportRequest(BaseModel):
    event_name: str = Field(min_length=3)
    event_date: str
    attendee_count: int = Field(gt=0)
    venue_id: Optional[str] = None          # existing venue template
    csv_text: Optional[str] = None
    template_event_id: Optional[str] = None  # clone an existing event's programme
    custom_city: Optional[str] = None        # brand-new city (geocoded) instead of venue_id
    custom_venue_name: Optional[str] = None
    airport_corridor: str = ""
    event_slug: Optional[str] = None
    description: str = ""


@app.post("/api/events/import-csv")
def import_event(request: Request, body: ImportRequest):
    if body.custom_city:
        if not body.csv_text:
            raise HTTPException(status_code=400, detail="A custom city needs CSV rows (rooms are inferred from them).")
        result = import_event_from_csv_custom_city(body.event_name, body.event_date, body.attendee_count, body.custom_city,
                                                   body.custom_venue_name or "", body.csv_text, body.airport_corridor,
                                                   body.event_slug, body.description)
    else:
        venue_id = body.venue_id
        if body.template_event_id and not venue_id:
            template = db.get_event(body.template_event_id)
            venue_id = template.venue_id if template else None
        if not venue_id:
            raise HTTPException(status_code=400, detail="Choose a venue template, a template event, or a custom city.")
        result = import_event_from_csv_or_template(body.event_name, body.event_date, body.attendee_count, venue_id,
                                                   csv_text=body.csv_text, template_event_id=body.template_event_id,
                                                   event_slug=body.event_slug, description=body.description)
    if result.get("ok"):
        return _creator_runs_it(request, result)
    return JSONResponse(status_code=403 if result.get("blocked") else 400, content=result)


@app.delete("/api/events/{event_id}")
def delete_event(event_id: str):
    try:
        return db.delete_event(event_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown event '{event_id}'.")


# ---- Tab assistants and confirm-before-save ----------------------------------------------------------


def _require_event(event_id: str) -> None:
    if db.get_event(event_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown event '{event_id}'.")


@app.get("/api/assistants")
def assistant_tabs():
    return {"tabs": [{"id": k, "label": v["label"], "dimension": v["dimension"], "editable": sorted(v["records"])}
                     for k, v in ASSISTANT_TABS.items()]}


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=6000)


@app.post("/api/events/{event_id}/assistants/{tab}/chat")
def assistant_chat(event_id: str, tab: str, body: ChatRequest):
    """One chat turn with a tab assistant. Edits come back as pending changes to confirm or discard."""
    _require_event(event_id)
    if tab not in ASSISTANT_TABS:
        raise HTTPException(status_code=404, detail=f"Unknown tab '{tab}'.")
    return run_assistant_turn(event_id, tab, body.message)


@app.get("/api/events/{event_id}/assistants/{tab}/history")
def assistant_history(event_id: str, tab: str, limit: int = 50):
    _require_event(event_id)
    if tab not in ASSISTANT_TABS:
        raise HTTPException(status_code=404, detail=f"Unknown tab '{tab}'.")
    return {"messages": changes.chat_history(event_id, tab, limit), "pending": changes.list_changes(event_id, tab)}


@app.get("/api/events/{event_id}/changes")
def list_pending_changes(event_id: str, status: str = "Pending", tab: Optional[str] = None):
    _require_event(event_id)
    return {"changes": changes.list_changes(event_id, tab, status or None)}


def _decide(request: Request, event_id: str, change_id: str, confirm: bool) -> dict:
    change = changes.get_change(change_id)
    if change is None or change["event_id"] != event_id:
        raise HTTPException(status_code=404, detail=f"Unknown change '{change_id}'.")
    user, actor = request.state.user, _actor(request)
    try:
        result = (changes.confirm_change(change_id, actor, user.id, notify.access_for(event_id, user)["can_approve_budget"]) if confirm
                  else changes.reject_change(change_id, actor))
    except changes.ChangeError as exc:
        return JSONResponse(status_code=409, content={"error": str(exc), "change": change})
    return {"change": result, "health": planning.event_health(event_id)}


@app.post("/api/events/{event_id}/changes/{change_id}/confirm")
def confirm_change(request: Request, event_id: str, change_id: str):
    """Apply a drafted change after re-validating it against current data (stale edits are refused)."""
    return _decide(request, event_id, change_id, confirm=True)


@app.post("/api/events/{event_id}/changes/{change_id}/reject")
def reject_change(request: Request, event_id: str, change_id: str):
    return _decide(request, event_id, change_id, confirm=False)


@app.get("/api/events/{event_id}/activity")
def event_activity(event_id: str, limit: int = 100):
    _require_event(event_id)
    return {"activity": changes.list_activity(event_id, limit=limit)}


class RecordEdit(BaseModel):
    changes: dict


@app.post("/api/events/{event_id}/records/{record_type}/{record_id}")
def edit_record(request: Request, event_id: str, record_type: str, record_id: str, body: RecordEdit):
    """A person's own edit (e.g. ticking a task done). Applied right away, still validated and logged.
    Assistant edits go through drafts and Confirm instead."""
    _require_event(event_id)
    if record_type not in changes.RECORD_TYPES:
        raise HTTPException(status_code=404, detail=f"Unknown record type '{record_type}'.")
    table = changes.RECORD_TYPES[record_type][0]
    record = db.get_record(table, record_id)
    if record is None or record.event_id != event_id:
        raise HTTPException(status_code=404, detail=f"Unknown {record_type} '{record_id}'.")
    if table == "budget_items" and not notify.access_for(event_id, request.state.user)["can_approve_budget"]:
        return JSONResponse(status_code=403, content={"error": "Only the PMO or an event lead can change the budget."})
    try:
        fields = (team.normalize_task_fields(event_id, body.changes) if table == "tasks"
                  else team.validate_member_fields(body.changes) if table == "team_members" else body.changes)
        updated = db.update_record(table, record_id, **fields)
        if table == "tasks" and "assignees" in fields:
            notify.assignment_changed(event_id, updated, record.assignees, updated.assignees, request.state.user.id, _actor(request))
        if table == "team_members":
            team.after_member_saved(event_id, record_id)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc).splitlines()[0], "detail": str(exc)})
    label = getattr(updated, "title", None) or getattr(updated, "name", None) or getattr(updated, "description", record_id)
    shown = {k: (team.names_for(event_id, v) if k == "assignees" else v) for k, v in fields.items() if not (k == "owner" and "assignees" in fields)}
    changes.log_activity(event_id, _actor(request), "edit", f"Updated {record_type.replace('_', ' ')} '{label}': "
                         + ", ".join(f"{k} = {v}" for k, v in shown.items()), record_id)
    return {"record": updated.model_dump(mode="json"), "health": planning.event_health(event_id)}


PERSON_CREATABLE = {"member", "task", "deadline"}


class RecordCreate(BaseModel):
    fields: dict


@app.post("/api/events/{event_id}/records/{record_type}")
def create_record(request: Request, event_id: str, record_type: str, body: RecordCreate):
    """A person adds a team member, task, or deadline directly (validated, applied right away, logged)."""
    _require_event(event_id)
    if record_type not in PERSON_CREATABLE:
        raise HTTPException(status_code=404, detail=f"Can't create '{record_type}' here.")
    table, prefix = changes.RECORD_TYPES[record_type]
    fields = {k: v for k, v in body.fields.items() if k not in ("id", "event_id")} | {"event_id": event_id}
    if table in ("tasks", "deadlines", "team_members"):
        fields.setdefault("source", "user")
    try:
        if table == "tasks":
            fields = team.normalize_task_fields(event_id, fields)
        elif table == "team_members":
            fields = team.validate_member_fields(fields)
        record = db.create_record(table, prefix, **fields)
        if table == "team_members":
            team.after_member_saved(event_id, record.id)
        if table == "tasks" and record.assignees:
            notify.assignment_changed(event_id, record, [], record.assignees, request.state.user.id, _actor(request))
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc).splitlines()[0], "detail": str(exc)})
    label = getattr(record, "title", None) or getattr(record, "name", record.id)
    changes.log_activity(event_id, _actor(request), "create", f"Added {record_type} '{label}'", record.id)
    return {"record": record.model_dump(mode="json"), "health": planning.event_health(event_id)}


@app.delete("/api/events/{event_id}/records/{record_type}/{record_id}")
def delete_record(request: Request, event_id: str, record_type: str, record_id: str):
    _require_event(event_id)
    if record_type not in PERSON_CREATABLE:
        raise HTTPException(status_code=404, detail=f"Can't delete '{record_type}' here.")
    table = changes.RECORD_TYPES[record_type][0]
    record = db.get_record(table, record_id)
    if record is None or record.event_id != event_id:
        raise HTTPException(status_code=404, detail=f"Unknown {record_type} '{record_id}'.")
    untouched = team.remove_member(event_id, record_id) if table == "team_members" else (db.delete_record(table, record_id) and 0)
    label = getattr(record, "title", None) or getattr(record, "name", record_id)
    changes.log_activity(event_id, _actor(request), "delete", f"Removed {record_type} '{label}'"
                         + (f" (taken off {untouched} task{'s' if untouched != 1 else ''})" if untouched else ""), record_id)
    return {"ok": True, "health": planning.event_health(event_id)}


# ---- Notifications and "My work" -------------------------------------------------------------------


@app.get("/api/notifications")
def notifications(request: Request):
    return notify.for_user(request.state.user)


class ReadRequest(BaseModel):
    ids: Optional[list[str]] = None  # omit to mark everything read


@app.post("/api/notifications/read")
def notifications_read(request: Request, body: ReadRequest = ReadRequest()):
    ids = [i for i in (body.ids or []) if not i.startswith("approvals:")] or None
    return {"marked": notify.mark_read(request.state.user, ids if body.ids else None)}


@app.get("/api/me/work")
def my_work(request: Request):
    return notify.my_work(request.state.user)


class PmoRequest(BaseModel):
    member_id: str


@app.post("/api/events/{event_id}/team/pmo")
def set_pmo(request: Request, event_id: str, body: PmoRequest):
    _require_event(event_id)
    try:
        team.set_pmo(event_id, body.member_id)
    except team.TeamError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    name = db.get_record("team_members", body.member_id).name
    changes.log_activity(event_id, _actor(request), "team", f"Made {name} the PMO", body.member_id)
    return {"team": team.workload(event_id)}


# ---- Team updates, status reports, meeting notes, post-event feedback ---------------------------------


def _lead_only(request: Request, event_id: str, what: str):
    if not notify.access_for(event_id, request.state.user)["is_lead"]:
        return JSONResponse(status_code=403, content={"error": f"Only the PMO or an event lead can {what}."})
    return None


def _ai_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, team_writers.Blocked):
        return JSONResponse(status_code=403, content={"error": "That text looks like a prompt-injection attempt, so it was blocked and logged.",
                                                      "blocked": True, "incident_id": exc.check.get("incident_id")})
    if isinstance(exc, ValueError):
        return JSONResponse(status_code=422, content={"error": str(exc)})
    return JSONResponse(status_code=502, content={"error": f"The AI couldn't finish just now ({type(exc).__name__}). Try again."})


@app.get("/api/events/{event_id}/announcements")
def list_announcements(request: Request, event_id: str):
    _require_event(event_id)
    return {"announcements": notify.announcements(event_id), "can_post": notify.access_for(event_id, request.state.user)["is_lead"]}


class AnnouncementIn(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    body: str = Field(min_length=3, max_length=2000)
    ai_drafted: bool = False


@app.post("/api/events/{event_id}/announcements")
def post_announcement(request: Request, event_id: str, body: AnnouncementIn):
    _require_event(event_id)
    if denied := _lead_only(request, event_id, "post a team update"):
        return denied
    check = security.inspect_inbound_input(event_id, f"{body.title}\n{body.body}", source="team_update")
    if not check["allowed"]:
        return _ai_error(team_writers.Blocked(check))
    row = notify.announce(event_id, body.title.strip(), body.body.strip(), request.state.user.id, _actor(request), body.ai_drafted)
    changes.log_activity(event_id, _actor(request), "announcement", f"Posted a team update: {row['title']} (sent to {row['notified']})", row["id"])
    return row


class DraftNote(BaseModel):
    note: str = Field(default="", max_length=500)


@app.post("/api/events/{event_id}/announcements/draft")
def draft_announcement(request: Request, event_id: str, body: DraftNote = DraftNote()):
    _require_event(event_id)
    if denied := _lead_only(request, event_id, "post a team update"):
        return denied
    if body.note.strip() and not security.inspect_inbound_input(event_id, body.note, source="team_update")["allowed"]:
        return _ai_error(team_writers.Blocked({}))
    try:
        return team_writers.draft_announcement(event_id, body.note)
    except Exception as exc:  # noqa: BLE001 - shown to the person as a retryable error
        return _ai_error(exc)


class StatusReportIn(BaseModel):
    audience: str = "chair"


@app.post("/api/events/{event_id}/status-report")
def write_status_report(request: Request, event_id: str, body: StatusReportIn):
    _require_event(event_id)
    if body.audience not in team_writers.AUDIENCES:
        return JSONResponse(status_code=422, content={"error": f"Audience must be one of {', '.join(team_writers.AUDIENCES)}."})
    try:
        return team_writers.status_report(event_id, body.audience, _actor(request))
    except Exception as exc:  # noqa: BLE001
        return _ai_error(exc)


class NotesIn(BaseModel):
    notes: str = Field(max_length=team_writers.MAX_NOTES_CHARS + 1000)


@app.post("/api/events/{event_id}/team/notes-to-tasks")
def meeting_notes_to_tasks(event_id: str, body: NotesIn):
    _require_event(event_id)
    try:
        return team_writers.notes_to_tasks(event_id, body.notes)
    except Exception as exc:  # noqa: BLE001
        return _ai_error(exc)


@app.get("/api/events/{event_id}/feedback")
def event_feedback(event_id: str):
    _require_event(event_id)
    return {"summary": team_writers.latest_feedback(event_id)}


class FeedbackIn(BaseModel):
    responses: str = Field(max_length=team_writers.MAX_FEEDBACK_CHARS + 1000)


@app.post("/api/events/{event_id}/feedback/summarize")
def summarize_feedback(event_id: str, body: FeedbackIn):
    _require_event(event_id)
    try:
        return team_writers.summarize_feedback(event_id, body.responses)
    except Exception as exc:  # noqa: BLE001
        return _ai_error(exc)


def _actor(request: Request) -> str:
    user = getattr(request.state, "user", None)
    return user.display_name if user else "organizer"


# ---- Inbox Watcher, flags, and the Daily Brief -------------------------------------------------------


@app.get("/api/events/{event_id}/inbox")
def get_inbox(event_id: str):
    """Emails newest first, with the Watcher's triage and the changes it drafted from each one."""
    _require_event(event_id)
    drafted: dict[str, list] = {}
    for c in changes.list_changes(event_id, status=None):
        if (c.get("origin") or "").startswith("inbox:"):
            drafted.setdefault(c["origin"].split(":", 1)[1], []).append(c)
    msgs = sorted(db.list_records("inbox_messages", event_id), key=lambda m: m.received_at, reverse=True)
    flags = db.list_records("flags", event_id)
    return {"messages": [m.model_dump() | {"changes": drafted.get(m.id, []),
                                           "flags": [f.model_dump() for f in flags if f.source_ref == m.id]} for m in msgs],
            "counts": {st: sum(m.status == st for m in msgs) for st in ("New", "Needs Reply", "Triaged", "Done", "Quarantined")}}


class ProcessRequest(BaseModel):
    limit: Optional[int] = None


@app.post("/api/events/{event_id}/inbox/process")
def run_inbox_watcher(event_id: str, body: ProcessRequest = ProcessRequest()):
    """Run the Inbox Watcher over every New email (a few in parallel)."""
    _require_event(event_id)
    return process_inbox(event_id, body.limit)


class PasteRequest(BaseModel):
    from_name: str = ""
    from_email: str = Field(min_length=3)
    subject: str = ""
    body: str = Field(min_length=1, max_length=20000)


@app.post("/api/events/{event_id}/inbox/paste")
def paste_email(event_id: str, body: PasteRequest):
    """Paste or forward an email; it is stored and processed right away."""
    _require_event(event_id)
    mid = add_pasted_email(event_id, body.from_name, body.from_email, body.subject, body.body)
    return process_email(event_id, mid)


class InboxUpdate(BaseModel):
    status: Optional[str] = None
    reply_draft: Optional[str] = None


@app.post("/api/events/{event_id}/inbox/{message_id}")
def update_email(event_id: str, message_id: str, body: InboxUpdate):
    """Edit a reply draft or mark an email handled. Replies are never sent automatically."""
    _require_event(event_id)
    m = db.get_record("inbox_messages", message_id)
    if m is None or m.event_id != event_id:
        raise HTTPException(status_code=404, detail=f"Unknown email '{message_id}'.")
    fields = {k: v for k, v in (("status", body.status), ("reply_draft", body.reply_draft)) if v is not None}
    if "reply_draft" in fields:
        fields["reply_draft"] = security.outbound_pii_scrubber(fields["reply_draft"])
    try:
        return db.update_record("inbox_messages", message_id, **fields).model_dump()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc).splitlines()[0])


@app.get("/api/events/{event_id}/flags")
def event_flags(event_id: str, include_closed: bool = False):
    _require_event(event_id)
    return {"flags": planning.get_flags(event_id, include_closed)}


class FlagUpdate(BaseModel):
    status: str = Field(pattern="^(Resolved|Dismissed|Open)$")


@app.post("/api/events/{event_id}/flags/{flag_id}")
def update_flag(request: Request, event_id: str, flag_id: str, body: FlagUpdate):
    """Resolve or dismiss a stored flag. Rule-based flags clear on their own once the underlying data is fixed."""
    _require_event(event_id)
    if flag_id.startswith("sys_"):
        raise HTTPException(status_code=400, detail="This flag comes from a rule; it clears itself once the underlying data is fixed.")
    f = db.get_record("flags", flag_id)
    if f is None or f.event_id != event_id:
        raise HTTPException(status_code=404, detail=f"Unknown flag '{flag_id}'.")
    updated = db.update_record("flags", flag_id, status=body.status)
    changes.log_activity(event_id, _actor(request), "flag", f"Marked flag '{f.title}' {body.status.lower()}", flag_id)
    return updated.model_dump()


@app.get("/api/events/{event_id}/brief")
def daily_brief(event_id: str, regenerate: bool = False):
    """Today's brief (generated on first request each day, then cached). regenerate=true writes a fresh one."""
    _require_event(event_id)
    try:
        return get_or_create_brief(event_id, force=regenerate)
    except Exception as exc:  # model/API failure: keep the dashboard usable with the deterministic digest
        from backend.agents.brief_writer import build_digest
        return {"brief": None, "digest": build_digest(event_id), "error": f"{type(exc).__name__}: {exc}"}


# ---- Crisis war-room API --------------------------------------------------------------------------

MANUAL_BASELINE_MINUTES = 45  # typical time for an ops lead to re-plan a room/time change and message everyone by hand


class CrisisRunRequest(BaseModel):
    crisis_text: str = Field(min_length=5, max_length=6000)
    cost_saver: Optional[bool] = None


@app.post("/api/events/{event_id}/crisis/run")
def run_crisis(event_id: str, body: CrisisRunRequest):
    """Security gate -> Triage -> Solver -> Comms. Always 200 with a `status`; nothing is written to the schedule."""
    if db.get_event(event_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown event '{event_id}'.")
    result = run_crisis_war_room_pipeline(event_id, body.crisis_text, body.cost_saver)
    result["manual_baseline_minutes"] = MANUAL_BASELINE_MINUTES
    return result


class CommitRequest(BaseModel):
    run_id: str
    option_choice: str = "recommended"
    flag_id: Optional[str] = None     # the red flag this run was started from (marked Resolved on commit)
    message_id: Optional[str] = None  # the email this run was started from (marked Done on commit)


@app.post("/api/events/{event_id}/crisis/commit")
def commit_crisis(request: Request, event_id: str, body: CommitRequest):
    """Human-approved commit: Layer 2 re-check -> SQLite -> history (public banner + .ics) -> fresh event state."""
    result = commit_resolution_plan(event_id, body.run_id, body.option_choice)
    if not result["ok"]:
        return JSONResponse(status_code=409, content=result)
    moves = "; ".join(f"{c['title']}: {c['from']} -> {c['to']}" for c in result["applied_changes"]) or "room status only"
    changes.log_activity(event_id, _actor(request), "resolve", f"Approved {result['option_label']} ({moves})", body.run_id)
    if body.flag_id and not body.flag_id.startswith("sys_"):
        f = db.get_record("flags", body.flag_id)
        if f is not None and f.event_id == event_id:
            db.update_record("flags", body.flag_id, status="Resolved")
            result["flag_resolved"] = body.flag_id
    if body.message_id:
        m = db.get_record("inbox_messages", body.message_id)
        if m is not None and m.event_id == event_id and m.status != "Quarantined":
            db.update_record("inbox_messages", body.message_id, status="Done")
    return result | {"event_state": _event_payload(event_id), "health": planning.event_health(event_id)}


@app.get("/api/events/{event_id}/crisis/runs")
def crisis_runs(event_id: str, limit: int = 10):
    """Recent Resolve-an-issue runs (newest first) and which ones were approved."""
    _require_event(event_id)
    history = {h["run_id"]: h for h in db.get_resolution_history(event_id)}
    return {"runs": [{"run_id": r["run_id"], "status": r["status"], "scenario_title": r["scenario_title"], "started_at": r["started_at"],
                      "cost_usd": r["tokenomics_summary"].get("total_cost_usd", 0), "committed": r["run_id"] in history,
                      "option_label": history.get(r["run_id"], {}).get("option_label")}
                     for r in list_war_room_runs(event_id, limit)]}


@app.get("/api/events/{event_id}/crisis/runs/{run_id}")
def crisis_run(event_id: str, run_id: str):
    from backend.orchestrator import get_war_room_run
    run = get_war_room_run(run_id)
    if run is None or run["event_id"] != event_id:
        raise HTTPException(status_code=404, detail=f"Unknown run '{run_id}'.")
    return run | {"committed": db.run_already_committed(run_id), "manual_baseline_minutes": MANUAL_BASELINE_MINUTES}


@app.get("/api/events/{event_id}/calendar.ics")
def event_calendar(event_id: str, only_updated: bool = False):
    """Downloadable iCalendar: the whole current schedule, or only war-room-updated sessions."""
    event = _resolve_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail=f"Unknown event '{event_id}'.")
    filename = f"{event.slug}{'-updates' if only_updated else ''}.ics"
    return Response(content=build_event_calendar(event.id, only_updated), media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---- Admin console: tokenomics, guardrails, evidence ------------------------------------------------


def _default_event_id() -> str:
    events = db.get_events()
    live = next((e for e in events if e.status == "Live War-Room"), None)
    if not (live or events):
        raise HTTPException(status_code=404, detail="No events in the workspace.")
    return (live or events[0]).id


@app.get("/api/admin/tokenomics")
def admin_tokenomics(limit: int = 30):
    runs = tokenomics.list_workflow_runs(limit=limit)
    return {
        "cost_saver_enabled": tokenomics.get_cost_saver_mode(),
        "summary": tokenomics.get_tokenomics_dashboard_summary(scale_tiers=SCALE_TIERS),
        "runs": [r.model_dump() for r in runs],
        "agent_calls": [c.model_dump() | {"scenario_title": r.scenario_title} for r in runs for c in r.agent_calls],
        "latest_benchmark": next(iter(list_benchmarks(limit=1)), None),
    }


class ToggleRequest(BaseModel):
    enabled: Optional[bool] = None  # omit to flip the current value


@app.post("/api/admin/tokenomics/toggle-cost-saver")
def admin_toggle_cost_saver(body: ToggleRequest = ToggleRequest()):
    enabled = (not tokenomics.get_cost_saver_mode()) if body.enabled is None else body.enabled
    return {"cost_saver_enabled": tokenomics.set_cost_saver_mode(enabled)}


class BenchmarkRequest(BaseModel):
    event_id: Optional[str] = None
    crisis_text: Optional[str] = None
    use_cached: bool = True


@app.post("/api/admin/tokenomics/benchmark")
def admin_benchmark(body: BenchmarkRequest = BenchmarkRequest()):
    event_id = body.event_id or _default_event_id()
    if db.get_event(event_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown event '{event_id}'.")
    try:
        return run_cost_quality_benchmark(event_id, body.crisis_text, use_cached=body.use_cached)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/admin/security")
def admin_security(limit: int = 200):
    incidents = security.get_security_incidents(limit=limit)
    return {
        "config": security.get_guardrail_config().model_dump(),
        "incidents": [i.model_dump() for i in incidents],
        "counts": {layer: sum(1 for i in incidents if i.defense_layer == layer) for layer in (security.L1, security.L2, security.L3)},
    }


@app.post("/api/admin/security/update-config")
def admin_update_guardrails(changes: dict):
    try:
        return {"config": security.update_guardrail_config(**changes).model_dump()}
    except ValueError as exc:  # unknown keys or out-of-range values (pydantic ValidationError is a ValueError)
        return JSONResponse(status_code=400, content={"error": str(exc).splitlines()[0], "detail": str(exc)})


class RedTeamRequest(BaseModel):
    event_id: Optional[str] = None


@app.post("/api/admin/security/red-team-test")
def admin_red_team(body: RedTeamRequest = RedTeamRequest()):
    event_id = body.event_id or _default_event_id()
    results = security.run_red_team_self_test(event_id)
    if results and "error" in results[0]:
        raise HTTPException(status_code=404, detail=results[0]["error"])
    return {"event_id": event_id, "all_passed": all(r["passed"] for r in results), "results": results}


@app.get("/api/admin/system")
def admin_system():
    return system_status() | {"venues": [v.model_dump() | {"room_count": len(db.get_rooms(v.id))} for v in db.get_venues()]}


class VenueCreateRequest(BaseModel):
    name: str = Field(min_length=3)
    city: str = Field(min_length=2)
    state_or_country: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    airport_corridor: str = ""
    rooms: list[dict] = Field(default_factory=list)  # [{"name", "capacity", "av_specs": [...], "is_catering_hub"}]


@app.post("/api/admin/venues")
def admin_create_venue(body: VenueCreateRequest):
    """Quick venue creator: geocodes the city when coordinates are omitted so live weather works immediately."""
    lat, lon, region = body.latitude, body.longitude, body.state_or_country
    if lat is None or lon is None:
        geo = geocode_city(f"{body.city}, {body.state_or_country}" if body.state_or_country else body.city)
        if geo is None:
            raise HTTPException(status_code=400, detail=f"Could not geocode '{body.city}'. Enter latitude/longitude.")
        lat, lon, region = geo["latitude"], geo["longitude"], region or ", ".join(x for x in (geo["region"], geo["country"]) if x)
    try:
        venue = db.create_venue(body.name, body.city, region or "-", lat, lon, airport_corridor=body.airport_corridor)
        for r in body.rooms:
            db.create_room(venue.id, str(r["name"]), str(r.get("floor_or_zone", "Main level")), int(r["capacity"]),
                           [str(a) for a in r.get("av_specs", [])], bool(r.get("is_catering_hub", False)))
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid venue or room data: {exc}")
    return venue.model_dump() | {"rooms": [r.model_dump() for r in db.get_rooms(venue.id)]}


@app.get("/api/admin/export-evidence")
def admin_export_evidence():
    bundle = build_evidence_bundle()
    return JSONResponse(content=bundle, headers={"Content-Disposition": 'attachment; filename="stagecraft-evidence.json"'})


# ---- Gmail connector (MCP client of the course Gmail server; read-only) ---------------------------------


@app.get("/api/connectors/gmail/status")
def gmail_status():
    return gmail.status()


@app.post("/api/connectors/gmail/check")
def gmail_check():
    return gmail.check_connection()


# ---- Connect Gmail in the app (per user, read-only) --------------------------------------------------


GMAIL_SETUP = [
    {"step": "Create a Google Cloud project (signed in as the inbox you'll connect)", "url": "https://console.cloud.google.com/projectcreate"},
    {"step": "Enable the Gmail API", "url": "https://console.cloud.google.com/apis/library/gmail.googleapis.com"},
    {"step": "Set up the sign-in screen: External, Testing; add your address under Test users", "url": "https://console.cloud.google.com/auth/audience"},
    {"step": "Create an OAuth client: Desktop app, then Download JSON", "url": "https://console.cloud.google.com/auth/clients/create"},
    {"step": "Save it as gmail_credentials.json in the Final Project folder (or mcp_servers/), then come back and click Connect Gmail", "url": ""},
]


@app.get("/api/gmail/status")
def gmail_app_status(request: Request):
    conn = google_gmail.connection(request.state.user)
    return {"client_file": google_gmail.client_file() is not None, "connected": conn is not None, "email": (conn or {}).get("email"),
            "connected_at": (conn or {}).get("connected_at"), "read_only": True, "setup": GMAIL_SETUP}


class ConnectRequest(BaseModel):
    return_to: str = Field(default="#/", max_length=200)


@app.post("/api/gmail/connect")
def gmail_app_connect(request: Request, body: ConnectRequest = ConnectRequest()):
    try:
        uri = google_gmail.redirect_uri_for(request.url.hostname or "", request.url.port)
        return {"auth_url": google_gmail.start_connect(request.state.user, uri, body.return_to)}
    except google_gmail.GmailError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


@app.post("/api/gmail/disconnect")
def gmail_app_disconnect(request: Request):
    return {"disconnected": google_gmail.disconnect(request.state.user)}


class GmailPreviewRequest(BaseModel):
    after: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    before: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    keywords: str = Field(default="", max_length=200)
    sender: str = Field(default="", max_length=200)
    limit: int = Field(default=30, ge=1, le=google_gmail.MAX_PREVIEW)


@app.post("/api/events/{event_id}/gmail/preview")
def gmail_app_preview(request: Request, event_id: str, body: GmailPreviewRequest = GmailPreviewRequest()):
    """List matching emails (headers and snippet only: no AI, no cost) and mark which look related to this event."""
    _require_event(event_id)
    try:
        return google_gmail.preview(request.state.user, event_id, body.after, body.before, body.keywords, body.sender, body.limit)
    except (google_gmail.GmailError, httpx.HTTPError) as exc:
        return JSONResponse(status_code=409, content={"error": str(exc)})


class GmailImportRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=google_gmail.MAX_IMPORT)


@app.post("/api/events/{event_id}/gmail/import")
def gmail_app_import(request: Request, event_id: str, body: GmailImportRequest):
    """Import only the ticked emails and run the Inbox Watcher on them."""
    _require_event(event_id)
    try:
        result = google_gmail.import_selected(request.state.user, event_id, body.ids)
    except (google_gmail.GmailError, httpx.HTTPError) as exc:
        return JSONResponse(status_code=409, content={"error": str(exc)})
    if result["imported"]:
        changes.log_activity(event_id, _actor(request), "gmail", f"Imported {result['imported']} email(s) from Gmail")
    return result


class GmailSyncRequest(BaseModel):
    query: str = Field(default="newer_than:7d", min_length=1, max_length=300)
    max_results: int = Field(default=10, ge=1, le=gmail.MAX_IMPORT)
    process: bool = True


@app.post("/api/events/{event_id}/gmail/sync")
def gmail_sync(event_id: str, body: GmailSyncRequest = GmailSyncRequest()):
    """Import matching Gmail messages into this event's inbox, then run the Inbox Watcher on the new ones."""
    _require_event(event_id)
    result = gmail.import_emails(event_id, body.query, body.max_results, body.process)
    if not result["ok"]:
        return JSONResponse(status_code=409, content=result)
    if result["imported"]:
        changes.log_activity(event_id, "system", "gmail", f"Imported {result['imported']} email(s) from Gmail ({body.query})")
    return result


@app.post("/api/demo/reset")
def reset_demo():
    db.reset_demo_db()
    return {"ok": True, "events": [e.id for e in db.get_events()]}
