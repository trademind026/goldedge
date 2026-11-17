import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ...domain.ports.outbound.broker import IBrokerAdapter
from ...domain.ports.outbound.candle_repo import ICandleRepository
from ...domain.ports.outbound.event_bus import IEventBus
from ...domain.ports.outbound.expected_pnl_model import IExpectedPnlModel
from ...domain.ports.outbound.market_data import IMarketDataProvider
from ...domain.ports.outbound.shadow_journal import IShadowJournal
from ...domain.ports.outbound.trade_repo import ITradeRepository
from ...domain.services.denoise_service import DenoiseService
from ...domain.services.indicator_service import IndicatorService
from ...domain.services.mtf_trend import (
    aggregate_ohlcv_bars,
    compute_htf_ema_state,
    passes_regime_filters,
)
from ...domain.services.regime_detector import RegimeDetectorService
from ...domain.services.shadow_tracker import ShadowValidationTracker
from ...domain.services.signal_generator import SignalGeneratorService
from ...domain.services.sizing_service import PositionSizingService
from ...domain.services.trade_scorer import TradeScorer
from ...domain.models.signal import ScoredSignal
from ...config.settings import settings
from ...config.risk_profiles import get_profile
from ..services.execution_context import resolve_execution_context

from .execute_trade import ExecuteTradeUseCase

logger = logging.getLogger(__name__)


