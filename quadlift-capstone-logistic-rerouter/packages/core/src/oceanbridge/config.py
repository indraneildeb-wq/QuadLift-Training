"""Settings loader: config/settings.yaml, overridden by OB_* environment variables and .env."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

def _find_project_root() -> Path:
    """The workspace root: OB_PROJECT_ROOT, else the nearest folder holding config/settings.yaml, searching
    upwards from the current directory and then from this file (packages/core/src/oceanbridge/)."""
    if os.environ.get("OB_PROJECT_ROOT"):
        return Path(os.environ["OB_PROJECT_ROOT"]).resolve()
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        for folder in (start, *start.parents):
            if (folder / "config" / "settings.yaml").is_file():
                return folder
    return Path.cwd()


PROJECT_ROOT = _find_project_root()
SETTINGS_FILE = Path(os.environ.get("OB_SETTINGS_FILE", PROJECT_ROOT / "config" / "settings.yaml"))


class HitlSettings(BaseModel):
    max_cost_increase_pct: float = 5.0
    max_cargo_value_usd: float = 250_000
    approver_role: str = "Logistics Operations Manager"


class ModelSettings(BaseModel):
    light: str = "gpt-4o-mini"
    heavy: str = "gpt-4o"
    embedding: str = "text-embedding-3-small"
    escalation_threshold: int = 3
    pricing: dict[str, tuple[float, float]] = Field(
        default_factory=lambda: {"gpt-4o-mini": (0.15, 0.60), "gpt-4o": (2.50, 10.00)}
    )


class CacheSettings(BaseModel):
    l1_max_entries: int = 2048
    l1_ttl_seconds: int = 900
    l2_similarity_threshold: float = 0.92
    l2_max_entries: int = 5000
    l3_ttl_seconds: int = 86_400
    disrupted_ttl_seconds: int = 300
    weight_tolerance: float = 0.10
    embedder: Literal["auto", "openai", "local"] = "auto"


class MonitoringSettings(BaseModel):
    risk_threshold: float = 0.45
    delay_threshold_days: float = 3.0


class EconomicsSettings(BaseModel):
    holding_cost_per_day: float = 0.0005
    lateness_penalty_per_day: float = 0.004


class ChatSettings(BaseModel):
    history_messages: int = 12            # recent messages sent to the LLM verbatim
    summarize_after_messages: int = 24    # older messages are folded into the session summary
    session_idle_timeout_minutes: int = 60
    max_tool_steps: int = 6               # tool-calling rounds per turn before forcing an answer
    default_user: str = "Logistics Operations Manager"


class _YamlSource(PydanticBaseSettingsSource):
    def get_field_value(self, field, field_name):  # pragma: no cover - unused, required by ABC
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        if SETTINGS_FILE.exists():
            return yaml.safe_load(SETTINGS_FILE.read_text(encoding="utf-8")) or {}
        return {}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OB_",
        env_nested_delimiter="__",
        env_file=PROJECT_ROOT / ".env",
        extra="ignore",
    )

    db_path: str = "data/oceanbridge.db"
    llm_mode: Literal["auto", "crewai", "offline"] = "auto"
    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    hitl: HitlSettings = HitlSettings()
    models: ModelSettings = ModelSettings()
    cache: CacheSettings = CacheSettings()
    monitoring: MonitoringSettings = MonitoringSettings()
    economics: EconomicsSettings = EconomicsSettings()
    chat: ChatSettings = ChatSettings()

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        return init_settings, env_settings, dotenv_settings, _YamlSource(settings_cls)

    @property
    def db_file(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def use_crewai(self) -> bool:
        if self.llm_mode == "offline":
            return False
        if self.llm_mode == "crewai":
            return True
        return bool(self.openai_api_key or os.environ.get("OPENAI_API_KEY"))


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
