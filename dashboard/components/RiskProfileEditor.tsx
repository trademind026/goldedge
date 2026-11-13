"use client";

import { useState } from "react";
import { type RiskProfile, type RiskLevel, postJSON } from "@/lib/api";
import { Save } from "lucide-react";

interface Props {
  level: RiskLevel;
  profile: RiskProfile;
  onSaved: () => void;
}

export default function RiskProfileEditor({ level, profile, onSaved }: Props) {
  const [data, setData] = useState<RiskProfile>(profile);
  const [saving, setSaving] = useState(false);

  const handleSave = async () => {
    setSaving(true);
    try {
      await postJSON("/risk/update", { level, profile: data });
      onSaved();
    } catch (e) {
      alert("Failed to save risk profile");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={{ background: "#0a0c10", padding: 20, borderRadius: 12, border: "1px solid #1a1a2e" }}>
      <h3 style={{ margin: "0 0 16px 0", fontSize: 16, color: "#fff" }}>
        Edit {profile.name} <span style={{ color: "#5a6270", fontSize: 13 }}>({level.toUpperCase()})</span>
      </h3>

      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <label style={{ fontSize: 13, color: "#c8cdd5", display: "flex", justifyContent: "space-between" }}>
          Risk per Trade %
          <input 
            type="number" step="0.1" 
            value={data.risk_per_trade_pct} 
            onChange={(e) => setData({ ...data, risk_per_trade_pct: parseFloat(e.target.value) })}
            style={{ width: 80, background: "#111318", border: "1px solid #1a1a2e", color: "#fff", padding: "4px 8px" }}
          />
        </label>
        
        <label style={{ fontSize: 13, color: "#c8cdd5", display: "flex", justifyContent: "space-between" }}>
          Max Daily Loss %
          <input 
            type="number" step="0.1" 
            value={data.max_daily_loss_pct} 
            onChange={(e) => setData({ ...data, max_daily_loss_pct: parseFloat(e.target.value) })}
            style={{ width: 80, background: "#111318", border: "1px solid #1a1a2e", color: "#fff", padding: "4px 8px" }}
          />
        </label>
        
        <label style={{ fontSize: 13, color: "#c8cdd5", display: "flex", justifyContent: "space-between" }}>
          Max Trades per Day
          <input 
            type="number" 
            value={data.max_trades_per_day} 
            onChange={(e) => setData({ ...data, max_trades_per_day: parseInt(e.target.value) })}
            style={{ width: 80, background: "#111318", border: "1px solid #1a1a2e", color: "#fff", padding: "4px 8px" }}
          />
        </label>
        
        <label style={{ fontSize: 13, color: "#c8cdd5", display: "flex", justifyContent: "space-between" }}>
          SL ATR Multiplier
          <input 
            type="number" step="0.1" 
            value={data.sl_atr_multiplier} 
            onChange={(e) => setData({ ...data, sl_atr_multiplier: parseFloat(e.target.value) })}
            style={{ width: 80, background: "#111318", border: "1px solid #1a1a2e", color: "#fff", padding: "4px 8px" }}
          />
        </label>
        
        <label style={{ fontSize: 13, color: "#c8cdd5", display: "flex", justifyContent: "space-between" }}>
          TP ATR Multiplier
          <input 
            type="number" step="0.1" 
            value={data.tp_atr_multiplier} 
            onChange={(e) => setData({ ...data, tp_atr_multiplier: parseFloat(e.target.value) })}
            style={{ width: 80, background: "#111318", border: "1px solid #1a1a2e", color: "#fff", padding: "4px 8px" }}
          />
        </label>
        
        <label style={{ fontSize: 13, color: "#c8cdd5", display: "flex", justifyContent: "space-between" }}>
          Min Score Threshold
          <input 
            type="number" step="0.01" 
            value={data.min_score_threshold} 
            onChange={(e) => setData({ ...data, min_score_threshold: parseFloat(e.target.value) })}
            style={{ width: 80, background: "#111318", border: "1px solid #1a1a2e", color: "#fff", padding: "4px 8px" }}
          />
        </label>
      </div>

      <button 
        onClick={handleSave} 
        disabled={saving}
        style={{ 
          marginTop: 20, width: "100%", padding: "10px", background: "#00b4ff", color: "#fff", 
          border: "none", borderRadius: 6, display: "flex", justifyContent: "center", alignItems: "center", gap: 8,
          cursor: saving ? "not-allowed" : "pointer"
        }}
      >
        <Save size={16} /> {saving ? "Saving..." : "Save Profile"}
      </button>
    </div>
  );
}
