import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ...domain.models.pip_calculator import PipCalculator
from ...domain.models.trade import TradeStatus
from ...domain.ports.outbound.broker import IBrokerAdapter
from ...domain.ports.outbound.market_data import IMarketDataProvider
from ...domain.ports.outbound.trade_repo import ITradeRepository
from ...domain.services.hwm_kill_switch_service import compute_account_equity, compute_unrealized_pnl

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LiveExecutionContext:
    """Real-time account, spread, and risk-state metrics for trade execution."""

    account_balance: float
    account_equity: float
    account_currency: str
    unrealized_pnl: float
    current_spread_pips: float
    recent_median_spread_pips: float
    current_daily_loss_pct: float
    current_streak: int


def resolve_execution_context(
    symbol: str,
    broker: IBrokerAdapter,
    trade_repo: ITradeRepository,
    market_data: Optional[IMarketDataProvider] = None,
) -> Optional[LiveExecutionContext]:
    """
    Gather live account balance, true MTM equity, spread, daily loss percentage, and
    consecutive loss streak from the configured broker and trade repository.

    Returns None if live account information cannot be obtained so callers
    never trade on fabricated balances.
    """
    account_info = broker.get_account_info()
    if not account_info or account_info.get("balance") is None:
        logger.error("Cannot resolve live account balance from broker; aborting execution.")
        return None

    account_balance = float(account_info["balance"])
    if account_balance <= 0.0:
        logger.error(f"Invalid non-positive account balance ({account_balance}); aborting execution.")
        return None
    account_currency = str(account_info.get("currency", "USD") or "USD").upper()

    # Compute MTM unrealized P&L using executable bid/ask quotes if equity is missing or open positions exist
    unrealized_pnl = float(account_info.get("unrealized_pnl", 0.0) or 0.0)
    raw_equity = account_info.get("equity")
    if raw_equity is not None and account_info.get("unrealized_pnl") is not None:
        account_equity = float(raw_equity)
    else:
        calc_unr = 0.0
        try:
            open_pos = list(broker.get_open_positions() or [])
        except Exception:
            open_pos = list(trade_repo.get_positions_by_status(TradeStatus.OPEN) or [])
        for pos in open_pos:
            if market_data is not None:
                try:
                    qt = market_data.get_current_tick(pos.symbol)
                    if qt and qt.bid > 0 and qt.ask > 0 and qt.bid < qt.ask:
                        cs = 100.0 if "XAU" in pos.symbol.upper() else 100000.0
                        calc_unr += compute_unrealized_pnl(
                            pos.type, pos.price_open, qt.bid, qt.ask, pos.volume, contract_size=cs
                        )
                        continue
                except Exception:
                    pass
            if pos.profit is not None:
                calc_unr += float(pos.profit)
        if raw_equity is not None and float(raw_equity) != account_balance:
            account_equity = float(raw_equity)
            unrealized_pnl = account_equity - account_balance
        else:
            unrealized_pnl = calc_unr
            account_equity = compute_account_equity(
                account_balance,
                unrealized_pnl,
                profit_currency="USD",
                account_currency=account_currency,
            )

    # Resolve live spread in pips from broker or market data tick
    current_spread_pips: Optional[float] = None
    spread_fn = getattr(broker, "get_live_spread_pips", None)
    if callable(spread_fn):
        try:
            val = float(spread_fn(symbol))
            if val > 0.0:
                current_spread_pips = val
        except Exception as exc:
            logger.warning(f"Failed to query live spread from broker for {symbol}: {exc}")

    if current_spread_pips is None and market_data is not None:
        try:
            tick = market_data.get_current_tick(symbol)
            if tick and tick.bid > 0 and tick.ask > 0:
                current_spread_pips = PipCalculator.calculate_spread_pips(tick.bid, tick.ask, symbol)
        except Exception as exc:
            logger.warning(f"Failed to query current tick for {symbol}: {exc}")

    if current_spread_pips is None:
        logger.error(f"Cannot resolve live spread for {symbol}; aborting execution.")
        return None

    # Compute today's realized loss % and consecutive loss streak from closed trades
    closed_positions = trade_repo.get_positions_by_status(TradeStatus.CLOSED)
    today_utc = datetime.now(timezone.utc).date()

    daily_pnl = 0.0
    for pos in closed_positions:
        close_dt = pos.time_closing or pos.time_setup
        if close_dt is not None:
            close_date = close_dt.astimezone(timezone.utc).date() if close_dt.tzinfo else close_dt.date()
            if close_date == today_utc and pos.profit is not None:
                daily_pnl += float(pos.profit)

    current_daily_loss_pct = abs(daily_pnl) / account_balance if daily_pnl < 0 else 0.0

    # Sort newest-first to count consecutive losses
    sorted_closed = sorted(
        closed_positions,
        key=lambda p: p.time_closing or p.time_setup or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    current_streak = 0
    for pos in sorted_closed:
        if pos.profit is not None and pos.profit < 0:
            current_streak += 1
        else:
            break

    return LiveExecutionContext(
        account_balance=account_balance,
        account_equity=account_equity,
        account_currency=account_currency,
        unrealized_pnl=unrealized_pnl,
        current_spread_pips=current_spread_pips,
        recent_median_spread_pips=current_spread_pips,
        current_daily_loss_pct=current_daily_loss_pct,
        current_streak=current_streak,
    )
