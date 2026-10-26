"""Accounts, passwords, and login sessions.

Passwords
  - Never stored or logged. Each one is hashed with scrypt (memory-hard, so GPU/ASIC guessing is expensive) using a
    random 16-byte salt per user and OWASP's recommended cost (N=2^17, r=8, p=1, about 0.25 s per guess).
  - Stored as "scrypt$N$r$p$salt$hash" (base64), so the cost can be raised later and old hashes still verify
    (and are upgraded on the next successful login).
  - Compared in constant time. Logins for unknown emails still run one scrypt, so response time doesn't reveal
    which emails have accounts, and every failure returns the same message.

Sessions
  - On login the browser gets a random 256-bit token in an HttpOnly, SameSite=Lax cookie (JavaScript can't read it,
    other sites can't send it on POSTs). Only the token's SHA-256 is stored, so a stolen database can't be used to
    hijack sessions. Sessions expire after 7 days; logging out deletes the row.

Brute force and abuse
  - 5 failed logins for an email within 15 minutes lock that email for 15 minutes.
  - Per client (IP): at most 20 login attempts a minute and 10 sign-ups / sensitive account changes per 10 minutes.

Names
  - Letters (any language), spaces, apostrophes, hyphens, and periods only, and screened by the same injection firewall
    as emails (names can end up in text the AI agents read).

The demo database always contains the test account test@stagecraft.com / password (demo only).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from backend import database as db

SCRYPT_N, SCRYPT_R, SCRYPT_P, SALT_BYTES, KEY_BYTES = 2**17, 8, 1, 16, 32
SESSION_DAYS = 7
MAX_FAILS, LOCK_MINUTES = 5, 15
COOKIE = "stagecraft_session"
TEST_USER = {"first_name": "Test", "last_name": "Organizer", "email": "test@stagecraft.com", "password": "password"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
NAME_RE = re.compile(r"^[^\W\d_]+(?:[ '’.\-]+[^\W\d_]+)*\.?$")  # "Anne-Marie", "O'Neil", "José", "Jr."
THEMES = ("system", "light", "dark")
ACCENTS = ("blue", "indigo", "green", "pink")

AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, first_name TEXT NOT NULL, last_name TEXT NOT NULL, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL, created_at TEXT NOT NULL, last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_failures (email TEXT NOT NULL, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_login_failures_email ON login_failures(email);
"""
# Columns added after the auth tables first shipped (init_auth adds any an older database lacks)
AUTH_MIGRATIONS = [("users", "theme", "TEXT DEFAULT 'system'"), ("users", "accent", "TEXT DEFAULT 'blue'"),
                   ("auth_sessions", "id", "TEXT"), ("auth_sessions", "user_agent", "TEXT"), ("auth_sessions", "last_seen_at", "TEXT")]


class User(BaseModel):
    """What the app may see about a user. The password hash never leaves this module."""

    id: str
    first_name: str
    last_name: str
    email: str
    created_at: str
    last_login_at: str | None = None
    theme: str = "system"
    accent: str = "blue"

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class AuthError(ValueError):
    """A problem the person can fix (shown to them as-is)."""


class RateLimited(AuthError):
    """Too many attempts from one client."""


class RateLimiter:
    """Sliding-window counter per key (client IP + action), in memory. Enough for one app process."""

    def __init__(self) -> None:
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window_s: int) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] > window_s:
                q.popleft()
            if len(q) >= limit:
                wait = int(window_s - (now - q[0])) + 1
                raise RateLimited(f"Too many attempts from this device. Try again in {wait} seconds.")
            q.append(now)


LIMITS = {"login": (20, 60), "signup": (10, 600), "sensitive": (10, 600)}  # action -> (attempts, seconds) per client
limiter = RateLimiter()


def check_rate(client: str, action: str) -> None:
    limit, window = LIMITS[action]
    limiter.hit(f"{action}:{client}", limit, window)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# ---- Password hashing --------------------------------------------------------------------------------


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    key = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=256 * 1024 * 1024, dklen=KEY_BYTES)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt)}${b64(key)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, key = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(key)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=256 * 1024 * 1024, dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


def _needs_rehash(stored: str) -> bool:
    return stored.split("$")[1:4] != [str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P)]


_DUMMY_HASH: str | None = None


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_hex(16))
    return _DUMMY_HASH


# ---- Setup -------------------------------------------------------------------------------------------


def init_auth() -> None:
    """Create the auth tables and make sure the demo test account exists. Called from database.init_db()."""
    with db.connect() as conn:
        conn.executescript(AUTH_SCHEMA)
        for table, column, kind in AUTH_MIGRATIONS:
            if column not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        conn.execute("UPDATE auth_sessions SET id = substr(token_hash, 1, 16) WHERE id IS NULL")
        exists = conn.execute("SELECT 1 FROM users WHERE email = ?", (TEST_USER["email"],)).fetchone()
    if not exists:
        create_user(TEST_USER["first_name"], TEST_USER["last_name"], TEST_USER["email"], TEST_USER["password"])


