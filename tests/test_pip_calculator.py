import pytest
from src.goldedge.domain.models.pip_calculator import PipCalculator


def test_pip_unit_standard_pairs():
    assert PipCalculator.get_pip_unit("EURUSD") == 0.0001
    assert PipCalculator.get_pip_unit("GBPUSD") == 0.0001
    assert PipCalculator.get_pip_unit("AUDUSD") == 0.0001
    assert PipCalculator.get_pip_unit("USDCAD") == 0.0001


def test_pip_unit_jpy_pairs():
    assert PipCalculator.get_pip_unit("USDJPY") == 0.01
    assert PipCalculator.get_pip_unit("EURJPY") == 0.01
    assert PipCalculator.get_pip_unit("GBPJPY") == 0.01


def test_pip_unit_gold():
    assert PipCalculator.get_pip_unit("XAUUSD") == 0.10


def test_price_to_pips_conversion():
    # EURUSD: 1.0850 to 1.0865 = 15 pips
    diff = 1.0865 - 1.0850
    pips = PipCalculator.price_to_pips(diff, "EURUSD")
    assert pytest.approx(pips, 0.01) == 15.0

    # USDJPY: 155.20 to 155.50 = 30 pips
    diff_jpy = 155.50 - 155.20
    pips_jpy = PipCalculator.price_to_pips(diff_jpy, "USDJPY")
    assert pytest.approx(pips_jpy, 0.01) == 30.0


def test_calculate_spread_pips():
    # EURUSD: 1.08500 bid, 1.08512 ask = 1.2 pips
    spread = PipCalculator.calculate_spread_pips(1.08500, 1.08512, "EURUSD")
    assert pytest.approx(spread, 0.01) == 1.2

    # USDJPY: 154.500 bid, 154.515 ask = 1.5 pips
    spread_jpy = PipCalculator.calculate_spread_pips(154.500, 154.515, "USDJPY")
    assert pytest.approx(spread_jpy, 0.01) == 1.5
