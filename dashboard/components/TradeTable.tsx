"use client";

import Badge from "./Badge";
import type { Trade } from "@/lib/api";
import { toUTCTimeFull } from "@/lib/time";

export interface ForexTableTrade extends Partial<Trade> {
  trade_id?: string;
  time?: string;
  open_time?: string;
  pair?: string;
  symbol?: string;
  type?: "BUY" | "SELL";
  direction?: "BUY" | "SELL";
  lots?: number;
  lot_size?: number;
  entry?: number;
  entry_price?: number;
  exit?: number;
  exit_price?: number;
  reason?: string;
  exit_reason?: string;
  result?: string;
  pnl?: number;
  pnl_usd?: number;
  pnl_pips?: number;
}

interface Props {
  trades: ForexTableTrade[];
  maxRows?: number;
}

const resultVariant = (r?: string) => {
  if (!r) return "yellow";
  const upper = r.toUpperCase();
  if (upper.includes("TARGET") || upper === "WIN") return "green";
  if (upper.includes("SL") || upper.includes("STOP") || upper.includes("LOSS")) return "red";
  if (upper.includes("KALMAN") || upper.includes("RL") || upper.includes("VELOCITY")) return "purple";
  if (upper.includes("TIMEOUT")) return "yellow";
  return "yellow";
};

const formatPrice = (symbol: string = "EURUSD", price?: number) => {
  if (price === undefined || price === null || isNaN(price)) return "--";
  const sym = symbol.toUpperCase();
  if (sym.includes("JPY") || sym.includes("XAU") || sym.includes("GOLD")) {
    return price.toFixed(2);
  }
  return price.toFixed(5);
};

const formatPnl = (val?: number) => {
  if (val === undefined || val === null || isNaN(val)) return "$0.00";
  const sign = val >= 0 ? "+" : "";
  return `${sign}$${val.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
};

export default function TradeTable({ trades, maxRows }: Props) {
  const rows = maxRows ? trades.slice(-maxRows).reverse() : [...trades].reverse();

  if (!rows.length) {
    return (
      <p className="text-[11px] text-center py-8 font-mono" style={{ color: "#3d4450" }}>
        NO FOREX TRADES RECORDED
      </p>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left border-collapse">
        <thead>
          <tr className="border-b border-[#252a33] text-[9px] uppercase tracking-wider text-[#5a6270]">
            <th className="py-2 px-3 font-semibold">Time</th>
            <th className="py-2 px-3 font-semibold">Pair</th>
            <th className="py-2 px-3 font-semibold">Type</th>
            <th className="py-2 px-3 font-semibold">Lots</th>
            <th className="py-2 px-3 font-semibold">Entry</th>
            <th className="py-2 px-3 font-semibold">Exit</th>
            <th className="py-2 px-3 font-semibold">Reason</th>
            <th className="py-2 px-3 font-semibold text-right">P&L ($)</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-[#1e222c] text-[11px] font-mono">
          {rows.map((t, i) => {
            const rawTime = t.time || t.open_time || t.entry_time || "";
            const displayTime = rawTime.includes("T") || rawTime.includes(":") 
              ? toUTCTimeFull(String(rawTime)) 
              : rawTime || "--:--:--";
            const pair = t.pair || t.symbol || "EURUSD";
            const dir = (t.type || t.direction || "BUY").toUpperCase() as "BUY" | "SELL";
            const lots = t.lots ?? t.lot_size ?? 1.0;
            const entry = t.entry ?? t.entry_price ?? 0;
            const exit = t.exit ?? t.exit_price;
            const reason = t.reason || t.exit_reason || t.result || "OPEN";
            const pnl = t.pnl_usd ?? t.pnl ?? 0;

            return (
              <tr key={i} className="hover:bg-[#181c24]/60 transition-colors">
                <td className="py-2.5 px-3 text-[#5a6270] whitespace-nowrap">
                  {displayTime}
                </td>
                <td className="py-2.5 px-3 text-[#c8cdd5] font-bold">
                  {pair}
                </td>
                <td className="py-2.5 px-3">
                  <Badge label={dir} variant={dir === "BUY" ? "green" : "red"} />
                </td>
                <td className="py-2.5 px-3 text-[#c8cdd5]">
                  {lots.toFixed(2)}
                </td>
                <td className="py-2.5 px-3 text-[#c8cdd5]">
                  {formatPrice(pair, entry)}
                </td>
                <td className="py-2.5 px-3 text-[#c8cdd5]">
                  {formatPrice(pair, exit)}
                </td>
                <td className="py-2.5 px-3">
                  <Badge label={reason} variant={resultVariant(reason)} />
                </td>
                <td
                  className="py-2.5 px-3 text-right font-bold whitespace-nowrap"
                  style={{ color: pnl > 0 ? "#00e87b" : pnl < 0 ? "#ff3e3e" : "#5a6270" }}
                >
                  {formatPnl(pnl)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

