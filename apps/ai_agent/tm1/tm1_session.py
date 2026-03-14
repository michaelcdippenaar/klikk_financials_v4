"""
TM1 session store — maps portal users to TM1 REST sessions.

Thread-safe in-memory store that associates each authenticated portal user
with their TM1 credentials / session cookies so that TM1 REST calls are
attributed to the correct user.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from requests.auth import HTTPBasicAuth

import logging

log = logging.getLogger('ai_agent')

_sessions: dict[str, dict] = {}  # username -> {tm1_user, auth, cookies, created_at}
_lock = threading.Lock()


def store_tm1_session(
    username: str,
    tm1_user: str,
    tm1_password: str,
    cookies: dict | None = None,
) -> None:
    """Store TM1 credentials for a portal user."""
    with _lock:
        _sessions[username] = {
            "tm1_user": tm1_user,
            "auth": HTTPBasicAuth(tm1_user, tm1_password),
            "cookies": cookies or {},
            "created_at": time.time(),
        }
    log.debug("Stored TM1 session for portal user %s (tm1_user=%s)", username, tm1_user)


def get_tm1_auth(username: str | None = None) -> HTTPBasicAuth:
    """Get TM1 auth for a user. Falls back to default config if no session."""
    if username:
        with _lock:
            session = _sessions.get(username)
            if session:
                return session["auth"]
    # Fallback to config defaults
    from apps.ai_agent.agent.config import settings
    return HTTPBasicAuth(settings.tm1_user, settings.tm1_password)


def get_tm1_cookies(username: str | None = None) -> dict:
    """Get stored TM1 cookies for a user, or empty dict."""
    if username:
        with _lock:
            session = _sessions.get(username)
            if session:
                return session.get("cookies", {})
    return {}


def has_tm1_session(username: str) -> bool:
    """Check if a user has a stored TM1 session."""
    with _lock:
        return username in _sessions


def clear_tm1_session(username: str) -> None:
    """Remove stored TM1 session for a user."""
    with _lock:
        removed = _sessions.pop(username, None)
    if removed:
        log.debug("Cleared TM1 session for portal user %s", username)


def clear_all_sessions() -> None:
    """Remove all stored TM1 sessions."""
    with _lock:
        _sessions.clear()
    log.debug("Cleared all TM1 sessions")
