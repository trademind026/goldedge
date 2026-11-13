import re

# Fix 6: RiskProfileCard.tsx
card_path = 'dashboard/components/RiskProfileCard.tsx'
with open(card_path, 'r') as f:
    c_content = f.read()

# Replace all the table rows with a simple valid one for Forex
new_table = """
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
"""
# Replace the table content
c_content = re.sub(r'<table.*?>.*?</table>', new_table, c_content, flags=re.DOTALL)
with open(card_path, 'w') as f:
    f.write(c_content)

# Fix 7: RiskProfileEditor.tsx
edit_path = 'dashboard/components/RiskProfileEditor.tsx'
with open(edit_path, 'r') as f:
    e_content = f.read()

# Just wipe out the extra options fields being passed to update
e_content = re.sub(r'disable_sideways_strategies:.*?,\s*', '', e_content)
e_content = re.sub(r'disable_strategies:.*?,\s*', '', e_content)
with open(edit_path, 'w') as f:
    f.write(e_content)

# Fix 8: TradeTable.tsx options fields
tt_path = 'dashboard/components/TradeTable.tsx'
with open(tt_path, 'r') as f:
    tt_content = f.read()
tt_content = tt_content.replace('t.entry_premium', 't.entry_price')
tt_content = tt_content.replace('t.exit_premium', 't.exit_price')
with open(tt_path, 'w') as f:
    f.write(tt_content)

print("Pass 2 applied")
