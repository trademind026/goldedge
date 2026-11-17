from datetime import datetime, timezone
from typing import Any, Sequence


def _get_field(bar: Any, key: str, default: float = 0.0) -> Any:
    if isinstance(bar, dict):
        return bar.get(key, default)
    return getattr(bar, key, default)


def aggregate_ohlcv_bars(bars_1m: Sequence[Any], period_minutes: int) -> list[dict]:
    """
    Pure-Python OHLCV aggregator from 1-minute bars (Bar objects or dicts)
    into `period_minutes` (e.g., 5 or 60) completed/current bars.
    """
    if not bars_1m or period_minutes <= 1:
        return []

    buckets: dict[int, dict] = {}
    period_secs = period_minutes * 60
    for bar in bars_1m:
        ts = _get_field(bar, "time", None)
        if ts is None:
            continue
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        epoch = int(ts.timestamp())
        bucket_epoch = (epoch // period_secs) * period_secs
        o = float(_get_field(bar, "open", 0.0))
        h = float(_get_field(bar, "high", o))
        l = float(_get_field(bar, "low", o))
        c = float(_get_field(bar, "close", o))
        v = float(_get_field(bar, "volume", _get_field(bar, "tick_volume", 1.0)) or 1.0)
        if bucket_epoch not in buckets:
            buckets[bucket_epoch] = {
                "time": datetime.fromtimestamp(bucket_epoch, tz=timezone.utc),
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": v,
            }
        else:
            b = buckets[bucket_epoch]
            if h > b["high"]:
                b["high"] = h
            if l < b["low"]:
                b["low"] = l
            b["close"] = c
            b["volume"] += v

    return [buckets[k] for k in sorted(buckets.keys())]


class RollingBarAggregator:
    """O(1) incremental aggregator from M1 candles into higher-timeframe bars."""

    def __init__(self, period_minutes: int, maxlen: int = 200):
        from collections import deque
        self.period_secs = period_minutes * 60
        self.bars: deque[dict] = deque(maxlen=maxlen)
        self._current_bucket: int | None = None
        self._current_bar: dict | None = None

    def update(self, candle: dict) -> bool:
        """Ingest one M1 candle. Returns True when a prior bucket just closed."""
        ts = candle["time"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        bucket = (int(ts.timestamp()) // self.period_secs) * self.period_secs
        o = float(candle["open"])
        h = float(candle["high"])
        l = float(candle["low"])
        c = float(candle["close"])
        v = float(candle.get("volume", 1.0) or 1.0)

        closed_prior = False
        if self._current_bucket is None or bucket != self._current_bucket:
            if self._current_bar is not None:
                self.bars.append(self._current_bar)
                closed_prior = True
            self._current_bucket = bucket
            self._current_bar = {
                "time": datetime.fromtimestamp(bucket, tz=timezone.utc),
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": v,
            }
        else:
            b = self._current_bar
            if h > b["high"]:
                b["high"] = h
            if l < b["low"]:
                b["low"] = l
            b["close"] = c
            b["volume"] += v
        return closed_prior

    def closed_closes(self) -> list[float]:
        return [b["close"] for b in self.bars]

    def all_bars(self) -> list[dict]:
        if self._current_bar is None:
            return list(self.bars)
        return list(self.bars) + [self._current_bar]


def _ema_series(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(alpha * float(v) + (1.0 - alpha) * out[-1])
    return out


def compute_htf_ema_state(
    closed_bar_closes: Sequence[float],
    fast: int = 20,
    slow: int = 50,
) -> tuple[int, float | None, float | None]:
    """
    Pure-Python directional trend filter on closed higher-timeframe bars.
    Returns (trend, ema_fast, ema_slow) where trend is +1 (EMA20 > EMA50),
    -1 (EMA20 < EMA50), or 0 (insufficient bars or equal).
    """
    if len(closed_bar_closes) < slow:
        return 0, None, None
    ema_fast = float(_ema_series(closed_bar_closes, fast)[-1])
    ema_slow = float(_ema_series(closed_bar_closes, slow)[-1])
    if ema_fast > ema_slow:
        return 1, ema_fast, ema_slow
    if ema_fast < ema_slow:
        return -1, ema_fast, ema_slow
    return 0, ema_fast, ema_slow


def compute_htf_ema_trend(closed_bar_closes: Sequence[float], fast: int = 20, slow: int = 50) -> int:
    """
    Pure-Python directional trend filter on closed higher-timeframe bars.
    Returns +1 (bullish: EMA20 > EMA50), -1 (bearish: EMA20 < EMA50), or 0 (insufficient/flat).
    """
    return compute_htf_ema_state(closed_bar_closes, fast=fast, slow=slow)[0]


def passes_regime_filters(
    direction: str,
    regime: str,
    block_high_volatility: bool = False,
    block_buy_sideways: bool = False,
) -> bool:
    """Configurable regime safety filters shared by live and backtest."""
    if block_high_volatility and regime == "HIGH_VOLATILITY":
        return False
    if block_buy_sideways and direction == "BUY" and regime == "SIDEWAYS":
        return False
    return True
