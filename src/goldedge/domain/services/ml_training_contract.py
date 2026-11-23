"""
Pure-Python Domain Contract for Leakage-Proof ML Training, Purged Walk-Forward CV,
and Exact Decimal Discrete-Tick Arithmetic (Task G3.3).

Strict Hexagonal Architecture (domain layer): ZERO pandas, numpy, sklearn, sqlalchemy, or IO imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from .ml_feature_contract import EXCLUDED_UNAVAILABLE_ML_FEATURES

FEATURE_SCHEMA_VERSION = "G3_3_FEATURE_SCHEMA_V1"
MACRO_PURGE_BASELINE_SEC = 900.0     # 15m nominal horizon (plus exact event_end intersection check)
MACRO_EMBARGO_BASELINE_SEC = 3600.0  # 60m embargo (12x 5m bars)
MICRO_EMBARGO_BASELINE_SEC = 300.0   # 300s (5m) embargo around variable 30-tick horizons

# Strict leakage blacklist: target & future-derived columns that may NEVER enter X
FORBIDDEN_TARGET_AND_LEAKAGE_COLUMNS: FrozenSet[str] = frozenset(
    {
        # Macro future quotes, returns & endpoint labels
        "exit_bid_15m",
        "exit_ask_15m",
        "buy_ret_raw",
        "sell_ret_raw",
        "buy_ret_net",
        "sell_ret_net",
        "raw_class_005",
        "net_class_005",
        "raw_class_010",
        "net_class_010",
        "raw_class_015",
        "net_class_015",
        "raw_class_020",
        "net_class_020",
        # Macro future barriers, MFE/MAE & H3b outcomes
        "buy_barrier_010",
        "buy_both_reached_010",
        "buy_first_touch_utc",
        "buy_time_to_tp_010_s",
        "buy_time_to_sl_010_s",
        "buy_mfe_usd",
        "buy_mae_usd",
        "sell_barrier_010",
        "sell_both_reached_010",
        "sell_first_touch_utc",
        "sell_time_to_tp_010_s",
        "sell_time_to_sl_010_s",
        "sell_mfe_usd",
        "sell_mae_usd",
        "h3b_sim_buy_exit",
        "h3b_sim_buy_net_oz",
        "h3b_sim_buy_r",
        "h3b_sim_sell_exit",
        "h3b_sim_sell_net_oz",
        "h3b_sim_sell_r",
        "post_entry_micro_30t_class",
        # Micro future quotes, moves, standalone RT & entry-timing targets
        "fut_bid_30t",
        "fut_ask_30t",
        "fut_mid_30t",
        "mid_delta_usd",
        "buy_exec_raw_usd",
        "sell_exec_raw_usd",
        "buy_net_usd",
        "sell_net_usd",
        "raw_exec_class",
        "net_class",
        "net_class_buf010",
        "quote_move_class_1x_spread",
        "quote_move_class_010",
        "buy_timing_adv_usd",
        "sell_timing_adv_usd",
        "buy_timing_class",
        "sell_timing_class",
        "elapsed_sec",
        "quote_intensity_tps",
        "net_class_5s",
        "net_class_10s",
        "net_class_15s",
        "ticks_in_5s",
        "ticks_in_10s",
        "ticks_in_15s",
        "label_obs_end_utc",
    }
) | EXCLUDED_UNAVAILABLE_ML_FEATURES

CAUSAL_MACRO_FEATURE_COLUMNS: Tuple[str, ...] = (
    "ema20_ema50_spread_atr",
    "ema20_slope_atr",
    "h1_ema_trend_sign",
    "rsi_14_5m",
    "atr_14_pct",
    "atr_ratio_20",
    "adx_14_5m",
    "bb_width_20",
    "bb_pos_20",
    "kaufman_er_20",
    "prior_ret_5m",
    "prior_ret_15m",
    "prior_ret_60m",
    "prior_realized_vol_12",
    "dist_swing_low_atr",
    "dist_swing_high_atr",
    "tick_rate_ratio",
    "entry_spread_bps",
    "hour_sin",
    "hour_cos",
    "regime_code",
)

CAUSAL_MICRO_FEATURE_COLUMNS: Tuple[str, ...] = (
    "entry_spread_usd",
    "entry_spread_bps",
    "short_term_vol_30t_usd",
    "prior_mid_ret_5t",
    "prior_mid_ret_10t",
    "prior_mid_ret_30t",
    "prior_interarrival_mean_10t",
    "prior_quote_intensity_30t",
    "hour_sin",
    "hour_cos",
)


def assert_zero_leakage_columns(feature_cols: Iterable[str]) -> List[str]:
    """
    Strictly reject any target, future-derived, or unavailable order-flow column from X.
    Raises ValueError on any violation.
    """
    cols = [str(c).strip() for c in feature_cols]
    leaked = sorted({c for c in cols if c.lower() in FORBIDDEN_TARGET_AND_LEAKAGE_COLUMNS})
    if leaked:
        raise ValueError(f"LEAKAGE_BLOCKED: Forbidden target/future/unavailable columns in X: {leaked}")
    # Also block any column prefixed with future/target/label patterns
    for c in cols:
        cl = c.lower()
        if cl.startswith(("fut_", "future_", "target_", "label_obs_end", "h3b_sim_")):
            raise ValueError(f"LEAKAGE_BLOCKED: Future-prefixed column in X: {c}")
    return cols


def assert_valid_timestamp_contract(
    feature_cutoff_utc: float,
    decision_ts_utc: float,
    earliest_entry_utc: float,
    label_obs_start_utc: float,
    label_obs_end_utc: float,
) -> None:
    """Enforce strict causal timestamp ordering for every single row."""
    if feature_cutoff_utc > decision_ts_utc + 1e-6:
        raise ValueError(f"FUTURE_FEATURE_TIMESTAMP: feature_cutoff ({feature_cutoff_utc}) > decision ({decision_ts_utc})")
    if earliest_entry_utc < decision_ts_utc - 1e-6:
        raise ValueError(f"ENTRY_BEFORE_DECISION: entry ({earliest_entry_utc}) < decision ({decision_ts_utc})")
    if label_obs_start_utc < earliest_entry_utc - 1e-6:
        raise ValueError(f"OBS_START_BEFORE_ENTRY: start ({label_obs_start_utc}) < entry ({earliest_entry_utc})")
    if label_obs_end_utc <= label_obs_start_utc:
        raise ValueError(f"NONPOSITIVE_LABEL_HORIZON: end ({label_obs_end_utc}) <= start ({label_obs_start_utc})")


@dataclass(frozen=True)
class WalkForwardFoldSpec:
    """Aligned chronological date boundaries shared by Macro and Micro pipelines."""

    fold_id: int
    train_start_utc: str
    train_end_utc: str
    val_start_utc: str
    val_end_utc: str
    cal_start_utc: str
    cal_end_utc: str
    test_start_utc: str
    test_end_utc: str


def assign_purged_walkforward_indices(
    event_starts: Sequence[float],
    event_ends: Sequence[float],
    train_start_s: float,
    eval_start_s: float,
    eval_end_s: float,
    embargo_sec: float,
) -> Dict[str, List[int] | int]:
    """
    Event-interval-exact Purged & Embargoed Chronological Split (Phase C).
    Uses actual per-row `[event_start, event_end]` timestamps rather than fixed row offsets:
      - Evaluation set: `eval_start_s <= event_start < eval_end_s`
      - Training set:   `train_start_s <= event_start < eval_start_s`
        EXCLUDING any sample where:
          1. `event_end >= eval_start_s` (PURGED: future outcome intersects evaluation window!)
          2. `event_start >= eval_start_s - embargo_sec` (EMBARGOED: within dependency buffer!)
    """
    if len(event_starts) != len(event_ends):
        raise ValueError("event_starts and event_ends must have identical length")
    if eval_start_s <= train_start_s or eval_end_s <= eval_start_s:
        raise ValueError("Invalid chronological fold boundaries")

    train_idx: List[int] = []
    eval_idx: List[int] = []
    purged_cnt = 0
    embargoed_cnt = 0

    embargo_cutoff = eval_start_s - float(embargo_sec)
    for i, (s, e) in enumerate(zip(event_starts, event_ends)):
        if e <= s:
            raise ValueError(f"Invalid event interval at idx={i}: start={s}, end={e}")
        if eval_start_s <= s < eval_end_s:
            eval_idx.append(i)
        elif train_start_s <= s < eval_start_s:
            if e >= eval_start_s:
                purged_cnt += 1
            elif s >= embargo_cutoff:
                embargoed_cnt += 1
            else:
                train_idx.append(i)

    # Hard invariant: max(train_event_end) < eval_start_s
    if train_idx:
        max_tr_end = max(event_ends[i] for i in train_idx)
        if max_tr_end >= eval_start_s:
            raise AssertionError(f"PURGE_INVARIANT_VIOLATION: max_tr_end={max_tr_end} >= eval_start={eval_start_s}")

    return {
        "train_indices": train_idx,
        "eval_indices": eval_idx,
        "purged_count": purged_cnt,
        "embargoed_count": embargoed_cnt,
    }


def assign_purged_4way_walkforward_fold(
    event_starts: Sequence[float],
    event_ends: Sequence[float],
    train_start_s: float,
    val_start_s: float,
    cal_start_s: float,
    test_start_s: float,
    test_end_s: float,
    embargo_sec: float,
) -> Dict[str, Any]:
    """
    Event-interval-exact 4-way Chronological Purged & Embargoed Walk-Forward Fold.
    Enforces strict event-end purging AND embargo across ALL THREE adjacent split boundaries:
      1. Train -> Inner Validation (`max(train_end) < val_start`, `max(train_start) < val_start - embargo`)
      2. Inner Validation -> Calibration (`max(val_end) < cal_start`, `max(val_start) < cal_start - embargo`)
      3. Calibration -> Out-of-Time Test (`max(cal_end) < test_start`, `max(cal_start) < test_start - embargo`)
    """
    if len(event_starts) != len(event_ends):
        raise ValueError("event_starts and event_ends must have identical length")
    if not (train_start_s < val_start_s < cal_start_s < test_start_s < test_end_s):
        raise ValueError("4-way fold boundaries must be strictly increasing")

    emb = float(embargo_sec)
    train_idx: List[int] = []
    val_idx: List[int] = []
    cal_idx: List[int] = []
    test_idx: List[int] = []
    counts = {
        "train_to_val_purged": 0,
        "train_to_val_embargoed": 0,
        "val_to_cal_purged": 0,
        "val_to_cal_embargoed": 0,
        "cal_to_test_purged": 0,
        "cal_to_test_embargoed": 0,
    }

    for i, (s, e) in enumerate(zip(event_starts, event_ends)):
        if e <= s:
            raise ValueError(f"Invalid event interval at idx={i}: start={s}, end={e}")
        if train_start_s <= s < val_start_s:
            if e >= val_start_s:
                counts["train_to_val_purged"] += 1
            elif s >= val_start_s - emb:
                counts["train_to_val_embargoed"] += 1
            else:
                train_idx.append(i)
        elif val_start_s <= s < cal_start_s:
            if e >= cal_start_s:
                counts["val_to_cal_purged"] += 1
            elif s >= cal_start_s - emb:
                counts["val_to_cal_embargoed"] += 1
            else:
                val_idx.append(i)
        elif cal_start_s <= s < test_start_s:
            if e >= test_start_s:
                counts["cal_to_test_purged"] += 1
            elif s >= test_start_s - emb:
                counts["cal_to_test_embargoed"] += 1
            else:
                cal_idx.append(i)
        elif test_start_s <= s < test_end_s:
            test_idx.append(i)

    # Hard assertions across all 3 adjacent boundaries
    max_tr_end = max((event_ends[i] for i in train_idx), default=None)
    max_val_end = max((event_ends[i] for i in val_idx), default=None)
    max_cal_end = max((event_ends[i] for i in cal_idx), default=None)

    if max_tr_end is not None and max_tr_end >= val_start_s:
        raise AssertionError(f"TRAIN_TO_VAL_LEAK: max_tr_end={max_tr_end} >= val_start={val_start_s}")
    if max_val_end is not None and max_val_end >= cal_start_s:
        raise AssertionError(f"VAL_TO_CAL_LEAK: max_val_end={max_val_end} >= cal_start={cal_start_s}")
    if max_cal_end is not None and max_cal_end >= test_start_s:
        raise AssertionError(f"CAL_TO_TEST_LEAK: max_cal_end={max_cal_end} >= test_start={test_start_s}")

    return {
        "train_indices": train_idx,
        "val_indices": val_idx,
        "cal_indices": cal_idx,
        "test_indices": test_idx,
        "boundary_counts": counts,
        "max_train_end": max_tr_end,
        "max_val_end": max_val_end,
        "max_cal_end": max_cal_end,
        "all_3_boundaries_strict_purge_hold": True,
    }


def classify_micro_timing_exact_decimal(
    entry_bid: float | str | Decimal,
    entry_ask: float | str | Decimal,
    fut_bid: float | str | Decimal,
    fut_ask: float | str | Decimal,
    policy: str = "strict_gt",
    comm_rate: Decimal = Decimal("0.000012"),
    tick_size: Decimal = Decimal("0.01"),
) -> Dict[str, str | bool | Decimal]:
    """
    Phase E: Exact Decimal arithmetic Micro entry-timing classifier.
    Eliminates binary floating-point rounding drift at discrete $0.01 broker tick boundaries.
    Supported policies:
      - `strict_gt`:        `adv > entry_spread` (frozen G3.2 policy)
      - `inclusive_ge`:     `adv >= entry_spread`
      - `half_tick_buffer`: `adv > entry_spread + 0.5 * tick_size` (+$0.005)
      - `raw_half_tick`:    `raw_price_diff > entry_spread + 0.5 * tick_size` (commission-free tie check)
    """
    eb = Decimal(str(entry_bid)).quantize(tick_size, rounding=ROUND_HALF_UP)
    ea = Decimal(str(entry_ask)).quantize(tick_size, rounding=ROUND_HALF_UP)
    fb = Decimal(str(fut_bid)).quantize(tick_size, rounding=ROUND_HALF_UP)
    fa = Decimal(str(fut_ask)).quantize(tick_size, rounding=ROUND_HALF_UP)

    if eb <= 0 or ea <= 0 or eb >= ea:
        raise ValueError("Invalid entry bid/ask quote")
    if fb <= 0 or fa <= 0 or fb >= fa:
        raise ValueError("Invalid future bid/ask quote")

    esp = ea - eb
    half_tick = tick_size / Decimal("2")
    raw_buy = fa - ea
    raw_sell = eb - fb
    buy_adv = raw_buy * (Decimal("1") + comm_rate)
    sell_adv = raw_sell * (Decimal("1") - comm_rate)

    buy_exact_tie_raw = abs(raw_buy) == esp
    sell_exact_tie_raw = abs(raw_sell) == esp

    if policy == "strict_gt":
        b_cls = "ENTER_NOW" if buy_adv > esp else ("WAIT_ADVERSE" if buy_adv < -esp else "NEUTRAL")
        s_cls = "ENTER_NOW" if sell_adv > esp else ("WAIT_ADVERSE" if sell_adv < -esp else "NEUTRAL")
    elif policy == "inclusive_ge":
        b_cls = "ENTER_NOW" if buy_adv >= esp else ("WAIT_ADVERSE" if buy_adv <= -esp else "NEUTRAL")
        s_cls = "ENTER_NOW" if sell_adv >= esp else ("WAIT_ADVERSE" if sell_adv <= -esp else "NEUTRAL")
    elif policy == "half_tick_buffer":
        thr = esp + half_tick
        b_cls = "ENTER_NOW" if buy_adv > thr else ("WAIT_ADVERSE" if buy_adv < -thr else "NEUTRAL")
        s_cls = "ENTER_NOW" if sell_adv > thr else ("WAIT_ADVERSE" if sell_adv < -thr else "NEUTRAL")
    elif policy == "raw_half_tick":
        thr = esp + half_tick
        b_cls = "ENTER_NOW" if raw_buy > thr else ("WAIT_ADVERSE" if raw_buy < -thr else "NEUTRAL")
        s_cls = "ENTER_NOW" if raw_sell > thr else ("WAIT_ADVERSE" if raw_sell < -thr else "NEUTRAL")
    else:
        raise ValueError(f"Unknown boundary policy: {policy}")

    return {
        "policy": policy,
        "buy_adv_dec": buy_adv,
        "sell_adv_dec": sell_adv,
        "buy_class": b_cls,
        "sell_class": s_cls,
        "buy_exact_raw_tie": buy_exact_tie_raw,
        "sell_exact_raw_tie": sell_exact_tie_raw,
    }
