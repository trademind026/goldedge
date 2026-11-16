import pytest
from datetime import datetime, timezone
from src.goldedge.gold.application.live_execution_engine import ForexLiveExecutionEngine
from src.goldedge.gold.adapters.broker.gold_order_manager import GoldOrderManager


def test_live_execution_engine_telemetry_contract():
    order_mgr = GoldOrderManager(default_mode="test", initial_balance=10000.0)
    engine = ForexLiveExecutionEngine(order_manager=order_mgr, symbol="XAUUSD", default_mode="paper")

    snapshot = engine.get_telemetry_snapshot()
    assert snapshot["mode"] == "paper"
    assert snapshot["status"] in ("awaiting_ticks", "scanning", "trading", "idle", "rollover_freeze", "broker_disconnected")
    assert "account" in snapshot
    assert snapshot["account"]["balance"] == 10000.0
    assert snapshot["account"]["equity"] == 10000.0
    assert snapshot["account"]["currency"] == "USD"
    assert "positions" in snapshot
    assert "suggestions" in snapshot
    assert "latency_ms" in snapshot


def test_order_placement_rejected_without_tick():
    """Verify that paper and live orders are rejected when no market tick is received."""
    order_mgr = GoldOrderManager(default_mode="test", initial_balance=10000.0)
    engine = ForexLiveExecutionEngine(order_manager=order_mgr, symbol="XAUUSD", default_mode="paper")

    # Order without tick must be rejected (no fake 2400.0 fallback!)
    ticket = order_mgr.place_order("XAUUSD", "BUY", volume=0.5, sl=2395.0, tp=2415.0)
    assert ticket is None
    assert len(order_mgr.get_open_positions()) == 0


def test_live_execution_engine_on_tick_and_position_flow():
    order_mgr = GoldOrderManager(default_mode="test", initial_balance=10000.0)
    engine = ForexLiveExecutionEngine(order_manager=order_mgr, symbol="XAUUSD", default_mode="paper")

    # Ingest incoming tick
    engine.on_tick("XAUUSD", price=2400.0, bid=2399.85, ask=2400.15, volume=50)
    assert engine.last_tick_price == 2400.0

    # Place an order through order manager
    ticket = order_mgr.place_order("XAUUSD", "BUY", volume=0.5, sl=2395.0, tp=2415.0)
    assert ticket is not None

    # Process next tick with favorable move to 2403.0 (+30 pips)
    engine.on_tick("XAUUSD", price=2403.0, bid=2402.85, ask=2403.15, volume=50)

    snapshot = engine.get_telemetry_snapshot()
    assert len(snapshot["positions"]) == 1
    pos = snapshot["positions"][0]
    assert pos["ticket"] == ticket
    assert pos["pnl_pips"] > 0
    assert pos["pnl_usd"] > 0
    assert len(pos["journey"]) > 0


def test_live_execution_engine_risk_overrides():
    order_mgr = GoldOrderManager(default_mode="test", initial_balance=10000.0)
    engine = ForexLiveExecutionEngine(order_manager=order_mgr, symbol="XAUUSD", default_mode="paper")

    # Feed market tick first
    engine.on_tick("XAUUSD", price=2400.0, bid=2399.85, ask=2400.15)

    # Place order
    ticket = order_mgr.place_order("XAUUSD", "BUY", volume=0.5, sl=2395.0, tp=2415.0)
    assert ticket is not None

    # Move price into profit (+ $3.00)
    engine.on_tick("XAUUSD", price=2403.0, bid=2402.85, ask=2403.15)

    # 1. Zero Loss override
    success_zl, new_sl, msg = engine.zero_loss(ticket)
    assert success_zl is True
    assert new_sl > 2395.0  # SL moved to breakeven + buffer

    # 2. Lock 60% Profit override
    success_lp, locked_sl, msg_lp = engine.lock_profit(ticket, lock_pct=0.60)
    assert success_lp is True
    assert locked_sl >= new_sl

    # 3. Exit position
    success_exit, msg_exit = engine.exit_position(ticket)
    assert success_exit is True
    assert len(order_mgr.get_open_positions()) == 0


def test_live_execution_engine_kill_switch():
    order_mgr = GoldOrderManager(default_mode="test", initial_balance=10000.0)
    engine = ForexLiveExecutionEngine(order_manager=order_mgr, symbol="XAUUSD", default_mode="paper")

    # Feed market tick first
    engine.on_tick("XAUUSD", price=2400.0, bid=2399.85, ask=2400.15)

    # Open 2 positions
    t1 = order_mgr.place_order("XAUUSD", "BUY", volume=0.2, sl=2390.0, tp=2420.0)
    t2 = order_mgr.place_order("XAUUSD", "BUY", volume=0.3, sl=2392.0, tp=2420.0)
    assert len(order_mgr.get_open_positions()) == 2

    closed_tickets = engine.kill_switch()
    assert len(closed_tickets) == 2
    assert t1 in closed_tickets and t2 in closed_tickets
    assert len(order_mgr.get_open_positions()) == 0
    assert engine.halted is True
    assert engine.status == "idle"


def test_live_execution_engine_demo_mode_switching_and_routing():
    """Verify demo mode switching, validation, and broker interaction."""
    order_mgr = GoldOrderManager(default_mode="paper", initial_balance=10000.0)
    engine = ForexLiveExecutionEngine(order_manager=order_mgr, symbol="XAUUSD", default_mode="paper")

    assert engine.mode == "paper"
    assert order_mgr.get_mode() == "paper"

    # Switch to DEMO mode
    engine.set_mode("demo")
    assert engine.mode == "demo"
    assert order_mgr.get_mode() == "demo"

    # Switch to LIVE mode
    engine.set_mode("live")
    assert engine.mode == "live"
    assert order_mgr.get_mode() == "live"

    # Invalid mode should raise ValueError
    with pytest.raises(ValueError):
        engine.set_mode("unsupported_mode")

    # In demo mode, status reflects broker connection status
    engine.set_mode("demo")
    snapshot = engine.get_telemetry_snapshot()
    assert snapshot["mode"] == "demo"
    # When broker is disconnected, status is broker_disconnected
    assert snapshot["status"] == "broker_disconnected"

