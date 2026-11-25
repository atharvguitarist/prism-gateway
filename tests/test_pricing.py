from decimal import Decimal

import pytest

from prism.config import load_pricing
from prism.pricing import NANO, PriceTable, UnknownModelPrice, format_usd, usd_to_nusd

from conftest import ROOT


@pytest.fixture
def prices():
    return PriceTable(load_pricing(ROOT / "data" / "model_pricing.json"))


def test_cost_from_price_table_is_exact(prices):
    # alpha-small: $0.15/M in, $0.60/M out  -> 5*150 + 17*600 nUSD
    assert prices.cost_nusd("alpha-small", 5, 17) == 5 * 150 + 17 * 600 == 10_950
    # alpha-large: $3/M in, $15/M out
    assert prices.cost_nusd("alpha-large", 1_000_000, 1_000_000) == 18 * NANO
    assert prices.cost_nusd("beta-small", 0, 0) == 0


def test_cost_has_no_float_drift_when_summed(prices):
    # 10_000 requests of a "nasty" size must sum to exactly 10_000x one request
    one = prices.cost_nusd("beta-large", 123, 457)
    assert sum(prices.cost_nusd("beta-large", 123, 457) for _ in range(10_000)) == one * 10_000


def test_unknown_model_and_negative_tokens(prices):
    with pytest.raises(UnknownModelPrice):
        prices.cost_nusd("gpt-nope", 1, 1)
    with pytest.raises(ValueError):
        prices.cost_nusd("alpha-small", -1, 1)


@pytest.mark.parametrize("n,s", [(0, "0"), (14_000, "0.000014"), (1_000_000_000, "1"), (8_700, "0.0000087")])
def test_format_usd(n, s):
    assert format_usd(n) == s


def test_usd_roundtrip_for_seed_budget():
    assert usd_to_nusd(0.00001) == 10_000 and usd_to_nusd(50) == 50 * NANO
