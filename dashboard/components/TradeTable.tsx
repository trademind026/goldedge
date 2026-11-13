"use client";

import Badge from "./Badge";
import type { Trade } from "@/lib/api";

import { toDateStr, toUTCTimeFull } from "@/lib/time";

type TradeWithOptionalCapital = Trade & { initial_capital?: number };


interface Props {
  trades: Trade[];
  maxRows?: number;
}

const resultVariant = (r: string) => {
  if (r === "TARGET") return "green";
  if (r === "SL" || r === "TRAILING_SL") return "red";
  if (r === "RL_EXIT" || r === "DQN_EXIT") return "purple";
  return "yellow";
};

const fmt = (p: number) =>
  `₹${p >= 0 ? "+" : ""}${p.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;

export default function TradeTable({ trades, maxRows }: Props) {
  const tradesWithCapital = trades as TradeWithOptionalCapital[];
  const rows = maxRows ? tradesWithCapital.slice(-maxRows).reverse() : [...tradesWithCapital].reverse();


  if (!rows.length) {
    return (
      <p className="text-[11px] text-center py-8" style={{ color: '#3d4450' }}>NO TRADES TO DISPLAY</p>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table>
        <thead>
          <tr>
            {["Date", "Time", "Symbol", "Dir", "Strategy", "Entry", "Exit", "P&L", "ROI %", "Result", "Score", "Regime"].map(h => (
              <th key={h}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((t, i) => (
            <tr key={i}>
              <td style={{ color: '#5a6270' }}>
                {toDateStr(String(t.entry_time))}
              </td>
              <td style={{ color: '#5a6270' }}>
                {toUTCTimeFull(String(t.entry_time))}
              </td>
              <td style={{ color: '#c8cdd5' }}>{t.symbol}</td>
              <td>
                <Badge label={t.direction} variant={t.direction === "BUY" ? "green" : "red"} />
              </td>
              <td style={{ color: '#5a6270' }}>{t.strategy?.replace(/_/g, " ")}</td>
              <td>₹{t.entry_price?.toFixed(1)}</td>
              <td>₹{t.exit_price?.toFixed(1) ?? "--"}</td>
              <td style={{ color: t.pnl > 0 ? '#00e87b' : t.pnl < 0 ? '#ff3e3e' : '#5a6270', fontWeight: 600 }}>
                {fmt(t.pnl)}
              </td>
              <td style={{ color: t.pnl > 0 ? '#00e87b' : t.pnl < 0 ? '#ff3e3e' : '#5a6270' }}>
                {t.initial_capital ? `${((t.pnl / t.initial_capital) * 100).toFixed(2)}%` : '--'}
              </td>
              <td>
                <Badge label={t.exit_reason} variant={resultVariant(t.exit_reason)} />
              </td>
              <td>{(t.final_score * 100).toFixed(0)}%</td>
              <td style={{ color: '#5a6270' }}>{t.regime}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
