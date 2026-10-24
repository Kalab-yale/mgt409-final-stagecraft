"""Stagecraft AI configuration: .env loading, paths, model choice, and the pydantic-ai model factory."""

from __future__ import annotations

import os
import threading
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider

BACKEND_DIR = Path(__file__).resolve().parent
REPO_ROOT = BACKEND_DIR.parent
AGENTS_DIR = BACKEND_DIR / "agents"
FRONTEND_DIR = REPO_ROOT / "frontend"
TEMPLATES_DIR = FRONTEND_DIR / "templates"
STATIC_DIR = FRONTEND_DIR / "static"
DB_PATH = Path(os.environ.get("STAGECRAFT_DB_PATH", BACKEND_DIR / "stagecraft.db"))

# Local dev keeps .env in the repo root; the grading script puts it one folder above the repo.
# Paths come from __file__, so this works from any working directory.
# load_dotenv never overwrites a variable that is already set, so ./.env wins over ../.env.
ENV_PATHS = (REPO_ROOT / ".env", REPO_ROOT.parent / ".env")
for env_path in ENV_PATHS:
    if env_path.exists():
        load_dotenv(env_path, override=False)
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

# One model for every agent (course budget model, routed through Portkey).
MODEL_NAME = os.environ.get("STAGECRAFT_MODEL", "gpt-6-luna")
PORTKEY_BASE_URL = "https://api.portkey.ai/v1"


def demo_today() -> date:
    """'Today' for deadlines, countdowns, and the seeded demo. Set STAGECRAFT_TODAY=YYYY-MM-DD to pin it (tests do)."""
    pinned = os.environ.get("STAGECRAFT_TODAY")
    return date.fromisoformat(pinned) if pinned else date.today()


def loaded_env_files() -> list[Path]:
    """Which of the two .env locations actually exist (for diagnostics; never prints values)."""
    return [p for p in ENV_PATHS if p.exists()]


def get_llm_backend() -> str:
    """Return which key will be used: 'portkey' or 'openai'."""
    if os.environ.get("PORTKEY_API_KEY"):
        return "portkey"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    raise RuntimeError(
        "No API key found. Set PORTKEY_API_KEY (or OPENAI_API_KEY) in ./.env or ../.env."
    )


def get_agent_settings(max_tokens: int = 2000) -> OpenAIChatModelSettings:
    """Model settings every Stagecraft agent should use.

    gpt-6-luna on /chat/completions rejects function tools (and structured output, which uses a tool)
    unless reasoning_effort is 'none' - the API returns HTTP 400 otherwise.
    """
    return OpenAIChatModelSettings(max_tokens=max_tokens, openai_reasoning_effort="none")


def get_pydantic_ai_model() -> OpenAIChatModel:
    """Build the pydantic-ai model that every Stagecraft agent shares."""
    if get_llm_backend() == "portkey":
        client = AsyncOpenAI(
            api_key=os.environ["PORTKEY_API_KEY"],
            base_url=PORTKEY_BASE_URL,
            default_headers={"x-portkey-provider": "openai"},
        )
    else:
        client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return OpenAIChatModel(MODEL_NAME, provider=OpenAIProvider(openai_client=client))


_thread_local = threading.local()


def get_thread_model() -> OpenAIChatModel:
    """The model client for the current thread. pydantic-ai keeps one asyncio event loop per thread, and an HTTP
    connection pool belongs to the loop that opened it, so sharing one client across threads (the web server's worker
    threads, or parallel inbox processing) fails with 'bound to a different event loop'. One client per thread is safe."""
    model = getattr(_thread_local, "model", None)
    if model is None:
        model = _thread_local.model = get_pydantic_ai_model()
    return model

