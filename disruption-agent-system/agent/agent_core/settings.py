import yaml

from .paths import resolve


def load_agent_config(config_path: str) -> dict:
    with open(resolve(config_path), "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
