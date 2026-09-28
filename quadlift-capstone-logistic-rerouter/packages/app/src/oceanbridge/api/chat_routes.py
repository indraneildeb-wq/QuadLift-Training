"""Chat endpoints: session management and chat turns."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from oceanbridge.agents.chat_assistant import catalog
from oceanbridge.chat import sessions, store
from oceanbridge.chat.service import get_chat_service
from oceanbridge.chat.sessions import SessionClosed

router = APIRouter(prefix="/chat", tags=["chat"])


class NewSessionRequest(BaseModel):
    user: str | None = Field(default=None, description="Who is chatting; approvals are recorded under this name")
    invalidate_session_id: str | None = Field(default=None, description="End this session before starting the new one")


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


def _session_or_404(session_id: str):
    try:
        return sessions.get_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Chat session not found")


@router.post("/sessions")
def new_session(req: NewSessionRequest):
    """Start a fresh session. Pass invalidate_session_id to end the current one at the same time."""
    s = sessions.new_session(req.user, req.invalidate_session_id)
    return {**s.model_dump(mode="json"), "mode": get_chat_service().mode}


@router.get("/sessions")
def list_sessions(include_closed: bool = True, limit: int = 50):
    return [s.model_dump(mode="json") for s in sessions.list_sessions(include_closed, limit)]


@router.get("/sessions/{session_id}")
def get_session(session_id: str):
    s = _session_or_404(session_id)
    return {**s.model_dump(mode="json"),
            "messages": [m.model_dump(mode="json") for m in store.get_messages(session_id)]}


@router.post("/sessions/{session_id}/messages")
def send_message(session_id: str, req: MessageRequest):
    _session_or_404(session_id)
    try:
        return get_chat_service().send(session_id, req.message)
    except SessionClosed as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/sessions/{session_id}/invalidate")
def invalidate_session(session_id: str):
    """End the session: it becomes read-only and its working memory is wiped."""
    _session_or_404(session_id)
    return sessions.end_session(session_id).model_dump(mode="json")


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    if not sessions.delete_session(session_id):
        raise HTTPException(status_code=404, detail="Chat session not found")
    return {"deleted": session_id}


@router.get("/tools")
def tools():
    """Tools the assistant can call, with their source (mcp / local) and effect (read / write / confirm)."""
    return catalog()
