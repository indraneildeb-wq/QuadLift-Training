"""Chat feature: session management around the Chat Assistant agent (its memory lives in the agent package).

    sessions.py  create / resume / invalidate / expire sessions
    store.py     SQLite persistence (chat_sessions, chat_messages)
    service.py   one chat turn: session checks, confirmations, agent call, memory update
"""
