import pytest
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np

from src.goldedge.gold import (
    GoldPipEngine,
    GoldOpenTrade,
    GoldKalmanExitAgent,
    GoldDecisionRules,
    gold_adaptive_position_size,
    apply_gold_adaptive_kalman,
    StreamingGoldKalmanFilter,
    GoldOrderManager,
    GoldTickReplayUseCase,
    GOLD_PROFILES,
    get_gold_profile,
)


def test_gold_pip_engine_conversions():
    # $1.00 move = 10 pips
    assert GoldPipEngine.price_to_pips(1.00) == 10.0
    assert GoldPipEngine.price_to_pips(0.10) == 1.0
    assert GoldPipEngine.price_to_pips(0.01) == 0.1
    assert GoldPipEngine.pips_to_price(10.0) == 1.00
    assert GoldPipEngine.pips_to_price(1.0) == 0.10


def test_gold_pip_engine_cash_pnl():
    # 1.00 standard lot (100 oz): $1.00 price gain = $100.00 cash PnL
    pnl_buy = GoldPipEngine.calculate_cash_pnl("BUY", 2400.0, 2401.0, 1.0)
    assert pytest.approx(pnl_buy, 1e-4) == 100.0

    # 0.50 lot: $2.50 price gain = 0.5 * 100 * 2.50 = $125.00
    pnl_buy_half = GoldPipEngine.calculate_cash_pnl("BUY", 2400.0, 2402.50, 0.5)
    assert pytest.approx(pnl_buy_half, 1e-4) == 125.0

    # SELL: entry 2405.0, exit 2400.0 -> $5.00 drop = +$500 for 1.00 lot
    pnl_sell = GoldPipEngine.calculate_cash_pnl("SELL", 2405.0, 2400.0, 1.0)
    assert pytest.approx(pnl_sell, 1e-4) == 500.0


def test_gold_pip_engine_margin_and_lot_sizing():
    # Margin for 1.00 lot @ $2400 with 1:100 leverage = (1 * 100 * 2400) / 100 = $2400
    margin = GoldPipEngine.calculate_required_margin(2400.0, 1.0, leverage=100.0)
    assert pytest.approx(margin, 1e-4) == 2400.0

    # Lot sizing: $10,000 equity, 1% risk = $100 risk
    # Entry $2400, SL $2398 -> price risk $2.00 -> $200 risk per lot
    # Expected lots = $100 / $200 = 0.50 lots
    lots = GoldPipEngine.calculate_lot_size(
        account_equity=10000.0,
        risk_pct=1.0,
        entry_price=2400.0,
        sl_price=2398.0,
        max_leverage=100.0
    )
    assert pytest.approx(lots, 1e-2) == 0.50


def test_gold_pip_engine_spread_validation():
    # Typical spread: $0.25 (2.5 pips) -> VALID
    valid, spread_dollars, spread_pips = GoldPipEngine.validate_spread(2400.00, 2400.25)
    assert valid is True
    assert pytest.approx(spread_dollars, 1e-4) == 0.25
    assert pytest.approx(spread_pips, 1e-4) == 2.5

    # Spread blowout: $0.75 (> $0.60 gate) -> REJECTED
    valid_blowout, spread_dollars_b, _ = GoldPipEngine.validate_spread(2400.00, 2400.75)
    assert valid_blowout is False
    assert pytest.approx(spread_dollars_b, 1e-4) == 0.75


def test_gold_kalman_filter():
    prices = pd.Series([2400.0, 2400.5, 2401.2, 2402.0, 2402.5, 2403.0, 2402.8, 2403.5])
    volume = pd.Series([100, 150, 120, 200, 180, 250, 110, 300])
    df = apply_gold_adaptive_kalman(prices, volume)

    assert "kalman_price" in df.columns
    assert "kalman_velocity" in df.columns
    assert len(df) == len(prices)
    assert df["kalman_price"].iloc[-1] > 2400.0


def test_streaming_gold_kalman_filter():
    skf = StreamingGoldKalmanFilter()
    p1, v1, z1 = skf.update(2400.0)
    assert p1 == 2400.0
    assert v1 == 0.0

    # Ramp up prices
    for p in [2400.5, 2401.0, 2401.8, 2402.5, 2403.5, 2404.5]:
        kp, kv, kz = skf.update(p, volume=50.0, spread=0.25)

    assert kp > 2400.0
    assert kv > 0.0  # Positive velocity during ramp-up


