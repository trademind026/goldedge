"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import Sidebar from "@/components/Sidebar";
import EquityChart from "@/components/EquityChart";
import TradeTable from "@/components/TradeTable";
import {
  fetchJSON,
  type BacktestResults,
  type EquityCurvePoint,
  type BacktestHistoryRun,
  type BacktestHistoryDetail,
  type BacktestHistoryResponse,
  type KalmanComparisonReport,
} from "@/lib/api";
import { Play, RefreshCw, Terminal, Calendar, Copy, Check, Download, Shield, Activity, History, ChevronLeft, ChevronRight } from "lucide-react";

type RiskLevel = "low" | "medium" | "high";
const riskColors: Record<RiskLevel, string> = { low: "#4da6ff", medium: "#e8c300", high: "#00e87b" };

const FOREX_PAIRS = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD"];

interface AvailableDay {
  day: string;
  ticks: number;
}

export default function BacktestPage() {
  const [results, setResults] = useState<BacktestResults>({});
  const [curves, setCurves] = useState<Record<string, EquityCurvePoint[]>>({});
  const [selectedRisk, setSelectedRisk] = useState<RiskLevel>("medium");
  const [selectedSymbol, setSelectedSymbol] = useState("XAUUSD");
  const [exitAgent, setExitAgent] = useState<"Kalman" | "None">("Kalman");
  const [compareKalman, setCompareKalman] = useState(false);
  const [comparisonReport, setComparisonReport] = useState<KalmanComparisonReport | null>(null);

  // Live progress from backend SSE
  const [liveProgressStats, setLiveProgressStats] = useState<{
    live_trades: number;
    live_wins: number;
    live_pnl: number;
    live_equity: number;
  } | null>(null);

  // Saved DB history
  const [historyRuns, setHistoryRuns] = useState<BacktestHistoryRun[]>([]);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [historyOffset, setHistoryOffset] = useState(0);
  const [historyRisk, setHistoryRisk] = useState("");
  const [historyLoading, setHistoryLoading] = useState(false);
  const [selectedHistoryRun, setSelectedHistoryRun] = useState<BacktestHistoryDetail | null>(null);
  const historyPageSize = 25;

  const [isRunning, setIsRunning] = useState(false);
  const [runningRisk, setRunningRisk] = useState<RiskLevel | null>(null);
  const [simProgress, setSimProgress] = useState(0);
  const [simDay, setSimDay] = useState(0);
  const [totalSimDays, setTotalSimDays] = useState(0);
  const [currentSimDate, setCurrentSimDate] = useState("");
  const [etaSeconds, setEtaSeconds] = useState(0);

  const [terminalLines, setTerminalLines] = useState<string[]>([]);
  const termRef = useRef<HTMLDivElement>(null);
  const [copied, setCopied] = useState(false);
  const eventSourceRef = useRef<EventSource | null>(null);
  const pollTimerRef = useRef<NodeJS.Timeout | null>(null);

  // Date range
  const [availDays, setAvailDays] = useState<AvailableDay[]>([]);
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");

  const loadHistory = useCallback(async () => {
    setHistoryLoading(true);
    try {
      const params = new URLSearchParams({
        limit: String(historyPageSize),
        offset: String(historyOffset),
      });
      if (historyRisk) params.set("risk", historyRisk);
      const res = await fetchJSON<BacktestHistoryResponse>(`/backtest/history?${params.toString()}`);
      setHistoryRuns(res.runs || []);
      setHistoryTotal(res.total || 0);
    } catch {
    } finally {
      setHistoryLoading(false);
    }
  }, [historyOffset, historyRisk]);

  const loadHistoryDetail = async (runId: string) => {
    try {
      const detail = await fetchJSON<BacktestHistoryDetail>(`/backtest/history/${encodeURIComponent(runId)}`);
      setSelectedHistoryRun(detail);
      const rKey = (detail.risk || "medium") as RiskLevel;
      setSelectedRisk(rKey);
      setResults((prev) => ({
        ...prev,
        [rKey]: {
          trades: detail.trades,
          win_rate: detail.win_rate,
          pnl: detail.pnl,
          max_dd: detail.max_dd,
          pf: detail.pf,
          avg_win: detail.avg_win,
          avg_loss: detail.avg_loss,
          rr: detail.rr,
          trade_list: detail.trade_list,
        },
      }));
      setCurves((prev) => ({
        ...prev,
        [rKey]: detail.equity_curve || [],
      }));
    } catch {}
  };

  const datesInitRef = useRef(false);
  const load = useCallback(async () => {
    try {
      const [r, c, days, cmp] = await Promise.all([
        fetchJSON<BacktestResults>("/backtest/results").catch(() => ({})),
        fetchJSON<Record<string, EquityCurvePoint[]>>("/equity/curve").catch(() => ({})),
        fetchJSON<AvailableDay[]>("/days").catch(() => []),
        fetchJSON<KalmanComparisonReport>("/backtest/compare").catch(() => null),
      ]);
      setResults(r);
      setCurves(c);
      if (cmp && cmp.available) {
        setComparisonReport(cmp);
      }
      if (days && days.length > 0) {
        setAvailDays(days);
        if (!datesInitRef.current) {
          datesInitRef.current = true;
          setStartDate(String(days[0].day));
          setEndDate(String(days[days.length - 1].day));
          setTotalSimDays(days.length);
        }
      }
    } catch {}
    loadHistory();
  }, [loadHistory]);

  useEffect(() => {
    load();
    return () => {
      if (pollTimerRef.current) clearInterval(pollTimerRef.current);
    };
  }, [load]);

  // Auto-scroll terminal
  useEffect(() => {
    if (termRef.current) {
      const { scrollTop, scrollHeight, clientHeight } = termRef.current;
      if (scrollHeight - scrollTop - clientHeight < 150) {
        termRef.current.scrollTop = termRef.current.scrollHeight;
      }
    }
  }, [terminalLines.length]);

  // Connect to SSE for live backend terminal output
  const connectSSE = useCallback(() => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
    }
    const es = new EventSource("/api/flask/backtest/progress");
    es.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.log) {
          setTerminalLines((prev) => [...prev, data.log]);
        }
        if (data.progress) {
          const p = data.progress;
          if (p.running) setIsRunning(true);
          if (typeof p.pct_complete === "number") setSimProgress(p.pct_complete);
          if (typeof p.current_day === "number") setSimDay(p.current_day);
          if (typeof p.total_days === "number" && p.total_days > 0) setTotalSimDays(p.total_days);
          if (typeof p.current_date === "string" && p.current_date) setCurrentSimDate(p.current_date);
          if (typeof p.eta_secs === "number") setEtaSeconds(p.eta_secs);
          setLiveProgressStats({
            live_trades: p.live_trades ?? 0,
            live_wins: p.live_wins ?? 0,
            live_pnl: p.live_pnl ?? 0,
            live_equity: p.live_equity ?? 10000,
          });
        }
        if (data.status === "done" || data.status === "error") {
          if (pollTimerRef.current) {
            clearInterval(pollTimerRef.current);
            pollTimerRef.current = null;
          }
          setIsRunning(false);
          setRunningRisk(null);
          setSimProgress(data.status === "done" ? 100 : 0);
          setEtaSeconds(0);
          setLiveProgressStats(null);
          load();
        }
      } catch {}
    };
    es.onerror = () => {};
    eventSourceRef.current = es;
  }, [load]);

  useEffect(() => {
    connectSSE();
    return () => {
      if (eventSourceRef.current) eventSourceRef.current.close();
    };
  }, [connectSSE]);

  const handleCopyTerminal = () => {
    if (terminalLines.length === 0) return;
    navigator.clipboard.writeText(terminalLines.join("\n"));
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const runBacktest = async (risk: RiskLevel) => {
    const effectiveStart = startDate || (availDays.length > 0 ? String(availDays[0].day) : "");
    const effectiveEnd = endDate || (availDays.length > 0 ? String(availDays[availDays.length - 1].day) : "");
    if (!effectiveStart || !effectiveEnd) {
      setTerminalLines((prev) => [
        ...prev,
        "[WARN] Waiting for available dates from /api/days. Please select start and end dates and try again.",
      ]);
      return;
    }

    setIsRunning(true);
    setSelectedRisk(risk);
    setRunningRisk(risk);
    setSimProgress(5.0);
    setSimDay(0);
    setTotalSimDays(daysInRange);
    setCurrentSimDate(effectiveStart);
    setEtaSeconds(0);
    setTerminalLines([]);
    if (!eventSourceRef.current || eventSourceRef.current.readyState === EventSource.CLOSED) {
      connectSSE();
    }

    if (pollTimerRef.current) clearInterval(pollTimerRef.current);
    pollTimerRef.current = setInterval(() => {
      load();
    }, 2000);

    try {
      const res = await fetch("/api/flask/backtest/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol: selectedSymbol,
          start_date: effectiveStart,
          end_date: effectiveEnd,
          risk,
          exit_agent: exitAgent,
          compare_kalman: compareKalman,
        }),
      });
      if (!res.ok) {
        setIsRunning(false);
        setRunningRisk(null);
        setSimProgress(0);
        if (pollTimerRef.current) {
          clearInterval(pollTimerRef.current);
          pollTimerRef.current = null;
        }
        setTerminalLines((prev) => [...prev, `[ERROR] Backtest request failed (${res.status})`]);
      }
    } catch (err) {
      setIsRunning(false);
      setRunningRisk(null);
      setSimProgress(0);
      if (pollTimerRef.current) {
        clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
      setTerminalLines((prev) => [...prev, `[ERROR] Failed to start backtest: ${String(err)}`]);
    }
  };

  const handleExportCSV = () => {
    const p = results[selectedRisk];
    const tradeList = p?.trade_list || [];
    if (tradeList.length === 0) return;

    let csvContent = "data:text/csv;charset=utf-8,";
    csvContent += "Time,Pair,Type,Lots,Entry,Exit,Reason,PnL_USD\n";

    tradeList.forEach((t) => {
      csvContent += `${t.entry_time || ""},${t.symbol || selectedSymbol},${t.direction || ""},${t.qty ?? t.lot_size ?? ""},${t.entry_price ?? ""},${t.exit_price ?? ""},${t.exit_reason || ""},${t.pnl ?? 0}\n`;
    });

    const encodedUri = encodeURI(csvContent);
    const link = document.createElement("a");
    link.setAttribute("href", encodedUri);
    link.setAttribute("download", `forex_audit_log_${selectedSymbol}_${selectedRisk}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  // Count available days in selected range
  const daysInRange = availDays.filter((d) => {
    const ds = String(d.day);
    return (!startDate || ds >= startDate) && (!endDate || ds <= endDate);
  }).length;

  const totalDays = availDays.length;

  const currentResult = results[selectedRisk];
  const livePnl = isRunning && liveProgressStats ? liveProgressStats.live_pnl : (currentResult?.pnl ?? 0);
  const liveWinRate = isRunning && liveProgressStats
    ? (liveProgressStats.live_trades > 0 ? (liveProgressStats.live_wins / liveProgressStats.live_trades) * 100 : 0)
    : (currentResult ? currentResult.win_rate * 100 : 0);
  const liveEquity = isRunning && liveProgressStats ? liveProgressStats.live_equity : (10000 + livePnl);
  const liveNetPips = currentResult?.trade_list?.reduce((sum, t) => {
    const entry = Number(t.entry_price || 0);
    const exit = Number(t.exit_price || 0);
    if (!entry || !exit) return sum;
    const pipUnit = (t.symbol || selectedSymbol).includes("XAU") ? 0.1 : 0.0001;
    const diff = t.direction === "SELL" ? entry - exit : exit - entry;
    return sum + diff / pipUnit;
  }, 0) ?? 0;

  // Dynamic trades for table (real results only, no hardcoded fallback)
  const tableTrades = currentResult?.trade_list && currentResult.trade_list.length > 0
    ? currentResult.trade_list.map((t) => ({
        time: String(t.entry_time || ""),
        pair: t.symbol || selectedSymbol,
        type: (t.direction || "BUY") as "BUY" | "SELL",
        lots: Number(t.qty ?? t.lot_size ?? 0),
        entry: Number(t.entry_price ?? 0),
        exit: Number(t.exit_price ?? 0),
        reason: t.exit_reason || "",
        pnl: Number(t.pnl ?? 0),
      }))
    : [];

  const getLineColor = (line: string) => {
    if (line.includes("ERROR") || line.includes("Cut early") || line.includes("Stop Loss") || line.includes("reversal")) return "#ff3e3e";
    if (line.includes("WARN") || line.includes("Break-even") || line.includes("WARMUP")) return "#e8c300";
    if (line.includes("ENTRY") || line.includes("fill") || line.includes("Buy") || line.includes("Sell")) return "#4da6ff";
    if (line.includes("EXIT") || line.includes("TARGET") || line.includes("Target") || line.includes("COMPLETE") || line.includes("+")) return "#00e87b";
    if (line.includes("SYSTEM") || line.includes("CONFIG")) return "#8b949e";
    return "#c8cdd5";
  };

  return (
    <div className="flex min-h-screen bg-[#0a0c10] text-[#c8cdd5]">
      <Sidebar />
      <main className="flex-1 p-5 overflow-y-auto max-w-[1440px] mx-auto">
        {/* ============================================================== */}
        {/* TOP BAR: TICK DATA DAYS: 142 / 180  [ALL DATES]  EXIT AGENT    */}
        {/* ============================================================== */}
        <div className="t-panel px-4 py-3 mb-5 flex items-center justify-between flex-wrap gap-4 border border-[#252a33] bg-[#0e1117]">
          {/* Left: Tick data days + All dates */}
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-2">
              <Calendar className="w-3.5 h-3.5 text-[#4da6ff]" />
              <span className="text-[10px] font-bold uppercase tracking-wider text-[#5a6270]">
                TICK DATA DAYS:
              </span>
              <span className="text-[11px] font-mono font-bold text-[#c8cdd5]">
                {daysInRange} / {totalDays}
              </span>
            </div>
            <button
              onClick={() => {
                if (availDays.length > 0) {
                  setStartDate(String(availDays[0].day));
                  setEndDate(String(availDays[availDays.length - 1].day));
                }
              }}
              className="text-[9px] font-bold uppercase tracking-wider px-2.5 py-1 transition-colors hover:bg-[#1f2633]"
              style={{ background: "#181c24", border: "1px solid #252a33", color: "#4da6ff" }}
            >
              [ ALL DATES ]
            </button>
          </div>

          {/* Middle: Currency Pair Picker */}
          <div className="flex items-center gap-2">
            <span className="text-[9px] font-bold uppercase tracking-wider text-[#5a6270]">PAIR:</span>
            <select
              value={selectedSymbol}
              onChange={(e) => setSelectedSymbol(e.target.value)}
              className="text-[11px] font-mono font-bold px-2.5 py-1 outline-none bg-[#181c24] border border-[#252a33] text-[#00e87b]"
            >
              {FOREX_PAIRS.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </div>

          {/* Right: Exit Agent Dropdown + Refresh */}
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-2">
              <span className="text-[9px] font-bold uppercase tracking-wider text-[#5a6270]">EXIT AGENT:</span>
              <select
                value={exitAgent}
                onChange={(e) => setExitAgent(e.target.value as "Kalman" | "None")}
                disabled={compareKalman}
                className="text-[11px] font-mono font-bold px-2.5 py-1 outline-none bg-[#181c24] border border-[#252a33] text-[#4da6ff] disabled:opacity-50"
              >
                <option value="Kalman">Kalman Filter</option>
                <option value="None">None / Fixed SL-TP</option>
              </select>
            </div>
            <label className="flex items-center gap-1.5 text-[10px] font-mono font-bold uppercase tracking-wider text-[#e8c300] cursor-pointer select-none">
              <input
                type="checkbox"
                checked={compareKalman}
                onChange={(e) => setCompareKalman(e.target.checked)}
                className="accent-[#e8c300]"
              />
              COMPARE KALMAN
            </label>
            <button
              onClick={load}
              className="flex items-center gap-1.5 px-3 py-1 text-[10px] font-bold uppercase tracking-wider bg-[#181c24] border border-[#252a33] hover:bg-[#202633] text-[#c8cdd5]"
            >
              <RefreshCw className="w-3 h-3 text-[#5a6270]" /> REFRESH
            </button>
          </div>
        </div>

        {/* ============================================================== */}
        {/* ROW 1: THREE RISK PROFILE CARDS (Side by side)                 */}
        {/* ============================================================== */}
        <div className="mb-5">
          <p className="text-[9px] uppercase tracking-widest text-[#5a6270] mb-2 font-semibold">
            ROW 1: RISK PROFILES & EXECUTION TIER
          </p>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {/* LOW RISK CARD */}
            <div
              onClick={() => setSelectedRisk("low")}
              className={`p-4 border transition-all cursor-pointer ${
                selectedRisk === "low" ? "ring-1 ring-[#4da6ff]" : "hover:border-[#4da6ff]/40"
              }`}
              style={{
                background: selectedRisk === "low" ? "#0d1a29" : "#11141a",
                borderColor: selectedRisk === "low" ? "#4da6ff" : "#1e222c",
              }}
            >
              <div className="flex items-center justify-between mb-3">
                <span className="text-xs font-bold uppercase tracking-wider text-[#4da6ff]">
                  LOW RISK (Blue)
                </span>
                <span className="text-[9px] font-bold px-2 py-0.5 rounded bg-[#4da6ff]/20 text-[#4da6ff]">
                  Conservative
                </span>
              </div>
              <div className="space-y-1 text-[11px] font-mono mb-4 text-[#8b949e]">
                <div className="flex justify-between">
                  <span>Target:</span>
                  <span className="font-bold text-[#00e87b]">+15 Pips</span>
                </div>
                <div className="flex justify-between">
                  <span>Stop:</span>
                  <span className="font-bold text-[#ff3e3e]">-10 Pips</span>
                </div>
                <div className="flex justify-between">
                  <span>Max Lots:</span>
                  <span className="font-bold text-[#c8cdd5]">0.5 Lots</span>
                </div>
              </div>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  runBacktest("low");
                }}
                disabled={isRunning}
                className="w-full flex items-center justify-center gap-2 py-2.5 text-[11px] font-bold uppercase tracking-wider transition-all disabled:opacity-50"
                style={{
                  background: isRunning && runningRisk === "low" ? "#1a2a40" : "#4da6ff",
                  color: isRunning && runningRisk === "low" ? "#4da6ff" : "#000",
                }}
              >
                {isRunning && runningRisk === "low" ? (
                  <>
                    <RefreshCw className="w-3.5 h-3.5 animate-spin" /> RUNNING...
                  </>
                ) : (
                  <>
                    <Play className="w-3.5 h-3.5 fill-current" /> ▶ RUN LOW
                  </>
                )}
              </button>
            </div>

            {/* MEDIUM RISK CARD */}
            <div
              onClick={() => setSelectedRisk("medium")}
              className={`p-4 border transition-all cursor-pointer ${
                selectedRisk === "medium" ? "ring-1 ring-[#e8c300]" : "hover:border-[#e8c300]/40"
              }`}
              style={{
                background: selectedRisk === "medium" ? "#1f1d0d" : "#11141a",
                borderColor: selectedRisk === "medium" ? "#e8c300" : "#1e222c",
              }}
            >
              <div className="flex items-center justify-between mb-3">
                <span className="text-xs font-bold uppercase tracking-wider text-[#e8c300]">
                  MEDIUM RISK (Yellow)
                </span>
                <span className="text-[9px] font-bold px-2 py-0.5 rounded bg-[#e8c300]/20 text-[#e8c300]">
                  Balanced
                </span>
              </div>
              <div className="space-y-1 text-[11px] font-mono mb-4 text-[#8b949e]">
                <div className="flex justify-between">
                  <span>Target:</span>
                  <span className="font-bold text-[#00e87b]">+30 Pips</span>
                </div>
                <div className="flex justify-between">
                  <span>Stop:</span>
                  <span className="font-bold text-[#ff3e3e]">-15 Pips</span>
                </div>
                <div className="flex justify-between">
                  <span>Max Lots:</span>
                  <span className="font-bold text-[#c8cdd5]">1.5 Lots</span>
                </div>
              </div>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  runBacktest("medium");
                }}
                disabled={isRunning}
                className="w-full flex items-center justify-center gap-2 py-2.5 text-[11px] font-bold uppercase tracking-wider transition-all disabled:opacity-50"
                style={{
                  background: isRunning && runningRisk === "medium" ? "#2a2200" : "#e8c300",
                  color: isRunning && runningRisk === "medium" ? "#e8c300" : "#000",
                }}
              >
                {isRunning && runningRisk === "medium" ? (
                  <>
                    <RefreshCw className="w-3.5 h-3.5 animate-spin" /> RUNNING...
                  </>
                ) : (
                  <>
                    <Play className="w-3.5 h-3.5 fill-current" /> ▶ RUN MEDIUM
                  </>
                )}
              </button>
            </div>

            {/* HIGH RISK CARD */}
            <div
              onClick={() => setSelectedRisk("high")}
              className={`p-4 border transition-all cursor-pointer ${
                selectedRisk === "high" ? "ring-1 ring-[#00e87b]" : "hover:border-[#00e87b]/40"
              }`}
              style={{
                background: selectedRisk === "high" ? "#0d1f14" : "#11141a",
                borderColor: selectedRisk === "high" ? "#00e87b" : "#1e222c",
              }}
            >
              <div className="flex items-center justify-between mb-3">
                <span className="text-xs font-bold uppercase tracking-wider text-[#00e87b]">
                  HIGH RISK (Green)
                </span>
                <span className="text-[9px] font-bold px-2 py-0.5 rounded bg-[#00e87b]/20 text-[#00e87b]">
                  Aggressive
                </span>
              </div>
              <div className="space-y-1 text-[11px] font-mono mb-4 text-[#8b949e]">
                <div className="flex justify-between">
                  <span>Target:</span>
                  <span className="font-bold text-[#00e87b]">+60 Pips</span>
                </div>
                <div className="flex justify-between">
                  <span>Stop:</span>
                  <span className="font-bold text-[#ff3e3e]">-25 Pips</span>
                </div>
                <div className="flex justify-between">
                  <span>Max Lots:</span>
                  <span className="font-bold text-[#c8cdd5]">3.0 Lots</span>
                </div>
              </div>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  runBacktest("high");
                }}
                disabled={isRunning}
                className="w-full flex items-center justify-center gap-2 py-2.5 text-[11px] font-bold uppercase tracking-wider transition-all disabled:opacity-50"
                style={{
                  background: isRunning && runningRisk === "high" ? "#0d2b14" : "#00e87b",
                  color: isRunning && runningRisk === "high" ? "#00e87b" : "#000",
                }}
              >
                {isRunning && runningRisk === "high" ? (
                  <>
                    <RefreshCw className="w-3.5 h-3.5 animate-spin" /> RUNNING...
                  </>
                ) : (
                  <>
                    <Play className="w-3.5 h-3.5 fill-current" /> ▶ RUN HIGH
                  </>
                )}
              </button>
            </div>
          </div>
        </div>

        {/* ============================================================== */}
        {/* ROW 2: LIVE SIMULATION MONITOR (Appears when RUN is clicked)    */}
        {/* ============================================================== */}
        {(isRunning || simProgress > 0) && (
          <div className="mb-5 t-panel p-4 border border-[#252a33] bg-[#0e1117] transition-all">
            <div className="flex items-center justify-between mb-2 text-[11px] font-mono">
              <div className="flex items-center gap-2">
                <span className="w-2 h-2 rounded-full animate-ping bg-[#00e87b]" />
                <span className="font-bold text-[#c8cdd5]">
                  Day {simDay} of {totalSimDays} · {currentSimDate}
                </span>
                {isRunning && (
                  <span className="text-[10px] text-[#e8c300]">· REPLAYING TICKS</span>
                )}
              </div>
              <div className="flex items-center gap-3">
                <span className="font-bold text-[#00e87b]">{simProgress.toFixed(1)}%</span>
                <span className="text-[#5a6270]">ETA {etaSeconds}s</span>
              </div>
            </div>

            {/* Gradient Progress Bar */}
            <div className="w-full h-2.5 bg-[#181c24] rounded-full overflow-hidden mb-4 border border-[#252a33]">
              <div
                className="h-full transition-all duration-300"
                style={{
                  width: `${simProgress}%`,
                  background: "linear-gradient(90deg, #4da6ff 0%, #e8c300 50%, #00e87b 100%)",
                }}
              />
            </div>

            {/* 4 Stat Tiles: LIVE P&L | NET PIPS | WIN RATE | ACCOUNT EQUITY */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-px bg-[#252a33]">
              <div className="bg-[#11141a] p-3">
                <p className="text-[9px] uppercase tracking-wider text-[#5a6270] mb-1 font-semibold">
                  LIVE P&L
                </p>
                <p className={`text-lg font-bold font-mono ${livePnl >= 0 ? "text-[#00e87b]" : "text-[#ff3e3e]"}`}>
                  {livePnl >= 0 ? "+" : "-"}${Math.abs(livePnl).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                </p>
              </div>
              <div className="bg-[#11141a] p-3">
                <p className="text-[9px] uppercase tracking-wider text-[#5a6270] mb-1 font-semibold">
                  NET PIPS
                </p>
                <p className={`text-lg font-bold font-mono ${liveNetPips >= 0 ? "text-[#4da6ff]" : "text-[#ff3e3e]"}`}>
                  {liveNetPips >= 0 ? "+" : ""}{liveNetPips.toFixed(1)} Pips
                </p>
              </div>
              <div className="bg-[#11141a] p-3">
                <p className="text-[9px] uppercase tracking-wider text-[#5a6270] mb-1 font-semibold">
                  WIN RATE
                </p>
                <p className="text-lg font-bold font-mono text-[#c8cdd5]">
                  {liveWinRate.toFixed(1)}%
                </p>
              </div>
              <div className="bg-[#11141a] p-3">
                <p className="text-[9px] uppercase tracking-wider text-[#5a6270] mb-1 font-semibold">
                  ACCOUNT EQUITY
                </p>
                <p className="text-lg font-bold font-mono text-[#c8cdd5]">
                  ${liveEquity.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 0 })}
                </p>
              </div>
            </div>
          </div>
        )}

        {/* ============================================================== */}
        {/* ROW 3: RETRO TERMINAL LOG BOX                                  */}
        {/* ============================================================== */}
        <div
          className="t-panel mb-5 border transition-all"
          style={{
            borderColor: isRunning ? "#e8c300" : terminalLines.length > 0 ? "#252a33" : "#1e222c",
          }}
        >
          <div
            className="flex items-center justify-between px-4 py-2 border-b"
            style={{
              background: "#0a0c10",
              borderColor: isRunning ? "#e8c300" : "#1e222c",
            }}
          >
            <div className="flex items-center gap-2">
              <Terminal className="w-3.5 h-3.5 text-[#00e87b]" />
              <span className="text-[10px] font-mono font-bold tracking-wider text-[#00e87b]">
                [&gt;_] TICK REPLAY ({selectedSymbol}) — {isRunning ? "RUNNING..." : terminalLines.length > 0 ? "FINISHED" : "READY"}
              </span>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={() => setTerminalLines([])}
                className="px-2 py-1 text-[9px] font-mono uppercase bg-[#181c24] border border-[#252a33] text-[#5a6270] hover:text-[#c8cdd5]"
              >
                CLEAR
              </button>
              <button
                onClick={handleCopyTerminal}
                className="flex items-center gap-1 px-2.5 py-1 text-[9px] font-mono uppercase font-bold bg-[#181c24] border border-[#252a33] text-[#4da6ff] hover:bg-[#1f2633]"
              >
                {copied ? <Check className="w-3 h-3 text-[#00e87b]" /> : <Copy className="w-3 h-3" />}
                {copied ? "COPIED" : "[ COPY LOG ]"}
              </button>
            </div>
          </div>
          <div
            ref={termRef}
            className="px-4 py-3 overflow-y-auto font-mono text-[10px] leading-relaxed"
            style={{
              background: "#07090e",
              height: 240,
            }}
          >
            {terminalLines.length === 0 ? (
              <div className="space-y-1 text-[#5a6270]">
                <div className="text-[#5a6270] italic">
                  {isRunning
                    ? "> Backtest running... waiting for live logs from backend."
                    : "> Engine idle. Click [ ▶ RUN ] on any risk card above to start tick replay."}
                </div>
              </div>
            ) : (
              terminalLines.map((line, i) => (
                <div key={i} style={{ color: getLineColor(line), whiteSpace: "pre-wrap" }}>
                  <span className="text-[#3d4450] mr-2 select-none">{String(i + 1).padStart(3, "0")}</span>
                  {line}
                </div>
              ))
            )}
          </div>
        </div>

        {/* ============================================================== */}
        {/* KALMAN VS BASELINE COMPARISON CARD                             */}
        {/* ============================================================== */}
        {comparisonReport && comparisonReport.available && (
          <div className="t-panel p-4 mb-5 border border-[#4da6ff] bg-[#0e1117]">
            <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
              <div>
                <h3 className="text-[11px] font-bold uppercase tracking-wider text-[#4da6ff]">
                  Kalman Benchmark Comparison — Baseline (Fixed SL/TP) vs With Kalman Exit
                </h3>
                <p className="text-[9px] mt-0.5 text-[#5a6270] font-mono">
                  {comparisonReport.symbol} · {comparisonReport.risk.toUpperCase()} · {comparisonReport.start_date} to {comparisonReport.end_date}
                </p>
              </div>
              <div
                className="px-3 py-1 text-[11px] font-bold font-mono"
                style={{
                  background: comparisonReport.net_kalman_difference >= 0 ? "rgba(0, 232, 123, 0.12)" : "rgba(255, 62, 62, 0.12)",
                  border: `1px solid ${comparisonReport.net_kalman_difference >= 0 ? "#00e87b" : "#ff3e3e"}`,
                  color: comparisonReport.net_kalman_difference >= 0 ? "#00e87b" : "#ff3e3e",
                }}
              >
                NET KALMAN EDGE: {comparisonReport.net_kalman_difference >= 0 ? "+" : "-"}${Math.abs(comparisonReport.net_kalman_difference).toFixed(2)}
              </div>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-[11px] font-mono">
                <thead>
                  <tr className="text-left text-[#5a6270] border-b border-[#252a33]">
                    <th className="py-2">Metric</th>
                    <th className="py-2">Without Kalman</th>
                    <th className="py-2">With Kalman</th>
                    <th className="py-2">Difference</th>
                  </tr>
                </thead>
                <tbody>
                  {[
                    {
                      label: "Trades",
                      a: String(comparisonReport.without_kalman.trades),
                      b: String(comparisonReport.with_kalman.trades),
                      d: `${comparisonReport.with_kalman.trades - comparisonReport.without_kalman.trades >= 0 ? "+" : ""}${comparisonReport.with_kalman.trades - comparisonReport.without_kalman.trades}`,
                      pos: true,
                    },
                    {
                      label: "Win Rate",
                      a: `${comparisonReport.without_kalman.win_rate}%`,
                      b: `${comparisonReport.with_kalman.win_rate}%`,
                      d: `${comparisonReport.with_kalman.win_rate - comparisonReport.without_kalman.win_rate >= 0 ? "+" : ""}${(comparisonReport.with_kalman.win_rate - comparisonReport.without_kalman.win_rate).toFixed(1)}%`,
                      pos: comparisonReport.with_kalman.win_rate >= comparisonReport.without_kalman.win_rate,
                    },
                    {
                      label: "Total P&L",
                      a: `$${comparisonReport.without_kalman.total_pnl.toFixed(2)}`,
                      b: `$${comparisonReport.with_kalman.total_pnl.toFixed(2)}`,
                      d: `${comparisonReport.net_kalman_difference >= 0 ? "+" : "-"}$${Math.abs(comparisonReport.net_kalman_difference).toFixed(2)}`,
                      pos: comparisonReport.net_kalman_difference >= 0,
                    },
                    {
                      label: "Profit Factor",
                      a: comparisonReport.without_kalman.pf.toFixed(2),
                      b: comparisonReport.with_kalman.pf.toFixed(2),
                      d: `${comparisonReport.with_kalman.pf - comparisonReport.without_kalman.pf >= 0 ? "+" : ""}${(comparisonReport.with_kalman.pf - comparisonReport.without_kalman.pf).toFixed(2)}`,
                      pos: comparisonReport.with_kalman.pf >= comparisonReport.without_kalman.pf,
                    },
                    {
                      label: "Max Drawdown",
                      a: `$${comparisonReport.without_kalman.max_drawdown.toFixed(2)}`,
                      b: `$${comparisonReport.with_kalman.max_drawdown.toFixed(2)}`,
                      d: `${comparisonReport.with_kalman.max_drawdown - comparisonReport.without_kalman.max_drawdown >= 0 ? "+" : "-"}$${Math.abs(comparisonReport.with_kalman.max_drawdown - comparisonReport.without_kalman.max_drawdown).toFixed(2)}`,
                      pos: comparisonReport.with_kalman.max_drawdown >= comparisonReport.without_kalman.max_drawdown,
                    },
                    {
                      label: "Kalman Exits",
                      a: String(comparisonReport.without_kalman.kalman_exits),
                      b: String(comparisonReport.with_kalman.kalman_exits),
                      d: `+${comparisonReport.with_kalman.kalman_exits - comparisonReport.without_kalman.kalman_exits}`,
                      pos: true,
                    },
                  ].map((r) => (
                    <tr key={r.label} className="border-b border-[#181c24]">
                      <td className="py-1.5 text-[#8b949e]">{r.label}</td>
                      <td className="py-1.5">{r.a}</td>
                      <td className="py-1.5 text-[#c8cdd5] font-semibold">{r.b}</td>
                      <td className="py-1.5 font-semibold" style={{ color: r.pos ? "#00e87b" : "#ff3e3e" }}>{r.d}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* ============================================================== */}
        {/* BACKTEST KPI SUMMARY GRID (Real Backend Results)               */}
        {/* ============================================================== */}
        {currentResult && (() => {
          const leverage = 100;
          const contractSize = selectedSymbol.includes("XAU") ? 100 : 100000;
          const peakMargin = (currentResult.trade_list || []).reduce((maxM, t) => {
            const lots = Number(t.qty ?? t.lot_size ?? 0);
            const entry = Number(t.entry_price ?? 0);
            if (!lots || !entry) return maxM;
            const margin = (lots * contractSize * entry) / leverage;
            return Math.max(maxM, margin);
          }, 0);
          const requiredCapital = peakMargin > 0 ? peakMargin + Math.abs(currentResult.max_dd) : 10000;
          const roi = requiredCapital > 0 ? (currentResult.pnl / requiredCapital) * 100 : 0;
          const avgTrade = currentResult.trades > 0 ? currentResult.pnl / currentResult.trades : 0;

          return (
            <div className="mb-5 border border-[#252a33] bg-[#0e1117]">
              <div className="grid grid-cols-2 md:grid-cols-5 gap-px bg-[#252a33]">
                <div className="bg-[#11141a] p-3">
                  <p className="text-[9px] uppercase tracking-wider text-[#5a6270] mb-1 font-semibold">TOTAL P&L</p>
                  <p className={`text-lg font-bold font-mono ${currentResult.pnl >= 0 ? "text-[#00e87b]" : "text-[#ff3e3e]"}`}>
                    {currentResult.pnl >= 0 ? "+" : "-"}${Math.abs(currentResult.pnl).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                  </p>
                </div>
                <div className="bg-[#11141a] p-3">
                  <p className="text-[9px] uppercase tracking-wider text-[#5a6270] mb-1 font-semibold">WIN RATE</p>
                  <p className="text-lg font-bold font-mono text-[#c8cdd5]">
                    {(currentResult.win_rate * 100).toFixed(1)}%
                  </p>
                </div>
                <div className="bg-[#11141a] p-3">
                  <p className="text-[9px] uppercase tracking-wider text-[#5a6270] mb-1 font-semibold">RISK-REWARD</p>
                  <p className="text-lg font-bold font-mono text-[#4da6ff]">
                    {currentResult.rr.toFixed(2)}x
                  </p>
                </div>
                <div className="bg-[#11141a] p-3">
                  <p className="text-[9px] uppercase tracking-wider text-[#5a6270] mb-1 font-semibold">TOTAL TRADES</p>
                  <p className="text-lg font-bold font-mono text-[#c8cdd5]">
                    {currentResult.trades}
                  </p>
                </div>
                <div className="bg-[#11141a] p-3">
                  <p className="text-[9px] uppercase tracking-wider text-[#5a6270] mb-1 font-semibold">AVG / TRADE</p>
                  <p className={`text-lg font-bold font-mono ${avgTrade >= 0 ? "text-[#00e87b]" : "text-[#ff3e3e]"}`}>
                    {avgTrade >= 0 ? "+" : "-"}${Math.abs(avgTrade).toFixed(2)}
                  </p>
                </div>

                <div className="bg-[#11141a] p-3">
                  <p className="text-[9px] uppercase tracking-wider text-[#5a6270] mb-1 font-semibold">REQUIRED CAPITAL</p>
                  <p className="text-lg font-bold font-mono text-[#c8cdd5]">
                    ${requiredCapital.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                  </p>
                  <p className="text-[9px] font-mono text-[#5a6270] mt-0.5">
                    Peak Margin: ${peakMargin.toFixed(0)} (1:100)
                  </p>
                </div>
                <div className="bg-[#11141a] p-3">
                  <p className="text-[9px] uppercase tracking-wider text-[#5a6270] mb-1 font-semibold">RETURN (ROI)</p>
                  <p className={`text-lg font-bold font-mono ${roi >= 0 ? "text-[#00e87b]" : "text-[#ff3e3e]"}`}>
                    {roi >= 0 ? "+" : ""}{roi.toFixed(2)}%
                  </p>
                </div>
                <div className="bg-[#11141a] p-3">
                  <p className="text-[9px] uppercase tracking-wider text-[#5a6270] mb-1 font-semibold">FINAL EQUITY</p>
                  <p className="text-lg font-bold font-mono text-[#4da6ff]">
                    ${(10000 + currentResult.pnl).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                  </p>
                </div>
                <div className="bg-[#11141a] p-3">
                  <p className="text-[9px] uppercase tracking-wider text-[#5a6270] mb-1 font-semibold">AVG WINNER</p>
                  <p className="text-lg font-bold font-mono text-[#00e87b]">
                    +${Math.abs(currentResult.avg_win).toFixed(2)}
                  </p>
                </div>
                <div className="bg-[#11141a] p-3">
                  <p className="text-[9px] uppercase tracking-wider text-[#5a6270] mb-1 font-semibold">AVG LOSER</p>
                  <p className="text-lg font-bold font-mono text-[#ff3e3e]">
                    -${Math.abs(currentResult.avg_loss).toFixed(2)}
                  </p>
                </div>

                <div className="bg-[#11141a] p-3">
                  <p className="text-[9px] uppercase tracking-wider text-[#5a6270] mb-1 font-semibold">MAX DRAWDOWN</p>
                  <p className="text-lg font-bold font-mono text-[#ff3e3e]">
                    -${Math.abs(currentResult.max_dd).toFixed(2)}
                  </p>
                </div>
                <div className="bg-[#11141a] p-3">
                  <p className="text-[9px] uppercase tracking-wider text-[#5a6270] mb-1 font-semibold">PROFIT FACTOR</p>
                  <p className={`text-lg font-bold font-mono ${currentResult.pf >= 1 ? "text-[#00e87b]" : "text-[#ff3e3e]"}`}>
                    {currentResult.pf.toFixed(2)}
                  </p>
                </div>
              </div>
            </div>
          );
        })()}

        {/* ============================================================== */}
        {/* ROW 4: EQUITY CURVE (After run finishes / results exist)       */}
        {/* ============================================================== */}
        <div className="t-panel p-4 mb-5 border border-[#252a33] bg-[#0e1117]">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-[11px] font-bold uppercase tracking-wider text-[#c8cdd5] flex items-center gap-2">
              <Activity className="w-3.5 h-3.5 text-[#00e87b]" />
              EQUITY TRAJECTORY ($) — <span style={{ color: riskColors[selectedRisk] }}>{selectedRisk.toUpperCase()}</span>
            </h3>
            <button
              onClick={handleExportCSV}
              className="flex items-center gap-1.5 px-3 py-1.5 text-[10px] font-mono font-bold uppercase tracking-wider bg-[#181c24] border border-[#252a33] text-[#00e87b] hover:bg-[#1a2d20] transition-colors"
            >
              <Download className="w-3 h-3" /> [ EXPORT CSV ]
            </button>
          </div>
          <EquityChart curves={curves} selected={selectedRisk} />
        </div>

        {/* ============================================================== */}
        {/* ROW 5: TRADES TABLE                                            */}
        {/* ============================================================== */}
        <div className="t-panel p-4 border border-[#252a33] bg-[#0e1117]">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-[11px] font-bold uppercase tracking-wider text-[#c8cdd5]">
              ROW 5: TRADES TABLE ({selectedSymbol})
            </h3>
            <span className="text-[10px] font-mono text-[#5a6270]">
              {tableTrades.length} RECORDED TRADES
            </span>
          </div>
          <TradeTable trades={tableTrades} />
        </div>

        {/* ============================================================== */}
        {/* ROW 6: SAVED BACKTEST HISTORY (PostgreSQL / TimescaleDB)       */}
        {/* ============================================================== */}
        <div className="t-panel p-4 mt-5 border border-[#252a33] bg-[#0e1117]">
          <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
            <div className="flex items-center gap-2">
              <History className="w-4 h-4 text-[#4da6ff]" />
              <h3 className="text-[11px] font-bold uppercase tracking-wider text-[#c8cdd5]">
                ROW 6: SAVED BACKTEST HISTORY ({historyTotal} RUN{historyTotal === 1 ? "" : "S"})
              </h3>
              {selectedHistoryRun && (
                <span className="text-[10px] font-mono px-2 py-0.5 bg-[#181c24] border border-[#4da6ff]/40 text-[#4da6ff]">
                  VIEWING: {selectedHistoryRun.run_id}
                </span>
              )}
            </div>
            <div className="flex items-center gap-2">
              <select
                value={historyRisk}
                onChange={(e) => {
                  setHistoryOffset(0);
                  setHistoryRisk(e.target.value);
                }}
                className="text-[10px] font-mono px-2 py-1 bg-[#181c24] border border-[#252a33] text-[#c8cdd5] outline-none"
              >
                <option value="">ALL RISKS</option>
                <option value="low">LOW</option>
                <option value="medium">MEDIUM</option>
                <option value="high">HIGH</option>
              </select>
              <button
                onClick={loadHistory}
                disabled={historyLoading}
                className="flex items-center gap-1.5 px-3 py-1 text-[10px] font-bold uppercase tracking-wider bg-[#181c24] border border-[#252a33] hover:bg-[#202633] text-[#c8cdd5]"
              >
                <RefreshCw className={`w-3 h-3 ${historyLoading ? "animate-spin" : ""}`} /> REFRESH
              </button>
            </div>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-[11px] font-mono">
              <thead>
                <tr className="text-left text-[#5a6270] border-b border-[#252a33]">
                  <th className="py-2">Run ID</th>
                  <th className="py-2">Pair</th>
                  <th className="py-2">Risk</th>
                  <th className="py-2">Agent</th>
                  <th className="py-2">Period</th>
                  <th className="py-2">Trades</th>
                  <th className="py-2">Win %</th>
                  <th className="py-2">PF</th>
                  <th className="py-2">P&L</th>
                  <th className="py-2">Max DD</th>
                  <th className="py-2">Action</th>
                </tr>
              </thead>
              <tbody>
                {historyRuns.map((run) => (
                  <tr
                    key={run.run_id}
                    className={`border-b border-[#181c24] hover:bg-[#141820] ${
                      selectedHistoryRun?.run_id === run.run_id ? "bg-[#141e2e]" : ""
                    }`}
                  >
                    <td className="py-2 text-[#8b949e]">{run.run_id}</td>
                    <td className="py-2 text-[#c8cdd5]">{run.symbol}</td>
                    <td className="py-2 font-bold" style={{ color: riskColors[run.risk as RiskLevel] || "#c8cdd5" }}>
                      {run.risk.toUpperCase()}
                    </td>
                    <td className="py-2 text-[#8b949e]">{run.exit_agent}</td>
                    <td className="py-2 text-[#8b949e]">{run.start_date} → {run.end_date}</td>
                    <td className="py-2">{run.trades}</td>
                    <td className="py-2">{(run.win_rate * 100).toFixed(1)}%</td>
                    <td className="py-2">{run.pf.toFixed(2)}</td>
                    <td className="py-2 font-bold" style={{ color: run.pnl >= 0 ? "#00e87b" : "#ff3e3e" }}>
                      {run.pnl >= 0 ? "+" : "-"}${Math.abs(run.pnl).toFixed(2)}
                    </td>
                    <td className="py-2 text-[#ff3e3e]">${run.max_dd.toFixed(2)}</td>
                    <td className="py-2">
                      <button
                        onClick={() => loadHistoryDetail(run.run_id)}
                        className="px-2 py-0.5 text-[10px] font-bold bg-[#181c24] border border-[#4da6ff]/40 text-[#4da6ff] hover:bg-[#1a2940]"
                      >
                        LOAD
                      </button>
                    </td>
                  </tr>
                ))}
                {!historyLoading && historyRuns.length === 0 && (
                  <tr>
                    <td colSpan={11} className="py-4 text-center text-[#5a6270]">
                      No saved backtest runs in database yet. Run a backtest above to save it.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          {historyTotal > historyPageSize && (
            <div className="flex items-center justify-between mt-3 text-[10px] font-mono text-[#5a6270]">
              <button
                disabled={historyOffset === 0}
                onClick={() => setHistoryOffset(Math.max(0, historyOffset - historyPageSize))}
                className="flex items-center gap-1 px-2 py-1 bg-[#181c24] border border-[#252a33] disabled:opacity-40"
              >
                <ChevronLeft className="w-3 h-3" /> PREV
              </button>
              <span>
                {historyOffset + 1}–{Math.min(historyOffset + historyPageSize, historyTotal)} OF {historyTotal}
              </span>
              <button
                disabled={historyOffset + historyPageSize >= historyTotal}
                onClick={() => setHistoryOffset(historyOffset + historyPageSize)}
                className="flex items-center gap-1 px-2 py-1 bg-[#181c24] border border-[#252a33] disabled:opacity-40"
              >
                NEXT <ChevronRight className="w-3 h-3" />
              </button>
            </div>
          )}
        </div>
      </main>
    </div>
  );
}

