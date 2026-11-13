"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import Sidebar from "@/components/Sidebar";
import EquityChart from "@/components/EquityChart";
import TradeTable from "@/components/TradeTable";
import { fetchJSON, type BacktestResults, type EquityCurvePoint } from "@/lib/api";
import { Play, RefreshCw, Terminal, Calendar, Copy, Check, Square } from "lucide-react";

type RiskLevel = "low" | "medium" | "high";
const riskColors: Record<string, string> = { low: "#4da6ff", medium: "#e8c300", high: "#00e87b" };

const pnlFmt = (v: number) =>
  `$${v >= 0 ? "+" : ""}${v.toLocaleString("en-US", { maximumFractionDigits: 2 })}`;

interface AvailableDay { day: string; ticks: number; }

export default function BacktestPage() {
  const [results, setResults] = useState<BacktestResults>({});
  const [curves, setCurves] = useState<Record<string, EquityCurvePoint[]>>({});
  const [selectedRisk, setSelectedRisk] = useState<RiskLevel>("medium");
  const [isRunning, setIsRunning] = useState(false);
  const [runningRisk, setRunningRisk] = useState<string | null>(null);
  const [terminalLines, setTerminalLines] = useState<string[]>([]);
  const termRef = useRef<HTMLDivElement>(null);
  const [copied, setCopied] = useState(false);
  const eventSourceRef = useRef<EventSource | null>(null);

  // Date range
  const [availDays, setAvailDays] = useState<AvailableDay[]>([]);
  const [startDate, setStartDate] = useState("2026-08-01");
  const [endDate, setEndDate] = useState("2026-08-28");

  const handleCopyTerminal = () => {
    navigator.clipboard.writeText(terminalLines.join('\n'));
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const datesInitRef = useRef(false);
  const load = useCallback(async () => {
    try {
      const [r, c, days] = await Promise.all([
        fetchJSON<BacktestResults>("/backtest/results").catch(() => ({})),
        fetchJSON<Record<string, EquityCurvePoint[]>>("/equity/curve").catch(() => ({})),
        fetchJSON<AvailableDay[]>("/days").catch(() => []),
      ]);
      setResults(r);
      setCurves(c);
      if (days.length > 0) {
        setAvailDays(days);
        if (!datesInitRef.current) {
          datesInitRef.current = true;
          setStartDate(String(days[0].day));
          setEndDate(String(days[days.length - 1].day));
        }
      }
    } catch {}
  }, []);

  useEffect(() => { load(); }, [load]);

  // Auto-scroll terminal
  useEffect(() => {
    if (termRef.current) {
      const { scrollTop, scrollHeight, clientHeight } = termRef.current;
      if (scrollHeight - scrollTop - clientHeight < 150) {
        termRef.current.scrollTop = termRef.current.scrollHeight;
      }
    }
  }, [terminalLines.length]);

  // Connect to SSE for live terminal output
  const connectSSE = useCallback(() => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
    }
    const es = new EventSource("/api/flask/backtest/progress");
    es.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.log) {
          setTerminalLines(prev => [...prev, data.log]);
        }
        if (data.status === "done") {
          setIsRunning(false);
          setRunningRisk(null);
          load();
        }
      } catch {}
    };
    es.onerror = () => {
      // Reconnect on error
    };
    eventSourceRef.current = es;
  }, [load]);

  useEffect(() => {
    connectSSE();
    return () => {
      if (eventSourceRef.current) eventSourceRef.current.close();
    };
  }, [connectSSE]);

  const runBacktest = async (risk: RiskLevel) => {
    setIsRunning(true);
    setRunningRisk(risk);
    setTerminalLines([]); // Clear terminal
    try {
      await fetch("/api/flask/backtest/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ 
          start_date: startDate, 
          end_date: endDate, 
          risk 
        }),
      });
      // Terminal output will come via SSE
      // Set a safety timeout to clear running state
      setTimeout(() => {
        setIsRunning(false);
        setRunningRisk(null);
        load();
      }, 300000); // 5min max
    } catch {
      setIsRunning(false);
      setRunningRisk(null);
    }
  };

  // Count available days in selected range
  const daysInRange = availDays.filter(d => {
    const ds = String(d.day);
    return (!startDate || ds >= startDate) && (!endDate || ds <= endDate);
  }).length;

  const ticksInRange = availDays.filter(d => {
    const ds = String(d.day);
    return (!startDate || ds >= startDate) && (!endDate || ds <= endDate);
  }).reduce((sum, d) => sum + d.ticks, 0);

  const p = results[selectedRisk];

  // Color-code terminal lines
  const getLineColor = (line: string) => {
    if (line.includes("ERROR") || line.includes("error")) return '#ff3e3e';
    if (line.includes("WARN") || line.includes("WARNING")) return '#e8c300';
    if (line.includes("ENTRY")) return '#4da6ff';
    if (line.includes("EXIT") && line.includes("SL")) return '#ff3e3e';
    if (line.includes("EXIT") && (line.includes("TARGET") || line.includes("+"))) return '#00e87b';
    if (line.includes("Day result") || line.includes("P&L")) return '#c8cdd5';
    if (line.includes("Total") || line.includes("Avg") || line.includes("Max") || line.includes("Peak") || line.includes("Required") || line.includes("ROI")) return '#00e87b';
    if (line.includes("Replaying") || line.includes("STARTING") || line.includes("FINAL")) return '#4da6ff';
    if (line.includes("REGIME")) return '#e8c300';
    if (line.includes("SKIP")) return '#3d4450';
    if (line.includes("[") && line.includes("bars]")) return '#5a6270';
    return '#5a6270';
  };

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 p-5 overflow-y-auto">
        <div className="flex items-center justify-between mb-5">
          <div>
            <h1 className="text-sm font-bold uppercase tracking-wider" style={{ color: '#00e87b' }}>Backtest</h1>
            <p className="text-[10px] mt-0.5" style={{ color: '#3d4450' }}>TICK REPLAY ENGINE — XAU/USD SPOT GOLD</p>
          </div>
          <button onClick={load} className="t-btn flex items-center gap-1.5">
            <RefreshCw className="w-3 h-3" /> REFRESH
          </button>
        </div>

        {/* Date range picker */}
        <div className="t-panel px-4 py-3 mb-5 flex items-center gap-4 flex-wrap">
          <div className="flex items-center gap-2">
            <Calendar className="w-3.5 h-3.5" style={{ color: '#4da6ff' }} />
            <span className="text-[10px] font-bold uppercase tracking-wider" style={{ color: '#5a6270' }}>DATE RANGE</span>
          </div>
          <div className="flex items-center gap-2">
            <label className="text-[9px] uppercase tracking-wider" style={{ color: '#3d4450' }}>FROM</label>
            <input
              type="date"
              value={startDate}
              onChange={e => setStartDate(e.target.value)}
              className="text-[11px] px-2 py-1 outline-none"
              style={{
                background: '#181c24', border: '1px solid #252a33', color: '#c8cdd5',
                fontFamily: "'JetBrains Mono', monospace",
              }}
            />
          </div>
          <div className="flex items-center gap-2">
            <label className="text-[9px] uppercase tracking-wider" style={{ color: '#3d4450' }}>TO</label>
            <input
              type="date"
              value={endDate}
              onChange={e => setEndDate(e.target.value)}
              className="text-[11px] px-2 py-1 outline-none"
              style={{
                background: '#181c24', border: '1px solid #252a33', color: '#c8cdd5',
                fontFamily: "'JetBrains Mono', monospace",
              }}
            />
          </div>
          <span className="text-[9px]" style={{ color: '#5a6270' }}>
            {daysInRange} TICK-DATA DAYS IN RANGE
            {availDays.length > 0 && (
              <span style={{ color: '#3d4450' }}> / {availDays.length} TOTAL</span>
            )}
            <span style={{ color: '#4da6ff', marginLeft: 8 }}>
              {ticksInRange.toLocaleString()} TICKS
            </span>
          </span>
          {availDays.length > 0 && (
            <button
              onClick={() => {
                setStartDate(String(availDays[0].day));
                setEndDate(String(availDays[availDays.length - 1].day));
              }}
              className="text-[9px] font-bold uppercase tracking-wider px-2 py-0.5"
              style={{ background: '#181c24', border: '1px solid #252a33', color: '#4da6ff' }}
            >
              ALL DATES
            </button>
          )}
        </div>

        {/* Available days breakdown */}
        {availDays.length > 0 && (
          <div className="t-panel px-4 py-2 mb-5">
            <div className="flex flex-wrap gap-1">
              {availDays.map(d => {
                const inRange = (!startDate || d.day >= startDate) && (!endDate || d.day <= endDate);
                return (
                  <button
                    key={d.day}
                    onClick={() => { setStartDate(d.day); setEndDate(d.day); }}
                    className="px-2 py-1 text-[8px] font-mono transition-all"
                    title={`${d.day}: ${d.ticks.toLocaleString()} ticks`}
                    style={{
                      background: inRange ? '#181c24' : '#0e1117',
                      border: `1px solid ${inRange ? '#4da6ff' : '#1a1e26'}`,
                      color: inRange ? '#c8cdd5' : '#3d4450',
                    }}
                  >
                    {d.day.slice(5)}
                    <span style={{ color: '#4da6ff', marginLeft: 4, fontSize: 7 }}>
                      {(d.ticks / 1000).toFixed(0)}k
                    </span>
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {/* Risk profile run buttons */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-[1px] mb-5">
          {(["low", "medium", "high"] as RiskLevel[]).map(r => (
            <button
              key={r}
              onClick={() => runBacktest(r)}
              disabled={isRunning}
              className="w-full flex items-center justify-center gap-2 px-3 py-3 text-[10px] font-bold uppercase tracking-wider transition-all"
              style={{
                background: isRunning && runningRisk === r ? '#252a33' : riskColors[r],
                color: '#000',
                opacity: isRunning && runningRisk !== r ? 0.4 : 1,
              }}
            >
              {isRunning && runningRisk === r ? (
                <><RefreshCw className="w-3 h-3 animate-spin" /> RUNNING...</>
              ) : (
                <><Play className="w-3 h-3" /> RUN {r.toUpperCase()} RISK</>
              )}
            </button>
          ))}
        </div>

        {/* Terminal output */}
        <div className="t-panel mb-5" style={{
          borderColor: isRunning ? '#e8c300' : terminalLines.length > 0 ? '#00e87b' : '#252a33',
        }}>
          <div className="flex items-center justify-between px-4 py-2" style={{
            background: '#0e1117',
            borderBottom: `1px solid ${isRunning ? '#e8c300' : terminalLines.length > 0 ? '#00e87b' : '#252a33'}`,
          }}>
            <div className="flex items-center gap-2">
              <Terminal className="w-3 h-3" style={{ color: isRunning ? '#e8c300' : '#00e87b' }} />
              <span className="text-[10px] font-bold uppercase tracking-wider" style={{
                color: isRunning ? '#e8c300' : '#00e87b',
              }}>
                BACKTEST TERMINAL {runningRisk ? `— ${runningRisk.toUpperCase()} RISK` : ''} {isRunning ? '— RUNNING' : terminalLines.length > 0 ? '— DONE' : '— IDLE'}
              </span>
            </div>
            <div className="flex items-center gap-3 text-[9px]" style={{ color: '#5a6270' }}>
              {isRunning && (
                <RefreshCw className="w-3 h-3 animate-spin" style={{ color: '#e8c300' }} />
              )}
              <button
                onClick={() => setTerminalLines([])}
                className="flex items-center gap-1 px-2 py-1 bg-[#3d4450]/20 hover:bg-[#3d4450]/40 transition-colors text-white/70"
                style={{ borderRadius: '2px' }}
              >
                CLEAR
              </button>
              <button 
                onClick={handleCopyTerminal}
                className="flex items-center gap-1 px-2 py-1 bg-[#3d4450]/20 hover:bg-[#3d4450]/40 transition-colors text-white/70"
                title="Copy terminal output"
                style={{ borderRadius: '2px' }}
              >
                {copied ? <Check className="w-3 h-3 text-[#00e87b]" /> : <Copy className="w-3 h-3" />}
                {copied ? 'COPIED' : 'COPY'}
              </button>
            </div>
          </div>
          <div
            ref={termRef}
            className="px-4 py-3 overflow-y-auto font-mono"
            style={{
              background: '#0a0c10',
              height: 500,
              fontSize: 10,
              lineHeight: '1.7',
            }}
          >
            {terminalLines.length === 0 ? (
              <span style={{ color: '#3d4450' }}>WAITING FOR OUTPUT... SELECT DATE RANGE AND CLICK RUN</span>
            ) : (
              terminalLines.map((line, i) => (
                <div key={i} style={{
                  color: getLineColor(line),
                  whiteSpace: 'pre',
                }}>
                  <span style={{ color: '#2a2f3a', marginRight: 8, userSelect: 'none' }}>{String(i + 1).padStart(4, ' ')}</span>
                  {line}
                </div>
              ))
            )}
          </div>
        </div>

        {/* Selected profile results */}
        {/* Risk tabs */}
        <div className="flex gap-[1px] mb-4">
          {(["low", "medium", "high"] as const).map(r => (
            <button
              key={r}
              onClick={() => setSelectedRisk(r)}
              className="px-4 py-[6px] text-[10px] font-semibold tracking-wider uppercase transition-all"
              style={{
                background: selectedRisk === r ? riskColors[r] : '#181c24',
                color: selectedRisk === r ? '#000' : '#5a6270',
                border: `1px solid ${selectedRisk === r ? riskColors[r] : '#252a33'}`,
              }}
            >
              {r} Risk
            </button>
          ))}
        </div>

        {p ? (
          <>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-[1px] mb-4">
              {[
                { label: "Total P&L",    value: pnlFmt(p.pnl),        color: p.pnl >= 0 ? '#00e87b' : '#ff3e3e' },
                { label: "Win Rate",     value: `${(p.win_rate * 100).toFixed(0)}%`, color: '#c8cdd5' },
                { label: "Risk-Reward",  value: `${p.rr.toFixed(2)}×`, color: '#4da6ff' },
                { label: "Total Trades", value: String(p.trades),      color: '#c8cdd5' },
                { label: "Avg Winner",   value: pnlFmt(p.avg_win),     color: '#00e87b' },
                { label: "Avg Loser",    value: pnlFmt(p.avg_loss),    color: '#ff3e3e' },
                { label: "Max Drawdown", value: pnlFmt(p.max_dd),      color: '#ff3e3e' },
                { label: "Profit Factor",value: p.pf?.toFixed(2) ?? "N/A", color: '#4da6ff' },
              ].map(({ label, value, color }) => (
                <div key={label} className="t-panel p-3">
                  <p className="text-[9px] uppercase tracking-[1.5px] mb-1" style={{ color: '#5a6270' }}>{label}</p>
                  <p className="text-lg font-bold" style={{ color }}>{value}</p>
                </div>
              ))}
            </div>

            <div className="t-panel p-4 mb-4">
              <h3 className="text-[11px] font-semibold mb-3 uppercase tracking-wider" style={{ color: '#5a6270' }}>
                Equity Curve — <span style={{ color: riskColors[selectedRisk] }}>{selectedRisk}</span>
              </h3>
              <EquityChart curves={curves} selected={selectedRisk} />
            </div>

            {p.trade_list && p.trade_list.length > 0 && (
              <div className="t-panel p-4 mb-4">
                <h3 className="text-[11px] font-semibold mb-3 uppercase tracking-wider" style={{ color: '#5a6270' }}>
                  Trade List — <span style={{ color: riskColors[selectedRisk] }}>{selectedRisk}</span>
                  <span className="ml-2 font-normal" style={{ color: '#3d4450' }}>{p.trade_list.length} trades</span>
                </h3>
                <TradeTable trades={p.trade_list} />
              </div>
            )}
          </>
        ) : (
          <div className="t-panel p-6 mb-4 text-center text-[11px]" style={{ color: '#3d4450' }}>
            NO RESULTS FOR <span style={{ color: riskColors[selectedRisk] }}>{selectedRisk.toUpperCase()}</span>. SELECT DATE RANGE AND CLICK RUN.
          </div>
        )}

        {/* All-profile comparison */}
        <div className="t-panel p-4">
          <h3 className="text-[11px] font-semibold mb-3 uppercase tracking-wider" style={{ color: '#5a6270' }}>
            All Profiles — Equity Curves
          </h3>
          <EquityChart curves={curves} selected="all" />
          <table className="mt-4">
            <thead>
              <tr>
                {["Profile", "Trades", "P&L", "Win Rate", "R:R", "Max DD", "Avg/Trade"].map(h => (
                  <th key={h}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {(["low", "medium", "high"] as RiskLevel[]).map(r => {
                const rp = results[r];
                if (!rp) return (
                  <tr key={r}>
                    <td className="uppercase font-semibold" style={{ color: riskColors[r] }}>{r}</td>
                    <td colSpan={6} style={{ color: '#3d4450' }}>NO DATA</td>
                  </tr>
                );
                return (
                  <tr key={r} style={{ background: selectedRisk === r ? '#1e222c' : undefined }}>
                    <td className="uppercase font-semibold" style={{ color: riskColors[r] }}>{r}</td>
                    <td>{rp.trades}</td>
                    <td style={{ color: rp.pnl >= 0 ? '#00e87b' : '#ff3e3e', fontWeight: 600 }}>{pnlFmt(rp.pnl)}</td>
                    <td>{(rp.win_rate * 100).toFixed(0)}%</td>
                    <td>{rp.rr.toFixed(2)}×</td>
                    <td style={{ color: '#ff3e3e' }}>{pnlFmt(rp.max_dd)}</td>
                    <td>{pnlFmt(rp.pnl / Math.max(rp.trades, 1))}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </main>
    </div>
  );
}
