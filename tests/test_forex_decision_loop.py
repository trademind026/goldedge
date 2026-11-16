import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from src.goldedge.domain.models.signal import Signal, SignalDirection
from src.goldedge.domain.models.bar import Tick, Bar
from src.goldedge.domain.models.trade import Position, TradeStatus
from src.goldedge.domain.services.forex_decision_rules import ForexDecisionRules
from src.goldedge.application.use_cases.forex_decision_loop import ForexDecisionLoopUseCase


def test_forex_decision_rules_gates():
    rules = ForexDecisionRules(
        min_kalman_vel_z=0.5,
        min_meta_prob=0.62,
        max_spread_pips=1.8
    )

    sig_buy = Signal(
        strategy="bullish_momentum",
        direction=SignalDirection.BUY,
        symbol="EURUSD",
        entry_price=1.0850,
        technical_strength=0.9
    )

    # 1. Rejection: Spread >= 1.8 pips
    res1 = rules.evaluate(sig_buy, kalman_vel_z=0.8, meta_probability=0.70, spread_pips=2.2)
    assert not res1.approved
    assert any("Live spread" in r for r in res1.rejection_reasons)

    # 2. Rejection: Kalman Velocity Z <= 0.5 in signal direction
    res2 = rules.evaluate(sig_buy, kalman_vel_z=0.3, meta_probability=0.70, spread_pips=1.2)
    assert not res2.approved
    assert any("Kalman Velocity Z-score" in r for r in res2.rejection_reasons)

    # 3. Rejection: Meta probability <= 0.62
    res3 = rules.evaluate(sig_buy, kalman_vel_z=0.8, meta_probability=0.55, spread_pips=1.2)
    assert not res3.approved
    assert any("Meta-Model Calibrated Win Probability" in r for r in res3.rejection_reasons)

    # 4. Approval: All gates pass
    res4 = rules.evaluate(sig_buy, kalman_vel_z=0.8, meta_probability=0.68, spread_pips=1.2)
    assert res4.approved
    assert len(res4.rejection_reasons) == 0

    # 5. Rejection: Acceleration Gate (negative acceleration on BUY candidate)
    accel_rules = ForexDecisionRules(min_kalman_vel_z=0.5, require_acceleration=True)
    res5 = accel_rules.evaluate(sig_buy, kalman_vel_z=0.8, meta_probability=0.70, spread_pips=1.0, kalman_accel=-0.005)
    assert not res5.approved
    assert any("Kalman Acceleration" in r for r in res5.rejection_reasons)

    # 6. Rejection: Shannon Entropy Chop Lockout (H >= 3.4)
    res6 = rules.evaluate(sig_buy, kalman_vel_z=0.8, meta_probability=0.70, spread_pips=1.0, entropy=3.6)
    assert not res6.approved
    assert any("CHOP LOCKOUT" in r for r in res6.rejection_reasons)



def test_forex_decision_loop_handoff_to_exit_agent():
    mock_broker = MagicMock()
    mock_market_data = MagicMock()
    mock_candle_repo = MagicMock()
    mock_meta_labeler = MagicMock()
    mock_execute_trade_uc = MagicMock()
    mock_manage_position_uc = MagicMock()

    t0 = datetime(2026, 11, 10, 14, 0, 0, tzinfo=timezone.utc)
    mock_market_data.get_current_tick.return_value = Tick(
        symbol="EURUSD",
        time=t0,
        bid=1.08500,
        ask=1.08512,  # 1.2 pips
        last=1.08506,
        volume=150
    )

    # Return 60 bars with unique timestamps so indicator calculation works
    bars = []
    for i in range(60):
        p = 1.0800 + (i * 0.0001)
        bars.append(Bar(
            symbol="EURUSD",
            time=t0 - timedelta(minutes=60 - i),
            open=p,
            high=p + 0.0002,
            low=p - 0.0001,
            close=p + 0.0001,
            tick_volume=100
        ))
    mock_candle_repo.get_latest_bars.return_value = bars

    # Meta-labeler high win probability
    mock_meta_labeler.predict_probability.return_value = 0.75

    # Manage position open positions
    open_pos = Position(
        ticket=998877,
        symbol="EURUSD",
        type="BUY",
        volume=0.1,
        price_open=1.0850,
        price_sl=1.0820,
        price_tp=1.0890,
        time_setup=t0,
        status=TradeStatus.OPEN
    )
    mock_manage_position_uc.trade_repo.get_positions_by_status.return_value = [open_pos]
    mock_exit_agent = MagicMock()
    mock_manage_position_uc.get_or_create_exit_agent.return_value = mock_exit_agent

    decision_loop = ForexDecisionLoopUseCase(
        broker=mock_broker,
        market_data=mock_market_data,
        candle_repo=mock_candle_repo,
        meta_labeler=mock_meta_labeler,
        execute_trade_uc=mock_execute_trade_uc,
        manage_position_uc=mock_manage_position_uc
    )

    # Mock kalman update to return positive velocity z-score (> 0.5)
    kf = decision_loop.get_or_create_kalman("EURUSD")
    kf.update = MagicMock(return_value=(1.08506, 0.0005, 1.25))

    candidate_signal = Signal(
        strategy="bullish_momentum",
        direction=SignalDirection.BUY,
        symbol="EURUSD",
        entry_price=1.0850,
        technical_strength=1.0
    )

    with patch(
        "src.goldedge.application.use_cases.forex_decision_loop.SignalGeneratorService.generate_signals",
        return_value=[candidate_signal]
    ):
        ticket = decision_loop.evaluate_and_execute("EURUSD")

    # Verify execution and exit agent handoff
    assert mock_execute_trade_uc.execute.called
    assert mock_manage_position_uc.get_or_create_exit_agent.called
    assert mock_exit_agent.reset.called
    assert ticket == 998877
