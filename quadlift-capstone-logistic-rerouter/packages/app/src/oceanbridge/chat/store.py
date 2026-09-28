"""Persistence for chat sessions and messages (tables chat_sessions, chat_messages)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from oceanbridge.core.db import ChatMessageRow, ChatSessionRow, session_scope
from oceanbridge.core.repository import utcnow


class ChatSession(BaseModel):
    id: str
    user: str
    title: str
    status: str
    summary: str = ""
    state: dict = Field(default_factory=dict)
    message_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    created_at: datetime
    updated_at: datetime
    ended_at: datetime | None = None
    end_reason: str | None = None


class ChatMessage(BaseModel):
    id: int
    session_id: str
    role: str
    content: str
    tool_calls: list = Field(default_factory=list)
    model: str | None = None
    created_at: datetime


def _session(row: ChatSessionRow) -> ChatSession:
    return ChatSession(
        id=row.id, user=row.user, title=row.title, status=row.status, summary=row.summary or "",
        state=dict(row.state or {}), message_count=row.message_count, prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens, created_at=row.created_at, updated_at=row.updated_at,
        ended_at=row.ended_at, end_reason=row.end_reason,
    )


def _message(row: ChatMessageRow) -> ChatMessage:
    return ChatMessage(id=row.id, session_id=row.session_id, role=row.role, content=row.content,
                       tool_calls=list(row.tool_calls or []), model=row.model, created_at=row.created_at)


def insert_session(session_id: str, user: str) -> ChatSession:
    now = utcnow()
    with session_scope() as s:
        row = ChatSessionRow(id=session_id, user=user, title="New chat", status="active", summary="", state={},
                             message_count=0, prompt_tokens=0, completion_tokens=0, created_at=now, updated_at=now)
        s.add(row)
        s.flush()
        return _session(row)


def get_session(session_id: str) -> ChatSession | None:
    with session_scope() as s:
        row = s.get(ChatSessionRow, session_id)
        return _session(row) if row else None


def list_sessions(include_closed: bool = True, limit: int = 50) -> list[ChatSession]:
    with session_scope() as s:
        q = select(ChatSessionRow).order_by(ChatSessionRow.updated_at.desc()).limit(limit)
        if not include_closed:
            q = q.where(ChatSessionRow.status == "active")
        return [_session(r) for r in s.scalars(q)]


def update_session(session_id: str, **fields) -> ChatSession:
    with session_scope() as s:
        row = s.get(ChatSessionRow, session_id)
        if row is None:
            raise KeyError(session_id)
        for k, v in fields.items():
            setattr(row, k, v)
        return _session(row)


def add_message(session_id: str, role: str, content: str, tool_calls: list | None = None,
                model: str | None = None) -> ChatMessage:
    with session_scope() as s:
        row = ChatMessageRow(session_id=session_id, role=role, content=content, tool_calls=tool_calls or [],
                             model=model, created_at=utcnow())
        s.add(row)
        s.flush()
        return _message(row)


def get_messages(session_id: str, last: int | None = None) -> list[ChatMessage]:
    with session_scope() as s:
        q = select(ChatMessageRow).where(ChatMessageRow.session_id == session_id).order_by(ChatMessageRow.id.desc())
        if last:
            q = q.limit(last)
        return list(reversed([_message(r) for r in s.scalars(q)]))


def delete_session(session_id: str) -> bool:
    with session_scope() as s:
        s.execute(delete(ChatMessageRow).where(ChatMessageRow.session_id == session_id))
        return s.execute(delete(ChatSessionRow).where(ChatSessionRow.id == session_id)).rowcount > 0
