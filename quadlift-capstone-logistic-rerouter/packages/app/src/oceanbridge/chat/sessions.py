"""Session management: create, resume, invalidate, expire.

A session is `active` until the user invalidates it (`ended`) or it sits idle longer than
`chat.session_idle_timeout_minutes` (`expired`). Closed sessions stay readable but accept no new
messages, and their working memory is never carried into a new session."""

from __future__ import annotations

import uuid
from datetime import timedelta

from oceanbridge.chat import store
from oceanbridge.chat.store import ChatSession
from oceanbridge.config import get_settings
from oceanbridge.core.repository import utcnow

ACTIVE, ENDED, EXPIRED = "active", "ended", "expired"


class SessionClosed(Exception):
    """The session no longer accepts messages; start a new one."""

    def __init__(self, session: ChatSession):
        self.session = session
        super().__init__(f"Chat session {session.id} is {session.status}"
                         + (f" ({session.end_reason})" if session.end_reason else "")
                         + ". Start a new session to continue.")


def _expire_if_idle(session: ChatSession) -> ChatSession:
    if session.status != ACTIVE:
        return session
    minutes = get_settings().chat.session_idle_timeout_minutes
    if utcnow() - session.updated_at > timedelta(minutes=minutes):
        return store.update_session(session.id, status=EXPIRED, ended_at=utcnow(),
                                    end_reason=f"idle for more than {minutes} minutes", state={})
    return session


def new_session(user: str | None = None, invalidate_session_id: str | None = None) -> ChatSession:
    """Start a fresh session, optionally invalidating the caller's current one first."""
    if invalidate_session_id:
        existing = store.get_session(invalidate_session_id)
        if existing and existing.status == ACTIVE:
            end_session(invalidate_session_id, reason="replaced by a new session")
    session_id = f"CHAT-{uuid.uuid4().hex[:10].upper()}"
    return store.insert_session(session_id, user or get_settings().chat.default_user)


def get_session(session_id: str) -> ChatSession:
    session = store.get_session(session_id)
    if session is None:
        raise KeyError(session_id)
    return _expire_if_idle(session)


def require_active(session_id: str) -> ChatSession:
    session = get_session(session_id)
    if session.status != ACTIVE:
        raise SessionClosed(session)
    return session


def end_session(session_id: str, reason: str = "invalidated by user") -> ChatSession:
    """Invalidate a session: it becomes read-only and its working memory (entities, pending action) is wiped."""
    session = get_session(session_id)
    if session.status != ACTIVE:
        return session
    return store.update_session(session_id, status=ENDED, ended_at=utcnow(), end_reason=reason, state={})


def list_sessions(include_closed: bool = True, limit: int = 50) -> list[ChatSession]:
    return [_expire_if_idle(s) for s in store.list_sessions(include_closed, limit)]


def delete_session(session_id: str) -> bool:
    return store.delete_session(session_id)