# ---- Accounts ----------------------------------------------------------------------------------------


def clean_name(first: str, last: str) -> tuple[str, str]:
    first, last = " ".join(first.split()), " ".join(last.split())
    if not first or not last:
        raise AuthError("Please enter your first and last name.")
    if len(first) > 60 or len(last) > 60:
        raise AuthError("Names can be at most 60 characters.")
    if not (NAME_RE.match(first) and NAME_RE.match(last)):
        raise AuthError("Names can use letters, spaces, apostrophes, hyphens, and periods.")
    screen_text(f"{first} {last}", "account name")
    return first, last


def screen_text(text: str, what: str) -> None:
    """Run free text that the app stores (names, roles) through the Layer 1 injection firewall."""
    from backend import security  # local import: security imports database at load time

    result = security.inspect_inbound_input("account", text, source=what)
    if not result["allowed"]:
        raise AuthError(f"That {what} looks like an attempt to instruct the AI, so it was blocked.")


def clean_email(email: str) -> str:
    email = email.strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise AuthError("Please enter a valid email address.")
    return email


def check_password_rules(password: str) -> None:
    if len(password) < 8:
        raise AuthError("Use a password with at least 8 characters.")
    if len(password) > 128:
        raise AuthError("Passwords can be at most 128 characters.")


def _clean(first: str, last: str, email: str, password: str) -> tuple[str, str, str]:
    first, last = clean_name(first, last)
    email = clean_email(email)
    check_password_rules(password)
    return first, last, email


def create_user(first_name: str, last_name: str, email: str, password: str) -> User:
    first, last, email = _clean(first_name, last_name, email, password)
    user = User(id=f"usr_{uuid.uuid4().hex[:12]}", first_name=first, last_name=last, email=email, created_at=_iso(_now()))
    pw_hash = hash_password(password)
    with db.connect() as conn:
        conn.executescript(AUTH_SCHEMA)
        if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            raise AuthError("An account with this email already exists. Try logging in.")
        conn.execute("INSERT INTO users (id, first_name, last_name, email, password_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (user.id, user.first_name, user.last_name, user.email, pw_hash, user.created_at))
    return user


def _user_from_row(row) -> User:
    keys = row.keys()
    return User(id=row["id"], first_name=row["first_name"], last_name=row["last_name"], email=row["email"], created_at=row["created_at"],
                last_login_at=row["last_login_at"], theme=(row["theme"] if "theme" in keys else None) or "system",
                accent=(row["accent"] if "accent" in keys else None) or "blue")


def get_user(user_id: str) -> User | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _user_from_row(row) if row else None


def _recent_failures(conn, email: str) -> int:
    since = _iso(_now() - timedelta(minutes=LOCK_MINUTES))
    return conn.execute("SELECT COUNT(*) FROM login_failures WHERE email = ? AND at >= ?", (email, since)).fetchone()[0]


def authenticate(email: str, password: str) -> User:
    """Return the user for a correct email + password, or raise AuthError with the same message for any mistake."""
    email = email.strip().lower()
    with db.connect() as conn:
        if _recent_failures(conn, email) >= MAX_FAILS:
            raise AuthError(f"Too many failed attempts. Try again in {LOCK_MINUTES} minutes.")
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    ok = verify_password(password, row["password_hash"] if row else _dummy_hash())
    if not (row and ok):
        with db.connect() as conn:  # commit the failure first (raising inside the block would roll it back)
            conn.execute("INSERT INTO login_failures (email, at) VALUES (?, ?)", (email, _iso(_now())))
        raise AuthError("Email or password is incorrect.")
    with db.connect() as conn:
        conn.execute("DELETE FROM login_failures WHERE email = ?", (email,))
        updates = {"last_login_at": _iso(_now())}
        if _needs_rehash(row["password_hash"]):
            updates["password_hash"] = hash_password(password)
        conn.execute(f"UPDATE users SET {', '.join(f'{k} = ?' for k in updates)} WHERE id = ?", (*updates.values(), row["id"]))
    return _user_from_row(row)


# ---- Sessions ----------------------------------------------------------------------------------------


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: str, user_agent: str = "") -> str:
    """Start a session and return the raw token (it goes in the cookie; only its hash is stored)."""
    token = secrets.token_urlsafe(32)
    now = _now()
    with db.connect() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (_iso(now),))
        conn.execute("INSERT INTO auth_sessions (token_hash, user_id, created_at, expires_at, id, user_agent, last_seen_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (_token_hash(token), user_id, _iso(now), _iso(now + timedelta(days=SESSION_DAYS)), secrets.token_hex(8),
                      user_agent[:300], _iso(now)))
    return token


