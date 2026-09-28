"""One chat turn, end to end.

    1. the session must be active (not invalidated, not idle-expired)
    2. the user message is stored and working memory picks up any ids in it
    3. a pending confirmation is resolved in code: "confirm" executes it, "cancel" drops it
    4. otherwise the Chat Assistant runs: LLM (routed model) when configured, else the offline planner;
       if the LLM fails the offline planner answers instead
    5. the reply, tool calls and usage are stored; long sessions get their older messages summarised
"""

from __future__ import annotations

import re
import threading
from collections import defaultdict

from oceanbridge.agents.chat_assistant import TurnOutput, ToolContext, is_analytical, run_llm, run_offline
from oceanbridge.agents.chat_assistant.agent import summarize_llm
from oceanbridge.agents.chat_assistant.tools import ToolCallRecord
from oceanbridge.agents.common.mcp_tools import MCPToolbox
from oceanbridge.agents.chat_assistant import memory
from oceanbridge.chat import sessions, store
from oceanbridge.config import get_settings
from oceanbridge.core import repository as repo
from oceanbridge.core.repository import utcnow
from oceanbridge.flow.reroute_flow import optimize_what_if, run_pipeline
from oceanbridge.llm.router import ComplexitySignals, ModelRouter, TaskType
from oceanbridge.runtime import execute_approval, reject_approval

# The whole message must be a confirmation (optionally naming the approval), so "ok, what about SHP-1004?"
# can never execute a pending approval by accident.
CONFIRM = re.compile(r"\s*(yes|y|confirm(ed)?|yes,? confirm|go ahead|proceed|do it|ok(ay)?)"
                     r"(\s+APR-[0-9A-F]{8})?[\s.!]*", re.I)
CANCEL = re.compile(r"\s*(no|n|cancel|stop|abort|don'?t|do not( do it)?)(\s+APR-[0-9A-F]{8})?[\s.!]*", re.I)


