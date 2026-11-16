import csv
import io
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from pathlib import Path


@dataclass
class ForensicTradeRecord:
    trade_id: str
    symbol: str
    direction: str
    entry_time: datetime
    exit_time: datetime
    entry_fill: float
    exit_fill: float
    lots: float
    slippage_incurred_usd: float
    total_swap_usd: float
    exit_reason: str
    net_pnl_usd: float
    net_pips: float
    mae_pips: float
    mfe_pips: float


class ForexAuditService:
    """
    STAGE 7: Daily Settlement & Forensic Auditing.
    7.1 Overnight Carry & Swap Accounting (with Wednesday 3x multiplier).
    7.2 Forensic Audit Trail Logging with MAE and MFE tracking and CSV export.
    """

    def __init__(self, contract_size: float = 100000.0):
        self.contract_size = contract_size
        self.records: List[ForensicTradeRecord] = []

    def calculate_swap(
        self,
        lots: float,
        point_size: float,
        broker_swap_points: float,
        rollover_time: datetime,
    ) -> float:
        """
        Calculates swap PnL in USD.
        Wednesday rollover (crossing Wednesday 21:00 UTC) carries 3x multiplier
        for weekend settlement.
        """
        # Wednesday is weekday 2 (Monday=0, Tuesday=1, Wednesday=2, Thursday=3, Friday=4)
        is_wednesday_roll = rollover_time.weekday() == 2
        multiplier = 3.0 if is_wednesday_roll else 1.0

        swap_usd = lots * self.contract_size * point_size * broker_swap_points * multiplier
        return round(float(swap_usd), 2)

    def record_closed_trade(
        self,
        trade_id: str,
        symbol: str,
        direction: str,
        entry_time: datetime,
        exit_time: datetime,
        entry_fill: float,
        exit_fill: float,
        lots: float,
        slippage_incurred_usd: float,
        total_swap_usd: float,
        exit_reason: str,
        net_pnl_usd: float,
        pip_unit: float,
        mae_pips: float = 0.0,
        mfe_pips: float = 0.0,
    ) -> ForensicTradeRecord:
        """Creates and stores an institutional forensic audit record."""
        is_buy = direction.upper() in ("BUY", "CALL")
        if is_buy:
            net_pips = (exit_fill - entry_fill) / pip_unit
        else:
            net_pips = (entry_fill - exit_fill) / pip_unit

        record = ForensicTradeRecord(
            trade_id=trade_id,
            symbol=symbol,
            direction=direction.upper(),
            entry_time=entry_time,
            exit_time=exit_time,
            entry_fill=round(entry_fill, 5 if "JPY" not in symbol and "XAU" not in symbol else 2),
            exit_fill=round(exit_fill, 5 if "JPY" not in symbol and "XAU" not in symbol else 2),
            lots=round(lots, 2),
            slippage_incurred_usd=round(slippage_incurred_usd, 2),
            total_swap_usd=round(total_swap_usd, 2),
            exit_reason=exit_reason,
            net_pnl_usd=round(net_pnl_usd, 2),
            net_pips=round(net_pips, 1),
            mae_pips=round(abs(mae_pips), 1),
            mfe_pips=round(abs(mfe_pips), 1),
        )
        self.records.append(record)
        return record

    def export_to_csv(self, file_path: Optional[str] = None) -> str:
        """Exports all forensic trade records to CSV format."""
        fieldnames = [
            "trade_id", "symbol", "direction", "entry_time", "exit_time",
            "entry_fill", "exit_fill", "lots", "slippage_incurred_usd",
            "total_swap_usd", "exit_reason", "net_pnl_usd", "net_pips",
            "mae_pips", "mfe_pips"
        ]
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()

        for rec in self.records:
            d = asdict(rec)
            d["entry_time"] = d["entry_time"].isoformat() if hasattr(d["entry_time"], "isoformat") else str(d["entry_time"])
            d["exit_time"] = d["exit_time"].isoformat() if hasattr(d["exit_time"], "isoformat") else str(d["exit_time"])
            writer.writerow(d)

        csv_str = output.getvalue()
        if file_path:
            p = Path(file_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(csv_str)

        return csv_str
