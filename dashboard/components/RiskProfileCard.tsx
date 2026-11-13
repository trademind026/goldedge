import { type RiskProfile } from "@/lib/api";

export default function RiskProfileCard({ profile }: { profile: RiskProfile }) {
  return (
    <div style={{ background: "#0a0c10", padding: 20, borderRadius: 12, border: "1px solid #1a1a2e" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <h3 style={{ margin: 0, fontSize: 16, fontWeight: 600, color: "#fff" }}>
          {profile.name} <span style={{ color: "#5a6270", fontSize: 13, fontWeight: 400 }}>({profile.context})</span>
        </h3>
      </div>
      
      <div style={{ background: "#111318", borderRadius: 8, padding: 12 }}>
        <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
          <tbody>
            <tr style={{ borderBottom: "1px solid #1a1a2e" }}>
              <td style={{ padding: "8px 0", color: "#5a6270" }}>Risk per Trade</td>
              <td style={{ padding: "8px 0", textAlign: "right" }}>{profile.risk_per_trade_pct}%</td>
            </tr>
            <tr style={{ borderBottom: "1px solid #1a1a2e" }}>
              <td style={{ padding: "8px 0", color: "#5a6270" }}>Max Daily Loss</td>
              <td style={{ padding: "8px 0", textAlign: "right" }}>{profile.max_daily_loss_pct}%</td>
            </tr>
            <tr style={{ borderBottom: "1px solid #1a1a2e" }}>
              <td style={{ padding: "8px 0", color: "#5a6270" }}>Max Trades/Day</td>
              <td style={{ padding: "8px 0", textAlign: "right" }}>{profile.max_trades_per_day}</td>
            </tr>
            <tr style={{ borderBottom: "1px solid #1a1a2e" }}>
              <td style={{ padding: "8px 0", color: "#5a6270" }}>SL / TP Multiplier</td>
              <td style={{ padding: "8px 0", textAlign: "right" }}>{profile.sl_atr_multiplier}x / {profile.tp_atr_multiplier}x</td>
            </tr>
            <tr style={{ borderBottom: "1px solid #1a1a2e" }}>
              <td style={{ padding: "8px 0", color: "#5a6270" }}>Min ML Score</td>
              <td style={{ padding: "8px 0", textAlign: "right" }}>{profile.min_score_threshold}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}
