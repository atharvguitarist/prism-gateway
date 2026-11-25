"""Cost accounting.

Money is stored and summed as **integer nano-USD** (1 USD = 1_000_000_000 nUSD).
With prices quoted per 1M tokens, `price_per_1m * 1000` is the nano-USD cost of a
single token, so for any price with <= 3 decimals the arithmetic is exact: there
is no floating-point drift when thousands of requests are summed, and the value
shown in the `x-prism-cost-usd` header is *exactly* what is added to the usage
totals. This is what lets the load test reconcile to the last digit.
"""
from __future__ import annotations

from decimal import Decimal

NANO = 1_000_000_000


class UnknownModelPrice(KeyError):
    pass


class PriceTable:
    def __init__(self, prices: dict[str, dict[str, float]]):
        self._prices = {
            m: (Decimal(str(p["input_per_1m"])), Decimal(str(p["output_per_1m"])))
            for m, p in prices.items()
        }

    def __contains__(self, model: str) -> bool:
        return model in self._prices

    def cost_nusd(self, model: str, prompt_tokens: int, completion_tokens: int) -> int:
        """Cost in integer nano-USD, computed from *provider-reported* token usage."""
        try:
            inp, out = self._prices[model]
        except KeyError:
            raise UnknownModelPrice(model) from None
        if prompt_tokens < 0 or completion_tokens < 0:
            raise ValueError("token counts must be non-negative")
        # (tokens * USD_per_1M / 1e6) USD  ==  tokens * USD_per_1M * 1000 nUSD
        total = (Decimal(prompt_tokens) * inp + Decimal(completion_tokens) * out) * 1000
        return int(total.to_integral_value(rounding="ROUND_HALF_UP"))


def format_usd(nusd: int) -> str:
    """Render nano-USD as a plain decimal USD string, e.g. 14000 -> '0.000014', 0 -> '0'."""
    if nusd == 0:
        return "0"
    s = f"{nusd / NANO:.9f}".rstrip("0")
    return s.rstrip(".") if s.endswith(".") else s


def nusd_to_usd(nusd: int) -> float:
    return round(nusd / NANO, 9)


def usd_to_nusd(usd: float) -> int:
    return int((Decimal(str(usd)) * NANO).to_integral_value(rounding="ROUND_HALF_UP"))
