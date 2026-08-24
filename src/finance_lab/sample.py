from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd

from finance_lab.sources import CANONICAL_COLUMNS


def make_synthetic_daily_prices(periods: int = 900, seed: int = 20260814) -> pd.DataFrame:
    """Create deterministic synthetic bars for offline learning and tests."""
    if periods < 120:
        raise ValueError("演示数据至少需要120个交易日")
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2022-01-04", periods=periods)
    daily_returns = rng.normal(0.00025, 0.012, periods)
    close = 100 * np.exp(np.cumsum(daily_returns))
    open_price = close * (1 + rng.normal(0, 0.003, periods))
    high = np.maximum(open_price, close) * (1 + rng.uniform(0.0005, 0.012, periods))
    low = np.minimum(open_price, close) * (1 - rng.uniform(0.0005, 0.012, periods))
    volume = rng.integers(1_000_000, 8_000_000, periods)
    ingested = datetime.now(UTC).replace(microsecond=0).isoformat()
    frame = pd.DataFrame(
        {
            "symbol": "demo.000300",
            "trade_date": dates,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "volume_unit": "synthetic_unit",
            "amount": volume * close,
            "adjustment": "none",
            "source": "SYNTHETIC_DEMO_NOT_MARKET_DATA",
            "ingested_at": ingested,
        }
    )
    return frame[CANONICAL_COLUMNS]
