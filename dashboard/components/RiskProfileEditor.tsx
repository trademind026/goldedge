"use client";

import { useState } from "react";
import type { RiskProfile, RiskLevel } from "@/lib/api";
import { postJSON } from "@/lib/api";
import { X, Save, RefreshCcw } from "lucide-react";

interface Props {
  level: RiskLevel;
  profile: RiskProfile;
  context: "live" | "backtest";
  onClose: () => void;
  onSaved: () => void;
}

export default function RiskProfileEditor({ level, profile, context, onClose, onSaved }: Props) {
  const [formData, setFormData] = useState<Partial<RiskProfile>>(profile);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const { name, value, type } = e.target;
    let parsedValue: string | number | boolean = value;
    if (type === "number") {
      parsedValue = parseFloat(value);
    } else if (type === "checkbox") {
      parsedValue = e.target.checked;
    }
    setFormData((prev) => ({ ...prev, [name]: parsedValue }));
  };

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      await postJSON("/api/risk/profiles/update", { level, context, overrides: formData });
      onSaved();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to save profile");
    } finally {
      setSaving(false);
    }
  };

  const handleReset = async () => {
    if (!confirm(`Are you sure you want to reset ${level.toUpperCase()} ${context.toUpperCase()} to hardcoded defaults?`)) return;
    setSaving(true);
    setError(null);
    try {
      await postJSON("/api/risk/profiles/reset", { level, context });
      onSaved();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to reset profile");
    } finally {
      setSaving(false);
    }
  };

  const editableFields = [
    { key: "base_lot_size", label: "Base Lot Size", type: "number", step: "1" },
    { key: "lot_multiplier", label: "Lot Multiplier", type: "number", step: "0.1" },
    { key: "max_capital_per_trade", label: "Max Capital/Trade", type: "number", step: "0.001" },
    { key: "sl_pct", label: "Stop Loss %", type: "number", step: "0.01" },
    { key: "tgt_pct", label: "Target %", type: "number", step: "0.01" },
    { key: "sl_min_pct", label: "SL Min %", type: "number", step: "0.01" },
    { key: "sl_max_pct", label: "SL Max %", type: "number", step: "0.01" },
    { key: "tgt_min_pct", label: "Target Min %", type: "number", step: "0.01" },
    { key: "tgt_max_pct", label: "Target Max %", type: "number", step: "0.01" },
    { key: "trailing_trigger", label: "Trailing Trigger %", type: "number", step: "0.01" },
    { key: "trailing_lock", label: "Trailing Lock %", type: "number", step: "0.01" },
    { key: "score_threshold", label: "Min Score (CALL)", type: "number", step: "0.01" },
    { key: "put_score_threshold", label: "Min Score (PUT)", type: "number", step: "0.01" },
    { key: "vwap_score_threshold", label: "Min Score (VWAP)", type: "number", step: "0.01" },
    { key: "max_trades_day", label: "Max Trades/Day", type: "number", step: "1" },
    { key: "max_premium", label: "Max Premium (₹)", type: "number", step: "1" },
    { key: "skip_first_min", label: "Skip First Min", type: "number", step: "1" },
    { key: "skip_last_min", label: "Skip Last Min", type: "number", step: "1" },
    { key: "afternoon_cut", label: "Afternoon Cut (Mins from open)", type: "number", step: "1" },
    { key: "max_hold_bars", label: "Max Hold Bars (Mins)", type: "number", step: "1" },
    { key: "news_block_threshold", label: "News Block Threshold", type: "number", step: "0.01" },
    { key: "news_boost_threshold", label: "News Boost Threshold", type: "number", step: "0.01" },
    { key: "news_boost_amount", label: "News Boost Amount", type: "number", step: "0.01" },
    { key: "max_strike_offset", label: "Max Strike Offset", type: "number", step: "1" },
  ];

  const formatIST = (key: string, val: number | string | undefined) => {
    if (val === undefined || val === "") return null;
    const num = Number(val);
    if (isNaN(num)) return null;

    if (key === "afternoon_cut" || key === "skip_first_min") {
      const totalMins = 9 * 60 + 15 + num;
      const h = Math.floor(totalMins / 60) % 24;
      const m = totalMins % 60;
      const period = h >= 12 ? 'PM' : 'AM';
      const h12 = h % 12 || 12;
      return `(${h12}:${m.toString().padStart(2, "0")} ${period} IST)`;
    }
    if (key === "skip_last_min") {
      const totalMins = 15 * 60 + 30 - num; // 3:30 PM = 15:30
      const h = Math.floor(totalMins / 60) % 24;
      const m = totalMins % 60;
      const period = h >= 12 ? 'PM' : 'AM';
      const h12 = h % 12 || 12;
      return `(${h12}:${m.toString().padStart(2, "0")} ${period} IST)`;
    }
    return null;
  };

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center p-4 bg-black/80 backdrop-blur-sm">
      <div className="bg-[#111318] border border-[#252a33] rounded-lg w-full max-w-4xl max-h-[90vh] flex flex-col shadow-2xl">
        <div className="flex items-center justify-between p-5 border-b border-[#252a33]">
          <h2 className="text-sm font-bold uppercase tracking-wider text-white flex items-center gap-2">
            Edit Profile: <span style={{ color: context === "live" ? "#00e87b" : "#a78bfa" }}>{level.toUpperCase()}</span>
            <span className="text-[10px] px-1.5 py-0.5 rounded border" style={{ color: context === "live" ? "#00e87b" : "#a78bfa", borderColor: context === "live" ? "#00e87b40" : "#a78bfa40", background: context === "live" ? "#00e87b10" : "#a78bfa10" }}>
              {context.toUpperCase()}
            </span>
          </h2>
          <button onClick={onClose} className="p-1 hover:bg-[#252a33] rounded text-[#5a6270] hover:text-white transition-colors">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="p-5 overflow-y-auto flex-1 grid grid-cols-2 md:grid-cols-3 gap-x-6 gap-y-4">
          {editableFields.map((field) => {
            const timePreview = formatIST(field.key, (formData as Record<string, string | number>)[field.key]);
            return (
              <div key={field.key} className="flex flex-col gap-1">
                <label className="text-[10px] uppercase tracking-wider text-[#5a6270]">
                  {field.label}
                  {timePreview && <span className="text-[#00e87b] normal-case tracking-normal ml-1 font-semibold">{timePreview}</span>}
                </label>
                <input
                type={field.type}
                name={field.key}
                step={field.step}
                value={((formData as Record<string, string | number>)[field.key]) ?? ""}
                onChange={handleChange}
                className="bg-[#181c24] border border-[#252a33] text-[#c8cdd5] px-3 py-1.5 text-sm focus:outline-none focus:border-[#00e87b] transition-colors"
              />
            </div>
          )})}
          
          <div className="col-span-2 md:col-span-3 flex flex-col gap-2 mt-2 pt-4 border-t border-[#252a33]">
            <label className="text-[10px] uppercase tracking-wider text-[#5a6270]">
              Block in Sideways Regime
            </label>
            <div className="flex flex-wrap gap-4">
              {["bearish_momentum", "bullish_momentum", "vwap_momentum_breakout", "mean_reversion"].map(strategy => {
                const checked = (formData.disable_sideways_strategies || []).includes(strategy);
                return (
                  <label key={strategy} className="flex items-center gap-2 text-xs text-[#c8cdd5] cursor-pointer hover:text-white transition-colors">
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={(e) => {
                        const newStrats = new Set(formData.disable_sideways_strategies || []);
                        if (e.target.checked) newStrats.add(strategy);
                        else newStrats.delete(strategy);
                        setFormData(prev => ({ ...prev, disable_sideways_strategies: Array.from(newStrats) }));
                      }}
                      className="accent-[#00e87b] cursor-pointer w-3.5 h-3.5"
                    />
                    {strategy}
                  </label>
                );
              })}
            </div>
          </div>

          <div className="col-span-2 md:col-span-3 flex flex-col gap-2 mt-2 pt-4 border-t border-[#252a33]">
            <label className="text-[10px] uppercase tracking-wider text-[#5a6270]">
              Globally Disabled Strategies (All Regimes)
            </label>
            <div className="flex flex-wrap gap-4">
              {["bearish_momentum", "bullish_momentum", "vwap_momentum_breakout", "mean_reversion"].map(strategy => {
                const checked = (formData.disable_strategies || []).includes(strategy);
                return (
                  <label key={`global-${strategy}`} className="flex items-center gap-2 text-xs text-[#ff6b6b] cursor-pointer hover:text-[#ff8a8a] transition-colors">
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={(e) => {
                        const newStrats = new Set(formData.disable_strategies || []);
                        if (e.target.checked) newStrats.add(strategy);
                        else newStrats.delete(strategy);
                        setFormData(prev => ({ ...prev, disable_strategies: Array.from(newStrats) }));
                      }}
                      className="accent-[#ff6b6b] cursor-pointer w-3.5 h-3.5"
                    />
                    {strategy}
                  </label>
                );
              })}
            </div>
          </div>
        </div>

        {error && (
          <div className="px-5 py-3 bg-[#3d1414] border-t border-[#ff3e3e] text-[#ff3e3e] text-xs">
            {error}
          </div>
        )}

        <div className="p-5 border-t border-[#252a33] flex justify-between items-center bg-[#0d1015]">
          <button
            onClick={handleReset}
            disabled={saving}
            className="flex items-center gap-2 px-4 py-2 bg-[#2a2200] hover:bg-[#3d3200] text-[#e8c300] text-[10px] font-bold uppercase tracking-wider transition-colors disabled:opacity-50"
          >
            <RefreshCcw className="w-3.5 h-3.5" />
            Reset to Default
          </button>
          
          <div className="flex gap-3">
            <button
              onClick={onClose}
              disabled={saving}
              className="px-6 py-2 border border-[#252a33] hover:bg-[#181c24] text-[#a0a5b0] text-[10px] font-bold uppercase tracking-wider transition-colors disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              onClick={handleSave}
              disabled={saving}
              className="flex items-center gap-2 px-6 py-2 bg-[#00e87b] hover:bg-[#00c96b] text-black text-[10px] font-bold uppercase tracking-wider transition-colors disabled:opacity-50"
            >
              <Save className="w-3.5 h-3.5" />
              {saving ? "Saving..." : "Save Changes"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