def user_for_token(token: str | None) -> User | None:
    if not token or len(token) > 200:
        return None
    now = _now()
    with db.connect() as conn:
        row = conn.execute("""SELECT u.*, s.last_seen_at AS s_seen FROM auth_sessions s JOIN users u ON u.id = s.user_id
                              WHERE s.token_hash = ? AND s.expires_at > ?""", (_token_hash(token), _iso(now))).fetchone()
        if row and (not row["s_seen"] or row["s_seen"] < _iso(now - timedelta(minutes=5))):  # "last active", updated at most every 5 min
            conn.execute("UPDATE auth_sessions SET last_seen_at = ? WHERE token_hash = ?", (_iso(now), _token_hash(token)))
    return _user_from_row(row) if row else None


def end_session(token: str | None) -> None:
    if token:
        with db.connect() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (_token_hash(token),))


# ---- Account settings ---------------------------------------------------------------------------------


def _password_ok(user_id: str, password: str) -> bool:
    with db.connect() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row) and verify_password(password, row["password_hash"])


def require_password(user: User, password: str) -> None:
    """Sensitive changes need the current password again (a borrowed laptop with an open session isn't enough)."""
    if not _password_ok(user.id, password or ""):
        raise AuthError("Your current password is incorrect.")


def update_profile(user: User, first_name: str, last_name: str) -> User:
    first, last = clean_name(first_name, last_name)
    with db.connect() as conn:
        conn.execute("UPDATE users SET first_name = ?, last_name = ? WHERE id = ?", (first, last, user.id))
    return get_user(user.id)


def change_email(user: User, new_email: str, password: str) -> User:
    require_password(user, password)
    email = clean_email(new_email)
    if email == user.email:
        raise AuthError("That's already your email.")
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM users WHERE email = ? AND id != ?", (email, user.id)).fetchone():
            raise AuthError("Another account already uses that email.")
        conn.execute("UPDATE users SET email = ? WHERE id = ?", (email, user.id))
    return get_user(user.id)


def change_password(user: User, current: str, new: str, keep_token: str | None) -> int:
    """Set a new password and sign out every other session. Returns how many sessions were ended."""
    require_password(user, current)
    check_password_rules(new)
    if current == new:
        raise AuthError("Choose a password you haven't been using.")
    with db.connect() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new), user.id))
    return end_other_sessions(user, keep_token)


def set_preferences(user: User, theme: str | None = None, accent: str | None = None) -> User:
    updates = {}
    if theme is not None:
        if theme not in THEMES:
            raise AuthError(f"Theme must be one of {', '.join(THEMES)}.")
        updates["theme"] = theme
    if accent is not None:
        if accent not in ACCENTS:
            raise AuthError(f"Accent must be one of {', '.join(ACCENTS)}.")
        updates["accent"] = accent
    if updates:
        with db.connect() as conn:
            conn.execute(f"UPDATE users SET {', '.join(f'{k} = ?' for k in updates)} WHERE id = ?", (*updates.values(), user.id))
    return get_user(user.id)


def _device(ua: str) -> str:
    ua = ua or ""
    if not ua:
        return "Unknown device"
    browser = next((name for key, name in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"),
                                           ("Safari/", "Safari"), ("python-httpx", "Script"), ("testclient", "Test client")) if key in ua), "Browser")
    os_ = next((name for key, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Mac OS X", "macOS"),
                                       ("Macintosh", "macOS"), ("Windows", "Windows"), ("Linux", "Linux")) if key in ua), "")
    return f"{browser} on {os_}" if os_ else browser


def list_sessions(user: User, current_token: str | None) -> list[dict]:
    mine = _token_hash(current_token) if current_token else ""
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM auth_sessions WHERE user_id = ? AND expires_at > ? ORDER BY last_seen_at DESC",
                            (user.id, _iso(_now()))).fetchall()
    return [{"id": r["id"], "device": _device(r["user_agent"]), "created_at": r["created_at"], "last_seen_at": r["last_seen_at"] or r["created_at"],
             "expires_at": r["expires_at"], "current": r["token_hash"] == mine} for r in rows]


def end_session_by_id(user: User, session_id: str) -> bool:
    with db.connect() as conn:
        return conn.execute("DELETE FROM auth_sessions WHERE id = ? AND user_id = ?", (session_id, user.id)).rowcount > 0


def end_other_sessions(user: User, keep_token: str | None) -> int:
    keep = _token_hash(keep_token) if keep_token else ""
    with db.connect() as conn:
        return conn.execute("DELETE FROM auth_sessions WHERE user_id = ? AND token_hash != ?", (user.id, keep)).rowcount


def delete_account(user: User, password: str, confirm_text: str) -> None:
    if user.email == TEST_USER["email"]:
        raise AuthError("The demo test account can't be deleted.")
    require_password(user, password)
    if confirm_text.strip().upper() != "DELETE":
        raise AuthError('Type DELETE to confirm.')
    with db.connect() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user.id,))
        conn.execute("DELETE FROM login_failures WHERE email = ?", (user.email,))
        conn.execute("DELETE FROM users WHERE id = ?", (user.id,))
