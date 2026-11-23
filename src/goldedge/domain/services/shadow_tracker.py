from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
import math


def _parse_iso_utc(val: Any) -> datetime:
    if isinstance(val, datetime):
        return val if val.tzinfo is not None else val.replace(tzinfo=timezone.utc)
    s = str(val)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _floor_lots(lots: float, step: float = 0.01, min_lot: float = 0.01) -> float:
    floored = round(math.floor((lots + 1e-12) / step) * step, 2)
    return max(min_lot, floored) if lots >= min_lot else 0.0


def _evaluate_hwm_risk_alerts(
    mtm_dd_pct: float,
    daily_loss_pct: float,
    weekly_loss_pct: float,
) -> List[str]:
    alerts: List[str] = []
    if mtm_dd_pct >= 10.0:
        alerts.append("EMERGENCY_STOP_10PCT")
    elif mtm_dd_pct >= 8.0:
        alerts.append("HARD_STOP_8PCT")
    elif mtm_dd_pct >= 5.0:
        alerts.append("WARNING_DD_5PCT")
    if daily_loss_pct >= 1.5:
        alerts.append("DAILY_LOSS_1_5PCT")
    if weekly_loss_pct >= 3.0:
        alerts.append("WEEKLY_LOSS_3PCT")
    return alerts


@dataclass
class _DirectionStats:
    signals_raw: int = 0
    signals_approved: int = 0
    signals_blocked: int = 0
    trades_closed: int = 0
    wins: int = 0
    losses: int = 0
    pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    equity: float = 10000.0
    peak_equity: float = 10000.0
    max_drawdown: float = 0.0
    mtm_equity: float = 10000.0
    mtm_peak_equity: float = 10000.0
    mtm_max_drawdown: float = 0.0
    mtm_max_drawdown_pct: float = 0.0
    sl_hits: int = 0
    tp_hits: int = 0
    be_hits: int = 0
    trail_hits: int = 0
    time_squeeze_hits: int = 0
    e1_15m_triggered: int = 0

    def update_mtm(self, floating_pnl: float) -> None:
        self.mtm_equity = round(self.equity + floating_pnl, 4)
        if self.mtm_equity > self.mtm_peak_equity:
            self.mtm_peak_equity = self.mtm_equity
        dd = round(self.mtm_equity - self.mtm_peak_equity, 4)
        if dd < self.mtm_max_drawdown:
            self.mtm_max_drawdown = dd
        if self.mtm_peak_equity > 0:
            pct = max(0.0, (self.mtm_peak_equity - self.mtm_equity) / self.mtm_peak_equity * 100.0)
            if pct > self.mtm_max_drawdown_pct:
                self.mtm_max_drawdown_pct = round(pct, 4)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["win_rate"] = round((self.wins / self.trades_closed) * 100.0, 2) if self.trades_closed else 0.0
        d["profit_factor"] = round(self.gross_profit / self.gross_loss, 2) if self.gross_loss > 0 else (None if self.gross_profit == 0 else 999.0)
        d["expectancy_usd"] = round(self.pnl / self.trades_closed, 4) if self.trades_closed else 0.0
        return d


@dataclass
class _PortfolioStats:
    """Stats & HWM drawdown tracker for one independent virtual shadow portfolio."""
    name: str
    signals_considered: int = 0
    signals_skipped_rule: int = 0
    trades_closed: int = 0
    wins: int = 0
    losses: int = 0
    pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    equity: float = 10000.0
    peak_equity: float = 10000.0
    closed_max_drawdown: float = 0.0
    closed_max_drawdown_pct: float = 0.0
    mtm_equity: float = 10000.0
    mtm_peak_equity: float = 10000.0
    mtm_max_drawdown: float = 0.0
    mtm_max_drawdown_pct: float = 0.0
    cur_day: Optional[str] = None
    daily_pnl: float = 0.0
    cur_week: Optional[str] = None
    weekly_pnl: float = 0.0
    sl_hits: int = 0
    tp_hits: int = 0
    be_hits: int = 0
    trail_hits: int = 0
    time_squeeze_hits: int = 0
    e1_exits: int = 0

    def roll_periods(self, ts: datetime) -> None:
        d_key = ts.strftime("%Y-%m-%d")
        iso_cal = ts.isocalendar()
        w_key = f"{iso_cal.year}-W{iso_cal.week:02d}"
        if self.cur_day != d_key:
            self.cur_day = d_key
            self.daily_pnl = 0.0
        if self.cur_week != w_key:
            self.cur_week = w_key
            self.weekly_pnl = 0.0

    def update_mtm(self, floating_pnl: float, ts: datetime) -> None:
        self.roll_periods(ts)
        self.mtm_equity = round(self.equity + floating_pnl, 4)
        if self.mtm_equity > self.mtm_peak_equity:
            self.mtm_peak_equity = self.mtm_equity
        dd = round(self.mtm_equity - self.mtm_peak_equity, 4)
        if dd < self.mtm_max_drawdown:
            self.mtm_max_drawdown = dd
        if self.mtm_peak_equity > 0:
            pct = max(0.0, (self.mtm_peak_equity - self.mtm_equity) / self.mtm_peak_equity * 100.0)
            if pct > self.mtm_max_drawdown_pct:
                self.mtm_max_drawdown_pct = round(pct, 4)

    def current_hwm_dd_pct(self) -> float:
        if self.mtm_peak_equity <= 0:
            return 0.0
        return round(max(0.0, (self.mtm_peak_equity - self.mtm_equity) / self.mtm_peak_equity * 100.0), 4)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["win_rate"] = round((self.wins / self.trades_closed) * 100.0, 2) if self.trades_closed else 0.0
        d["profit_factor"] = round(self.gross_profit / self.gross_loss, 2) if self.gross_loss > 0 else (None if self.gross_profit == 0 else 999.0)
        d["expectancy_usd"] = round(self.pnl / self.trades_closed, 4) if self.trades_closed else 0.0
        cur_dd_pct = self.current_hwm_dd_pct()
        daily_loss_pct = (abs(min(0.0, self.daily_pnl)) / max(self.mtm_peak_equity, 1.0)) * 100.0
        weekly_loss_pct = (abs(min(0.0, self.weekly_pnl)) / max(self.mtm_peak_equity, 1.0)) * 100.0
        alerts = _evaluate_hwm_risk_alerts(max(cur_dd_pct, self.mtm_max_drawdown_pct), daily_loss_pct, weekly_loss_pct)
        d["current_hwm_dd_pct"] = cur_dd_pct
        d["risk_alerts"] = alerts
        d["risk_status"] = alerts[0] if alerts else "OK"
        return d


