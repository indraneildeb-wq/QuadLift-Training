import yaml

from .paths import resolve


class PricingTable:
    """Versioned USD-per-token pricing, loaded from config rather than hardcoded,
    since model prices change independently of code."""

    def __init__(self, file_path: str):
        with open(resolve(file_path), "r", encoding="utf-8") as f:
            self._table = yaml.safe_load(f)

    def cost_usd(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        prices = self._table.get(model, self._table["mock"])
        return (
            prompt_tokens / 1_000_000 * prices["prompt_per_million"]
            + completion_tokens / 1_000_000 * prices["completion_per_million"]
        )
