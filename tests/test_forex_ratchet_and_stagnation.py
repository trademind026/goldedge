from datetime import datetime, timezone
import pytest
from goldedge.domain.services.forex_kalman_exit_agent import ForexKalmanEntropyExitAgent
from goldedge.domain.services.forex_audit_service import ForexAuditService


def test_four_tier_profit_ratchet_buy():
    agent = ForexKalmanEntropyExitAgent(pip_unit=0.0001, r_scale=1.0, q_base=0.001)
    entry_price = 1.08000
    initial_sl = 1.07700
    t0 = datetime(2026, 11, 10, 10, 0, 0, tzinfo=timezone.utc)

    # 1. Warm-up
    for _ in range(10):
        agent.update(entry_price, bid=entry_price - 0.00012, ask=entry_price, direction="BUY")

    # 2. Tier 4: Peak reaches +12 pips -> Locks to BE + 2*spread = 1.08024
    agent.update(1.08120, bid=1.08108, ask=1.08120, direction="BUY")
    action, sl_t4 = agent.decide(entry_price, 1.08110, initial_sl, t0, direction="BUY")
    assert action == "TIGHTEN"
    assert sl_t4 == pytest.approx(entry_price + (2.0 * 1.2 * 0.0001), abs=1e-5)

    # 3. Tier 3: Peak reaches +22 pips -> Locks to 55% of peak = 1.08000 + 22*0.55*0.0001
    agent.update(1.08220, bid=1.08208, ask=1.08220, direction="BUY")
    action, sl_t3 = agent.decide(entry_price, 1.08210, sl_t4, t0, direction="BUY")
    assert action == "TIGHTEN"
    expected_t3 = entry_price + (1.08220 - entry_price) * 0.55
    assert sl_t3 == pytest.approx(expected_t3, abs=1e-5)

    # 4. Tier 2: Peak reaches +38 pips -> Locks to 70% of peak
    agent.update(1.08380, bid=1.08368, ask=1.08380, direction="BUY")
    action, sl_t2 = agent.decide(entry_price, 1.08370, sl_t3, t0, direction="BUY")
    assert action == "TIGHTEN"
    expected_t2 = entry_price + (1.08380 - entry_price) * 0.70
    assert sl_t2 == pytest.approx(expected_t2, abs=1e-5)

    # 5. Tier 1: Peak reaches +55 pips -> Locks to 80% of peak
    agent.update(1.08550, bid=1.08538, ask=1.08550, direction="BUY")
    action, sl_t1 = agent.decide(entry_price, 1.08540, sl_t2, t0, direction="BUY")
    assert action == "TIGHTEN"
    expected_t1 = entry_price + (1.08550 - entry_price) * 0.80
    assert sl_t1 == pytest.approx(expected_t1, abs=1e-5)


def test_four_tier_profit_ratchet_sell():
    agent = ForexKalmanEntropyExitAgent(pip_unit=0.0001, r_scale=1.0, q_base=0.001)
    entry_price = 1.08000
    initial_sl = 1.08300
    t0 = datetime(2026, 11, 10, 10, 0, 0, tzinfo=timezone.utc)

    # Warm-up
    for _ in range(10):
        agent.update(entry_price, bid=entry_price, ask=entry_price + 0.00012, direction="SELL")

    # Peak reaches +52 pips short (price drops to 1.07480)
    agent.update(1.07480, bid=1.07480, ask=1.07492, direction="SELL")
    action, new_sl = agent.decide(entry_price, 1.07500, initial_sl, t0, direction="SELL")
    assert action == "TIGHTEN"
    expected_sl = entry_price - (entry_price - 1.07480) * 0.80
    assert new_sl == pytest.approx(expected_sl, abs=1e-5)


def test_linear_stagnation_defense():
    agent = ForexKalmanEntropyExitAgent(pip_unit=0.0001, r_scale=1.0, q_base=0.001)
    entry_price = 1.08000
    sl = 1.07800
    t0 = datetime(2026, 11, 10, 10, 0, 0, tzinfo=timezone.utc)

    for _ in range(10):
        agent.update(entry_price, bid=entry_price - 0.00012, ask=entry_price, direction="BUY")

    # Simulate 18 bars of stagnation since peak
    agent.bars_since_peak = 18
    # Compression: (18 - 15) * 0.5 = 1.5 pips = 0.00015
    action, compressed_sl = agent.decide(entry_price, 1.08050, sl, t0, direction="BUY")
    assert compressed_sl == pytest.approx(sl + 0.00015, abs=1e-5)


def test_forex_audit_service_swap_and_csv_export():
    audit_service = ForexAuditService(contract_size=100000.0)

    # Tuesday rollover (normal 1x)
    t_tue = datetime(2026, 11, 10, 21, 5, 0, tzinfo=timezone.utc)  # Tuesday = 1
    swap_tue = audit_service.calculate_swap(
        lots=1.0,
        point_size=0.00001,
        broker_swap_points=-0.5,
        rollover_time=t_tue,
    )
    # 1.0 * 100000 * 0.00001 * -0.5 * 1.0 = -0.5 USD
    assert swap_tue == -0.5

    # Wednesday rollover (3x multiplier)
    t_wed = datetime(2026, 11, 11, 21, 5, 0, tzinfo=timezone.utc)  # Wednesday = 2
    swap_wed = audit_service.calculate_swap(
        lots=1.0,
        point_size=0.00001,
        broker_swap_points=-0.5,
        rollover_time=t_wed,
    )
    # 1.0 * 100000 * 0.00001 * -0.5 * 3.0 = -1.5 USD
    assert swap_wed == -1.5

    # Record trade with MAE/MFE
    record = audit_service.record_closed_trade(
        trade_id="TRD-EURUSD-001",
        symbol="EURUSD",
        direction="BUY",
        entry_time=t_tue,
        exit_time=t_wed,
        entry_fill=1.08000,
        exit_fill=1.08500,
        lots=1.5,
        slippage_incurred_usd=4.50,
        total_swap_usd=-1.50,
        exit_reason="TP_HIT",
        net_pnl_usd=744.0,
        pip_unit=0.0001,
        mae_pips=5.2,
        mfe_pips=54.8,
    )

    assert record.net_pips == 50.0
    assert record.mae_pips == 5.2
    assert record.mfe_pips == 54.8

    csv_data = audit_service.export_to_csv()
    assert "TRD-EURUSD-001" in csv_data
    assert "EURUSD,BUY" in csv_data
    assert "mae_pips" in csv_data
    assert "mfe_pips" in csv_data
