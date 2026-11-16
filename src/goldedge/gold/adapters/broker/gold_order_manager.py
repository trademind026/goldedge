import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from ....adapters.secondary.broker.mt5_broker import MT5BrokerAdapter
from ...domain.models.gold_pip_engine import GoldPipEngine
from ....domain.models.trade import Position, TradeStatus
from ....domain.ports.outbound.broker import IBrokerAdapter

logger = logging.getLogger("gold_order_manager")


class GoldOrderManager(IBrokerAdapter):
    """
    Unified Dual-Mode Broker Adapter specifically for Spot Gold (XAUUSD).
    Seamlessly switches between:
    - 'test' (Paper Trading): realistic half-spread slippage, 100 oz margin accounting,
      dynamic floating PnL, paper balance/equity tracking.
    - 'live' (MetaTrader 5): real order placement (TRADE_ACTION_DEAL, ORDER_FILLING_IOC),
      live position management, and SL/TP modifications.
    """

    def __init__(
        self,
        mt5_broker: Optional[MT5BrokerAdapter] = None,
        default_mode: str = "test",
        initial_balance: float = 10000.0,
        half_spread_dollars: float = 0.15,
        default_leverage: float = 100.0
    ):
        clean_init = default_mode.lower()
        self.mode = "paper" if clean_init in ("test", "paper") else clean_init
        self.mt5_broker = mt5_broker or MT5BrokerAdapter()
        self.half_spread_dollars = half_spread_dollars
        self.leverage = default_leverage

        # Paper State
        self.paper_balance: float = initial_balance
        self.paper_equity: float = initial_balance
        self.paper_positions: Dict[int, Position] = {}
        self.closed_paper_positions: List[Dict[str, Any]] = []
        self.last_ticks: Dict[str, Dict[str, float]] = {}
        self._next_ticket: int = 1001

    def set_mode(self, mode: str):
        mode_clean = mode.lower()
        if mode_clean in ("test", "paper"):
            mode_clean = "paper"
        if mode_clean not in ("paper", "demo", "live"):
            raise ValueError(f"Invalid mode '{mode}'. Must be 'paper', 'demo', or 'live'.")
        self.mode = mode_clean
        logger.info(f"GoldOrderManager mode switched to: {self.mode.upper()}")

    def get_mode(self) -> str:
        return self.mode

    def place_order(
        self,
        symbol: str,
        order_type: str,
        volume: float,
        sl: float,
        tp: float,
        mode: Optional[str] = None,
        price: Optional[float] = None
    ) -> Optional[int]:
        """
        Executes an order in either 'test' (paper) or 'live' (MT5) mode.
        """
        active_mode = (mode or self.mode).lower()
        if active_mode in ("test", "paper"):
            active_mode = "paper"

        if active_mode in ("live", "demo"):
            return self.mt5_broker.place_order(symbol, order_type, volume, sl, tp)

        # --- Paper Execution (STRICT: Requires real tick or explicit execution price) ---
        tick = self.last_ticks.get(symbol)
        dir_upper = order_type.upper()
        is_buy = dir_upper in ("BUY", "CALL", "LONG")
        pos_type = "BUY" if is_buy else "SELL"

        if price is not None:
            fill_price = float(price) + (self.half_spread_dollars if is_buy else -self.half_spread_dollars)
        elif tick:
            fill_price = float(tick.get("ask", tick["price"])) if is_buy else float(tick.get("bid", tick["price"]))
        else:
            logger.error(f"[PAPER] Cannot place paper order for {symbol}: No market tick or execution price received.")
            return None

        ticket = self._next_ticket
        self._next_ticket += 1

        # Check required margin
        req_margin = GoldPipEngine.calculate_required_margin(fill_price, volume, self.leverage)
        used_margin = sum(
            GoldPipEngine.calculate_required_margin(p.price_open, p.volume, self.leverage)
            for p in self.paper_positions.values()
        )
        if (used_margin + req_margin) > self.paper_equity:
            logger.warning(f"Paper trade rejected: Insufficient margin (Need ${req_margin:.2f}, Used ${used_margin:.2f})")
            return None

        pos = Position(
            ticket=ticket,
            symbol=symbol,
            type=pos_type,
            volume=float(volume),
            price_open=fill_price,
            price_sl=float(sl),
            price_tp=float(tp),
            time_setup=datetime.now(timezone.utc),
            status=TradeStatus.OPEN,
            price_close=fill_price,
            profit=0.0
        )
        self.paper_positions[ticket] = pos
        logger.info(f"[PAPER] Opened {pos_type} #{ticket} {volume} lots @ ${fill_price:.2f}")
        return ticket

    def close_position(
        self,
        ticket: int,
        volume: Optional[float] = None,
        mode: Optional[str] = None
    ) -> bool:
        """
        Closes an open position in either mode.
        """
        active_mode = (mode or self.mode).lower()
        if active_mode in ("test", "paper"):
            active_mode = "paper"

        if active_mode in ("live", "demo"):
            return self.mt5_broker.close_position(ticket, volume)

        # Paper Close
        pos = self.paper_positions.pop(ticket, None)
        if not pos:
            logger.warning(f"[PAPER] Position #{ticket} not found.")
            return False

        is_buy = pos.type.upper() in ("BUY", "CALL", "LONG")
        # Exit fill with real tick or known position price
        tick = self.last_ticks.get(pos.symbol)
        if tick:
            exit_fill = float(tick.get("bid", tick["price"])) if is_buy else float(tick.get("ask", tick["price"]))
        else:
            current_market = pos.price_close if pos.price_close else pos.price_open
            exit_fill = (current_market - self.half_spread_dollars) if is_buy else (current_market + self.half_spread_dollars)

        realised = GoldPipEngine.calculate_cash_pnl(pos.type, pos.price_open, exit_fill, pos.volume)
        self.paper_balance += realised
        self.paper_equity = self.paper_balance

        self.closed_paper_positions.append({
            "id": ticket,
            "symbol": pos.symbol,
            "direction": pos.type,
            "entry_price": pos.price_open,
            "exit_price": exit_fill,
            "lots": pos.volume,
            "realised_pnl": realised,
            "entry_time": pos.time_setup.isoformat() if pos.time_setup else None,
            "exit_time": datetime.now(timezone.utc).isoformat(),
            "status": "CLOSED"
        })

        logger.info(f"[PAPER] Closed #{ticket} @ ${exit_fill:.2f} (PnL: ${realised:.2f})")
        return True

    def modify_position(
        self,
        ticket: int,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        mode: Optional[str] = None
    ) -> bool:
        """
        Modifies SL or TP for an open position.
        """
        active_mode = (mode or self.mode).lower()
        if active_mode in ("test", "paper"):
            active_mode = "paper"

        if active_mode in ("live", "demo"):
            return self.mt5_broker.modify_position(ticket, sl, tp)

        pos = self.paper_positions.get(ticket)
        if not pos:
            return False

        from dataclasses import replace
        new_sl = float(sl) if sl is not None else pos.price_sl
        new_tp = float(tp) if tp is not None else pos.price_tp
        self.paper_positions[ticket] = replace(pos, price_sl=new_sl, price_tp=new_tp)
        return True

    def get_open_positions(
        self,
        symbol: Optional[str] = None,
        mode: Optional[str] = None
    ) -> List[Position]:
        active_mode = (mode or self.mode).lower()
        if active_mode in ("test", "paper"):
            active_mode = "paper"

        if active_mode in ("live", "demo"):
            return self.mt5_broker.get_open_positions(symbol)

        positions = list(self.paper_positions.values())
        if symbol:
            positions = [p for p in positions if p.symbol == symbol]
        return positions

    def update_paper_tick(self, symbol: str, price: float, bid: float, ask: float):
        """Updates open paper trades with live tick prices to update floating PnL."""
        self.last_ticks[symbol] = {
            "price": float(price),
            "bid": float(bid),
            "ask": float(ask)
        }
        from dataclasses import replace
        for ticket, pos in list(self.paper_positions.items()):
            if pos.symbol == symbol:
                eval_price = bid if pos.type == "BUY" else ask
                profit = GoldPipEngine.calculate_cash_pnl(pos.type, pos.price_open, eval_price, pos.volume)
                self.paper_positions[ticket] = replace(pos, price_close=price, profit=profit)

        # Update paper equity
        floating = sum(p.profit for p in self.paper_positions.values())
        self.paper_equity = self.paper_balance + floating

    def get_positions_by_mode(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        Returns structured dictionary of positions separated by 'test' and 'live'
        for the dashboard SSE stream.
        """
        test_list = []
        for p in self.paper_positions.values():
            test_list.append({
                "id": p.ticket,
                "entry_time": p.time_setup.isoformat() if p.time_setup else datetime.now(timezone.utc).isoformat(),
                "symbol": p.symbol,
                "direction": p.type,
                "strategy": "Quant_Gold_Kalman",
                "entry_price": p.price_open,
                "sl": p.price_sl,
                "initial_sl": p.price_sl,
                "target": p.price_tp,
                "trailing_active": False,
                "lot_size": p.volume,
                "status": "OPEN",
                "current_price": p.price_close,
                "unrealised_pnl": p.profit,
                "exit_time": None,
                "exit_price": None,
                "realised_pnl": None,
                "exit_reason": None
            })

        live_positions = self.mt5_broker.get_open_positions() if self.mode == "live" else []
        live_list = []
        for p in live_positions:
            live_list.append({
                "id": p.ticket,
                "entry_time": p.time_setup.isoformat() if p.time_setup else datetime.now(timezone.utc).isoformat(),
                "symbol": p.symbol,
                "direction": p.type,
                "strategy": "Quant_Gold_Kalman",
                "entry_price": p.price_open,
                "sl": p.price_sl,
                "initial_sl": p.price_sl,
                "target": p.price_tp,
                "trailing_active": False,
                "lot_size": p.volume,
                "status": "OPEN",
                "current_price": p.price_close,
                "unrealised_pnl": p.profit,
                "exit_time": None,
                "exit_price": None,
                "realised_pnl": None,
                "exit_reason": None
            })

        return {"test": test_list, "live": live_list}

    def get_account_summary(self) -> Dict[str, Any]:
        """Returns account balance, equity, margin, and PnL breakdown for paper and live."""
        test_open_pnl = sum(p.profit for p in self.paper_positions.values())
        test_closed_pnl = sum(cp.get("realised_pnl", 0.0) for cp in self.closed_paper_positions)

        live_balance = 0.0
        live_equity = 0.0
        live_open_pnl = 0.0
        live_margin = 0.0
        live_free_margin = 0.0
        live_margin_level = 999.0
        live_currency = "USD"
        live_leverage = self.leverage
        live_connected = False

        if self.mode in ("live", "demo"):
            acc_info = self.mt5_broker.get_account_info() if hasattr(self.mt5_broker, "get_account_info") else None
            if acc_info:
                live_connected = True
                live_balance = acc_info.get("balance", 0.0)
                live_equity = acc_info.get("equity", 0.0)
                live_open_pnl = live_equity - live_balance
                live_margin = acc_info.get("margin", 0.0)
                live_free_margin = acc_info.get("free_margin", 0.0)
                live_margin_level = acc_info.get("margin_level", 999.0)
                live_currency = acc_info.get("currency", "USD")
                live_leverage = acc_info.get("leverage", self.leverage)

        return {
            "mode": self.mode,
            "paper_balance": self.paper_balance,
            "paper_equity": self.paper_equity,
            "total_open_pnl_test": test_open_pnl,
            "total_closed_pnl_test": test_closed_pnl,
            "total_pnl_test": test_open_pnl + test_closed_pnl,
            "live_connected": live_connected,
            "live_balance": live_balance,
            "live_equity": live_equity,
            "live_margin": live_margin,
            "live_free_margin": live_free_margin,
            "live_margin_level": live_margin_level,
            "live_currency": live_currency,
            "live_leverage": live_leverage,
            "total_open_pnl_live": live_open_pnl,
            "total_closed_pnl_live": 0.0,
            "total_pnl_live": live_open_pnl,
        }
