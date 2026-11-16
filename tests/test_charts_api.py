import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock
from flask import Flask

from src.goldedge.adapters.primary.rest_api.routes.charts import bp as charts_bp
from src.goldedge.domain.models.bar import Bar
from src.goldedge.config.container import Container
from src.goldedge.adapters.secondary.in_memory_event_bus import InMemoryEventBus


@pytest.fixture
def mock_bars():
    base_time = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
    bars = []
    base_price = 1.0850
    for i in range(100):
        t = base_time + timedelta(minutes=i)
        p = base_price + (i * 0.0001)
        bars.append(
            Bar(
                symbol="EURUSD",
                time=t,
                open=p,
                high=p + 0.0003,
                low=p - 0.0002,
                close=p + 0.0001,
                tick_volume=150 + (i % 20),
                spread=10
            )
        )
    return bars


def test_charts_api_returns_404_when_no_data():
    """Verify that absent data strictly returns 404 with ZERO synthetic fallbacks."""
    app = Flask(__name__)
    app.register_blueprint(charts_bp)

    container = Container(
        db_url="sqlite:///:memory:",
        event_bus=InMemoryEventBus(),
        config={"BROKER_TYPE": "mt5"}
    )
    mock_repo = MagicMock()
    mock_repo.get_latest_bars.return_value = []
    container.candle_repo.override(mock_repo)

    mock_broker = MagicMock()
    mock_broker.fetch_historical_bars.return_value = []
    container.market_data_provider.override(mock_broker)

    container.wire(modules=["src.goldedge.adapters.primary.rest_api.routes.charts"])

    client = app.test_client()
    resp = client.get("/api/charts/data?symbol=EURUSD&timeframe=1m")
    assert resp.status_code == 404
    data = resp.get_json()
    assert "error" in data
    assert "No candle data available" in data["error"]

    container.unwire()


def test_charts_api_with_real_bars(mock_bars):
    """Verify chart data response contains Kalman, Entropy, ATR, and Session telemetry."""
    app = Flask(__name__)
    app.register_blueprint(charts_bp)

    container = Container(
        db_url="sqlite:///:memory:",
        event_bus=InMemoryEventBus(),
        config={"BROKER_TYPE": "mt5"}
    )
    mock_repo = MagicMock()
    mock_repo.get_latest_bars.return_value = mock_bars
    container.candle_repo.override(mock_repo)

    mock_broker = MagicMock()
    container.market_data_provider.override(mock_broker)

    container.wire(modules=["src.goldedge.adapters.primary.rest_api.routes.charts"])

    client = app.test_client()
    resp = client.get("/api/charts/data?symbol=EURUSD&timeframe=1m&limit=50")
    assert resp.status_code == 200
    data = resp.get_json()

    assert data["symbol"] == "EURUSD"
    assert data["timeframe"] == "1m"
    assert data["pip_unit"] == 0.0001
    assert "current_quote" in data
    assert "bars" in data
    assert len(data["bars"]) == 50

    latest_bar = data["bars"][-1]
    assert "kalman_price" in latest_bar
    assert "kalman_velocity_z" in latest_bar
    assert "entropy" in latest_bar
    assert "order_flow_momentum" in latest_bar
    assert "session" in latest_bar
    assert latest_bar["session"] in ("LONDON_NY_OVERLAP", "LONDON", "NEW_YORK", "ROLLOVER", "TOKYO", "SYDNEY")

    stats = data["statistics"]
    assert "volatility_atr_pips" in stats
    assert "current_regime" in stats
    assert "current_kalman_velocity" in stats
    assert "current_entropy" in stats

    container.unwire()


def test_charts_api_gold_symbol(mock_bars):
    """Verify XAUUSD chart response correctly applies Gold pip unit and Kalman."""
    gold_bars = []
    for b in mock_bars:
        gold_bars.append(
            Bar(
                symbol="XAUUSD",
                time=b.time,
                open=b.open * 2200.0,
                high=b.high * 2200.0,
                low=b.low * 2200.0,
                close=b.close * 2200.0,
                tick_volume=b.tick_volume,
                spread=25
            )
        )

    app = Flask(__name__)
    app.register_blueprint(charts_bp)

    container = Container(
        db_url="sqlite:///:memory:",
        event_bus=InMemoryEventBus(),
        config={"BROKER_TYPE": "mt5"}
    )
    mock_repo = MagicMock()
    mock_repo.get_latest_bars.return_value = gold_bars
    container.candle_repo.override(mock_repo)

    mock_broker = MagicMock()
    container.market_data_provider.override(mock_broker)

    container.wire(modules=["src.goldedge.adapters.primary.rest_api.routes.charts"])

    client = app.test_client()
    resp = client.get("/api/charts/data?symbol=XAUUSD&timeframe=5m&limit=30")
    assert resp.status_code == 200
    data = resp.get_json()

    assert data["symbol"] == "XAUUSD"
    assert data["pip_unit"] == 0.10
    assert len(data["bars"]) > 0

    container.unwire()