class ChatService:
    def __init__(self, llm_client=None):
        self._toolbox: MCPToolbox | None = None
        self._toolbox_lock = threading.Lock()
        self._session_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self.llm_client = llm_client  # injectable for tests
        self.router = ModelRouter()

    # ------------------------------------------------------------------ resources
    def toolbox(self) -> MCPToolbox:
        """One MCP toolbox per process, started on the first MCP tool call and reused across turns."""
        with self._toolbox_lock:
            if self._toolbox is None:
                self._toolbox = MCPToolbox()
            return self._toolbox

    def close(self) -> None:
        with self._toolbox_lock:
            if self._toolbox is not None:
                self._toolbox.close()
                self._toolbox = None

    @property
    def mode(self) -> str:
        return "llm" if (get_settings().use_crewai or self.llm_client is not None) else "offline"

    # ------------------------------------------------------------------ turn
    def send(self, session_id: str, text: str) -> dict:
        with self._session_locks[session_id]:
            session = sessions.require_active(session_id)
            cfg = get_settings().chat
            state = dict(session.state)
            history = store.get_messages(session_id, last=cfg.history_messages)
            store.add_message(session_id, "user", text)
            memory.remember(state, text)
            ctx = ToolContext(session_id=session_id, user=session.user, state=state, toolbox_factory=self.toolbox,
                              run_pipeline=run_pipeline, optimize_what_if=optimize_what_if)

            pending = state.get("pending_action")
            note = ""
            if pending and CONFIRM.fullmatch(text):
                out = self._execute_pending(ctx, pending)
            elif pending and CANCEL.fullmatch(text):
                state.pop("pending_action", None)
                out = TurnOutput(reply=f"Cancelled. {pending['approval_id']} is still pending.", model="rule")
            elif not pending and (CONFIRM.fullmatch(text) or CANCEL.fullmatch(text)):
                out = TurnOutput(reply="Nothing is waiting for confirmation. Ask me to approve or reject an "
                                       "approval first, e.g. 'approve APR-1A2B3C4D'.", model="rule")
            else:
                if pending:  # a new request replaces the unconfirmed action, so a later "yes" can't trigger it
                    state.pop("pending_action", None)
                    note = f"_(Dropped the unconfirmed {pending['decision']} of {pending['approval_id']}.)_\n\n"
                out = self._agent_turn(text, history, ctx, session.summary)
                out.reply = note + out.reply

            assistant = store.add_message(session_id, "assistant", out.reply,
                                          [r.to_dict() for r in out.tool_calls], out.model)
            summary = self._maintain_summary(session, cfg)
            updated = store.update_session(
                session_id, state=state, summary=summary, updated_at=utcnow(),
                message_count=session.message_count + 2,
                prompt_tokens=session.prompt_tokens + out.prompt_tokens,
                completion_tokens=session.completion_tokens + out.completion_tokens,
                title=session.title if session.title != "New chat" else (text.strip().split("\n")[0][:60] or "New chat"),
            )
            return {"reply": out.reply, "tool_calls": [r.to_dict() for r in out.tool_calls], "model": out.model,
                    "mode": self.mode, "pending_action": state.get("pending_action"),
                    "message_id": assistant.id, "session": updated.model_dump(mode="json")}

    def _agent_turn(self, text, history, ctx: ToolContext, summary: str) -> TurnOutput:
        decision = self.router.select(TaskType.CHAT, ComplexitySignals(analytical=is_analytical(text)))
        if self.mode == "llm":
            try:
                out = run_llm(text, history, ctx, summary, decision, client=self.llm_client)
                cost = self.router.estimate_cost(decision.model, out.prompt_tokens, out.completion_tokens)
                repo.record_llm_usage(ctx.session_id, TaskType.CHAT.value, out.model, decision.reason,
                                      out.prompt_tokens, out.completion_tokens, cost)
                return out
            except Exception as e:
                out = run_offline(text, ctx, decision)
                out.reply = f"_(LLM unavailable: {type(e).__name__}; answered with the offline assistant.)_\n\n" + out.reply
        else:
            out = run_offline(text, ctx, decision)
        repo.record_llm_usage(ctx.session_id, TaskType.CHAT.value, out.model, decision.reason)
        return out

    def _execute_pending(self, ctx: ToolContext, pending: dict) -> TurnOutput:
        ctx.state.pop("pending_action", None)
        approval_id, decision = pending["approval_id"], pending["decision"]
        args = {"approval_id": approval_id, "decision": decision, "comment": pending.get("comment")}
        try:
            if decision == "approve":
                r = execute_approval(approval_id, ctx.user, pending.get("comment") or "approved via chat")
                reply = (f"✅ Approved {approval_id} as {ctx.user}. The negotiation agent issued **{r['po_id']}** "
                         f"for {r['shipment_id']} at ${r['amount_usd']:,.2f} after {r['rounds']} round(s) "
                         f"(list ${r['list_price_usd']:,.2f}).")
                summary, ok = f"PO {r['po_id']}", True
            else:
                r = reject_approval(approval_id, ctx.user, pending.get("comment") or "rejected via chat")
                reply = f"Rejected {approval_id}. Shipment {pending['shipment_id']} is back to {r['shipment_status']}."
                summary, ok = "rejected", True
        except Exception as e:
            reply, summary, ok = f"⚠️ Could not {decision} {approval_id}: {e}", str(e), False
        record = ToolCallRecord("execute_approval_decision", "local", "confirm", args, ok, summary, 0.0)
        return TurnOutput(reply=reply, tool_calls=[record], model="rule (confirmed by user)")

    def _maintain_summary(self, session, cfg) -> str:
        """Once a session is long, fold messages that fell out of the history window into the summary."""
        total = session.message_count + 2
        if total < cfg.summarize_after_messages or total % cfg.history_messages:
            return session.summary
        older = store.get_messages(session.id)[:-cfg.history_messages]
        if not older:
            return session.summary
        if self.mode == "llm":
            try:
                model = get_settings().models.light
                return summarize_llm(session.summary, older[-cfg.history_messages:], model, client=self.llm_client)
            except Exception:
                pass
        return memory.summarize_offline(older[-cfg.history_messages:], session.summary)


_service: ChatService | None = None
_service_lock = threading.Lock()


def get_chat_service() -> ChatService:
    global _service
    with _service_lock:
        if _service is None:
            _service = ChatService()
        return _service


def reset_chat_service() -> None:
    global _service
    with _service_lock:
        if _service is not None:
            _service.close()
        _service = None
