import numpy as np
from typing import Dict, Any
from ...domain.ports.outbound.trade_repo import ITradeRepository
from ...domain.models.trade import TradeStatus

class ComputeMetricsUseCase:
    def __init__(self, trade_repo: ITradeRepository):
        self.trade_repo = trade_repo

    def execute(self, initial_capital: float = 10000.0) -> Dict[str, Any]:
        """
        Calculates performance metrics from closed trades.
        """
        closed_trades = self.trade_repo.get_positions_by_status(TradeStatus.CLOSED)
        if not closed_trades:
            return {"error": "No closed trades available"}
            
        profits = [t.profit for t in closed_trades if t.profit is not None]
        if not profits:
            return {"error": "No profits recorded"}
            
        wins = [p for p in profits if p > 0]
        losses = [p for p in profits if p <= 0]
        
        win_rate = len(wins) / len(profits) if profits else 0.0
        
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        
        # Simplified R-Multiple (assuming 1R was the risked amount, we'd need exact risk per trade)
        # We will just do average win / average loss
        avg_win = np.mean(wins) if wins else 0.0
        avg_loss = abs(np.mean(losses)) if losses else 0.0
        rr_ratio = avg_win / avg_loss if avg_loss > 0 else 0.0
        
        # Drawdown (simplified)
        cumulative = np.cumsum(profits)
        equity_curve = initial_capital + cumulative
        peak = np.maximum.accumulate(equity_curve)
        drawdown_usd = peak - equity_curve
        max_drawdown_usd = np.max(drawdown_usd) if len(drawdown_usd) > 0 else 0.0
        
        # Calculate % Drawdown relative to the peak equity at the time
        drawdown_pct = (drawdown_usd / peak) * 100 if len(peak) > 0 else 0.0
        max_drawdown_pct = np.max(drawdown_pct) if len(drawdown_pct) > 0 else 0.0
        
        net_profit = sum(profits)
        return_pct = (net_profit / initial_capital) * 100
        
        return {
            "total_trades": len(profits),
            "win_rate": round(win_rate * 100, 2),
            "profit_factor": round(profit_factor, 2),
            "avg_rr": round(rr_ratio, 2),
            "max_drawdown_usd": round(max_drawdown_usd, 2),
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "net_profit": round(net_profit, 2),
            "return_pct": round(return_pct, 2)
        }