@dataclass
class _ShadowPosition:
    symbol: str
    direction: str
    entry_price: float
    sl: float
    tp: float
    lots: float
    atr: float
    score: float
    regime: str
    opened_at: datetime
    kalman_velocity: Optional[float] = None
    kalman_acceleration: Optional[float] = None
    distance_to_swing_low_atr: Optional[float] = None
    distance_to_vwap_atr: Optional[float] = None
    ridge_pred_pnl: Optional[float] = None
    best_price: float = 0.0
    worst_price: float = 0.0
    be_moved: bool = False
    trailing_active: bool = False
    exit_reason_flag: str = ""
    mfe_5m_atr: Optional[float] = None
    mfe_10m_atr: Optional[float] = None
    mfe_15m_atr: Optional[float] = None
    mae_5m_atr: Optional[float] = None
    mae_10m_atr: Optional[float] = None
    mae_15m_atr: Optional[float] = None
    e1_15m_would_exit: Optional[bool] = None

    def __post_init__(self) -> None:
        if self.best_price == 0.0:
            self.best_price = self.entry_price
        if self.worst_price == 0.0:
            self.worst_price = self.entry_price


class ShadowValidationTracker:
    """
    Pure-Python virtual position & signal tracker for Shadow Live Validation.
    Tracks:
      1. Raw BUY & SELL directional streams (backward-compatible SHADOW_OPEN / SHADOW_EXIT).
      2. Three independent Candidate portfolios (`CANDIDATE`, `BUY_ONLY`, `SELL_ONLY`) enforcing
         `< 15:00 UTC` entry cutoff, `E1 (15m MFE < 0.4 ATR)` early failure exit, and HWM
         tick-level MTM drawdown monitoring with zero broker order placement.
    """

    PORTFOLIO_NAMES = ("CANDIDATE", "BUY_ONLY", "SELL_ONLY")

    def __init__(self, initial_equity: float = 10000.0, contract_size: float = 100.0):
        self.initial_equity: float = float(initial_equity)
        self.equity: float = float(initial_equity)
        self.contract_size: float = float(contract_size)
        self.open_positions: Dict[Tuple[str, str], _ShadowPosition] = {}
        self.stats: Dict[str, Dict[str, _DirectionStats]] = {}
        self.open_portfolio_positions: Dict[Tuple[str, str], _ShadowPosition] = {}
        self.portfolio_stats: Dict[str, Dict[str, _PortfolioStats]] = {}
        self._pending_portfolio_exits: deque = deque()
        self.restored: bool = False

    def _dir_bucket(self, symbol: str, direction: str) -> _DirectionStats:
        sym_map = self.stats.setdefault(
            symbol,
            {
                "BUY": _DirectionStats(
                    equity=self.initial_equity, peak_equity=self.initial_equity,
                    mtm_equity=self.initial_equity, mtm_peak_equity=self.initial_equity,
                ),
                "SELL": _DirectionStats(
                    equity=self.initial_equity, peak_equity=self.initial_equity,
                    mtm_equity=self.initial_equity, mtm_peak_equity=self.initial_equity,
                ),
            },
        )
        key = direction.upper()
        if key not in sym_map:
            sym_map[key] = _DirectionStats(
                equity=self.initial_equity, peak_equity=self.initial_equity,
                mtm_equity=self.initial_equity, mtm_peak_equity=self.initial_equity,
            )
        return sym_map[key]

    def _portfolio_bucket(self, symbol: str, portfolio_name: str) -> _PortfolioStats:
        sym_map = self.portfolio_stats.setdefault(
            symbol,
            {
                name: _PortfolioStats(
                    name=name,
                    equity=self.initial_equity,
                    peak_equity=self.initial_equity,
                    mtm_equity=self.initial_equity,
                    mtm_peak_equity=self.initial_equity,
                )
                for name in self.PORTFOLIO_NAMES
            },
        )
        if portfolio_name not in sym_map:
            sym_map[portfolio_name] = _PortfolioStats(
                name=portfolio_name,
                equity=self.initial_equity,
                peak_equity=self.initial_equity,
                mtm_equity=self.initial_equity,
                mtm_peak_equity=self.initial_equity,
            )
        return sym_map[portfolio_name]

    def _record_closed_stats(
        self,
        symbol: str,
        direction: str,
        pnl: float,
        reason: str,
        e1_flagged: Optional[bool],
    ) -> _DirectionStats:
        self.equity = round(self.equity + pnl, 4)
        bucket = self._dir_bucket(symbol, direction)
        bucket.trades_closed += 1
        bucket.pnl = round(bucket.pnl + pnl, 4)
        if pnl > 0:
            bucket.wins += 1
            bucket.gross_profit = round(bucket.gross_profit + pnl, 4)
        elif pnl < 0:
            bucket.losses += 1
            bucket.gross_loss = round(bucket.gross_loss + abs(pnl), 4)
        bucket.equity = round(bucket.equity + pnl, 4)
        if bucket.equity > bucket.peak_equity:
            bucket.peak_equity = bucket.equity
        dd = round(bucket.equity - bucket.peak_equity, 4)
        if dd < bucket.max_drawdown:
            bucket.max_drawdown = dd
        bucket.update_mtm(0.0)

        if e1_flagged:
            bucket.e1_15m_triggered += 1

        if reason == "SL":
            bucket.sl_hits += 1
        elif reason == "TARGET":
            bucket.tp_hits += 1
        elif reason == "BREAKEVEN":
            bucket.be_hits += 1
        elif reason == "TRAILING_SL":
            bucket.trail_hits += 1
        elif reason == "TIME_SQUEEZE":
            bucket.time_squeeze_hits += 1
        return bucket

    def _record_portfolio_closed_stats(
        self,
        symbol: str,
        portfolio_name: str,
        pnl: float,
        reason: str,
        ts: datetime,
    ) -> _PortfolioStats:
        bucket = self._portfolio_bucket(symbol, portfolio_name)
        bucket.roll_periods(ts)
        bucket.trades_closed += 1
        bucket.pnl = round(bucket.pnl + pnl, 4)
        bucket.daily_pnl = round(bucket.daily_pnl + pnl, 4)
        bucket.weekly_pnl = round(bucket.weekly_pnl + pnl, 4)
        if pnl > 0:
            bucket.wins += 1
            bucket.gross_profit = round(bucket.gross_profit + pnl, 4)
        elif pnl < 0:
            bucket.losses += 1
            bucket.gross_loss = round(bucket.gross_loss + abs(pnl), 4)
        bucket.equity = round(bucket.equity + pnl, 4)
        if bucket.equity > bucket.peak_equity:
            bucket.peak_equity = bucket.equity
        dd = round(bucket.equity - bucket.peak_equity, 4)
        if dd < bucket.closed_max_drawdown:
            bucket.closed_max_drawdown = dd
        if bucket.peak_equity > 0:
            cdd_pct = max(0.0, (bucket.peak_equity - bucket.equity) / bucket.peak_equity * 100.0)
            if cdd_pct > bucket.closed_max_drawdown_pct:
                bucket.closed_max_drawdown_pct = round(cdd_pct, 4)
        bucket.update_mtm(0.0, ts)

        if reason == "EARLY_FAIL_15M":
            bucket.e1_exits += 1
        elif reason == "SL":
            bucket.sl_hits += 1
        elif reason == "TARGET":
            bucket.tp_hits += 1
        elif reason == "BREAKEVEN":
            bucket.be_hits += 1
        elif reason == "TRAILING_SL":
            bucket.trail_hits += 1
        elif reason == "TIME_SQUEEZE":
            bucket.time_squeeze_hits += 1
        return bucket

    def _open_matching_portfolios(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        sl: float,
        tp: float,
        lots: float,
        atr: float,
        score: float,
        regime: str,
        opened_at: datetime,
        kalman_velocity: Optional[float],
        kalman_acceleration: Optional[float],
        distance_to_swing_low_atr: Optional[float],
        distance_to_vwap_atr: Optional[float],
        ridge_pred_pnl: Optional[float],
        base_equity_at_open: float,
    ) -> None:
        in_window = opened_at.hour < 15
        for p_name in self.PORTFOLIO_NAMES:
            p_bucket = self._portfolio_bucket(symbol, p_name)
            if p_name == "BUY_ONLY" and direction != "BUY":
                continue
            if p_name == "SELL_ONLY" and direction != "SELL":
                continue
            p_bucket.signals_considered += 1
            slot = (symbol, p_name)
            if not in_window or slot in self.open_portfolio_positions:
                p_bucket.signals_skipped_rule += 1
                continue
            scale = p_bucket.equity / max(base_equity_at_open, 1e-6)
            p_lots = _floor_lots(lots * scale)
            if p_lots <= 0:
                p_bucket.signals_skipped_rule += 1
                continue
            self.open_portfolio_positions[slot] = _ShadowPosition(
                symbol=symbol,
                direction=direction,
                entry_price=float(entry_price),
                sl=float(sl),
                tp=float(tp),
                lots=p_lots,
                atr=float(atr),
                score=float(score),
                regime=regime,
                opened_at=opened_at,
                kalman_velocity=kalman_velocity,
                kalman_acceleration=kalman_acceleration,
                distance_to_swing_low_atr=distance_to_swing_low_atr,
                distance_to_vwap_atr=distance_to_vwap_atr,
                ridge_pred_pnl=ridge_pred_pnl,
            )

    def restore_from_events(self, events: Iterable[Dict[str, Any]]) -> int:
        """Reconstruct open virtual positions and cumulative BUY/SELL + 3-portfolio stats after restart."""
        if self.restored:
            return len(self.open_positions)

        ev_list = list(events)
        closed_portfolio_keys: Set[Tuple[str, str, str]] = set()
        for ev in ev_list:
            if ev.get("event") == "SHADOW_PORTFOLIO_EXIT":
                sym = str(ev.get("symbol") or "")
                p_name = str(ev.get("portfolio") or "")
                opened = str(ev.get("opened_at") or ev.get("entry_timestamp") or "")
                if sym and p_name and opened:
                    closed_portfolio_keys.add((sym, p_name, _parse_iso_utc(opened).isoformat()))

        for ev in ev_list:
            kind = ev.get("event")
            sym = ev.get("symbol")
            if not sym:
                continue
            if kind == "SHADOW_ROLLOVER":
                d = ev.get("direction")
                if d in ("BUY", "SELL"):
                    self.record_candidate(sym, d, approved=(ev.get("status") == "APPROVED_SHADOW"))
            elif kind == "SHADOW_OPEN":
                d = str(ev.get("direction", "")).upper()
                if d not in ("BUY", "SELL"):
                    continue
                opened_raw = ev.get("entry_timestamp") or ev.get("opened_at")
                if not opened_raw:
                    continue
                opened_dt = _parse_iso_utc(opened_raw)
                pos_kwargs = dict(
                    symbol=sym,
                    direction=d,
                    entry_price=float(ev["entry_price"]),
                    sl=float(ev["sl"]),
                    tp=float(ev["tp"]),
                    lots=float(ev["lots"]),
                    atr=float(ev["atr"]),
                    score=float(ev.get("score") or 0.0),
                    regime=str(ev.get("regime") or ""),
                    opened_at=opened_dt,
                    kalman_velocity=ev.get("kalman_velocity"),
                    kalman_acceleration=ev.get("kalman_acceleration"),
                    distance_to_swing_low_atr=ev.get("distance_to_swing_low_atr"),
                    distance_to_vwap_atr=ev.get("distance_to_vwap_atr"),
                    ridge_pred_pnl=ev.get("ridge_pred_pnl"),
                )
                self.open_positions[(sym, d)] = _ShadowPosition(**pos_kwargs)
                self._open_matching_portfolios(
                    **pos_kwargs,
                    base_equity_at_open=self.equity,
                )
            elif kind == "SHADOW_EXIT":
                d = str(ev.get("direction", "")).upper()
                if d not in ("BUY", "SELL"):
                    continue
                self.open_positions.pop((sym, d), None)
                pnl = float(ev.get("net_pnl") if ev.get("net_pnl") is not None else ev.get("pnl", 0.0))
                reason = str(ev.get("exit_reason") or ev.get("reason") or "")
                self._record_closed_stats(sym, d, pnl, reason, ev.get("e1_15m_would_exit"))
            elif kind == "SHADOW_PORTFOLIO_EXIT":
                p_name = str(ev.get("portfolio") or "")
                if not p_name:
                    continue
                self.open_portfolio_positions.pop((sym, p_name), None)
                pnl = float(ev.get("net_pnl") if ev.get("net_pnl") is not None else ev.get("pnl", 0.0))
                reason = str(ev.get("exit_reason") or ev.get("reason") or "")
                closed_raw = ev.get("closed_at") or ev.get("opened_at") or ev.get("entry_timestamp")
                closed_dt = _parse_iso_utc(closed_raw) if closed_raw else datetime.now(timezone.utc)
                self._record_portfolio_closed_stats(sym, p_name, pnl, reason, closed_dt)

        # Drop already-closed portfolio positions if SHADOW_PORTFOLIO_EXIT appeared cleanly
        for sym, p_name in list(self.open_portfolio_positions.keys()):
            p = self.open_portfolio_positions[(sym, p_name)]
            if (sym, p_name, p.opened_at.isoformat()) in closed_portfolio_keys:
                self.open_portfolio_positions.pop((sym, p_name), None)

        self.restored = True
        return len(self.open_positions)

    def record_candidate(self, symbol: str, direction: str, approved: bool) -> None:
        if direction not in ("BUY", "SELL"):
            return
        bucket = self._dir_bucket(symbol, direction)
        bucket.signals_raw += 1
        if approved:
            bucket.signals_approved += 1
        else:
            bucket.signals_blocked += 1

    def open_virtual_position(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        sl: float,
        tp: float,
        lots: float,
        atr: float,
        score: float,
        regime: str,
        opened_at: datetime,
        ridge_pred_pnl: Optional[float] = None,
        kalman_velocity: Optional[float] = None,
        kalman_acceleration: Optional[float] = None,
        distance_to_swing_low_atr: Optional[float] = None,
        distance_to_vwap_atr: Optional[float] = None,
        planned_risk_usd: Optional[float] = None,
        planned_risk_pct: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        dir_key = direction.upper()
        slot = (symbol, dir_key)
        if slot in self.open_positions or lots <= 0:
            return None
        pos = _ShadowPosition(
            symbol=symbol,
            direction=dir_key,
            entry_price=float(entry_price),
            sl=float(sl),
            tp=float(tp),
            lots=float(lots),
            atr=float(atr),
            score=float(score),
            regime=regime,
            opened_at=opened_at,
            kalman_velocity=kalman_velocity,
            kalman_acceleration=kalman_acceleration,
            distance_to_swing_low_atr=distance_to_swing_low_atr,
            distance_to_vwap_atr=distance_to_vwap_atr,
            ridge_pred_pnl=ridge_pred_pnl,
        )
        self.open_positions[slot] = pos
        self._open_matching_portfolios(
            symbol=symbol,
            direction=dir_key,
            entry_price=pos.entry_price,
            sl=pos.sl,
            tp=pos.tp,
            lots=pos.lots,
            atr=pos.atr,
            score=pos.score,
            regime=regime,
            opened_at=opened_at,
            kalman_velocity=kalman_velocity,
            kalman_acceleration=kalman_acceleration,
            distance_to_swing_low_atr=distance_to_swing_low_atr,
            distance_to_vwap_atr=distance_to_vwap_atr,
            ridge_pred_pnl=ridge_pred_pnl,
            base_equity_at_open=self.equity,
        )
        iso_open = opened_at.isoformat()
        return {
            "event": "SHADOW_OPEN",
            "symbol": symbol,
            "entry_timestamp": iso_open,
            "opened_at": iso_open,
            "direction": dir_key,
            "regime": regime,
            "entry_price": pos.entry_price,
            "sl": pos.sl,
            "tp": pos.tp,
            "lots": pos.lots,
            "atr": pos.atr,
            "score": pos.score,
            "planned_risk_usd": planned_risk_usd,
            "planned_risk_pct": planned_risk_pct,
            "kalman_velocity": kalman_velocity,
            "kalman_acceleration": kalman_acceleration,
            "distance_to_swing_low_atr": distance_to_swing_low_atr,
            "distance_to_vwap_atr": distance_to_vwap_atr,
            "in_15utc_window": opened_at.hour < 15,
            "ridge_pred_pnl": ridge_pred_pnl,
        }

    def _update_excursion_and_checkpoints(self, pos: _ShadowPosition, exit_quote: float, ts: datetime) -> Tuple[float, float, float]:
        atr_norm = max(pos.atr, 1e-6)
        if pos.direction == "BUY":
            if exit_quote > pos.best_price:
                pos.best_price = exit_quote
            if exit_quote < pos.worst_price:
                pos.worst_price = exit_quote
            favorable = pos.best_price - pos.entry_price
            adverse = pos.entry_price - pos.worst_price
        else:
            if exit_quote < pos.best_price:
                pos.best_price = exit_quote
            if exit_quote > pos.worst_price:
                pos.worst_price = exit_quote
            favorable = pos.entry_price - pos.best_price
            adverse = pos.worst_price - pos.entry_price

        mfe_atr_now = round(max(0.0, favorable / atr_norm), 4)
        mae_atr_now = round(max(0.0, adverse / atr_norm), 4)
        age_secs = (ts - pos.opened_at).total_seconds()

        if pos.mfe_5m_atr is None and age_secs >= 5 * 60:
            pos.mfe_5m_atr = mfe_atr_now
            pos.mae_5m_atr = mae_atr_now
        if pos.mfe_10m_atr is None and age_secs >= 10 * 60:
            pos.mfe_10m_atr = mfe_atr_now
            pos.mae_10m_atr = mae_atr_now
        if pos.e1_15m_would_exit is None and age_secs >= 15 * 60:
            pos.mfe_15m_atr = mfe_atr_now
            pos.mae_15m_atr = mae_atr_now
            pos.e1_15m_would_exit = (favorable / atr_norm) < 0.4
        return favorable, age_secs, mfe_atr_now

    def _step_position(self, slot: Tuple[str, str], pos: _ShadowPosition, bid: float, ask: float, ts: datetime) -> Optional[Dict[str, Any]]:
        symbol, dir_key = slot
        exit_quote = float(bid) if pos.direction == "BUY" else float(ask)
        atr = max(pos.atr, 0.5)
        favorable, age_secs, _ = self._update_excursion_and_checkpoints(pos, exit_quote, ts)

        # Update live tick-MTM drawdown for directional bucket
        floating_pnl = ((exit_quote - pos.entry_price) if pos.direction == "BUY" else (pos.entry_price - exit_quote)) * pos.lots * self.contract_size
        self._dir_bucket(symbol, dir_key).update_mtm(floating_pnl)

        if pos.direction == "BUY":
            if not pos.be_moved and favorable >= atr:
                new_sl = pos.entry_price + 0.20
                if new_sl > pos.sl:
                    pos.sl = new_sl
                    pos.be_moved = True
            if favorable >= 1.5 * atr:
                trail_sl = pos.best_price - 1.0 * atr
                if trail_sl > pos.sl:
                    pos.sl = trail_sl

            if exit_quote <= pos.sl:
                reason = "BREAKEVEN" if pos.be_moved and abs(pos.sl - (pos.entry_price + 0.20)) < 1e-6 else ("TRAILING_SL" if pos.sl > pos.entry_price else "SL")
                return self._close_position(slot, exit_quote, reason, ts)
            if exit_quote >= pos.tp:
                return self._close_position(slot, exit_quote, "TARGET", ts)
        else:
            if not pos.be_moved and favorable >= atr:
                new_sl = pos.entry_price - 0.20
                if new_sl < pos.sl:
                    pos.sl = new_sl
                    pos.be_moved = True
            if favorable >= 1.5 * atr:
                trail_sl = pos.best_price + 1.0 * atr
                if trail_sl < pos.sl:
                    pos.sl = trail_sl

            if exit_quote >= pos.sl:
                reason = "BREAKEVEN" if pos.be_moved and abs(pos.sl - (pos.entry_price - 0.20)) < 1e-6 else ("TRAILING_SL" if pos.sl < pos.entry_price else "SL")
                return self._close_position(slot, exit_quote, reason, ts)
            if exit_quote <= pos.tp:
                return self._close_position(slot, exit_quote, "TARGET", ts)

        if age_secs >= 45 * 60:
            return self._close_position(slot, exit_quote, "TIME_SQUEEZE", ts)

        return None

    def _step_portfolio_position(
        self,
        slot: Tuple[str, str],
        pos: _ShadowPosition,
        bid: float,
        ask: float,
        ts: datetime,
    ) -> Optional[Dict[str, Any]]:
        """Step one independent Candidate portfolio position (1.0/1.6/2.5 ATR trailing + E1 15m<0.4 ATR + 60m squeeze)."""
        symbol, p_name = slot
        is_bull = pos.direction == "BUY"
        exit_quote = float(bid) if is_bull else float(ask)
        floating_pnl = ((exit_quote - pos.entry_price) if is_bull else (pos.entry_price - exit_quote)) * pos.lots * self.contract_size
        self._portfolio_bucket(symbol, p_name).update_mtm(floating_pnl, ts)

        favorable, age_secs, _ = self._update_excursion_and_checkpoints(pos, exit_quote, ts)

        if pos.e1_15m_would_exit:
            return self._close_portfolio_position(slot, exit_quote, "EARLY_FAIL_15M", ts)

        atr = max(pos.atr, 1e-9)
        if is_bull and exit_quote <= pos.sl:
            reason = pos.exit_reason_flag if pos.exit_reason_flag else ("TRAILING_SL" if pos.trailing_active else "SL")
            return self._close_portfolio_position(slot, float(pos.sl), reason, ts)
        if not is_bull and exit_quote >= pos.sl:
            reason = pos.exit_reason_flag if pos.exit_reason_flag else ("TRAILING_SL" if pos.trailing_active else "SL")
            return self._close_portfolio_position(slot, float(pos.sl), reason, ts)
        if is_bull and exit_quote >= pos.tp:
            return self._close_portfolio_position(slot, float(pos.tp), "TARGET", ts)
        if not is_bull and exit_quote <= pos.tp:
            return self._close_portfolio_position(slot, float(pos.tp), "TARGET", ts)

        # Ratchet Experiment E trailing stops matching RunBacktestUseCase
        if favorable >= atr * 2.5:
            pos.trailing_active = True
            t_sl = (pos.best_price - atr * 0.75) if is_bull else (pos.best_price + atr * 0.75)
            if (is_bull and t_sl > pos.sl) or (not is_bull and t_sl < pos.sl):
                pos.sl = t_sl
                pos.exit_reason_flag = "TRAILING_SL"
        elif favorable >= atr * 1.6:
            pos.trailing_active = True
            t_sl = (pos.best_price - atr * 1.10) if is_bull else (pos.best_price + atr * 1.10)
            if (is_bull and t_sl > pos.sl) or (not is_bull and t_sl < pos.sl):
                pos.sl = t_sl
                pos.exit_reason_flag = "TRAILING_SL"
        elif favorable >= atr * 1.0:
            be_sl = (pos.entry_price + 0.20) if is_bull else (pos.entry_price - 0.20)
            if (is_bull and be_sl > pos.sl) or (not is_bull and be_sl < pos.sl):
                pos.sl = be_sl
                pos.be_moved = True
                pos.exit_reason_flag = "BREAKEVEN"

        effective_max_hold_secs = (60 if (7 <= pos.opened_at.hour < 20) else 90) * 60.0
        if age_secs >= 0.75 * effective_max_hold_secs:
            progress = min((age_secs / effective_max_hold_secs - 0.75) / 0.25, 1.0)
            if is_bull:
                time_sl = pos.sl + progress * (exit_quote - pos.sl)
                if time_sl > pos.sl:
                    pos.sl = time_sl
                    pos.exit_reason_flag = "TIME_SQUEEZE"
            else:
                time_sl = pos.sl - progress * (pos.sl - exit_quote)
                if time_sl < pos.sl:
                    pos.sl = time_sl
                    pos.exit_reason_flag = "TIME_SQUEEZE"

        if age_secs >= effective_max_hold_secs:
            return self._close_portfolio_position(slot, exit_quote, "TIMEOUT", ts)

        return None

    def on_tick_all(self, symbol: str, bid: float, ask: float, ts: datetime) -> List[Dict[str, Any]]:
        """
        Evaluate raw BUY & SELL directional positions and all 3 independent virtual portfolios.
        Returns closed directional `SHADOW_EXIT` events for backward compatibility;
        closed portfolio exits are queued for `drain_portfolio_exits()`.
        """
        events: List[Dict[str, Any]] = []
        for dir_key in ("BUY", "SELL"):
            slot = (symbol, dir_key)
            pos = self.open_positions.get(slot)
            if pos is not None:
                ev = self._step_position(slot, pos, bid, ask, ts)
                if ev is not None:
                    events.append(ev)

        for p_name in self.PORTFOLIO_NAMES:
            p_slot = (symbol, p_name)
            p_pos = self.open_portfolio_positions.get(p_slot)
            if p_pos is not None:
                p_ev = self._step_portfolio_position(p_slot, p_pos, bid, ask, ts)
                if p_ev is not None:
                    self._pending_portfolio_exits.append(p_ev)

        return events

    def drain_portfolio_exits(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        while self._pending_portfolio_exits:
            out.append(self._pending_portfolio_exits.popleft())
        return out

    def on_tick(self, symbol: str, bid: float, ask: float, ts: datetime) -> Optional[Dict[str, Any]]:
        """Backward-compatible single-event wrapper returning first closed directional position if any."""
        closed = self.on_tick_all(symbol, bid, ask, ts)
        return closed[0] if closed else None

    def _close_position(self, slot: Tuple[str, str], exit_price: float, reason: str, ts: datetime) -> Dict[str, Any]:
        symbol, _ = slot
        pos = self.open_positions.pop(slot)
        price_diff = (exit_price - pos.entry_price) if pos.direction == "BUY" else (pos.entry_price - exit_price)
        pnl = round(price_diff * pos.lots * self.contract_size, 4)

        bucket = self._record_closed_stats(symbol, pos.direction, pnl, reason, pos.e1_15m_would_exit)
        iso_open = pos.opened_at.isoformat()

        return {
            "event": "SHADOW_EXIT",
            "symbol": symbol,
            "entry_timestamp": iso_open,
            "opened_at": iso_open,
            "closed_at": ts.isoformat(),
            "duration_mins": round((ts - pos.opened_at).total_seconds() / 60.0, 2),
            "direction": pos.direction,
            "regime": pos.regime,
            "exit_reason": reason,
            "reason": reason,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "sl": pos.sl,
            "tp": pos.tp,
            "lots": pos.lots,
            "atr": pos.atr,
            "score": pos.score,
            "net_pnl": pnl,
            "pnl": pnl,
            "equity": self.equity,
            "dir_equity": bucket.equity,
            "kalman_velocity": pos.kalman_velocity,
            "kalman_acceleration": pos.kalman_acceleration,
            "distance_to_swing_low_atr": pos.distance_to_swing_low_atr,
            "distance_to_vwap_atr": pos.distance_to_vwap_atr,
            "mfe_5m_atr": pos.mfe_5m_atr,
            "mfe_10m_atr": pos.mfe_10m_atr,
            "mfe_15m_atr": pos.mfe_15m_atr,
            "mae_5m_atr": pos.mae_5m_atr,
            "mae_10m_atr": pos.mae_10m_atr,
            "mae_15m_atr": pos.mae_15m_atr,
            "e1_15m_would_exit": bool(pos.e1_15m_would_exit) if pos.e1_15m_would_exit is not None else False,
            "in_15utc_window": pos.opened_at.hour < 15,
            "ridge_pred_pnl": pos.ridge_pred_pnl,
            "buy_sell_summary": self.summary(symbol),
        }

    def _close_portfolio_position(
        self, slot: Tuple[str, str], exit_price: float, reason: str, ts: datetime
    ) -> Dict[str, Any]:
        symbol, p_name = slot
        pos = self.open_portfolio_positions.pop(slot)
        price_diff = (exit_price - pos.entry_price) if pos.direction == "BUY" else (pos.entry_price - exit_price)
        pnl = round(price_diff * pos.lots * self.contract_size, 4)
        bucket = self._record_portfolio_closed_stats(symbol, p_name, pnl, reason, ts)
        iso_open = pos.opened_at.isoformat()
        return {
            "event": "SHADOW_PORTFOLIO_EXIT",
            "symbol": symbol,
            "portfolio": p_name,
            "entry_timestamp": iso_open,
            "opened_at": iso_open,
            "closed_at": ts.isoformat(),
            "duration_mins": round((ts - pos.opened_at).total_seconds() / 60.0, 2),
            "direction": pos.direction,
            "regime": pos.regime,
            "exit_reason": reason,
            "reason": reason,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "lots": pos.lots,
            "net_pnl": pnl,
            "pnl": pnl,
            "portfolio_equity": bucket.equity,
            "mtm_max_drawdown": bucket.mtm_max_drawdown,
            "mtm_max_drawdown_pct": bucket.mtm_max_drawdown_pct,
            "risk_status": bucket.to_dict()["risk_status"],
        }

    def portfolios_summary(self, symbol: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for p_name in self.PORTFOLIO_NAMES:
            b = self._portfolio_bucket(symbol, p_name)
            d = b.to_dict()
            open_pos = self.open_portfolio_positions.get((symbol, p_name))
            d["open_position"] = (
                {
                    "opened_at": open_pos.opened_at.isoformat(),
                    "direction": open_pos.direction,
                    "regime": open_pos.regime,
                    "entry_price": open_pos.entry_price,
                    "sl": open_pos.sl,
                    "tp": open_pos.tp,
                    "lots": open_pos.lots,
                }
                if open_pos is not None
                else None
            )
            out[p_name] = d
        return out

    def summary(self, symbol: str) -> Dict[str, Any]:
        sym_map = self.stats.get(symbol, {})
        open_by_dir = {
            d: {
                "entry_timestamp": p.opened_at.isoformat(),
                "opened_at": p.opened_at.isoformat(),
                "direction": p.direction,
                "regime": p.regime,
                "entry_price": p.entry_price,
                "sl": p.sl,
                "tp": p.tp,
                "lots": p.lots,
                "kalman_velocity": p.kalman_velocity,
                "kalman_acceleration": p.kalman_acceleration,
                "distance_to_swing_low_atr": p.distance_to_swing_low_atr,
                "distance_to_vwap_atr": p.distance_to_vwap_atr,
                "mfe_5m_atr": p.mfe_5m_atr,
                "mfe_10m_atr": p.mfe_10m_atr,
                "mfe_15m_atr": p.mfe_15m_atr,
                "mae_5m_atr": p.mae_5m_atr,
                "mae_10m_atr": p.mae_10m_atr,
                "mae_15m_atr": p.mae_15m_atr,
                "e1_15m_would_exit": p.e1_15m_would_exit,
            }
            for d in ("BUY", "SELL")
            if (p := self.open_positions.get((symbol, d))) is not None
        }
        return {
            "equity": self.equity,
            "open_position": open_by_dir.get("BUY") or open_by_dir.get("SELL"),
            "open_by_direction": open_by_dir,
            "BUY": sym_map.get("BUY", _DirectionStats(equity=self.initial_equity, peak_equity=self.initial_equity, mtm_equity=self.initial_equity, mtm_peak_equity=self.initial_equity)).to_dict(),
            "SELL": sym_map.get("SELL", _DirectionStats(equity=self.initial_equity, peak_equity=self.initial_equity, mtm_equity=self.initial_equity, mtm_peak_equity=self.initial_equity)).to_dict(),
            "portfolios": self.portfolios_summary(symbol),
        }
