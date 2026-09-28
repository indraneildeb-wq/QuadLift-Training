import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from oceanbridge.chat import sessions, store
from oceanbridge.chat.service import ChatService, reset_chat_service
from oceanbridge.chat.sessions import SessionClosed
from oceanbridge.core import repository as repo
from oceanbridge.core.repository import utcnow
from oceanbridge.models import ApprovalStatus


@pytest.fixture
def chat(fresh_db):
    svc = ChatService()
    yield svc
    svc.close()
    reset_chat_service()


# ---------------------------------------------------------------- a fake OpenAI client for the LLM loop
class FakeOpenAI:
    def __init__(self, script):
        self.script, self.requests = list(script), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def completion(content=None, tool_calls=(), prompt_tokens=100, completion_tokens=20):
    calls = [SimpleNamespace(id=f"call_{i}", type="function",
                             function=SimpleNamespace(name=name, arguments=json.dumps(args)))
             for i, (name, args) in enumerate(tool_calls)]
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls))],
                           usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens))


# ---------------------------------------------------------------- sessions
def test_session_lifecycle_invalidate_and_fresh_start(chat):
    s = sessions.new_session("Jane Doe")
    assert s.status == "active" and s.user == "Jane Doe"
    chat.send(s.id, "what is the HITL policy?")
    assert sessions.get_session(s.id).message_count == 2
    assert sessions.get_session(s.id).title == "what is the HITL policy?"

    new = sessions.new_session("Jane Doe", invalidate_session_id=s.id)
    old = sessions.get_session(s.id)
    assert old.status == "ended" and old.state == {} and old.end_reason == "replaced by a new session"
    assert new.status == "active" and new.state == {}
    with pytest.raises(SessionClosed):
        chat.send(s.id, "hello again")
    assert len(store.get_messages(s.id)) == 2  # history stays readable, nothing appended


def test_idle_session_expires(chat):
    s = sessions.new_session()
    store.update_session(s.id, updated_at=utcnow() - timedelta(hours=3))
    assert sessions.get_session(s.id).status == "expired"
    with pytest.raises(SessionClosed, match="expired"):
        chat.send(s.id, "hi")


# ---------------------------------------------------------------- offline agent: tools (incl. MCP) + memory
def test_offline_calls_mcp_tools_and_resolves_pronouns(chat):
    s = sessions.new_session()
    r1 = chat.send(s.id, "status of SHP-1004")
    assert r1["mode"] == "offline"
    assert [(t["tool"], t["source"], t["ok"]) for t in r1["tool_calls"]] == [("get_shipment_status", "mcp", True)]
    assert "SHP-1004" in r1["reply"]

    r2 = chat.send(s.id, "show its alternatives")  # "its" -> SHP-1004 from working memory
    call = r2["tool_calls"][0]
    assert call["tool"] == "list_route_alternatives" and call["source"] == "mcp"
    assert call["arguments"]["shipment_id"] == "SHP-1004"
    assert "| Option |" in r2["reply"]

    r3 = chat.send(s.id, "quote sea freight Shanghai to Rotterdam 18000 kg 60 cbm")
    assert r3["tool_calls"][0]["arguments"] == {"origin": "CNSHA", "destination": "NLRTM", "mode": "sea",
                                                "weight_kg": 18000.0, "volume_cbm": 60.0}


def test_approval_needs_explicit_confirmation(chat):
    s = sessions.new_session("Jane Doe")
    run = chat.send(s.id, "activate the rotterdam strike and run the pipeline")
    assert run["tool_calls"][0]["tool"] == "run_rerouting_pipeline" and run["tool_calls"][0]["ok"]
    approval = repo.list_approvals(ApprovalStatus.PENDING)[0]

    proposed = chat.send(s.id, f"approve {approval.id}")
    assert proposed["pending_action"]["approval_id"] == approval.id
    assert repo.get_approval(approval.id).status == ApprovalStatus.PENDING  # nothing executed yet

    # Anything other than a plain confirmation drops the proposal instead of executing it.
    other = chat.send(s.id, "ok, what about the HITL policy?")
    assert "Dropped the unconfirmed approve" in other["reply"] and other["pending_action"] is None
    assert repo.get_approval(approval.id).status == ApprovalStatus.PENDING

    chat.send(s.id, "approve it")  # "it" -> the approval discussed last
    done = chat.send(s.id, "confirm")
    assert done["tool_calls"][0]["tool"] == "execute_approval_decision" and done["tool_calls"][0]["ok"]
    executed = repo.get_approval(approval.id)
    assert executed.status == ApprovalStatus.EXECUTED and executed.approver == "Jane Doe"
    assert "PO-" in done["reply"]
    assert "Nothing is waiting" in chat.send(s.id, "confirm")["reply"]


