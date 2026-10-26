"""Shared test helper: every /api call needs a login, so suites log their TestClient in as the demo test user."""

from backend.auth import TEST_USER


def login(client) -> None:
    r = client.post("/api/auth/login", json={"email": TEST_USER["email"], "password": TEST_USER["password"]})
    assert r.status_code == 200, f"test login failed: {r.status_code} {r.text}"
