"""Single-user auth: password → in-memory session token cookie."""
from __future__ import annotations

import hmac
import secrets
import time

from fastapi import Depends, HTTPException, Request
from starlette.responses import Response

from .config import settings

COOKIE = "asst_session"
COOKIE_MAX_AGE = 7 * 24 * 3600
LOGIN_WINDOW = 300
MAX_LOGIN_ATTEMPTS = 5

_sessions: dict[str, tuple[float, str]] = {}
_login_attempts: dict[str, list[float]] = {}


def verify_password(candidate: str) -> bool:
    return hmac.compare_digest(candidate.encode(), settings.admin_password.encode())


def login_allowed(client: str) -> bool:
    now = time.time()
    attempts = [ts for ts in _login_attempts.get(client, []) if now - ts < LOGIN_WINDOW]
    if attempts:
        _login_attempts[client] = attempts
    else:
        _login_attempts.pop(client, None)
    return len(attempts) < MAX_LOGIN_ATTEMPTS


def record_login_failure(client: str) -> None:
    _login_attempts.setdefault(client, []).append(time.time())


def clear_login_failures(client: str) -> None:
    _login_attempts.pop(client, None)


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    _sessions[token] = (time.time() + COOKIE_MAX_AGE, csrf)
    return token


def csrf_token_from_session(token: str) -> str:
    session = _sessions.get(token)
    return session[1] if session and session[0] > time.time() else ""

def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax", max_age=COOKIE_MAX_AGE)


def destroy_session(request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        _sessions.pop(token, None)
    response.delete_cookie(COOKIE)

def _valid(request: Request) -> bool:
    token = request.cookies.get(COOKIE)
    session = _sessions.get(token or "")
    return bool(token and session and session[0] > time.time())


def user_authed(request: Request) -> bool:
    return _valid(request)


def require_user(request: Request) -> None:
    if not _valid(request):
        raise HTTPException(status_code=401, detail="Not authenticated")


def csrf_token(request: Request) -> str:
    token = request.cookies.get(COOKIE)
    session = _sessions.get(token or "")
    return session[1] if session and session[0] > time.time() else ""


def csrf_guard(request: Request) -> None:
    """Mutations must echo the server-side CSRF token for this session."""
    expected = csrf_token(request)
    header = request.headers.get("x-csrf", "")
    if not expected or not hmac.compare_digest(expected.encode(), header.encode()):
        raise HTTPException(status_code=403, detail="CSRF token mismatch")
