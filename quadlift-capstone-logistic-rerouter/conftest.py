import os

import pytest

os.environ["OB_LLM_MODE"] = "offline"
os.environ["CREWAI_DISABLE_TELEMETRY"] = "true"
os.environ["OTEL_SDK_DISABLED"] = "true"


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Isolated, freshly seeded database + empty caches for each test."""
    from oceanbridge.cache import semantic_cache
    from oceanbridge.config import reset_settings_cache
    from oceanbridge.core import db
    from oceanbridge.seed import seed

    db_file = tmp_path / "test.db"
    monkeypatch.setenv("OB_DB_PATH", str(db_file))
    monkeypatch.setenv("OB_LLM_MODE", "offline")
    reset_settings_cache()
    db.dispose_engines()
    semantic_cache.reset_caches()
    seed(reset=True)
    yield db_file
    db.dispose_engines()
    semantic_cache.reset_caches()
    reset_settings_cache()


@pytest.fixture
def disrupted(fresh_db):
    """Activate the Rotterdam strike and persist the detected disruptions."""
    from oceanbridge.core import repository as repo
    from oceanbridge.core import risk
    from oceanbridge.feeds import scenarios

    scenarios.activate("rotterdam_strike")
    for d in risk.detect_all():
        repo.upsert_disruption(d)
    return fresh_db