def test_gold_kalman_exit_agent_trailing_ratchet():
    agent = GoldKalmanExitAgent()
    entry = 2400.0
    initial_sl = 2398.0
    agent.reset(entry)
    t0 = datetime(2026, 11, 10, 10, 0, 0, tzinfo=timezone.utc)

    # Initial updates in profit < $2.00: SL should not ratchet yet
    agent.update(2401.0, 2400.85, 2401.15, "BUY")
    action, sl_curr = agent.decide(entry, 2401.0, initial_sl, t0 + timedelta(seconds=1), "BUY")
    assert sl_curr == initial_sl

    # Price moves to +$3.50 (above $2.00 activation threshold)
    for i in range(12):
        t_tick = t0 + timedelta(seconds=i + 5)
        agent.update(2403.50, 2403.35, 2403.65, "BUY")

    action, new_sl = agent.decide(entry, 2403.50, initial_sl, t0 + timedelta(seconds=20), "BUY")
    # SL must have ratcheted up above initial_sl
    assert new_sl > initial_sl
    assert new_sl < 2403.50


def test_gold_decision_rules():
    rules = GoldDecisionRules(min_kalman_vel_z=0.5, min_meta_prob=0.62, max_spread_dollars=0.60)

    # Passes all gates
    passed, reason = rules.evaluate("BUY", kalman_vel_z=0.85, meta_probability=0.70, spread_dollars=0.25)
    assert passed is True
    assert reason == "PASSED_ALL_GATES"

    # Fails spread gate
    passed_spread, r_spread = rules.evaluate("BUY", kalman_vel_z=0.85, meta_probability=0.70, spread_dollars=0.65)
    assert passed_spread is False
    assert "GATE_4_FAILED" in r_spread

    # Fails Kalman velocity gate
    passed_vel, r_vel = rules.evaluate("BUY", kalman_vel_z=0.30, meta_probability=0.70, spread_dollars=0.25)
    assert passed_vel is False
    assert "GATE_2_FAILED" in r_vel

    # Fails Meta-model probability gate
    passed_meta, r_meta = rules.evaluate("BUY", kalman_vel_z=0.85, meta_probability=0.55, spread_dollars=0.25)
    assert passed_meta is False
    assert "GATE_3_FAILED" in r_meta


def test_gold_order_manager_dual_mode():
    mgr = GoldOrderManager(default_mode="test", initial_balance=10000.0)
    assert mgr.get_mode() in ("test", "paper")

    # Verify rejection if no market tick has arrived (zero fake data guarantee)
    assert mgr.place_order(symbol="XAUUSD", order_type="BUY", volume=0.5, sl=2395.0, tp=2415.0) is None

    # Feed market tick
    mgr.update_paper_tick("XAUUSD", price=2400.0, bid=2399.85, ask=2400.15)

    # Place paper order
    ticket = mgr.place_order(symbol="XAUUSD", order_type="BUY", volume=0.5, sl=2395.0, tp=2415.0)
    assert ticket is not None
    assert len(mgr.get_open_positions()) == 1

    # Modify SL
    mod_ok = mgr.modify_position(ticket, sl=2397.0)
    assert mod_ok is True
    pos = mgr.get_open_positions()[0]
    assert pos.price_sl == 2397.0

    # Simulate price gain and close position
    mgr.update_paper_tick("XAUUSD", price=2405.0, bid=2404.85, ask=2405.15)
    close_ok = mgr.close_position(ticket)
    assert close_ok is True
    assert len(mgr.get_open_positions()) == 0
    assert len(mgr.closed_paper_positions) == 1

    summary = mgr.get_account_summary()
    assert summary["mode"] in ("test", "paper")
    assert summary["total_closed_pnl_test"] > 0

    # Mode switching
    mgr.set_mode("live")
    assert mgr.get_mode() == "live"


def test_gold_tick_replay_synthetic():
    t0 = datetime(2026, 11, 10, 9, 0, 0, tzinfo=timezone.utc)
    records = []
    base_price = 2400.0

    # Generate 60 minutes with a strong upward trend
    for m in range(60):
        minute_ts = t0 + timedelta(minutes=m)
        base_price += 0.20
        for s in range(5):
            tick_ts = minute_ts + timedelta(seconds=s * 12)
            records.append({
                "timestamp": tick_ts,
                "price": base_price + (s * 0.02),
                "bid": base_price - 0.10,
                "ask": base_price + 0.10,
                "volume": 50
            })

    ticks_df = pd.DataFrame(records)
    replay = GoldTickReplayUseCase(symbol="XAUUSD", initial_equity=10000.0)
    result = replay.run(ticks_df)

    assert result["symbol"] == "XAUUSD"
    assert "trades" in result
    assert "win_rate" in result
    assert "net_pnl" in result
    assert len(result["equity_curve"]) > 0
