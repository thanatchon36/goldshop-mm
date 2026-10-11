from decimal import Decimal

import pytest

from goldshop.binance.symbol import SymbolFilters


@pytest.fixture
def btc_filters() -> SymbolFilters:
    return SymbolFilters("BTCUSDT", Decimal("0.10"), Decimal("0.001"), Decimal("0.001"), Decimal("50"))
