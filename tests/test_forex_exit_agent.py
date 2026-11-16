import pytest
from datetime import datetime, timezone, timedelta
from src.goldedge.domain.services.forex_kalman_exit_agent import ForexKalmanEntropyExitAgent


def test_exit_agent_initialization_and_update():
    agent = ForexKalmanEntropyExitAgent(pip_unit=0.0001)
    agent.reset(entry_price=1.0800)

    assert agent.peak_price == 1.0800
    assert agent.entropy == 0.0

    # Feed ticks
    res = agent.update(price=1.0805, bid=1.0804, ask=1.0806, direction="BUY")
    assert res is True
    assert agent.peak_price == 1.0805


def test_exit_agent_trailing_stop_in_profit():
    agent = ForexKalmanEntropyExitAgent(pip_unit=0.0001)
    entry_price = 1.0800
    initial_sl = 1.0770  # 30 pip initial SL
    agent.reset(entry_price)

    t0 = datetime.now(timezone.utc)
    # Simulate a 25 pip profit move
    for i in range(20):
        p = entry_price + (i + 1) * 0.00015
        agent.update(price=p, bid=p - 0.00005, ask=p + 0.00005, direction="BUY")

    current_price = 1.0825  # +25 pips
    action, new_sl = agent.decide(
        entry_price=entry_price,
        current_price=current_price,
        current_sl=initial_sl,
        tick_time=t0 + timedelta(seconds=20),
        direction="BUY"
    )

    # Because pnl > 15 pips, trailing stop should tighten
    assert new_sl > initial_sl
    assert action in ("HOLD", "TIGHTEN")


def test_exit_agent_early_exit_on_momentum_collapse():
    agent = ForexKalmanEntropyExitAgent(pip_unit=0.0001)
    entry_price = 1.0850
    initial_sl = 1.0800
    agent.reset(entry_price)

    t0 = datetime.now(timezone.utc)
    # Simulate price dropping by 8 pips with negative velocity
    for i in range(16):
        p = entry_price - (i * 0.00005)
        agent.update(price=p, bid=p - 0.0001, ask=p, direction="BUY")

    # Force exhaustion
    agent.fast_vel_z = -0.5
    agent.slow_vel_z = -0.5

    # First decide: marks weak_start_time
    agent.decide(entry_price, 1.0842, initial_sl, t0, direction="BUY")

    # Second decide after 6 seconds (weak duration >= 5, pnl = -8 pips < -5)
    action, _ = agent.decide(
        entry_price=entry_price,
        current_price=1.0842,  # -8 pips
        current_sl=initial_sl,
        tick_time=t0 + timedelta(seconds=6),
        direction="BUY"
    )

    assert action == "EARLY_EXIT"
    assert agent.exit_reason == "KALMAN_VELOCITY_REVERSAL"


def test_exit_agent_hard_velocity_reversal_shock():
    agent = ForexKalmanEntropyExitAgent(pip_unit=0.0001)
    entry_price = 1.0850
    initial_sl = 1.0800
    agent.reset(entry_price)

    t0 = datetime.now(timezone.utc)
    agent.update(price=1.0848, bid=1.0847, ask=1.0849, direction="BUY")

    # Hard negative velocity shock: v <= -1.8 sigma
    agent.fast_vel_z = -1.9

    action, _ = agent.decide(
        entry_price=entry_price,
        current_price=1.0848,
        current_sl=initial_sl,
        tick_time=t0,
        direction="BUY"
    )

    assert action == "EARLY_EXIT"
    assert agent.exit_reason == "KALMAN_VELOCITY_REVERSAL"


def test_exit_agent_tighten_stop_when_momentum_dies():
    agent = ForexKalmanEntropyExitAgent(pip_unit=0.0001)
    entry_price = 1.0800
    initial_sl = 1.0770  # 30 pip initial SL
    agent.reset(entry_price)

    t0 = datetime.now(timezone.utc)
    # Trade is up +10 pips
    current_price = 1.0810
    agent.update(price=current_price, bid=current_price - 0.00005, ask=current_price + 0.00005, direction="BUY")

    # Velocity dies down (|v| <= 0.15)
    agent.fast_vel_z = 0.05

    action, new_sl = agent.decide(
        entry_price=entry_price,
        current_price=current_price,
        current_sl=initial_sl,
        tick_time=t0,
        direction="BUY"
    )

    assert action == "TIGHTEN"
    assert agent.exit_reason == "KALMAN_TIGHTEN"
    # Guaranteed lock-in above initial SL
    assert new_sl > initial_sl