# ---------------------------------------------------------------- LLM agent loop (fake client, no key needed)
def test_llm_loop_calls_tools_then_answers(fresh_db):
    fake = FakeOpenAI([
        completion(tool_calls=[("get_hitl_policy", {}), ("list_disruptions", {})]),
        completion(content="Auto-execute below 5% and $250k; no disruptions are active.", prompt_tokens=150),
    ])
    svc = ChatService(llm_client=fake)
    s = sessions.new_session()
    r = svc.send(s.id, "What is the approval policy and is anything disrupted?")
    assert r["mode"] == "llm" and r["model"] == "gpt-4o-mini"
    assert [t["tool"] for t in r["tool_calls"]] == ["get_hitl_policy", "list_disruptions"]
    assert r["reply"].startswith("Auto-execute")
    # second request carried the tool results back to the model
    second = fake.requests[1]["messages"]
    assert [m["role"] for m in second[-3:]] == ["assistant", "tool", "tool"]
    assert "Session memory" in second[0]["content"]
    sess = sessions.get_session(s.id)
    assert (sess.prompt_tokens, sess.completion_tokens) == (250, 40)
    usage = [u for u in repo.llm_usage_rows() if u["task_type"] == "chat"][0]
    assert usage["model"] == "gpt-4o-mini" and usage["cost_usd"] > 0


def test_llm_routes_analytical_questions_to_heavy_model(fresh_db):
    fake = FakeOpenAI([completion(content="Rail is faster; sea is cheaper.")])
    svc = ChatService(llm_client=fake)
    s = sessions.new_session()
    assert svc.send(s.id, "Compare rail and sea for SHP-1004 and recommend one")["model"] == "gpt-4o"


def test_llm_failure_falls_back_to_offline(fresh_db):
    svc = ChatService(llm_client=FakeOpenAI([RuntimeError("no network")]))
    s = sessions.new_session()
    r = svc.send(s.id, "what is the HITL policy?")
    assert "LLM unavailable" in r["reply"] and "Auto-execute only if" in r["reply"]
    assert r["tool_calls"][0]["tool"] == "get_hitl_policy"


def test_long_sessions_are_summarised(fresh_db, monkeypatch):
    from oceanbridge.config import reset_settings_cache

    monkeypatch.setenv("OB_CHAT__SUMMARIZE_AFTER_MESSAGES", "4")
    monkeypatch.setenv("OB_CHAT__HISTORY_MESSAGES", "2")
    reset_settings_cache()
    svc = ChatService()
    s = sessions.new_session()
    for q in ["what is the HITL policy?", "list disruptions", "list scenarios", "cache metrics"]:
        svc.send(s.id, q)
    summary = sessions.get_session(s.id).summary
    assert summary.startswith("User asked:") and "HITL policy" in summary


# ---------------------------------------------------------------- REST API
def test_chat_api(fresh_db):
    from oceanbridge.api.main import app

    with TestClient(app) as c:
        s = c.post("/chat/sessions", json={"user": "Jane Doe"}).json()
        assert s["status"] == "active" and s["mode"] == "offline"
        r = c.post(f"/chat/sessions/{s['id']}/messages", json={"message": "what is the HITL policy?"}).json()
        assert r["tool_calls"][0]["tool"] == "get_hitl_policy"
        hist = c.get(f"/chat/sessions/{s['id']}").json()
        assert [m["role"] for m in hist["messages"]] == ["user", "assistant"]

        fresh = c.post("/chat/sessions", json={"invalidate_session_id": s["id"]}).json()
        assert c.get(f"/chat/sessions/{s['id']}").json()["status"] == "ended"
        assert c.post(f"/chat/sessions/{s['id']}/messages", json={"message": "hi"}).status_code == 409
        assert c.post(f"/chat/sessions/{fresh['id']}/invalidate").json()["status"] == "ended"
        assert c.delete(f"/chat/sessions/{s['id']}").status_code == 200
        assert c.get(f"/chat/sessions/{s['id']}").status_code == 404
        tools = {t["name"]: t for t in c.get("/chat/tools").json()}
        assert tools["get_shipment_status"]["source"] == "mcp" and tools["decide_approval"]["effect"] == "confirm"
        assert "issue_purchase_order" not in tools
