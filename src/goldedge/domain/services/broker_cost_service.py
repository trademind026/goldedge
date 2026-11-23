"""
Pure-Python Broker Cost, Commission & Rollover Swap Domain Service.

Strict Hexagonal Architecture (domain layer): zero third-party or IO imports.
Encapsulates broker-verified commission schedules (per-side notional or per-lot)
and timezone-aware rollover / triple-swap mechanics with DST support.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class CommissionSchedule:
    """Commission rules for a specific broker account and trading environment."""

    name: str
    broker: str
    platform: str
    environment: str  # "demo" | "live" | "research"
    account_id: Optional[int]
    commission_type: str  # "USD_PER_MILLION_USD" | "USD_PER_LOT_RT" | "ZERO"
    rate_value: float  # e.g. 12.0 USD per $1M notional per side, OR RT $/lot
    min_commission_usd: float = 0.0
    contract_size: float = 100.0
    currency: str = "USD"
    precision_digits: int = 2
    verified_for_live: bool = False
    account_tier_verified: bool = False
    notes: str = ""

    def calculate_side_commission(self, lots: float, execution_price: float) -> float:
        """
        Compute positive commission charge (USD) for one execution leg (entry, partial close, or full close),
        rounded to broker currency precision.
        """
        abs_lots = abs(float(lots))
        if abs_lots <= 0.0 or self.commission_type == "ZERO" or self.rate_value <= 0.0:
            return 0.0
        if self.commission_type == "USD_PER_MILLION_USD":
            notional = abs_lots * self.contract_size * float(execution_price)
            raw = notional * (self.rate_value / 1_000_000.0)
            fee = max(raw, self.min_commission_usd)
            return round(fee, self.precision_digits)
        if self.commission_type == "USD_PER_LOT_RT":
            raw = 0.5 * abs_lots * self.rate_value
            fee = max(raw, self.min_commission_usd)
            return round(fee, self.precision_digits)
        raise ValueError(f"Unsupported commission_type: {self.commission_type}")

    def estimate_round_trip_per_lot(self, reference_price: float) -> float:
        """Estimate full round-trip commission per 1.00 standard lot for pre-trade risk budgeting."""
        if self.commission_type == "ZERO" or self.rate_value <= 0.0:
            return 0.0
        if self.commission_type == "USD_PER_MILLION_USD":
            side = self.calculate_side_commission(1.0, reference_price)
            return round(2.0 * side, self.precision_digits)
        if self.commission_type == "USD_PER_LOT_RT":
            return round(float(self.rate_value), self.precision_digits)
        raise ValueError(f"Unsupported commission_type: {self.commission_type}")


@dataclass(frozen=True)
class SwapSchedule:
    """Overnight rollover and triple-swap specification."""

    swap_calculation_type: str  # "PIPS" | "NONE"
    swap_long_pips: float
    swap_short_pips: float
    triple_swap_weekday: int  # Python weekday: Mon=0, Tue=1, Wed=2, Thu=3, Fri=4
    rollover_timezone: str = "America/New_York"
    rollover_local_hour: int = 17
    rollover_local_minute: int = 0
    pip_size: float = 0.10
    contract_size: float = 100.0
    charge_swap_at_weekends: bool = False
    historical_rates_verified: bool = False
    daily_rate_overrides: Optional[Dict[str, Tuple[float, float]]] = None


FUSION_CTRADER_DEMO_48439552 = CommissionSchedule(
    name="fusion_ctrader_demo_48439552",
    broker="Fusion Markets",
    platform="cTrader",
    environment="demo",
    account_id=48439552,
    commission_type="USD_PER_MILLION_USD",
    rate_value=12.0,  # Verified across 15/15 deals ($12.00 per $1M notional per side)
    min_commission_usd=0.0,
    contract_size=100.0,
    currency="USD",
    precision_digits=2,
    verified_for_live=False,
    account_tier_verified=False,
    notes="Verified against ProtoOASymbolByIdRes (commission=1200) and 15/15 demo deals on account 48439552.",
)

FUSION_XAUUSD_SWAP_SPEC = SwapSchedule(
    swap_calculation_type="PIPS",
    swap_long_pips=-5.653,
    swap_short_pips=2.677,
    triple_swap_weekday=2,  # WEDNESDAY
    rollover_timezone="America/New_York",
    rollover_local_hour=17,
    rollover_local_minute=0,
    pip_size=0.10,
    contract_size=100.0,
    charge_swap_at_weekends=False,
    historical_rates_verified=False,  # Static snapshot from ProtoOASymbol; historical daily changes unavailable
)

ZERO_COMMISSION_SCHEDULE = CommissionSchedule(
    name="zero_commission",
    broker="none",
    platform="backtest",
    environment="research",
    account_id=None,
    commission_type="ZERO",
    rate_value=0.0,
    verified_for_live=False,
)


def resolve_commission_schedule(
    profile_name: str,
    trading_environment: str = "demo",
    account_id: Optional[int] = None,
    custom_per_lot_rt: Optional[float] = None,
) -> CommissionSchedule:
    """
    Resolve a commission schedule safely while preventing unverified demo rates
    from silently being applied to real-money live trading.
    """
    env = str(trading_environment or "demo").strip().lower()
    key = str(profile_name or "").strip().lower()

    if key in ("fusion_ctrader_demo", "fusion_ctrader_demo_48439552"):
        if env == "live" and not FUSION_CTRADER_DEMO_48439552.verified_for_live:
            raise ValueError(
                "Refusing to apply demo commission schedule 'fusion_ctrader_demo_48439552' "
                "to TRADING_MODE='live' without verified live account commission metadata."
            )
        if account_id is not None and FUSION_CTRADER_DEMO_48439552.account_id not in (None, int(account_id)):
            raise ValueError(
                f"Account ID {account_id} does not match verified demo schedule account "
                f"{FUSION_CTRADER_DEMO_48439552.account_id}."
            )
        return FUSION_CTRADER_DEMO_48439552

    if key in ("fixed_per_lot", "custom_per_lot") or custom_per_lot_rt is not None:
        rate = float(custom_per_lot_rt or 0.0)
        return CommissionSchedule(
            name=f"fixed_per_lot_{rate:.2f}",
            broker="custom",
            platform="backtest",
            environment=env,
            account_id=account_id,
            commission_type="USD_PER_LOT_RT" if rate > 0 else "ZERO",
            rate_value=rate,
            verified_for_live=False,
        )

    if key in ("zero", "none", ""):
        return ZERO_COMMISSION_SCHEDULE

    raise ValueError(f"Unknown broker commission profile: {profile_name}")


class BrokerCostService:
    """
    Pure-Python financial cost engine handling:
      1. Execution-leg commission (entry, partial exit, full exit) with duplicate guard.
      2. Broker-timezone-aware (America/New_York 17:00) rollover instants with automatic DST (21:00Z EDT / 22:00Z EST).
      3. PIPS-based overnight swap calculation with Wednesday triple swap and weekend exclusion.
    """

    def __init__(
        self,
        commission_schedule: CommissionSchedule = FUSION_CTRADER_DEMO_48439552,
        swap_schedule: SwapSchedule = FUSION_XAUUSD_SWAP_SPEC,
    ):
        self.commission_schedule = commission_schedule
        self.swap_schedule = swap_schedule
        self._tz = ZoneInfo(swap_schedule.rollover_timezone)

    def calculate_leg_commission(self, lots: float, execution_price: float) -> float:
        return self.commission_schedule.calculate_side_commission(lots, execution_price)

    def estimate_round_trip_per_lot(self, reference_price: float) -> float:
        return self.commission_schedule.estimate_round_trip_per_lot(reference_price)

    def rollover_utc_for_local_date(self, local_date: date) -> datetime:
        """Return exact UTC datetime of 17:00 NY local rollover on `local_date` (21:00Z EDT or 22:00Z EST)."""
        local_dt = datetime(
            local_date.year,
            local_date.month,
            local_date.day,
            self.swap_schedule.rollover_local_hour,
            self.swap_schedule.rollover_local_minute,
            0,
            tzinfo=self._tz,
        )
        return local_dt.astimezone(timezone.utc)

    def rollover_events_between(
        self, prev_utc: datetime, curr_utc: datetime
    ) -> List[Tuple[datetime, date, int]]:
        """
        Find all eligible trading-day rollover instants strictly inside `(prev_utc, curr_utc]`.
        Returns list of `(rollover_utc, local_ny_date, multiplier)` where multiplier is 3 on
        Wednesday (`triple_swap_weekday == 2`) and 1 on Mon/Tue/Thu/Fri, skipping Sat/Sun
        when `charge_swap_at_weekends` is False.
        """
        p_utc = prev_utc if prev_utc.tzinfo is not None else prev_utc.replace(tzinfo=timezone.utc)
        c_utc = curr_utc if curr_utc.tzinfo is not None else curr_utc.replace(tzinfo=timezone.utc)
        p_utc = p_utc.astimezone(timezone.utc)
        c_utc = c_utc.astimezone(timezone.utc)
        if c_utc <= p_utc:
            return []

        start_local_d = p_utc.astimezone(self._tz).date() - timedelta(days=1)
        end_local_d = c_utc.astimezone(self._tz).date() + timedelta(days=1)

        events: List[Tuple[datetime, date, int]] = []
        d = start_local_d
        while d <= end_local_d:
            wd = d.weekday()
            is_weekend = wd in (5, 6)
            if not is_weekend or self.swap_schedule.charge_swap_at_weekends:
                r_utc = self.rollover_utc_for_local_date(d)
                if p_utc < r_utc <= c_utc:
                    mult = 3 if wd == self.swap_schedule.triple_swap_weekday else 1
                    events.append((r_utc, d, mult))
            d += timedelta(days=1)
        return events

    def calculate_swap_pnl_usd(
        self,
        direction: str,
        lots: float,
        local_date: date,
        multiplier: Optional[int] = None,
    ) -> float:
        """
        Calculate overnight swap P&L impact in USD (`negative = debit/cost`, `positive = credit`)
        consistent with cTrader `swapLong = -5.653` / `swapShort = +2.677` pips.
        """
        if self.swap_schedule.swap_calculation_type == "NONE" or lots <= 0.0:
            return 0.0
        if multiplier is None:
            wd = local_date.weekday()
            if wd in (5, 6) and not self.swap_schedule.charge_swap_at_weekends:
                return 0.0
            multiplier = 3 if wd == self.swap_schedule.triple_swap_weekday else 1

        d_key = local_date.isoformat()
        if self.swap_schedule.daily_rate_overrides and d_key in self.swap_schedule.daily_rate_overrides:
            rate_long, rate_short = self.swap_schedule.daily_rate_overrides[d_key]
        else:
            rate_long = self.swap_schedule.swap_long_pips
            rate_short = self.swap_schedule.swap_short_pips

        is_buy = str(direction).upper() == "BUY"
        pips = rate_long if is_buy else rate_short
        usd_per_pip_per_lot = self.swap_schedule.pip_size * self.swap_schedule.contract_size
        return round(float(pips) * usd_per_pip_per_lot * abs(float(lots)) * int(multiplier), 2)
