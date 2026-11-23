"use client";

import Badge from "./Badge";
import type { Trade } from "@/lib/api";
import { toDateStr, toUTCTime, toUTCTimeFull } from "@/lib/time";

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
  detailed?: boolean;
}

const resultVariant = (r?: string) => {
  if (!r) return "yellow";
  const upper = r.toUpperCase();
  if (upper.includes("TARGET") || upper.includes("TP") || upper === "WIN") return "green";
  if (upper.includes("SL") || upper.includes("STOP") || upper.includes("LOSS") || upper.includes("EARLY")) return "red";
  if (upper.includes("KALMAN") || upper.includes("RL") || upper.includes("VELOCITY")) return "purple";
  if (upper.includes("TRAIL") || upper.includes("BE")) return "blue";
  if (upper.includes("TIMEOUT") || upper.includes("TIME")) return "yellow";
  return "yellow";
};

const formatPrice = (symbol: string = "XAUUSD", price?: number) => {
  if (price === undefined || price === null || isNaN(price)) return "--";
  const sym = symbol.toUpperCase();
  if (sym.includes("JPY") || sym.includes("XAU") || sym.includes("GOLD")) {
    return price.toFixed(2);
  }
  return price.toFixed(5);
};

const formatPnl = (val?: number) => {
  if (val === undefined || val === null || isNaN(val)) return "$0.00";
  const sign = val >= 0 ? "+" : "-";
  return `${sign}$${Math.abs(val).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
};

export default function TradeTable({ trades, maxRows, detailed = false }: Props) {
  const rows = maxRows ? trades.slice(-maxRows).reverse() : [...trades].reverse();

  if (!rows.length) {
    return (
      <p className="text-[11px] text-center py-8 font-mono" style={{ color: "#3d4450" }}>
        NO TRADES RECORDED
      </p>
    );
  }

  return (
    <div className="overflow-x-auto max-h-[520px] overflow-y-auto">
      <table className="w-full text-left border-collapse">
        <thead className="sticky top-0 z-10" style={{ background: "#0e1117" }}>
          <tr className="border-b border-[#252a33] text-[9px] uppercase tracking-wider text-[#5a6270]">
            {detailed && <th className="py-2 px-2.5 font-semibold">#</th>}
            <th className="py-2 px-3 font-semibold">{detailed ? "Entry -> Exit (UTC)" : "Time"}</th>
            <th className="py-2 px-3 font-semibold">Pair</th>
            <th className="py-2 px-3 font-semibold">Side</th>
            {detailed && <th className="py-2 px-3 font-semibold">Strategy</th>}
            <th className="py-2 px-3 font-semibold text-right">Lots</th>
            <th className="py-2 px-3 font-semibold text-right">Entry</th>
            <th className="py-2 px-3 font-semibold text-right">Exit</th>
            {detailed && <th className="py-2 px-3 font-semibold text-right">SL / Target</th>}
            {detailed && <th className="py-2 px-3 font-semibold text-right">Plan Risk</th>}
            {detailed && <th className="py-2 px-3 font-semibold">Regime</th>}
            <th className="py-2 px-3 font-semibold">Reason</th>
            <th className="py-2 px-3 font-semibold text-right">P&amp;L ($)</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-[#1e222c] text-[11px] font-mono">
          {rows.map((t, i) => {
            const rawEntry = t.entry_time || t.time || t.open_time || "";
            const rawExit = t.exit_time || "";
            const displayTime = detailed && rawEntry
              ? `${toDateStr(String(rawEntry))} ${toUTCTime(String(rawEntry))}${rawExit ? ` -> ${toUTCTime(String(rawExit))}` : ""}`
              : (rawEntry.includes("T") || rawEntry.includes(":"))
                ? toUTCTimeFull(String(rawEntry))
                : rawEntry || "--:--:--";
            const pair = t.pair || t.symbol || "XAUUSD";
            const dir = (t.type || t.direction || "BUY").toUpperCase() as "BUY" | "SELL";
            const lots = Number(t.lots ?? t.qty ?? t.lot_size ?? 0.01);
            const entry = t.entry ?? t.entry_price ?? 0;
            const exit = t.exit ?? t.exit_price;
            const sl = t.sl;
            const target = t.target;
            const reason = t.reason || t.exit_reason || t.result || "OPEN";
            const pnl = Number(t.pnl_usd ?? t.pnl ?? 0);
            const planRisk = t.planned_risk_pct;
            const score = t.score ?? t.final_score;

            return (
              <tr key={i} className="hover:bg-[#181c24]/60 transition-colors">
                {detailed && (
                  <td className="py-2 px-2.5 text-[#3d4450]">{rows.length - i}</td>
                )}
                <td className="py-2 px-3 text-[#5a6270] whitespace-nowrap">
                  {displayTime}
                </td>
                <td className="py-2 px-3 text-[#c8cdd5] font-bold">
                  {pair}
                </td>
                <td className="py-2 px-3">
                  <Badge label={dir} variant={dir === "BUY" ? "green" : "red"} />
                </td>
                {detailed && (
                  <td className="py-2 px-3 text-[#c8cdd5] whitespace-nowrap">
                    <span>{t.strategy || "--"}</span>
                    {score !== undefined && score !== null && (
                      <span className="ml-1.5 text-[9px] text-[#5a6270]">
                        ({Number(score).toFixed(2)})
                      </span>
                    )}
                  </td>
                )}
                <td className="py-2 px-3 text-right text-[#c8cdd5]">
                  {lots.toFixed(2)}
                </td>
                <td className="py-2 px-3 text-right text-[#c8cdd5]">
                  {formatPrice(pair, entry)}
                </td>
                <td className="py-2 px-3 text-right text-[#c8cdd5]">
                  {formatPrice(pair, exit)}
                </td>
                {detailed && (
                  <td className="py-2 px-3 text-right text-[10px] text-[#5a6270] whitespace-nowrap">
                    {formatPrice(pair, sl)} / {formatPrice(pair, target)}
                  </td>
                )}
                {detailed && (
                  <td
                    className="py-2 px-3 text-right whitespace-nowrap"
                    style={{
                      color:
                        planRisk === undefined
                          ? "#5a6270"
                          : planRisk <= 1.0
                            ? "#00e87b"
                            : "#ff3e3e",
                    }}
                  >
                    {planRisk !== undefined ? `${planRisk.toFixed(2)}%` : "--"}
                  </td>
                )}
                {detailed && (
                  <td className="py-2 px-3 text-[10px] text-[#5a6270] whitespace-nowrap">
                    {t.regime || "--"}
                  </td>
                )}
                <td className="py-2 px-3 whitespace-nowrap">
                  <Badge label={reason} variant={resultVariant(reason)} />
                </td>
                <td
                  className="py-2 px-3 text-right font-bold whitespace-nowrap"
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
