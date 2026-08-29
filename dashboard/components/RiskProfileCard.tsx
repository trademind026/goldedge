"use client";

import type { RiskProfile, RiskLevel } from "@/lib/api";

interface Props {
  level: RiskLevel;
  profile: RiskProfile;
  active: boolean;
  onSelect: (l: RiskLevel) => void;
  context?: "live" | "backtest";
}

const colors: Record<RiskLevel, string> = {
  low: "#4da6ff",
  medium: "#e8c300",
  high: "#00e87b",
};

export default function RiskProfileCard({ level, profile, active, onSelect, context = "live" }: Props) {
  const c = colors[level];
  const ctxColor = context === "live" ? "#00e87b" : "#a78bfa";
  const ctxLabel = context === "live" ? "LIVE" : "BACKTEST";
  return (
    <button
      onClick={() => onSelect(level)}
      className="text-left w-full p-4 transition-all"
      style={{
        background: active ? '#1e222c' : '#181c24',
        border: `1px solid ${active ? c : '#252a33'}`,
        color: active ? c : '#5a6270',
      }}
    >
      <div className="flex items-center justify-between mb-3">
        <div className="text-[11px] font-bold uppercase tracking-wider" style={{ color: active ? c : '#5a6270' }}>
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
          ["Lot size",           profile.base_lot_size],
          ["Lot mult",           `×${profile.lot_multiplier}`],
          ["SL",                 `${(profile.sl_pct * 100).toFixed(0)}%`],
          ["Target",             `${(profile.tgt_pct * 100).toFixed(0)}%`],
          ["Min score (Call)",   (profile.score_threshold * 100).toFixed(0) + "%"],
          ["Min score (Put)",    (profile.put_score_threshold * 100).toFixed(0) + "%"],
          ["Min score (VWAP)",   (profile.vwap_score_threshold * 100).toFixed(0) + "%"],
          ["Blocked (Sideways)", (profile.disable_sideways_strategies || []).length > 0 ? (profile.disable_sideways_strategies || []).join(", ") : "NONE"],
          ["Blocked (Global)",   (profile.disable_strategies || []).length > 0 ? (profile.disable_strategies || []).join(", ") : "NONE"],
          ["Max trades",         profile.max_trades_day],
          ["Max premium",        `₹${profile.max_premium}`],
          ["Capital/trade",      `${(profile.max_capital_per_trade * 100).toFixed(0)}%`],
        ].map(([k, v]) => (
          <div key={String(k)} className="flex justify-between gap-2">
            <span style={{ color: k === "Sideways momentum" ? '#a78bfa' : '#5a6270' }}>{k}</span>
            <span className="font-semibold" style={{ color: '#c8cdd5' }}>{v}</span>
          </div>
        ))}
      </div>
    </button>
  );
}
