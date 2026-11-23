"""
Pure-Python Domain Service for GoldEdge Macro & Micro ML Label Specification (Task G3.2).

Strict Hexagonal Architecture (domain layer): ZERO pandas, numpy, sqlalchemy, or IO imports.
Reuses `BrokerCostService` (`FUSION_CTRADER_DEMO_48439552` + `FUSION_XAUUSD_SWAP_SPEC`)
so label cost economics match the verified application backtest engine identically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

from .broker_cost_service import (
    BrokerCostService,
    CommissionSchedule,
    FUSION_CTRADER_DEMO_48439552,
    FUSION_XAUUSD_SWAP_SPEC,
)

LABEL_SPEC_VERSION = "G3_2_LABEL_SPEC_V1"


@dataclass(frozen=True)
class ExecutionCostSpec:
    """Verified Fusion Markets cTrader demo execution cost parameters."""

    profile_name: str = "fusion_ctrader_demo_48439552"
    account_id: int = 48439552
    commission_usd_per_million_per_side: float = 12.0
    slippage_pips_per_side: float = 0.0533
    pip_size: float = 0.10
    contract_size: float = 100.0
    apply_swap: bool = True

    @property
    def slippage_usd_per_oz_per_side(self) -> float:
        return self.slippage_pips_per_side * self.pip_size

    @property
    def round_trip_slippage_usd_per_oz(self) -> float:
        return 2.0 * self.slippage_usd_per_oz_per_side

    def round_trip_commission_usd_per_oz(self, entry_px: float, exit_px: float) -> float:
        """Exact notional commission per ounce across entry + exit legs ($12/$1M/side)."""
        return (float(entry_px) + float(exit_px)) * (self.commission_usd_per_million_per_side / 1_000_000.0)

    def round_trip_non_spread_cost_usd_per_oz(self, entry_px: float, exit_px: float) -> float:
        """Commission + slippage per ounce (spread is already captured in executable bid/ask)."""
        return self.round_trip_commission_usd_per_oz(entry_px, exit_px) + self.round_trip_slippage_usd_per_oz


DEFAULT_COST_SPEC = ExecutionCostSpec()


def classify_macro_endpoint(
    entry_bid: float,
    entry_ask: float,
    exit_bid: float,
    exit_ask: float,
    threshold_pct: float = 0.0010,
    cost_spec: ExecutionCostSpec = DEFAULT_COST_SPEC,
) -> Dict[str, float | str]:
    """
    Compute BOTH:
      1. Raw executable directional endpoint returns & mutually exclusive class (`UP` / `DOWN` / `FLAT`):
         - BUY raw return:  (exit_bid - entry_ask) / entry_ask
         - SELL raw return: (entry_bid - exit_ask) / entry_bid
      2. Net-of-cost tradable endpoint returns & class (deducting verified Fusion Markets
         $12/$1M/side commission + 0.0533 pips/side slippage):
         - BUY net entry  = entry_ask + slip_side; BUY net exit = exit_bid - slip_side
         - SELL net entry = entry_bid - slip_side; SELL net exit = exit_ask + slip_side
         - Plus per-oz round-trip commission `(entry_exec + exit_exec) * 12e-6`.
    """
    if entry_bid <= 0.0 or entry_ask <= 0.0 or entry_bid >= entry_ask:
        raise ValueError("Invalid entry bid/ask quote")
    if exit_bid <= 0.0 or exit_ask <= 0.0 or exit_bid >= exit_ask:
        raise ValueError("Invalid exit bid/ask quote")

    buy_ret_raw = (exit_bid - entry_ask) / entry_ask
    sell_ret_raw = (entry_bid - exit_ask) / entry_bid

    if buy_ret_raw >= threshold_pct:
        raw_class = "UP"
    elif sell_ret_raw >= threshold_pct:
        raw_class = "DOWN"
    else:
        raw_class = "FLAT"

    slip = cost_spec.slippage_usd_per_oz_per_side
    buy_entry_eff = entry_ask + slip
    buy_exit_eff = exit_bid - slip
    buy_comm_oz = cost_spec.round_trip_commission_usd_per_oz(buy_entry_eff, buy_exit_eff)
    buy_ret_net = (buy_exit_eff - buy_entry_eff - buy_comm_oz) / buy_entry_eff

    sell_entry_eff = entry_bid - slip
    sell_exit_eff = exit_ask + slip
    sell_comm_oz = cost_spec.round_trip_commission_usd_per_oz(sell_entry_eff, sell_exit_eff)
    sell_ret_net = (sell_entry_eff - sell_exit_eff - sell_comm_oz) / sell_entry_eff

    if buy_ret_net >= threshold_pct:
        net_class = "UP"
    elif sell_ret_net >= threshold_pct:
        net_class = "DOWN"
    else:
        net_class = "FLAT"

    return {
        "buy_ret_raw": buy_ret_raw,
        "sell_ret_raw": sell_ret_raw,
        "raw_class": raw_class,
        "buy_ret_net": buy_ret_net,
        "sell_ret_net": sell_ret_net,
        "net_class": net_class,
        "buy_net_profitable": float(buy_ret_net > 0.0),
        "sell_net_profitable": float(sell_ret_net > 0.0),
    }


def evaluate_tick_barrier_path(
    direction: str,
    entry_exec_px: float,
    entry_ts_s: float,
    tick_times_s: Sequence[float],
    tick_bids: Sequence[float],
    tick_asks: Sequence[float],
    tp_dist_usd: float,
    sl_dist_usd: float,
    max_gap_sec: float = 300.0,
) -> Dict[str, Optional[float] | str | bool]:
    """
    Strictly chronological tick-by-tick barrier evaluation.
    - BUY enters at ask; evaluates TP/SL/MFE/MAE strictly against subsequent executable `bid`.
    - SELL enters at bid; evaluates TP/SL/MFE/MAE strictly against subsequent executable `ask`.
    - Conservative tie-break if SL and TP were somehow equal (impossible when both > 0): SL checked first.
    """
    is_buy = str(direction).upper() == "BUY"
    tp_px = entry_exec_px + tp_dist_usd if is_buy else entry_exec_px - tp_dist_usd
    sl_px = entry_exec_px - sl_dist_usd if is_buy else entry_exec_px + sl_dist_usd

    tp_hit = False
    sl_hit = False
    tp_ts: Optional[float] = None
    sl_ts: Optional[float] = None
    first_outcome = "NEITHER"
    first_ts: Optional[float] = None
    first_fill_px: Optional[float] = None

    mfe = 0.0
    mae = 0.0
    prev_t = entry_ts_s
    gap_interrupted = False

    for t, b, a in zip(tick_times_s, tick_bids, tick_asks):
        if b <= 0.0 or a <= 0.0 or b >= a:
            continue
        if (t - prev_t) >= max_gap_sec:
            gap_interrupted = True
        prev_t = t

        eval_px = b if is_buy else a
        fav = (eval_px - entry_exec_px) if is_buy else (entry_exec_px - eval_px)
        adv = -fav
        if fav > mfe:
            mfe = fav
        if adv > mae:
            mae = adv

        hit_sl_now = (eval_px <= sl_px) if is_buy else (eval_px >= sl_px)
        hit_tp_now = (eval_px >= tp_px) if is_buy else (eval_px <= tp_px)

        if hit_sl_now and not sl_hit:
            sl_hit = True
            sl_ts = float(t)
            if first_outcome == "NEITHER":
                first_outcome = "SL_FIRST"
                first_ts = float(t)
                first_fill_px = float(eval_px)

        if hit_tp_now and not tp_hit:
            tp_hit = True
            tp_ts = float(t)
            if first_outcome == "NEITHER":
                first_outcome = "TP_FIRST"
                first_ts = float(t)
                first_fill_px = float(eval_px)

    return {
        "first_outcome": first_outcome,
        "tp_reached": tp_hit,
        "sl_reached": sl_hit,
        "both_reached": bool(tp_hit and sl_hit),
        "first_touch_ts_s": first_ts,
        "first_fill_px": first_fill_px,
        "time_to_tp_s": (tp_ts - entry_ts_s) if tp_ts is not None else None,
        "time_to_sl_s": (sl_ts - entry_ts_s) if sl_ts is not None else None,
        "mfe_usd": mfe,
        "mae_usd": mae,
        "gap_interrupted": gap_interrupted,
    }


def classify_micro_30tick(
    entry_bid: float,
    entry_ask: float,
    fut_bid: float,
    fut_ask: float,
    extra_buffer_usd: float = 0.0,
    cost_spec: ExecutionCostSpec = DEFAULT_COST_SPEC,
) -> Dict[str, float | str]:
    """
    Classify 30-tick directional quote outcome net of executable bid/ask spread,
    verified Fusion Markets $12/$1M/side commission, 0.0533p/side slippage,
    plus an optional extra neutral-band buffer (USD/oz).
    """
    if entry_bid <= 0.0 or entry_ask <= 0.0 or entry_bid >= entry_ask:
        raise ValueError("Invalid entry bid/ask quote")
    if fut_bid <= 0.0 or fut_ask <= 0.0 or fut_bid >= fut_ask:
        raise ValueError("Invalid future bid/ask quote")

    entry_mid = 0.5 * (entry_bid + entry_ask)
    fut_mid = 0.5 * (fut_bid + fut_ask)
    mid_delta_usd = fut_mid - entry_mid

    # Raw executable post-spread move (before commission & slippage)
    buy_exec_raw_usd = fut_bid - entry_ask
    sell_exec_raw_usd = entry_bid - fut_ask

    # Net-of-all-costs move (spread + verified commission + slippage)
    slip = cost_spec.slippage_usd_per_oz_per_side
    buy_comm = cost_spec.round_trip_commission_usd_per_oz(entry_ask + slip, fut_bid - slip)
    sell_comm = cost_spec.round_trip_commission_usd_per_oz(entry_bid - slip, fut_ask + slip)
    buy_net_usd = (fut_bid - slip) - (entry_ask + slip) - buy_comm
    sell_net_usd = (entry_bid - slip) - (fut_ask + slip) - sell_comm

    if buy_net_usd > extra_buffer_usd:
        net_class = "UP"
    elif sell_net_usd > extra_buffer_usd:
        net_class = "DOWN"
    else:
        net_class = "FLAT"

    if buy_exec_raw_usd > extra_buffer_usd:
        raw_exec_class = "UP"
    elif sell_exec_raw_usd > extra_buffer_usd:
        raw_exec_class = "DOWN"
    else:
        raw_exec_class = "FLAT"

    return {
        "mid_delta_usd": mid_delta_usd,
        "buy_exec_raw_usd": buy_exec_raw_usd,
        "sell_exec_raw_usd": sell_exec_raw_usd,
        "buy_net_usd": buy_net_usd,
        "sell_net_usd": sell_net_usd,
        "buy_ret_net": buy_net_usd / (entry_ask + slip),
        "sell_ret_net": sell_net_usd / (entry_bid - slip),
        "raw_exec_class": raw_exec_class,
        "net_class": net_class,
    }


def classify_micro_entry_timing(
    entry_bid: float,
    entry_ask: float,
    fut_bid: float,
    fut_ask: float,
    neutral_band_spread_mult: float = 1.0,
    fixed_neutral_band_usd: float = 0.10,
    cost_spec: ExecutionCostSpec = DEFAULT_COST_SPEC,
) -> Dict[str, float | str]:
    """
    Entry-timing & short-term quote-movement Micro labels (Task G3.2 corrective pass).
    Strictly distinguishes H3b entry-timing confirmation from a standalone 8s round-trip scalp:

    1. Short-term quote movement (no duplicate round-trip commission):
       - `mid_delta_usd = fut_mid - entry_mid`
       - `quote_move_class_1x_spread`: UP / DOWN / FLAT using +/- (neutral_band_spread_mult * entry_spread)
       - `quote_move_class_010`:       UP / DOWN / FLAT using +/- fixed_neutral_band_usd ($0.10)

    2. Ex-post training target for an ex-ante entry-timing decision made at t_0
       (BEFORE observing ticks t_1..t_30; a trained model predicts this from causal features <= t_0):
       - BUY pays ask * (1 + c_side):
         `buy_timing_adv_usd = Cost(wait) - Cost(now) = (fut_ask - entry_ask) * (1 + c_side)`
         (> 0 means price rose over the next 30 ticks, so entering immediately at t_0 saved money;
          < 0 means immediate adverse dip, so waiting 30 ticks yielded a better BUY entry)
       - SELL receives bid * (1 - c_side):
         `sell_timing_adv_usd = Proceeds(now) - Proceeds(wait) = (entry_bid - fut_bid) * (1 - c_side)`
         (> 0 means price fell over the next 30 ticks, so entering immediately at t_0 earned more;
          < 0 means immediate adverse bounce, so waiting 30 ticks yielded a better SELL entry)
       - Classified into `ENTER_NOW` / `WAIT_ADVERSE` / `NEUTRAL` using +/- (neutral_band_spread_mult * entry_spread).
    """
    if entry_bid <= 0.0 or entry_ask <= 0.0 or entry_bid >= entry_ask:
        raise ValueError("Invalid entry bid/ask quote")
    if fut_bid <= 0.0 or fut_ask <= 0.0 or fut_bid >= fut_ask:
        raise ValueError("Invalid future bid/ask quote")

    entry_spread = entry_ask - entry_bid
    band_sp = neutral_band_spread_mult * entry_spread
    entry_mid = 0.5 * (entry_bid + entry_ask)
    fut_mid = 0.5 * (fut_bid + fut_ask)
    mid_delta = fut_mid - entry_mid

    q_sp_cls = "UP" if mid_delta > band_sp else ("DOWN" if mid_delta < -band_sp else "FLAT")
    q_010_cls = "UP" if mid_delta > fixed_neutral_band_usd else ("DOWN" if mid_delta < -fixed_neutral_band_usd else "FLAT")

    # Incremental entry-leg notional commission delta (entry_now vs entry_later; exit leg is identical)
    c_side = cost_spec.commission_usd_per_million_per_side / 1_000_000.0
    buy_adv = (fut_ask - entry_ask) * (1.0 + c_side)
    sell_adv = (entry_bid - fut_bid) * (1.0 - c_side)

    buy_timing_cls = "ENTER_NOW" if buy_adv > band_sp else ("WAIT_ADVERSE" if buy_adv < -band_sp else "NEUTRAL")
    sell_timing_cls = "ENTER_NOW" if sell_adv > band_sp else ("WAIT_ADVERSE" if sell_adv < -band_sp else "NEUTRAL")

    return {
        "mid_delta_usd": mid_delta,
        "quote_move_class_1x_spread": q_sp_cls,
        "quote_move_class_010": q_010_cls,
        "buy_timing_adv_usd": buy_adv,
        "sell_timing_adv_usd": sell_adv,
        "buy_timing_class": buy_timing_cls,
        "sell_timing_class": sell_timing_cls,
    }


def verify_broker_cost_schedule_parity(
    lots: float,
    entry_px: float,
    exit_px: float,
    cost_spec: ExecutionCostSpec = DEFAULT_COST_SPEC,
) -> Tuple[float, float, float]:
    """Cross-check ExecutionCostSpec against BrokerCostService (FUSION_CTRADER_DEMO_48439552)."""
    svc = BrokerCostService(FUSION_CTRADER_DEMO_48439552, FUSION_XAUUSD_SWAP_SPEC)
    c_entry = svc.calculate_leg_commission(lots, entry_px)
    c_exit = svc.calculate_leg_commission(lots, exit_px)
    total_svc = round(c_entry + c_exit, 2)
    total_spec = round(cost_spec.round_trip_commission_usd_per_oz(entry_px, exit_px) * lots * cost_spec.contract_size, 2)
    return total_svc, total_spec, abs(total_svc - total_spec)
