"use client";

import type { RiskProfile, RiskLevel, BacktestProfile } from "@/lib/api";
import { fmtUsd, fmtWinRate } from "@/lib/backtestHelpers";

interface Props {
  level?: RiskLevel;
  profile: RiskProfile;
  active?: boolean;
  onSelect?: (l: RiskLevel) => void;
  context?: "live" | "backtest";
  result?: BacktestProfile;
}

const colors: Record<RiskLevel, string> = {
  low: "#4da6ff",
  medium: "#e8c300",
  high: "#00e87b",
};

export default function RiskProfileCard({
  level = "medium",
  profile,
  active = false,
  onSelect,
  context = "backtest",
  result,
}: Props) {
  const c = colors[level];
  const ctxColor = context === "live" ? "#00e87b" : "#a78bfa";
  const ctxLabel = context === "live" ? "LIVE" : "BACKTEST";
  const hasResult = Boolean(result && (result.trades ?? 0) > 0);

  return (
    <button
      type="button"
      onClick={() => onSelect?.(level)}
      className="text-left w-full p-4 transition-all"
      style={{
        background: active ? "#1e222c" : "#181c24",
        border: `1px solid ${active ? c : "#252a33"}`,
        color: active ? c : "#5a6270",
      }}
    >
      <div className="flex items-center justify-between mb-3">
        <div className="text-[11px] font-bold uppercase tracking-wider" style={{ color: active ? c : "#5a6270" }}>
          {level} — {profile.name}
        </div>
        <span
          className="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5"
          style={{ background: `${ctxColor}18`, color: ctxColor, border: `1px solid ${ctxColor}40` }}
        >
          {ctxLabel}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-x-6 gap-y-1.5 text-[11px]">
        {[
          ["Risk / trade", `${profile.risk_per_trade_pct}%`],
          ["Max daily loss", `${profile.max_daily_loss_pct}%`],
          ["SL ATR mult", `${profile.sl_atr_multiplier}×`],
          ["TP ATR mult", `${profile.tp_atr_multiplier}×`],
          ["Min score", `${profile.min_score_threshold.toFixed(2)}`],
          ["Max trades/day", `${profile.max_trades_per_day}`],
          ["Max consec loss", `${profile.max_consecutive_losses}`],
          [
            "Last run",
            hasResult && result ? `${fmtUsd(result.pnl, true)} (${fmtWinRate(result.win_rate)})` : "—",
          ],
        ].map(([k, v]) => (
          <div key={String(k)} className="flex justify-between gap-2">
            <span style={{ color: "#5a6270" }}>{k}</span>
            <span
              className="font-semibold"
              style={{
                color:
                  k === "Last run" && hasResult && result
                    ? result.pnl >= 0
                      ? "#00e87b"
                      : "#ff3e3e"
                    : "#c8cdd5",
              }}
            >
              {v}
            </span>
          </div>
        ))}
      </div>
    </button>
  );
}
