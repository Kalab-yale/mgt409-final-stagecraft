"""Connect Gmail from inside Stagecraft: each signed-in organizer connects their own inbox (read-only), previews mail by
date and keyword, sees which emails look related to the event, and imports only the ones they tick.

How it works
  - Google requires a registered OAuth client for any app that reads Gmail. The organizer (or whoever runs Stagecraft)
    downloads a "Desktop app" client JSON once from Google Cloud; Stagecraft finds it at GMAIL_OAUTH_CLIENT_FILE,
    ./gmail_credentials.json, or the course ../mcp_servers/gmail_credentials.json.
  - "Connect Gmail" sends the browser to Google's consent screen (scope gmail.readonly only, PKCE, a single-use state
    bound to the signed-in user). Google sends the browser back to http://127.0.0.1:<port>/ with a code, which the server
    exchanges for tokens. Desktop clients accept loopback redirects, the same flow Google's own Python library uses.
  - Tokens never reach the browser. They are stored per user, encrypted with a local key (Fernet), and refreshed when
    they expire. Disconnect deletes them and revokes them at Google.
  - Preview reads only headers and Google's snippet (no AI, no cost). Import reads the selected emails, stores them on the
    event, and runs the Inbox Watcher on just those (each one passes the injection firewall first). Nothing is ever sent.
"""

from __future__ import annotations

import base64
import email.utils
import hashlib
import html
import json
import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken

from backend import database as db
from backend.config import REPO_ROOT
from backend.models import InboxMessage

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_PREVIEW, MAX_IMPORT = 50, 25
STATE_TTL_MIN = 10
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
BROAD_DOMAINS = {"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com", "icloud.com", "me.com",
                 "aol.com", "proton.me", "protonmail.com", "yale.edu"}  # shared domains: an address there says nothing about the event

transport: httpx.BaseTransport | None = None  # tests plug in httpx.MockTransport here

SCHEMA = """
CREATE TABLE IF NOT EXISTS gmail_connections (
    user_id TEXT PRIMARY KEY, email TEXT NOT NULL, refresh_token TEXT, access_token TEXT, expires_at TEXT,
    scope TEXT, connected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY, user_id TEXT NOT NULL, verifier TEXT NOT NULL, redirect_uri TEXT NOT NULL, return_to TEXT,
    created_at TEXT NOT NULL
);
"""


class GmailError(ValueError):
    """Something the person can act on (shown as-is)."""


def init() -> None:
    with db.connect() as conn:
        conn.executescript(SCHEMA)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _http() -> httpx.Client:
    return httpx.Client(transport=transport, timeout=20)


# ---- the OAuth client file and token encryption -------------------------------------------------------


def client_file() -> Path | None:
    candidates = [os.environ.get("GMAIL_OAUTH_CLIENT_FILE"), REPO_ROOT / "gmail_credentials.json",
                  REPO_ROOT.parent / "mcp_servers" / "gmail_credentials.json"]
    return next((Path(c) for c in candidates if c and Path(c).exists()), None)


def _client() -> dict:
    f = client_file()
    if f is None:
        raise GmailError("Google sign-in isn't set up yet: add the OAuth client file (gmail_credentials.json). See the setup steps.")
    data = json.loads(f.read_text())
    c = data.get("installed") or data.get("web") or {}
    if not c.get("client_id"):
        raise GmailError(f"{f.name} doesn't look like a Google OAuth client file (no client_id).")
    return c


def _fernet() -> Fernet:
    key = os.environ.get("STAGECRAFT_SECRET_KEY")
    if not key:
        path = Path(os.environ.get("STAGECRAFT_KEY_FILE", REPO_ROOT / "backend" / ".secret_key"))
        if not path.exists():
            path.write_bytes(Fernet.generate_key())
            path.chmod(0o600)  # only this user can read it
        key = path.read_text().strip()
    return Fernet(key.encode() if isinstance(key, str) else key)


def _enc(value: str | None) -> str | None:
    return _fernet().encrypt(value.encode()).decode() if value else None


