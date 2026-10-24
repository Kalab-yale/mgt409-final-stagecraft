"""Gmail connector: Stagecraft as an MCP client of the course Gmail MCP server (mcp_servers/mcp_gmail_server.py).

Reads only (search_emails, read_email). Stagecraft never calls send_email: replies stay drafts the organizer sends
themselves. Imported emails are stored on the event and then go through the same Inbox Watcher as the demo inbox, so
every one passes the Layer 1 firewall before any model reads it.

Configuration (optional; without it the app uses the demo inbox):
  GMAIL_MCP_SERVER  path to mcp_gmail_server.py   (default: <course folder>/mcp_servers/mcp_gmail_server.py)
  GMAIL_MCP_PYTHON  python that has its packages   (default: <that folder>/.venv/bin/python)
Sign-in is one-time, in a terminal:  <python> mcp_gmail_server.py --login
"""

from __future__ import annotations

import asyncio
import email.utils
import os
import re
from datetime import datetime
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from backend import database as db
from backend.config import REPO_ROOT
from backend.models import InboxMessage

DEFAULT_SERVER = REPO_ROOT.parent / "mcp_servers" / "mcp_gmail_server.py"
CALL_TIMEOUT_SECS = 45
MAX_IMPORT = 25


def _paths() -> tuple[Path, Path]:
    server = Path(os.environ.get("GMAIL_MCP_SERVER", DEFAULT_SERVER))
    python = Path(os.environ.get("GMAIL_MCP_PYTHON", server.parent / ".venv" / "bin" / "python"))
    return server, python


def status() -> dict:
    """Cheap file checks (no process started): is the server there, and has someone signed in?"""
    server, python = _paths()
    creds = Path(os.environ.get("GMAIL_CREDENTIALS_FILE", server.parent / "gmail_credentials.json"))
    token = server.parent / "gmail_token.json"
    available = server.exists() and python.exists()
    return {
        "available": available,
        "server": str(server),
        "credentials_file_present": creds.exists(),
        "signed_in": token.exists(),
        "login_command": f'"{python}" "{server}" --login',
        "mode": "gmail" if available and token.exists() else "demo inbox",
        "note": ("Connected: Stagecraft can read your Gmail (read-only) through the course MCP server." if available and token.exists()
                 else "Gmail server found, but nobody has signed in yet." if available
                 else "Gmail MCP server not found here, so the demo inbox is used."),
        "setup_steps": [
            "Create an OAuth 'Desktop app' client in Google Cloud and download it as gmail_credentials.json into mcp_servers/.",
            "Run the login command below once in a terminal and approve access in the browser.",
            "Come back and click Test connection, then Import emails.",
        ],
    }


async def _call(tool: str, args: dict) -> str:
    server, python = _paths()
    params = StdioServerParameters(command=str(python), args=[str(server)], env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
            return "".join(getattr(c, "text", "") for c in result.content)


async def _call_many(calls: list[tuple[str, dict]]) -> list[str]:
    """Several tool calls over ONE server session (one process start)."""
    server, python = _paths()
    params = StdioServerParameters(command=str(python), args=[str(server)], env=dict(os.environ))
    out = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for tool, args in calls:
                result = await session.call_tool(tool, args)
                out.append("".join(getattr(c, "text", "") for c in result.content))
    return out


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, CALL_TIMEOUT_SECS))


def _not_signed_in(text: str) -> bool:
    return text.startswith("Not signed in") or "OAuth client file not found" in text


def check_connection() -> dict:
    """Start the server and run a 1-email search: proves the MCP link and the sign-in actually work."""
    st = status()
    if not st["available"]:
        return {"ok": False, "reason": st["note"]}
    try:
        text = _run(_call("search_emails", {"query": "in:inbox", "max_results": 1}))
    except Exception as exc:  # process failed to start, timeout, protocol error
        return {"ok": False, "reason": f"Couldn't talk to the Gmail MCP server: {type(exc).__name__}: {exc}"}
    if _not_signed_in(text):
        return {"ok": False, "reason": "Not signed in yet. Run the login command once in a terminal."}
    if text.startswith("Gmail API error"):
        return {"ok": False, "reason": text}
    return {"ok": True, "reason": "Connected. The MCP server answered a test search."}


_ID = re.compile(r"^id: (\S+)", re.M)


def parse_search(text: str) -> list[str]:
    """Message ids from search_emails output (blocks starting 'id: <id>')."""
    return _ID.findall(text)


def parse_email(text: str) -> dict:
    """read_email output: header lines, a blank line, then the body."""
    head, _, body = text.partition("\n\n")
    headers = {}
    for line in head.splitlines():
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    name, addr = email.utils.parseaddr(headers.get("from", ""))
    try:
        received = email.utils.parsedate_to_datetime(headers.get("date", "")).astimezone().replace(tzinfo=None)
    except (TypeError, ValueError):
        received = datetime.now()
    return {"from_name": name or addr, "from_email": addr or headers.get("from", "unknown"), "subject": headers.get("subject", "(no subject)"),
            "body": body.strip() or "(no text body)", "received_at": received.isoformat(timespec="minutes")}


def import_emails(event_id: str, query: str, max_results: int = 10, process: bool = True) -> dict:
    """Search Gmail, store new matching emails on the event (skipping ones already imported), then run the Watcher."""
    st = status()
    if not st["available"]:
        return {"ok": False, "error": st["note"]}
    max_results = max(1, min(max_results, MAX_IMPORT))
    try:
        listing = _run(_call("search_emails", {"query": query, "max_results": max_results}))
        if _not_signed_in(listing):
            return {"ok": False, "error": "Not signed in yet. Run the login command once in a terminal."}
        if listing.startswith("Gmail API error"):
            return {"ok": False, "error": listing}
        known = {m.external_id for m in db.list_records("inbox_messages", event_id) if m.source == "gmail"}
        new_ids = [i for i in parse_search(listing) if i not in known]
        bodies = _run(_call_many([("read_email", {"message_id": i, "max_chars": 8000}) for i in new_ids])) if new_ids else []
    except Exception as exc:
        return {"ok": False, "error": f"Couldn't read Gmail through the MCP server: {type(exc).__name__}: {exc}"}

    added = []
    for gid, text in zip(new_ids, bodies):
        if text.startswith("Gmail API error"):
            continue
        fields = parse_email(text)
        mid = f"msg_g{gid[-10:]}"
        db.insert_records("inbox_messages", [InboxMessage(id=mid, event_id=event_id, source="gmail", external_id=gid, **fields)])
        added.append(mid)
    result = {"ok": True, "query": query, "found": len(parse_search(listing)), "already_imported": len(parse_search(listing)) - len(new_ids),
              "imported": len(added), "message_ids": added}
    if process and added:
        from backend.agents.inbox_watcher import process_inbox

        summary = process_inbox(event_id, only_ids=added)  # just the emails this import added
        result["processing"] = {k: v for k, v in summary.items() if k != "results"}
    return result
