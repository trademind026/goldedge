"""
Task 12 Addendum — Exact Trade-Count & Cascade Decomposition for S04, S10, S15, S17
and H0 8% HWM Executed/Prevented Trades vs Raw Post-Halt Signals (W3).

Zero imports from `src/goldedge/`.
Produces: backtest_results/audit/task12_session_analysis/task12_trade_count_reconciliation.json
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any

import psycopg2

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import task12_session_analysis_and_diagnosis as t12p1
import task12_hypotheses_hwm_and_reconciliation as t12p2

OUT_DIR = t12p1.OUT_DIR


def _direct_filter_blocks(cfg: t12p2.SConfig, trade: dict[str, Any]) -> bool:
    """Return True if `cfg`'s session filter directly blocks `trade` at its entry_time."""
    et_utc = datetime.fromisoformat(trade["entry_time"]).astimezone(timezone.utc)
    sinfo = t12p1.classify_dst_session(et_utc)
    spread = float(trade["entry_ask"]) - float(trade["entry_bid"])
    return not t12p2._passes_session_filter(cfg, trade["direction"], et_utc, spread, sinfo)


def main() -> None:
    cfgs = [
        t12p2.SConfig("H4a_SCORE_CAP_080", "control", "H4a", "H4a control", max_score=0.80),
        t12p2.SConfig("S04_H4a_NO_UTC09", "utc_hour_h4a", "H4a", "Exclude UTC 09 on H4a", max_score=0.80, block_utc_hours=(9,)),
        t12p2.SConfig("S10_H4a_BUY_NO_07_08", "buy_session", "H4a", "Block BUY in UTC 07-08 on H4a", max_score=0.80, block_buy_utc_hours=(7, 8)),
        t12p2.SConfig("S15_H4a_NO_TRANS_15M", "transition", "H4a", "Block +-15m around session open/close transitions on H4a", max_score=0.80, block_transition_half_mins=15.0),
        t12p2.SConfig("S17_H4a_NO_FIRST30M", "open_close_30m", "H4a", "Block first 30m of London/NY open on H4a", max_score=0.80, block_first_n_mins_open=30.0),
    ]
    conn = psycopg2.connect(os.environ.get("DB_URL", "postgresql://postgres:password@db:5432/goldedge"))
    try:
        pfs, _ = t12p2._run_window(
            conn,
            "W3",
            datetime(2026, 8, 17, 0, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 18, 18, 9, 59, tzinfo=timezone.utc),
            cfgs,
        )
    finally:
        conn.close()

    by_name = {pf.cfg.name: pf for pf in pfs}
    base_trades = by_name["H4a_SCORE_CAP_080"].trades
    base_map = {(t["entry_time"], t["direction"]): t for t in base_trades}

    decomp: dict[str, Any] = {"base_H4a_w3_trades": len(base_trades), "candidates": {}}
    for cname in ("S04_H4a_NO_UTC09", "S10_H4a_BUY_NO_07_08", "S15_H4a_NO_TRANS_15M", "S17_H4a_NO_FIRST30M"):
        pf = by_name[cname]
        cfg = pf.cfg
        cur_map = {(t["entry_time"], t["direction"]): t for t in pf.trades}

        retained = [t for k, t in base_map.items() if k in cur_map]
        removed_all = [t for k, t in base_map.items() if k not in cur_map]
        direct_filtered = [t for t in removed_all if _direct_filter_blocks(cfg, t)]
        cascade_prevented = [t for t in removed_all if not _direct_filter_blocks(cfg, t)]
        cascade_created = [t for k, t in cur_map.items() if k not in base_map]

        identity_ok = (len(base_trades) - len(direct_filtered) - len(cascade_prevented) + len(cascade_created)) == len(pf.trades)
        assert identity_ok, f"Accounting identity failed for {cname}"

        decomp["candidates"][cname] = {
            "base_n": len(base_trades),
            "executed_n": len(pf.trades),
            "retained_n": len(retained),
            "total_removed_from_base_n": len(removed_all),
            "directly_filtered_n": len(direct_filtered),
            "directly_filtered_winners": sum(1 for t in direct_filtered if t["net_pnl"] > 0),
            "directly_filtered_losers": sum(1 for t in direct_filtered if t["net_pnl"] <= 0),
            "directly_filtered_net_pnl": round(sum(t["net_pnl"] for t in direct_filtered), 2),
            "cascade_prevented_n": len(cascade_prevented),
            "cascade_prevented_winners": sum(1 for t in cascade_prevented if t["net_pnl"] > 0),
            "cascade_prevented_losers": sum(1 for t in cascade_prevented if t["net_pnl"] <= 0),
            "cascade_prevented_net_pnl": round(sum(t["net_pnl"] for t in cascade_prevented), 2),
            "cascade_created_n": len(cascade_created),
            "cascade_created_winners": sum(1 for t in cascade_created if t["net_pnl"] > 0),
            "cascade_created_losers": sum(1 for t in cascade_created if t["net_pnl"] <= 0),
            "cascade_created_net_pnl": round(sum(t["net_pnl"] for t in cascade_created), 2),
            "accounting_identity": f"{len(base_trades)} - {len(direct_filtered)} (direct) - {len(cascade_prevented)} (cascade_prev) + {len(cascade_created)} (cascade_new) = {len(pf.trades)}",
            "accounting_identity_verified": identity_ok,
            "cascade_prevented_examples": [
                {"entry_time": t["entry_time"], "direction": t["direction"], "utc_hour": t["utc_hour"], "net_pnl": round(t["net_pnl"], 2)}
                for t in cascade_prevented
            ],
            "cascade_created_examples": [
                {"entry_time": t["entry_time"], "direction": t["direction"], "utc_hour": t["utc_hour"], "net_pnl": round(t["net_pnl"], 2)}
                for t in cascade_created
            ],
        }

    # Attach exact HWM clarification from task12_hwm_comparison.json
    hwm = json.loads((OUT_DIR / "task12_hwm_comparison.json").read_text(encoding="utf-8"))
    h0_w3 = hwm["W3"]["H0_HWM8"]
    decomp["hwm_w3_h0_clarification"] = {
        "executed_trades_inc_trigger": h0_w3["trades_executed"],
        "unprotected_h0_trades": h0_w3["unprotected_trades"],
        "prevented_counterfactual_trades": h0_w3["trades_prevented_by_halt"],
        "raw_post_halt_qualified_signals_blocked": h0_w3["signals_blocked_post_halt"],
        "explanation": (
            "78 trades executed up to and including the trigger trade (closed via HWM_KILL_SWITCH_8PCT at 2026-10-16T07:39:25.668Z). "
            "65 prevented trades = 143 unprotected H0 trades minus 78 executed trades (counterfactual trades that would have executed). "
            "149 prevented signals = raw post-halt 5m bars passing score/regime/H1/daily/cooldown gates while halted; this exceeds 65 because "
            "the halted portfolio has no open position (so it never blocks overlapping 5m signals during a ~23m hold) and never triggers the 15-bar (strategy, direction) cooldown."
        ),
    }

    (OUT_DIR / "task12_trade_count_reconciliation.json").write_text(json.dumps(decomp, indent=2), encoding="utf-8")
    print("[TASK 12 ADDENDUM COMPLETE]")
    for cname, info in decomp["candidates"].items():
        print(
            f"  {cname:22s} | {info['accounting_identity']} "
            f"| direct={info['directly_filtered_winners']}W/{info['directly_filtered_losers']}L ({info['directly_filtered_net_pnl']:+.2f}) "
            f"| casc_prev={info['cascade_prevented_winners']}W/{info['cascade_prevented_losers']}L ({info['cascade_prevented_net_pnl']:+.2f}) "
            f"| casc_new={info['cascade_created_winners']}W/{info['cascade_created_losers']}L ({info['cascade_created_net_pnl']:+.2f})"
        )


if __name__ == "__main__":
    main()