def _dec(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        return None  # key changed: treat as disconnected


# ---- connect / callback / disconnect ------------------------------------------------------------------


def redirect_uri_for(host: str, port: int | None) -> str:
    """Google sends the browser back to this machine. Only loopback hosts are allowed (the app runs locally)."""
    if host not in LOOPBACK_HOSTS:
        raise GmailError("Connecting Gmail works when Stagecraft runs on this computer (open it at http://127.0.0.1).")
    return f"http://{host}:{port}/" if port else f"http://{host}/"


def start_connect(user, redirect_uri: str, return_to: str = "") -> str:
    """Create a single-use state (with a PKCE verifier) for this user and return Google's consent URL."""
    c = _client()
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return_to = return_to if return_to.startswith("#/") else "#/"
    init()
    with db.connect() as conn:
        conn.execute("DELETE FROM oauth_states WHERE created_at < ?", ((_now() - timedelta(minutes=STATE_TTL_MIN)).isoformat(),))
        conn.execute("INSERT INTO oauth_states VALUES (?, ?, ?, ?, ?, ?)", (state, user.id, verifier, redirect_uri, return_to, _now().isoformat()))
    return AUTH_URL + "?" + urlencode({"client_id": c["client_id"], "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE,
                                       "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true",
                                       "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})


def is_callback(params) -> bool:
    return "state" in params and ("code" in params or "error" in params)


def finish_connect(user, params) -> tuple[str, str]:
    """Handle Google's redirect. Returns (return_to, email). The state must exist, be fresh, and belong to this user."""
    init()
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM oauth_states WHERE state = ?", (params.get("state", ""),)).fetchone()
        conn.execute("DELETE FROM oauth_states WHERE state = ?", (params.get("state", ""),))  # single use
    if row is None or user is None or row["user_id"] != user.id:
        raise GmailError("That Gmail sign-in link expired or belongs to another session. Try Connect Gmail again.")
    if datetime.fromisoformat(row["created_at"]) < _now() - timedelta(minutes=STATE_TTL_MIN):
        raise GmailError("The Gmail sign-in took too long. Try Connect Gmail again.")
    if params.get("error"):
        raise GmailError("Gmail wasn't connected: " + ("you cancelled on Google's screen." if params["error"] == "access_denied" else params["error"]))
    c = _client()
    with _http() as h:
        r = h.post(c.get("token_uri", TOKEN_URL), data={"code": params["code"], "client_id": c["client_id"], "client_secret": c.get("client_secret", ""),
                                                        "redirect_uri": row["redirect_uri"], "grant_type": "authorization_code",
                                                        "code_verifier": row["verifier"]})
        if r.status_code != 200:
            raise GmailError(f"Google refused the sign-in ({r.json().get('error_description') or r.json().get('error', r.status_code)}).")
        tok = r.json()
        if SCOPE not in tok.get("scope", SCOPE):
            raise GmailError("Read access to Gmail wasn't granted. Tick the Gmail box on Google's screen.")
        prof = h.get(f"{API}/profile", headers={"Authorization": f"Bearer {tok['access_token']}"})
        address = prof.json().get("emailAddress", "") if prof.status_code == 200 else ""
    with db.connect() as conn:
        old = conn.execute("SELECT refresh_token FROM gmail_connections WHERE user_id = ?", (user.id,)).fetchone()
        refresh = tok.get("refresh_token") or (old and _dec(old["refresh_token"]))
        conn.execute("INSERT OR REPLACE INTO gmail_connections VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (user.id, address, _enc(refresh), _enc(tok["access_token"]),
                      (_now() + timedelta(seconds=int(tok.get("expires_in", 3600)) - 60)).isoformat(), tok.get("scope", SCOPE), _now().isoformat()))
    return row["return_to"] or "#/", address


def connection(user) -> dict | None:
    init()
    with db.connect() as conn:
        row = conn.execute("SELECT email, connected_at, scope FROM gmail_connections WHERE user_id = ?", (user.id,)).fetchone()
    return dict(row) if row else None


def disconnect(user) -> bool:
    init()
    with db.connect() as conn:
        row = conn.execute("SELECT refresh_token, access_token FROM gmail_connections WHERE user_id = ?", (user.id,)).fetchone()
        conn.execute("DELETE FROM gmail_connections WHERE user_id = ?", (user.id,))
    if row:
        try:
            with _http() as h:
                h.post(REVOKE_URL, params={"token": _dec(row["refresh_token"]) or _dec(row["access_token"]) or ""})
        except httpx.HTTPError:
            pass  # removed here either way; Google also expires unused tokens
    return bool(row)


def _access_token(user) -> str:
    init()
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM gmail_connections WHERE user_id = ?", (user.id,)).fetchone()
    if row is None:
        raise GmailError("Gmail isn't connected for your account yet. Click Connect Gmail.")
    if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) > _now() and _dec(row["access_token"]):
        return _dec(row["access_token"])
    refresh = _dec(row["refresh_token"])
    if not refresh:
        raise GmailError("Your Gmail sign-in expired. Click Connect Gmail again.")
    c = _client()
    with _http() as h:
        r = h.post(c.get("token_uri", TOKEN_URL), data={"client_id": c["client_id"], "client_secret": c.get("client_secret", ""),
                                                        "refresh_token": refresh, "grant_type": "refresh_token"})
    if r.status_code != 200:
        raise GmailError("Your Gmail sign-in expired or was revoked. Click Connect Gmail again.")
    tok = r.json()
    with db.connect() as conn:
        conn.execute("UPDATE gmail_connections SET access_token = ?, expires_at = ? WHERE user_id = ?",
                     (_enc(tok["access_token"]), (_now() + timedelta(seconds=int(tok.get("expires_in", 3600)) - 60)).isoformat(), user.id))
    return tok["access_token"]


def _get(user, path: str, params=None) -> dict:
    with _http() as h:
        r = h.get(f"{API}/{path}", params=params, headers={"Authorization": f"Bearer {_access_token(user)}"})
    if r.status_code == 401:
        raise GmailError("Gmail rejected the sign-in. Click Connect Gmail again.")
    if r.status_code != 200:
        raise GmailError(f"Gmail API error {r.status_code}: {r.text[:160]}")
    return r.json()


# ---- preview: search by date/keyword, flag what looks related -------------------------------------------


def build_query(after: str | None, before: str | None, keywords: str = "", sender: str = "") -> str:
    parts = []
    if after:
        parts.append(f"after:{date.fromisoformat(after):%Y/%m/%d}")
    if before:
        parts.append(f"before:{date.fromisoformat(before) + timedelta(days=1):%Y/%m/%d}")  # Gmail's before: is exclusive
    if sender.strip():
        parts.append(f"from:{sender.strip()}")
    if keywords.strip():
        parts.append(keywords.strip())
    return " ".join(parts) or "newer_than:14d"


def _headers(msg: dict) -> dict:
    return {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}


def event_signals(event_id: str) -> dict:
    """What makes an email 'about this event': people it already knows, their organisations' domains, and its names."""
    e = db.get_event(event_id)
    v = db.get_venue(e.venue_id) if e else None
    people: dict[str, str] = {}
    for m in db.list_records("vendors", event_id):
        if m.contact_email:
            people[m.contact_email.lower()] = f"{m.relationship.lower()}: {m.name}"
    for s in db.get_speakers(event_id):
        if s.email:
            people[s.email.lower()] = f"speaker: {s.name}"
    for t in db.list_records("team_members", event_id):
        if t.email:
            people[t.email.lower()] = f"organizing team: {t.name}"
    for a in db.list_records("attendees", event_id):
        people.setdefault(a.email.lower(), "registered attendee")
    domains = {}
    for m in db.list_records("vendors", event_id):
        d = m.contact_email.lower().split("@")[-1] if m.contact_email else ""
        if d and d not in BROAD_DOMAINS:
            domains[d] = f"{m.relationship.lower()}: {m.name}"
    words = set()
    if e:
        words.update(re.findall(r"\(([A-Z]{2,})\)", e.name))           # "YBVS"
        words.add(re.sub(r"\s*\(.*?\)|\s*\d{4}", "", e.name).strip())   # "Yale Black Venture Summit"
    if v:
        words.add(v.name.split("–")[-1].split("-")[-1].strip())          # "Edward P. Evans Hall"
    words.update(m.name for m in db.list_records("vendors", event_id))
    return {"people": people, "domains": domains, "words": [w for w in words if len(w) >= 4]}


def relatedness(sig: dict, from_email: str, subject: str, snippet: str) -> list[str]:
    reasons = []
    addr = from_email.lower()
    if addr in sig["people"]:
        reasons.append(f"from {sig['people'][addr]}")
    elif addr.split("@")[-1] in sig["domains"]:
        reasons.append(f"same organisation as {sig['domains'][addr.split('@')[-1]]}")
    text = f"{subject} {snippet}".lower()
    hits = [w for w in sig["words"] if w.lower() in text]
    if hits:
        reasons.append("mentions " + ", ".join(f"“{w}”" for w in hits[:2]))
    return reasons


def preview(user, event_id: str, after: str | None, before: str | None, keywords: str = "", sender: str = "", limit: int = 30) -> dict:
    q = build_query(after, before, keywords, sender)
    listing = _get(user, "messages", {"q": q, "maxResults": max(1, min(limit, MAX_PREVIEW))})
    known = {m.external_id for m in db.list_records("inbox_messages", event_id) if m.source == "gmail"}
    sig = event_signals(event_id)
    rows = []
    for ref in listing.get("messages", []):
        m = _get(user, f"messages/{ref['id']}", [("format", "metadata"), ("metadataHeaders", "From"), ("metadataHeaders", "Subject"),
                                                  ("metadataHeaders", "Date")])
        h = _headers(m)
        name, addr = email.utils.parseaddr(h.get("from", ""))
        snippet = html.unescape(m.get("snippet", ""))
        reasons = relatedness(sig, addr, h.get("subject", ""), snippet)
        rows.append({"id": m["id"], "from_name": name or addr, "from_email": addr, "subject": h.get("subject", "(no subject)"),
                     "date": _when(h.get("date"), m.get("internalDate")), "snippet": snippet[:220], "related": bool(reasons),
                     "reasons": reasons, "already_imported": m["id"] in known})
    return {"query": q, "account": (connection(user) or {}).get("email"), "messages": rows,
            "related": sum(r["related"] for r in rows), "more": bool(listing.get("nextPageToken"))}


def _when(header: str | None, internal: str | None) -> str:
    try:
        return email.utils.parsedate_to_datetime(header).astimezone().replace(tzinfo=None).isoformat(timespec="minutes")
    except (TypeError, ValueError):
        if internal:
            return datetime.fromtimestamp(int(internal) / 1000).isoformat(timespec="minutes")
        return datetime.now().isoformat(timespec="minutes")


# ---- import the ticked emails -------------------------------------------------------------------------


def _body(payload: dict) -> str:
    """Prefer text/plain; fall back to text/html with tags stripped."""
    plain, rich = [], []

    def walk(p):
        data = p.get("body", {}).get("data")
        if data:
            text = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
            (plain if p.get("mimeType") == "text/plain" else rich if p.get("mimeType") == "text/html" else []).append(text)
        for part in p.get("parts", []) or []:
            walk(part)

    walk(payload)
    if plain:
        return "\n".join(plain).strip()
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", "\n".join(rich))
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
    return html.unescape(re.sub(r"<[^>]+>", " ", text)).strip()


def import_selected(user, event_id: str, ids: list[str], process: bool = True) -> dict:
    if not ids:
        raise GmailError("Tick at least one email to import.")
    ids = list(dict.fromkeys(ids))[:MAX_IMPORT]
    known = {m.external_id for m in db.list_records("inbox_messages", event_id) if m.source == "gmail"}
    added, skipped = [], 0
    for gid in ids:
        if gid in known:
            skipped += 1
            continue
        m = _get(user, f"messages/{gid}", {"format": "full"})
        h = _headers(m)
        name, addr = email.utils.parseaddr(h.get("from", ""))
        mid = f"msg_g{gid[-10:]}"
        db.insert_records("inbox_messages", [InboxMessage(
            id=mid, event_id=event_id, source="gmail", external_id=gid, from_name=name or addr or "Unknown", from_email=addr or "unknown",
            subject=h.get("subject", "(no subject)")[:300], body=(_body(m.get("payload", {})) or html.unescape(m.get("snippet", "")))[:20000] or "(no text body)",
            received_at=_when(h.get("date"), m.get("internalDate")))])
        added.append(mid)
    result = {"imported": len(added), "already_imported": skipped, "message_ids": added}
    if process and added:
        from backend.agents.inbox_watcher import process_inbox

        summary = process_inbox(event_id, only_ids=added)
        result["processing"] = {k: v for k, v in summary.items() if k != "results"}
    return result
