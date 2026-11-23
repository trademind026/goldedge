import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def _summarize_exits(exits: list[dict]) -> dict:
    if not exits:
        return {"n": 0}
    pnls = [float(e.get("net_pnl") if e.get("net_pnl") is not None else e["pnl"]) for e in exits]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gp = sum(wins)
    gl = abs(sum(losses))
    pf = round(gp / gl, 2) if gl > 0 else (999.0 if gp > 0 else 0.0)
    eq, pk, dd = 0.0, 0.0, 0.0
    for p in pnls:
        eq += p
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    return {
        "n": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "wr_pct": round(len(wins) / len(pnls) * 100.0, 1),
        "pf": pf,
        "expectancy_usd": round(sum(pnls) / len(pnls), 2),
        "pnl": round(sum(pnls), 2),
        "max_dd": round(dd, 2),
        "e1_15m_flagged": sum(1 for e in exits if e.get("e1_15m_would_exit")),
    }


def _med(vals: list) -> str:
    clean = [float(v) for v in vals if v is not None]
    return f"{statistics.median(clean):+.4f}" if clean else "NA"


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("backtest_results/shadow_live_rollovers.jsonl")
    if not path.exists():
        print(f"No shadow log found at {path}")
        return

    rollovers, opens, exits = [], [], []
    seen_rollovers = set()
    dup_rollovers = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        ev = json.loads(line)
        kind = ev.get("event")
        if kind == "SHADOW_ROLLOVER":
            k = (ev.get("symbol"), ev.get("rollover_utc"))
            if k in seen_rollovers:
                dup_rollovers += 1
            seen_rollovers.add(k)
            rollovers.append(ev)
        elif kind == "SHADOW_OPEN":
            opens.append(ev)
        elif kind == "SHADOW_EXIT":
            exits.append(ev)

    print(f"Shadow Log: {path}")
    print(f"Rollovers={len(rollovers)} (duplicates={dup_rollovers}) | Opens={len(opens)} | Exits={len(exits)}")
    if rollovers:
        print(f"Window: {rollovers[0].get('rollover_utc')} .. {rollovers[-1].get('rollover_utc')}")
        orders_true = sum(1 for r in rollovers if r.get("order_placed"))
        print(f"Orders Placed: {orders_true} (must be 0 in SHADOW_MODE)")

    # 1 & 2. BUY vs SELL Summary (WR, PF, expectancy, drawdown)
    for d in ("BUY", "SELL"):
        dir_rolls = [r for r in rollovers if r.get("direction") == d]
        dir_app = [r for r in dir_rolls if r.get("status") == "APPROVED_SHADOW"]
        dir_exits_all = [e for e in exits if e.get("direction") == d]
        print(f"\n=== {d}-ONLY MONITORING SEGMENT ===")
        print(f"  Candidates={len(dir_rolls)} Approved={len(dir_app)}")
        print(f"  Stats (WR/PF/Exp/DD): {_summarize_exits(dir_exits_all)}")

    # 3. TRENDING_BEAR SELL Diagnostic Segment
    tb_sell = [e for e in exits if e.get("direction") == "SELL" and e.get("regime") == "TRENDING_BEAR"]
    print(f"\n=== TRENDING_BEAR SELL SEGMENT (n={len(tb_sell)}) ===")
    print(f"  Stats: {_summarize_exits(tb_sell)}")
    if tb_sell:
        print(
            f"  Medians: k_vel={_med([e.get('kalman_velocity') for e in tb_sell])} | "
            f"swL20_atr={_med([e.get('distance_to_swing_low_atr') for e in tb_sell])} | "
            f"vwap_atr={_med([e.get('distance_to_vwap_atr') for e in tb_sell])} | "
            f"mfe_5m={_med([e.get('mfe_5m_atr') for e in tb_sell])} | "
            f"mfe_10m={_med([e.get('mfe_10m_atr') for e in tb_sell])} | "
            f"mfe_15m={_med([e.get('mfe_15m_atr') for e in tb_sell])}"
        )

    # 4. 09:00 UTC Monitoring Segment
    h09_exits = [e for e in exits if str(e.get("entry_timestamp") or e.get("opened_at", ""))[11:13] == "09"]
    early_sl_09 = sum(1 for e in h09_exits if (e.get("exit_reason") or e.get("reason")) == "SL" and float(e.get("duration_mins") or 99) <= 15.0)
    print(f"\n=== 09:00 UTC MONITORING SEGMENT (n={len(h09_exits)}) ===")
    print(f"  Stats: {_summarize_exits(h09_exits)} | early_SL(<=15m)={early_sl_09}")
    if h09_exits:
        by_dr = defaultdict(int)
        for e in h09_exits:
            by_dr[(e.get("direction"), e.get("regime"))] += 1
        print(f"  Dir/Regime Mix: {dict(by_dr)}")

    # 5. Before / After 15:00 UTC Segment
    before_15 = [e for e in exits if e.get("in_15utc_window")]
    after_15 = [e for e in exits if e.get("in_15utc_window") is False]
    rejected_ops = [r for r in rollovers if r.get("status") == "APPROVED_SHADOW" and r.get("in_15utc_window") is False]
    print("\n=== BEFORE vs AFTER 15:00 UTC SEGMENT ===")
    print(f"  < 15:00 UTC : {_summarize_exits(before_15)}")
    print(f"  >= 15:00 UTC: {_summarize_exits(after_15)} | rejected_opportunities={len(rejected_ops)}")


if __name__ == "__main__":
    main()
