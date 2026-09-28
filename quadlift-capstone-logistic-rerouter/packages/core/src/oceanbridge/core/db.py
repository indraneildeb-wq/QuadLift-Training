"""SQLite persistence (SQLAlchemy ORM)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, Integer, LargeBinary, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from oceanbridge.config import get_settings


class Base(DeclarativeBase):
    pass


class CarrierRow(Base):
    __tablename__ = "carriers"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    mode: Mapped[str] = mapped_column(String)
    capacity_remaining: Mapped[float] = mapped_column(Float)
    capacity_unit: Mapped[str] = mapped_column(String)


class ShipmentRow(Base):
    __tablename__ = "shipments"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    customer: Mapped[str] = mapped_column(String)
    commodity: Mapped[str] = mapped_column(String)
    origin: Mapped[str] = mapped_column(String)
    destination: Mapped[str] = mapped_column(String)
    mode: Mapped[str] = mapped_column(String)
    carrier_id: Mapped[str] = mapped_column(String)
    legs: Mapped[list] = mapped_column(JSON)
    weight_kg: Mapped[float] = mapped_column(Float)
    volume_cbm: Mapped[float] = mapped_column(Float)
    cargo_value_usd: Mapped[float] = mapped_column(Float)
    current_cost_usd: Mapped[float] = mapped_column(Float)
    departure_date: Mapped[date] = mapped_column(Date)
    eta: Mapped[date] = mapped_column(Date)
    required_delivery_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String)
    po_id: Mapped[str | None] = mapped_column(String, nullable=True)


class DisruptionRow(Base):
    __tablename__ = "disruptions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    type: Mapped[str] = mapped_column(String)
    location: Mapped[str] = mapped_column(String)
    severity: Mapped[float] = mapped_column(Float)
    expected_delay_days: Mapped[float] = mapped_column(Float)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String)
    scenario: Mapped[str | None] = mapped_column(String, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class PurchaseOrderRow(Base):
    __tablename__ = "purchase_orders"
    po_id: Mapped[str] = mapped_column(String, primary_key=True)
    shipment_id: Mapped[str] = mapped_column(String, index=True)
    carrier_id: Mapped[str] = mapped_column(String)
    amount_usd: Mapped[float] = mapped_column(Float)
    route_label: Mapped[str] = mapped_column(String)
    legs: Mapped[list] = mapped_column(JSON)
    sla: Mapped[dict] = mapped_column(JSON)
    approval_id: Mapped[str | None] = mapped_column(String, nullable=True)
    supersedes_po_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String)
    auto_executed: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class ApprovalRow(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(String, index=True)
    shipment_id: Mapped[str] = mapped_column(String, index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String, index=True)
    approver: Mapped[str | None] = mapped_column(String, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RunRow(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String)
    backend: Mapped[str] = mapped_column(String)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    trace: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class NegotiationRow(Base):
    __tablename__ = "negotiations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shipment_id: Mapped[str] = mapped_column(String, index=True)
    carrier_id: Mapped[str] = mapped_column(String)
    round: Mapped[int] = mapped_column(Integer)
    offer: Mapped[dict] = mapped_column(JSON)
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class AuditRow(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime)
    actor: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    subject: Mapped[str] = mapped_column(String)
    detail: Mapped[dict] = mapped_column(JSON)


class CacheRow(Base):
    __tablename__ = "cache_entries"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    namespace: Mapped[str] = mapped_column(String, index=True)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON)
    guard: Mapped[dict] = mapped_column(JSON)
    touchpoints: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    hits: Mapped[int] = mapped_column(Integer, default=0)


class ProposalRow(Base):
    """Latest set of priced route alternatives per shipment (shared by API process and MCP server)."""
    __tablename__ = "route_proposals"
    shipment_id: Mapped[str] = mapped_column(String, primary_key=True)
    options: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class ScenarioRow(Base):
    __tablename__ = "active_scenarios"
    name: Mapped[str] = mapped_column(String, primary_key=True)
    activated_at: Mapped[datetime] = mapped_column(DateTime)


class ChatSessionRow(Base):
    __tablename__ = "chat_sessions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String, default="New chat")
    status: Mapped[str] = mapped_column(String, index=True)  # active | ended | expired
    summary: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[dict] = mapped_column(JSON, default=dict)   # working memory: entities, pending action
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String, nullable=True)


class ChatMessageRow(Base):
    __tablename__ = "chat_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String, index=True)
    role: Mapped[str] = mapped_column(String)  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    tool_calls: Mapped[list] = mapped_column(JSON, default=list)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class LlmUsageRow(Base):
    __tablename__ = "llm_usage"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    task_type: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(Text)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)


_engines: dict[str, object] = {}


def get_engine(db_file: Path | None = None):
    db_file = Path(db_file or get_settings().db_file)
    key = str(db_file)
    if key not in _engines:
        db_file.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _):  # WAL lets the MCP subprocess and the API share the file safely
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        Base.metadata.create_all(engine)
        _engines[key] = engine
    return _engines[key]


def dispose_engines() -> None:
    for engine in _engines.values():
        engine.dispose()
    _engines.clear()


@contextmanager
def session_scope(db_file: Path | None = None) -> Iterator[Session]:
    session = sessionmaker(bind=get_engine(db_file), expire_on_commit=False)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