def _bar_utc_time(bar: Any) -> Optional[datetime]:
    ts = bar.get("time") if isinstance(bar, dict) else getattr(bar, "time", None)
    if ts is None:
        return None
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _floor_5m_utc(dt: Optional[datetime]) -> datetime:
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    floored_epoch = (int(dt.timestamp()) // 300) * 300
    return datetime.fromtimestamp(floored_epoch, tz=timezone.utc)


class ScanMarketUseCase:
    """
    Core trading loop. Aggregates M1 bars into M5 entry candles + H1 trend filter,
    computes indicators, detects market regime, and generates signals.
    """
    def __init__(
        self,
        candle_repo: ICandleRepository,
        event_bus: IEventBus,
        execute_trade_uc: ExecuteTradeUseCase,
        broker: Optional[IBrokerAdapter] = None,
        trade_repo: Optional[ITradeRepository] = None,
        market_data: Optional[IMarketDataProvider] = None,
        expected_pnl_model: Optional[IExpectedPnlModel] = None,
        min_expected_pnl: Optional[float] = None,
        sizing_service: Optional[PositionSizingService] = None,
        shadow_mode: Optional[bool] = None,
        shadow_journal: Optional[IShadowJournal] = None,
        shadow_tracker: Optional[ShadowValidationTracker] = None,
    ):
        self.candle_repo = candle_repo
        self.event_bus = event_bus
        self.execute_trade_uc = execute_trade_uc
        self.broker = broker
        self.trade_repo = trade_repo
        self.market_data = market_data
        self.expected_pnl_model = expected_pnl_model
        self.min_expected_pnl = min_expected_pnl
        self.sizing_service = sizing_service or PositionSizingService(contract_size=100.0)
        self.shadow_mode = settings.SHADOW_MODE if shadow_mode is None else bool(shadow_mode)
        self.shadow_journal = shadow_journal
        self.shadow_tracker = shadow_tracker or ShadowValidationTracker(contract_size=100.0)

        self.regime_detector = RegimeDetectorService()
        self.trade_scorer = TradeScorer(w_ml=0.0, w_quant=1.0)
        profile_name = "MEDIUM_BACKTEST" if settings.TRADING_MODE == "backtest" else "MEDIUM_LIVE"
        self.active_profile = get_profile(profile_name)

    def _emit_shadow(self, telemetry: Dict[str, Any]) -> None:
        if self.shadow_journal is not None:
            self.shadow_journal.record_event(telemetry)

    def execute(
        self,
        symbol: str,
        kalman_features: Optional[dict] = None,
        live_quote: Optional[dict] = None,
        as_of_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Called at each 5m bar rollover (and optionally manually via API).
        Evaluates ONLY closed M5 and closed H1 candles with frozen Experiment E rules.
        When `shadow_mode` is True, keeps real broker order placement strictly OFF
        and emits structured rollover telemetry.
        """
        kalman_features = dict(kalman_features or {})
        cutoff_5m = _floor_5m_utc(as_of_time)
        cutoff_1h = cutoff_5m.replace(minute=0, second=0, microsecond=0)

        already_logged = False
        if self.shadow_mode and self.shadow_journal is not None:
            if not getattr(self.shadow_tracker, "restored", True):
                self.shadow_tracker.restore_from_events(self.shadow_journal.read_all())
            already_logged = self.shadow_journal.has_rollover(symbol, cutoff_5m.isoformat())

        # Executable quote resolution (live tick preferred)
        live_bid = float(live_quote["bid"]) if live_quote and live_quote.get("bid") else None
        live_ask = float(live_quote["ask"]) if live_quote and live_quote.get("ask") else None
        if (live_bid is None or live_ask is None) and self.market_data is not None:
            try:
                tick = self.market_data.get_current_tick(symbol)
                if tick and tick.bid and tick.ask:
                    live_bid = float(tick.bid)
                    live_ask = float(tick.ask)
            except Exception:
                pass
        spread = round(live_ask - live_bid, 5) if (live_bid is not None and live_ask is not None) else None

        # 1. Load M1 bars & enforce strict closed-candle boundaries (< cutoff_5m)
        bars_1m_all = self.candle_repo.get_latest_bars(symbol, count=4500)
        closed_1m = [b for b in bars_1m_all if (bt := _bar_utc_time(b)) is not None and bt < cutoff_5m]
        bars_5m = [b for b in aggregate_ohlcv_bars(closed_1m, 5) if b["time"] < cutoff_5m]
        bars_1h = [b for b in aggregate_ohlcv_bars(closed_1m, 60) if b["time"] < cutoff_1h]

        if len(bars_5m) < 60:
            telemetry = {
                "event": "SHADOW_ROLLOVER",
                "status": "INSUFFICIENT_5M_BARS",
                "symbol": symbol,
                "rollover_utc": cutoff_5m.isoformat(),
                "bars_5m_count": len(bars_5m),
                "bars_1h_count": len(bars_1h),
                "kalman_price": kalman_features.get("kalman_price"),
                "kalman_vel": kalman_features.get("kalman_vel"),
                "kalman_accel": kalman_features.get("kalman_accel"),
                "bid": live_bid,
                "ask": live_ask,
                "spread": spread,
                "shadow_mode": self.shadow_mode,
                "order_placed": False,
            }
            logger.info(f"[SHADOW_ROLLOVER] {symbol} {cutoff_5m.isoformat()} | status=INSUFFICIENT_5M_BARS ({len(bars_5m)}/60)")
            if self.shadow_mode:
                self._emit_shadow(telemetry)
            return telemetry

        closed_5m_ts = bars_5m[-1]["time"]
        closed_1h_ts = bars_1h[-1]["time"] if bars_1h else None

        # 2. Compute H1 directional EMA20/EMA50 trend on closed H1 bars
        htf_trend, h1_ema20, h1_ema50 = compute_htf_ema_state([b["close"] for b in bars_1h])

        # 3. Compute Technical Indicators on closed 5m bars
        df = IndicatorService.compute_indicators(bars_5m)
        if df.empty:
            telemetry = {
                "event": "SHADOW_ROLLOVER",
                "status": "EMPTY_INDICATORS",
                "symbol": symbol,
                "rollover_utc": cutoff_5m.isoformat(),
                "closed_5m_ts": closed_5m_ts.isoformat(),
                "shadow_mode": self.shadow_mode,
                "order_placed": False,
            }
            if self.shadow_mode:
                self._emit_shadow(telemetry)
            return telemetry

        last_row = df.iloc[-1]
        latest_atr = float(last_row.get("atr", 0.0) or 0.0)
        latest_rsi = float(last_row.get("rsi", 50.0) or 50.0)
        latest_vwap = float(last_row.get("vwap", 0.0) or 0.0)
        latest_close = float(last_row.get("close", 0.0) or 0.0)
        ema20_5m = float(last_row.get("ema20", 0.0) or 0.0)
        ema50_5m = float(last_row.get("ema50", 0.0) or 0.0)
        ema20_slope = float(last_row.get("ema20_slope", 0.0) or 0.0)
        vwap_dist = float((latest_close - latest_vwap) / latest_close) if latest_close else 0.0
        dist_to_vwap_atr = float((latest_close - latest_vwap) / latest_atr) if latest_atr > 1e-9 else 0.0
        w20_low = float(df["low"].iloc[-21:-1].min()) if len(df) >= 21 else float(df["low"].min())
        dist_to_swing_low_atr = float((latest_close - w20_low) / latest_atr) if latest_atr > 1e-9 else 0.0
        vol_ratio = float(last_row.get("volume_ratio", 1.0) or 1.0)
        atr_tail = df["atr"].dropna().tail(20)
        atr_mean_20 = float(atr_tail.mean()) if len(atr_tail) > 0 else latest_atr
        atr_ratio = float(latest_atr / atr_mean_20) if atr_mean_20 > 1e-9 else 1.0

        returns_series = df["close"].pct_change().dropna()
        shannon_entropy = DenoiseService.compute_rolling_entropy(returns_series)
        kaufman_er = DenoiseService.compute_kaufman_er(df["close"])

        # Enrich features with indicator state for parity with RunBacktestUseCase TradeScorer
        score_features = {
            **kalman_features,
            "shannon_entropy": shannon_entropy,
            "kaufman_er": kaufman_er,
            "ema20_slope": ema20_slope,
            "vwap_dist": vwap_dist,
            "volume_ratio": vol_ratio,
            "utc_hour": closed_5m_ts.hour,
        }

        # 4. Detect Market Regime (deterministically warm hysteresis on trailing closed 5m bars)
        #    so Rollover #1 after any backend restart matches continuous replay.
        detector = RegimeDetectorService()
        start_idx = min(len(df), max(50, len(df) - 60))
        regime = detector.detect(df.iloc[:start_idx])
        for idx in range(start_idx + 1, len(df) + 1):
            regime = detector.detect(df.iloc[:idx])
        self.regime_detector = detector
        raw_signals = SignalGeneratorService.generate_signals(df, symbol, regime)

        # Rank all raw candidates even below min_score_threshold so rollover telemetry stays complete
        ranked_all = (
            self.trade_scorer.filter_and_rank(
                raw_signals,
                min_score_threshold=0.0,
                regime=regime.value,
                features=score_features,
            )
            if raw_signals
            else []
        )

        # 5. Evaluate Score, H1 Trend, and Regime Filters (Experiment E + SELL unchanged)
        approved_signals = []
        ridge_by_id: Dict[int, Optional[float]] = {}
        primary_candidate = ranked_all[0] if ranked_all else None
        gate_reason = "NO_CANDIDATE"
        ridge_pred_pnl: Optional[float] = None

        for st in ranked_all:
            dir_val = st.direction.value if hasattr(st.direction, "value") else str(st.direction)
            ridge_pred = None
            if self.expected_pnl_model is not None:
                hour_angle = 2.0 * math.pi * (float(closed_5m_ts.hour) / 24.0)
                ridge_pred = self.expected_pnl_model.predict_expected_pnl({
                    "rsi": latest_rsi,
                    "atr_ratio": atr_ratio,
                    "volume_ratio": vol_ratio,
                    "ema20_slope": ema20_slope,
                    "ema_spread": float((ema20_5m - ema50_5m) / ema50_5m) if ema50_5m else 0.0,
                    "vwap_dist": vwap_dist,
                    "kalman_velocity": float(kalman_features.get("kalman_vel", 0.0) or 0.0),
                    "spread_mean": float(spread if spread is not None else 0.07),
                    "hour_sin": math.sin(hour_angle),
                    "hour_cos": math.cos(hour_angle),
                    "quant_score": float(st.final_score),
                })
                ridge_by_id[id(st)] = ridge_pred
                if st is primary_candidate:
                    ridge_pred_pnl = ridge_pred

            reason = None
            if st.final_score < self.active_profile.min_score_threshold:
                reason = f"SCORE_BELOW_{self.active_profile.min_score_threshold:.2f}"
            elif (dir_val == "BUY" and htf_trend != 1) or (dir_val == "SELL" and htf_trend != -1):
                reason = f"H1_TREND_GATE(trend={htf_trend})"
            elif not passes_regime_filters(
                dir_val,
                regime.value,
                block_high_volatility=self.active_profile.block_high_volatility,
                block_buy_sideways=self.active_profile.block_buy_sideways,
            ):
                reason = f"REGIME_FILTER({regime.value})"
            elif ridge_pred is not None and self.min_expected_pnl is not None and ridge_pred <= self.min_expected_pnl:
                reason = f"ML_FILTER({ridge_pred:+.2f})"

            if reason is None:
                approved_signals.append((st, ridge_pred))
                if st is primary_candidate:
                    gate_reason = "APPROVED"
            else:
                if st is primary_candidate:
                    gate_reason = reason

        # Hypothetical SL/TP/Lots on primary candidate or approved signal around executable bid/ask
        chosen_st, chosen_ridge = approved_signals[0] if approved_signals else (primary_candidate, ridge_pred_pnl)
        chosen_dir = (
            (chosen_st.direction.value if hasattr(chosen_st.direction, "value") else str(chosen_st.direction))
            if chosen_st is not None
            else "NONE"
        )
        exec_price = None
        if chosen_dir in ("BUY", "SELL"):
            if live_bid is not None and live_ask is not None:
                exec_price = self.sizing_service.resolve_executable_entry(chosen_dir, live_bid, live_ask)
            else:
                exec_price = live_ask if chosen_dir == "BUY" else live_bid

        shadow_lots: Optional[float] = None
        shadow_sl: Optional[float] = None
        shadow_tp: Optional[float] = None
        size_err: Optional[str] = None
        planned_risk_usd: Optional[float] = None
        planned_risk_pct: Optional[float] = None
        if chosen_st is not None and exec_price is not None and latest_atr > 0:
            shadow_lots, shadow_sl, shadow_tp, size_err = self.sizing_service.calculate_size(
                account_balance=self.shadow_tracker.equity,
                risk_profile=self.active_profile,
                entry_price=float(exec_price),
                atr_value=latest_atr,
                direction=chosen_st.signal.direction,
                symbol=symbol,
            )
            if shadow_lots and shadow_sl is not None:
                planned_risk_usd = round(
                    abs(float(exec_price) - float(shadow_sl)) * float(shadow_lots) * self.sizing_service.contract_size,
                    4,
                )
                planned_risk_pct = round(
                    (planned_risk_usd / max(self.shadow_tracker.equity, 1e-6)) * 100.0,
                    4,
                )

        # Shadow vs Live Execution Path
        virtual_event = None
        if self.shadow_mode:
            if not already_logged:
                for st in ranked_all:
                    d_str = st.direction.value if hasattr(st.direction, "value") else str(st.direction)
                    r_pred = ridge_by_id.get(id(st))
                    is_app = any(st is ast for ast, _ in approved_signals)
                    self.shadow_tracker.record_candidate(symbol, d_str, approved=is_app)
                    if r_pred is not None:
                        logger.info(f"[SHADOW_RIDGE] {symbol} {d_str} score={st.final_score:.4f} ridge_pred=${r_pred:+.2f}")

                if approved_signals and chosen_st is not None and shadow_lots and shadow_sl and shadow_tp and exec_price:
                    virtual_event = self.shadow_tracker.open_virtual_position(
                        symbol=symbol,
                        direction=chosen_dir,
                        entry_price=float(exec_price),
                        sl=float(shadow_sl),
                        tp=float(shadow_tp),
                        lots=float(shadow_lots),
                        atr=latest_atr,
                        score=float(chosen_st.final_score),
                        regime=regime.value,
                        opened_at=cutoff_5m,
                        ridge_pred_pnl=chosen_ridge,
                        kalman_velocity=round(float(kalman_features.get("kalman_vel", 0.0) or 0.0), 6),
                        kalman_acceleration=round(float(kalman_features.get("kalman_accel", 0.0) or 0.0), 6),
                        distance_to_swing_low_atr=round(dist_to_swing_low_atr, 4),
                        distance_to_vwap_atr=round(dist_to_vwap_atr, 4),
                        planned_risk_usd=planned_risk_usd,
                        planned_risk_pct=planned_risk_pct,
                    )

            telemetry = {
                "event": "SHADOW_ROLLOVER",
                "status": "APPROVED_SHADOW" if approved_signals else gate_reason,
                "symbol": symbol,
                "rollover_utc": cutoff_5m.isoformat(),
                "closed_5m_ts": closed_5m_ts.isoformat(),
                "closed_5m_close": round(latest_close, 4),
                "closed_1h_ts": closed_1h_ts.isoformat() if closed_1h_ts else None,
                "kalman_price": round(float(kalman_features["kalman_price"]), 5) if kalman_features.get("kalman_price") is not None else None,
                "kalman_vel": round(float(kalman_features.get("kalman_vel", 0.0) or 0.0), 6),
                "kalman_accel": round(float(kalman_features.get("kalman_accel", 0.0) or 0.0), 6),
                "h1_trend": htf_trend,
                "h1_ema20": round(h1_ema20, 4) if h1_ema20 is not None else None,
                "h1_ema50": round(h1_ema50, 4) if h1_ema50 is not None else None,
                "rsi": round(latest_rsi, 3),
                "vwap": round(latest_vwap, 4),
                "atr": round(latest_atr, 4),
                "distance_to_swing_low_atr": round(dist_to_swing_low_atr, 4),
                "distance_to_vwap_atr": round(dist_to_vwap_atr, 4),
                "kaufman_er": round(kaufman_er, 4),
                "shannon_entropy": round(shannon_entropy, 4),
                "regime": regime.value,
                "strategy": chosen_st.strategy if chosen_st else None,
                "direction": chosen_dir,
                "score": round(float(chosen_st.final_score), 4) if chosen_st else None,
                "gate_reason": gate_reason,
                "bid": live_bid,
                "ask": live_ask,
                "spread": spread,
                "exec_price": exec_price,
                "sl": round(shadow_sl, 4) if shadow_sl is not None else None,
                "tp": round(shadow_tp, 4) if shadow_tp is not None else None,
                "lots": shadow_lots,
                "planned_risk_usd": planned_risk_usd,
                "planned_risk_pct": planned_risk_pct,
                "size_err": size_err,
                "ridge_pred_pnl": round(float(chosen_ridge), 4) if chosen_ridge is not None else None,
                "in_15utc_window": cutoff_5m.hour < 15,
                "shadow_mode": True,
                "order_placed": False,
                "duplicate_skipped": already_logged,
                "virtual_event": virtual_event,
                "buy_sell_summary": self.shadow_tracker.summary(symbol),
            }
            if not already_logged:
                logger.info(
                    f"[SHADOW_ROLLOVER] {symbol} bar_5m={closed_5m_ts.strftime('%Y-%m-%dT%H:%MZ')} "
                    f"k_p={telemetry['kalman_price']} k_v={telemetry['kalman_vel']} k_a={telemetry['kalman_accel']} | "
                    f"h1={htf_trend}(ema20={telemetry['h1_ema20']},ema50={telemetry['h1_ema50']}) | "
                    f"rsi={telemetry['rsi']} vwap={telemetry['vwap']} atr={telemetry['atr']} regime={regime.value} | "
                    f"dir={chosen_dir} score={telemetry['score']} gate={gate_reason} | "
                    f"bid={live_bid} ask={live_ask} sl={telemetry['sl']} tp={telemetry['tp']} "
                    f"ridge={telemetry['ridge_pred_pnl']} order_placed=False"
                )
                self._emit_shadow(telemetry)
                if virtual_event is not None:
                    self._emit_shadow(virtual_event)
            else:
                logger.debug(f"[SHADOW_DEDUP] Skipped duplicate rollover write for {symbol} @ {cutoff_5m.isoformat()}")
            return telemetry

        # Non-shadow live execution path (explicitly disabled when SHADOW_MODE=True)
        scored_signals = [st for st, _ in approved_signals]
        if not scored_signals:
            return {"event": "LIVE_ROLLOVER", "status": gate_reason, "symbol": symbol, "order_placed": False}

        if self.broker is None or self.trade_repo is None:
            logger.error("ScanMarketUseCase requires broker and trade_repo to resolve live execution context.")
            return {"event": "LIVE_ROLLOVER", "status": "BROKER_OR_REPO_MISSING", "symbol": symbol, "order_placed": False}

        ctx = resolve_execution_context(
            symbol=symbol,
            broker=self.broker,
            trade_repo=self.trade_repo,
            market_data=self.market_data,
        )
        if ctx is None:
            logger.warning(f"Skipping execution for {symbol}: live execution context unavailable.")
            return {"event": "LIVE_ROLLOVER", "status": "CONTEXT_UNAVAILABLE", "symbol": symbol, "order_placed": False}

        for scored_trade in scored_signals:
            logger.info(f"Signal Approved by Scorer: {scored_trade.strategy} | {scored_trade.direction} | Score: {scored_trade.final_score}")

            if live_bid is None or live_ask is None:
                logger.warning(f"Skipping live execution for {symbol}: executable bid/ask quote unavailable.")
                return {"event": "LIVE_ROLLOVER", "status": "QUOTE_UNAVAILABLE", "symbol": symbol, "order_placed": False}

            exec_entry = self.sizing_service.resolve_executable_entry(
                scored_trade.direction, live_bid, live_ask
            )
            scored_sig = ScoredSignal(
                signal=scored_trade.signal,
                score=scored_trade.final_score,
                ml_probability=scored_trade.ml_probability
            )

            self.execute_trade_uc.execute(
                scored_signal=scored_sig,
                current_spread=ctx.current_spread_pips,
                recent_median_spread=ctx.recent_median_spread_pips,
                account_balance=ctx.account_balance,
                current_daily_loss_pct=ctx.current_daily_loss_pct,
                current_streak=ctx.current_streak,
                entry_price=exec_entry,
                atr_value=latest_atr
            )
            break

        return {"event": "LIVE_ROLLOVER", "status": "EXECUTED", "symbol": symbol, "order_placed": True}
