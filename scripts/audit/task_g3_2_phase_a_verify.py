"""
TASK G3.2 — Phase A1 & A2 Verification:
  - A1: Authoritative 96,862 ML-eligible 1-minute candle source reconciliation
  - A2: Fusion Markets cTrader demo execution cost reconciliation ($12/$1M/side vs stale $0.045/oz)
Outputs:
  - backtest_results/audit/task_g3_2_label_design/g3_2_candle_source_reconciliation.json
  - backtest_results/audit/task_g3_2_label_design/g3_2_cost_parity_verification.json
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import psycopg2

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/src")

from src.goldedge.config.risk_profiles import get_profile
from src.goldedge.config.settings import settings
from src.goldedge.domain.models.signal import SignalDirection
from src.goldedge.domain.services.broker_cost_service import (
    BrokerCostService,
    FUSION_CTRADER_DEMO_48439552,
    FUSION_XAUUSD_SWAP_SPEC,
)
from src.goldedge.domain.services.label_spec_service import (
    DEFAULT_COST_SPEC,
    verify_broker_cost_schedule_parity,
)
from src.goldedge.domain.services.sizing_service import PositionSizingService

OUT_DIR = Path("backtest_results/audit/task_g3_2_label_design")
DB_URL = os.environ.get(
    "DB_URL", "postgresql://postgres:password@timescaledb:5432/goldedge"
).replace("+psycopg2", "")


def run_phase_a() -> None:
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = psycopg2.connect(DB_URL)
    conn.set_session(readonly=True, autocommit=True)

    # ------------------------------------------------------------------
    # PHASE A1: Full 96,862 ML-eligible 1m bar reconciliation
    # ------------------------------------------------------------------
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                count(*) AS raw_ticks,
                count(*) FILTER (WHERE is_valid_quote) AS valid_ticks,
                count(*) FILTER (WHERE is_quarantined) AS quarantined_ticks,
                count(*) FILTER (WHERE is_ml_eligible_quote) AS ml_eligible_ticks,
                min(time) FILTER (WHERE is_ml_eligible_quote) AS min_ml_t,
                max(time) FILTER (WHERE is_ml_eligible_quote) AS max_ml_t
            FROM v_market_ticks_quote_semantics_v1
            WHERE symbol = 'XAUUSD'
            """
        )
        raw_t, val_t, quar_t, ml_t, min_ml_t, max_ml_t = cur.fetchone()

        # Independently reconstruct ALL ML-eligible 1m bars directly from raw market_ticks + quarantine filter
        # and full-outer-join against refreshed market_bars_1m_quote_semantics_v1
        cur.execute(
            """
            WITH indep_1m AS (
                SELECT
                    time_bucket('1 minute', t.time) AS time,
                    first(t.bid, t.time) AS open,
                    max(t.bid) AS high,
                    min(t.bid) AS low,
                    last(t.bid, t.time) AS close,
                    count(*) AS tick_count,
                    GREATEST(0, ROUND((last(t.ask, t.time) - last(t.bid, t.time)) / 0.01))::int AS spread_points
                FROM market_ticks t
                LEFT JOIN market_ticks_quarantine q
                  ON q.symbol = t.symbol AND q.time = t.time
                WHERE t.symbol = 'XAUUSD'
                  AND t.bid IS NOT NULL AND t.ask IS NOT NULL
                  AND t.bid > 0 AND t.ask > 0 AND t.bid < t.ask
                  AND q.time IS NULL
                  AND NOT (
                      EXTRACT(DOW FROM t.time AT TIME ZONE 'UTC') = 6
                      OR (
                          EXTRACT(DOW FROM t.time AT TIME ZONE 'UTC') = 0
                          AND EXTRACT(HOUR FROM t.time AT TIME ZONE 'UTC') < 21
                      )
                  )
                GROUP BY 1
            )
            SELECT
                (SELECT count(*) FROM indep_1m) AS indep_cnt,
                (SELECT count(*) FROM market_bars_1m_quote_semantics_v1 WHERE symbol='XAUUSD') AS table_cnt,
                count(*) FILTER (WHERE i.time IS NULL) AS only_in_table,
                count(*) FILTER (WHERE b.time IS NULL) AS only_in_indep,
                count(*) FILTER (
                    WHERE i.time IS NOT NULL AND b.time IS NOT NULL AND (
                        abs(i.open - b.open) > 1e-9
                        OR abs(i.high - b.high) > 1e-9
                        OR abs(i.low - b.low) > 1e-9
                        OR abs(i.close - b.close) > 1e-9
                        OR i.tick_count != b.tick_count
                        OR i.spread_points != b.spread_points
                    )
                ) AS value_mismatches,
                (SELECT count(*) FROM market_bars_1m_quote_semantics_v1 WHERE symbol='XAUUSD' AND is_feed_gap = TRUE) AS forward_filled_rows
            FROM indep_1m i
            FULL OUTER JOIN market_bars_1m_quote_semantics_v1 b
              ON b.symbol = 'XAUUSD' AND b.time = i.time
            """
        )
        indep_cnt, tbl_cnt, only_tbl, only_indep, val_mm, ffill_rows = cur.fetchone()

        # Verify dynamic timeline view bounds and gap marking
        cur.execute(
            """
            SELECT count(*), min(time), max(time),
                   count(*) FILTER (WHERE NOT is_feed_gap) AS observed_bars,
                   count(*) FILTER (WHERE is_feed_gap) AS gap_minutes
            FROM v_market_bars_1m_timeline_v1
            """
        )
        tl_total, tl_min, tl_max, tl_obs, tl_gaps = cur.fetchone()

        # Dataset fingerprint (deterministic hash of min/max/count/sum of ML-eligible bars)
        cur.execute(
            """
            SELECT sum(open), sum(high), sum(low), sum(close), sum(tick_count)
            FROM market_bars_1m_quote_semantics_v1
            WHERE symbol = 'XAUUSD'
            """
        )
        s_o, s_h, s_l, s_c, s_tc = cur.fetchone()

    conn.close()

    fp_str = f"XAUUSD|ml_ticks={ml_t}|bars={tbl_cnt}|min={min_ml_t.isoformat()}|max={max_ml_t.isoformat()}|o={s_o:.4f}|c={s_c:.4f}|tc={s_tc}"
    dataset_sha256 = hashlib.sha256(fp_str.encode("utf-8")).hexdigest().upper()

    a1_pass = (
        int(indep_cnt) == 96862
        and int(tbl_cnt) == 96862
        and int(only_tbl) == 0
        and int(only_indep) == 0
        and int(val_mm) == 0
        and int(ffill_rows) == 0
        and int(tl_obs) == 96862
    )
    a1_out = {
        "task": "G3.2_PHASE_A1_CANDLE_SOURCE_RECONCILIATION",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "PASS" if a1_pass else "FAIL",
        "authoritative_tick_source": "v_market_ticks_quote_semantics_v1 (WHERE is_ml_eligible_quote = TRUE)",
        "authoritative_1m_bar_table": "market_bars_1m_quote_semantics_v1 (refreshed via refresh_market_bars_1m_quote_semantics_v1)",
        "continuous_refresh_function": "public.refresh_market_bars_1m_quote_semantics_v1(p_symbol, p_start, p_end)",
        "tick_counts": {
            "raw_ticks": int(raw_t),
            "valid_ticks": int(val_t),
            "quarantined_ticks": int(quar_t),
            "ml_eligible_ticks": int(ml_t),
            "ml_eligible_min_utc": min_ml_t.isoformat(),
            "ml_eligible_max_utc": max_ml_t.isoformat(),
        },
        "candle_1m_reconciliation": {
            "independent_reconstructed_bars": int(indep_cnt),
            "market_bars_1m_quote_semantics_v1_bars": int(tbl_cnt),
            "only_in_table": int(only_tbl),
            "only_in_independent": int(only_indep),
            "ohlc_tickcount_spread_mismatches": int(val_mm),
            "forward_filled_rows": int(ffill_rows),
        },
        "timeline_view_v1": {
            "total_minute_grid": int(tl_total),
            "min_utc": tl_min.isoformat(),
            "max_utc": tl_max.isoformat(),
            "observed_bars": int(tl_obs),
            "unfilled_gap_minutes": int(tl_gaps),
        },
        "dataset_fingerprint_string": fp_str,
        "dataset_fingerprint_sha256": dataset_sha256,
    }
    (OUT_DIR / "g3_2_candle_source_reconciliation.json").write_text(json.dumps(a1_out, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # PHASE A2: Execution Cost Reconciliation ($12/$1M/side vs $0.045/oz)
    # ------------------------------------------------------------------
    svc = BrokerCostService(FUSION_CTRADER_DEMO_48439552, FUSION_XAUUSD_SWAP_SPEC)
    sizing = PositionSizingService(contract_size=100.0)

    test_cases = [
        {"lots": 0.01, "entry_px": 3538.00, "exit_px": 3541.50},
        {"lots": 0.05, "entry_px": 4179.94, "exit_px": 4184.12},
        {"lots": 0.10, "entry_px": 4179.94, "exit_px": 4175.76},
        {"lots": 1.00, "entry_px": 4179.94, "exit_px": 4179.94},
        {"lots": 1.00, "entry_px": 4450.00, "exit_px": 4450.00},
    ]
    parity_rows: List[Dict[str, Any]] = []
    max_comm_diff = 0.0
    for tc in test_cases:
        svc_tot, spec_tot, diff = verify_broker_cost_schedule_parity(
            tc["lots"], tc["entry_px"], tc["exit_px"], DEFAULT_COST_SPEC
        )
        max_comm_diff = max(max_comm_diff, diff)
        # Note: per-side rounding to $0.01 can differ by at most $0.01 across 2 legs on micro-lots
        parity_rows.append(
            {
                **tc,
                "broker_cost_service_rt_usd": svc_tot,
                "label_cost_spec_rt_usd": spec_tot,
                "abs_diff_usd": round(diff, 4),
                "stale_g3_1_4_50_lot_rt_usd": round(4.50 * tc["lots"], 2),
                "understatement_ratio_vs_stale": round(svc_tot / max(1e-6, 4.50 * tc["lots"]), 3),
            }
        )

    ref_px = 4179.94
    rt_comm_per_oz = DEFAULT_COST_SPEC.round_trip_commission_usd_per_oz(ref_px, ref_px)
    rt_slip_per_oz = DEFAULT_COST_SPEC.round_trip_slippage_usd_per_oz
    med_spread_tick = 0.07
    med_spread_bar = 0.06
    total_rt_tick_usd = round(med_spread_tick + rt_comm_per_oz + rt_slip_per_oz, 5)
    total_rt_bar_usd = round(med_spread_bar + rt_comm_per_oz + rt_slip_per_oz, 5)

    # Verify PositionSizingService risk budget with verified RT commission at $4,179.94
    rt_comm_per_lot_ref = svc.estimate_round_trip_per_lot(ref_px)
    lots_sized, sl_sized, tp_sized, rej_reason = sizing.calculate_size(
        account_balance=10000.0,
        risk_profile=get_profile("MEDIUM_BACKTEST"),
        entry_price=4180.00,
        atr_value=6.67,
        direction=SignalDirection.BUY,
        symbol="XAUUSD",
        commission_per_lot=rt_comm_per_lot_ref,
    )

    a2_out = {
        "task": "G3.2_PHASE_A2_COST_PARITY_VERIFICATION",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "PASS" if max_comm_diff <= 0.01 else "FAIL",
        "reconciliation_verdict": (
            "RESOLVED: Actual configured Fusion Markets cTrader demo schedule (account 48439552, "
            "settings.BROKER_COMMISSION_PROFILE='fusion_ctrader_demo') is $12.00 per $1,000,000 notional "
            "per side ($24/$1M round-trip = 0.24 bps), verified across 15/15 demo deals in Task 5 & G1. "
            "At median XAUUSD price $4,179.94, this equals $10.03/lot ($0.1003/oz) RT — 2.23x HIGHER than "
            "the stale $4.50/lot ($0.045/oz) shorthand cited in G3.1. All G3.2 labels use the verified "
            "$12/$1M/side notional commission + 0.0533 pips/side slippage ($0.01066/oz RT) + executable bid/ask spread + swap."
        ),
        "configured_application_settings": {
            "BROKER_TYPE": settings.BROKER_TYPE,
            "TRADING_MODE": settings.TRADING_MODE,
            "BROKER_COMMISSION_PROFILE": settings.BROKER_COMMISSION_PROFILE,
            "account_id": FUSION_CTRADER_DEMO_48439552.account_id,
            "commission_type": FUSION_CTRADER_DEMO_48439552.commission_type,
            "commission_rate_per_million_per_side_usd": FUSION_CTRADER_DEMO_48439552.rate_value,
            "contract_size_oz_per_lot": FUSION_CTRADER_DEMO_48439552.contract_size,
            "slippage_pips_per_side": DEFAULT_COST_SPEC.slippage_pips_per_side,
            "pip_size_usd": DEFAULT_COST_SPEC.pip_size,
            "slippage_usd_per_oz_per_side": round(DEFAULT_COST_SPEC.slippage_usd_per_oz_per_side, 5),
            "slippage_usd_per_oz_rt": round(rt_slip_per_oz, 5),
            "swap_long_pips": FUSION_XAUUSD_SWAP_SPEC.swap_long_pips,
            "swap_short_pips": FUSION_XAUUSD_SWAP_SPEC.swap_short_pips,
            "swap_triple_weekday": "WEDNESDAY",
            "swap_rollover_tz": FUSION_XAUUSD_SWAP_SPEC.rollover_timezone,
            "swap_rollover_local_time": "17:00",
        },
        "unit_cost_breakdown_at_median_price_4179_94": {
            "reference_price_usd": ref_px,
            "commission_rt_usd_per_lot": rt_comm_per_lot_ref,
            "commission_rt_usd_per_oz": round(rt_comm_per_oz, 5),
            "commission_rt_bps": round(1e4 * rt_comm_per_oz / ref_px, 3),
            "slippage_rt_usd_per_oz": round(rt_slip_per_oz, 5),
            "slippage_rt_bps": round(1e4 * rt_slip_per_oz / ref_px, 3),
            "median_tick_spread_usd_per_oz": med_spread_tick,
            "median_1m_bar_close_spread_usd_per_oz": med_spread_bar,
            "total_rt_friction_tick_usd_per_oz": total_rt_tick_usd,
            "total_rt_friction_tick_bps": round(1e4 * total_rt_tick_usd / ref_px, 3),
            "total_rt_friction_1m_bar_usd_per_oz": total_rt_bar_usd,
            "total_rt_friction_1m_bar_bps": round(1e4 * total_rt_bar_usd / ref_px, 3),
            "stale_g3_1_total_tick_usd_per_oz": 0.115,
            "actual_vs_stale_friction_ratio": round(total_rt_tick_usd / 0.115, 3),
        },
        "sizing_service_check": {
            "equity_usd": 10000.0,
            "risk_profile": "MEDIUM_BACKTEST",
            "entry_price": 4180.00,
            "atr_value": 6.67,
            "commission_per_lot_rt": rt_comm_per_lot_ref,
            "sized_lots": lots_sized,
            "sl_price": sl_sized,
            "tp_price": tp_sized,
            "rejection_reason": rej_reason,
        },
        "parity_test_cases": parity_rows,
        "max_rounding_diff_usd": round(max_comm_diff, 4),
        "elapsed_sec": round(time.time() - t0, 2),
    }
    (OUT_DIR / "g3_2_cost_parity_verification.json").write_text(json.dumps(a2_out, indent=2), encoding="utf-8")

    print(f"[PHASE A1] status={a1_out['status']} | bars={tbl_cnt} (indep={indep_cnt}, mm={val_mm}, ffill={ffill_rows}) | sha256={dataset_sha256[:16]}")
    print(f"[PHASE A2] status={a2_out['status']} | RT comm=${rt_comm_per_oz:.4f}/oz + slip=${rt_slip_per_oz:.5f}/oz + spread=$0.0700 = ${total_rt_tick_usd:.4f}/oz ({a2_out['unit_cost_breakdown_at_median_price_4179_94']['total_rt_friction_tick_bps']} bps)")


if __name__ == "__main__":
    run_phase_a()
