import re

path = "dashboard/app/page.tsx"
with open(path, "r") as f:
    c = f.read()

# Add a button to run the backtest
replacement = """
            <div className="t-panel p-5 mb-5 text-center text-[11px]" style={{ color: '#3d4450' }}>
              NO BACKTEST DATA FOR <span style={{ color: riskColors[activeRisk] }}>{activeRisk.toUpperCase()}</span> RISK.
              <br/><br/>
              <button 
                onClick={() => {
                  fetch("/api/flask/backtest/run", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ start_date: "2026-08-01", end_date: "2026-08-31", risk: activeRisk })
                  }).then(() => alert("Backtest started in background. Refresh in a moment."));
                }}
                className="t-btn px-4 py-2"
              >
                RUN BACKTEST NOW
              </button>
            </div>
"""

c = re.sub(
    r'<div className="t-panel p-5 mb-5 text-center text-\[11px\]".*?RUN <code.*?</div>',
    replacement.strip(),
    c,
    flags=re.DOTALL
)

with open(path, "w") as f:
    f.write(c)
