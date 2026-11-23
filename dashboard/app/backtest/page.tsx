"use client";

import { useEffect, useState, useRef, useCallback, useMemo } from "react";
import Link from "next/link";
import {
  Play,
  RefreshCw,
  Terminal,
  Copy,
  Check,
  Calendar,
  History,
  ChevronLeft,
  ChevronRight,
  Download,
  TrendingUp,
} from "lucide-react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
} from "recharts";
import Sidebar from "@/components/Sidebar";
import RiskProfileCard from "@/components/RiskProfileCard";
import EquityChart from "@/components/EquityChart";
import TradeTable from "@/components/TradeTable";
import RetroCalendar from "@/components/RetroCalendar";
import {
  fetchJSON,
  postJSON,
  API_BASE,
  normalizeWinRate,
  type BacktestResults,
  type BacktestProfile,
  type RiskProfile,
  type RiskLevel,
  type CandleDateInfo,
  type KalmanComparisonReport,
  type BacktestHistoryRun,
  type BacktestHistoryDetail,
  type BacktestHistoryResponse,
} from "@/lib/api";
import {
  fmtUsd,
  fmtWinRate,
  fmtModeLabel,
  resolveRunMetrics,
} from "@/lib/backtestHelpers";

type StrategyMode = "candidate_15_e1" | "baseline";
type ExitAgentMode = "Standard" | "Kalman";

const riskColors: Record<RiskLevel, string> = {
  low: "#4da6ff",
  medium: "#e8c300",
  high: "#00e87b",
};

function historyDateToIso(val: string): string {
  const d = new Date(val);
  return Number.isNaN(d.getTime()) ? val : d.toISOString();
}

function formatEta(secs: number): string {
  if (!secs || secs <= 0) return "";
  const total = Math.round(secs);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return m > 0 ? `${h}h ${m}m` : `${h}h`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function downloadCSV(profile: BacktestProfile, risk: string) {
  const rows = (profile.trade_list ?? []).map((t, i) => [
    i + 1,
    t.entry_time,
    t.exit_time,
    t.symbol,
    t.direction,
    t.strategy,
    (t.qty ?? t.lot_size ?? 0).toFixed(2),
    t.entry_price,
    t.exit_price,
    t.sl,
    t.target,
    (t.planned_risk_pct ?? 0).toFixed(3),
    t.regime ?? "",
    t.exit_reason,
    (t.score ?? t.final_score ?? 0).toFixed(3),
    t.pnl.toFixed(2),
  ]);
  const headers = [
    "#",
    "EntryTime",
    "ExitTime",
    "Pair",
    "Direction",
    "Strategy",
    "Lots",
    "EntryPrice",
    "ExitPrice",
    "SL",
    "Target",
    "PlannedRiskPct",
    "Regime",
    "ExitReason",
    "Score",
    "PnL_USD",
  ];
  const csv = [headers.join(","), ...rows.map((r) => r.join(","))].join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `goldedge_backtest_${risk}_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

export default function BacktestPage() {
  const [results, setResults] = useState<BacktestResults>({});
  const [profiles, setProfiles] = useState<Record<RiskLevel, RiskProfile> | null>(null);
  const [curves, setCurves] = useState<{
    low?: { time: string; equity: number }[];
    medium?: { time: string; equity: number }[];
    high?: { time: string; equity: number }[];
  }>({});
  const [selectedRisk, setSelectedRisk] = useState<RiskLevel>("medium");
  const [availDays, setAvailDays] = useState<CandleDateInfo[]>([]);
  const [startDate, setStartDate] = useState<string>("");
  const [endDate, setEndDate] = useState<string>("");
  const [symbol, setSymbol] = useState<string>("XAUUSD");
  const [strategyMode, setStrategyMode] = useState<StrategyMode>("candidate_15_e1");
  const [exitAgent, setExitAgent] = useState<ExitAgentMode>("Standard");
  const [commissionPerLot, setCommissionPerLot] = useState<number>(0);
  const [compareKalman, setCompareKalman] = useState<boolean>(false);
  const [compareReport, setCompareReport] = useState<KalmanComparisonReport | null>(null);

  // Live execution & terminal state
  const [running, setRunning] = useState(false);
  const [statusText, setStatusText] = useState<"idle" | "running" | "done" | "error" | "stopping">("idle");
  const [logs, setLogs] = useState<string[]>([]);
  const [copied, setCopied] = useState(false);
  const [progress, setProgress] = useState({
    risk: "medium",
    run_id: "",
    pct_complete: 0,
    current_day: 0,
    total_days: 0,
    current_date: "",
    live_trades: 0,
    live_wins: 0,
    live_pnl: 0,
    live_equity: 10000,
    elapsed_secs: 0,
    eta_secs: 0,
  });

  // Historical runs & comparison state
  const [history, setHistory] = useState<BacktestHistoryRun[]>([]);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [historyOffset, setHistoryOffset] = useState(0);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const [historyFrom, setHistoryFrom] = useState("");
  const [historyTo, setHistoryTo] = useState("");
  const [historyRisk, setHistoryRisk] = useState("");
  const [selectedRunId, setSelectedRunId] = useState("");
  const [selectedRun, setSelectedRun] = useState<BacktestHistoryDetail | null>(null);
  const [selectedRunError, setSelectedRunError] = useState("");
  const [compareRunIds, setCompareRunIds] = useState<string[]>([]);

  const historyPageSize = 25;
  const datesInitRef = useRef(false);
  const termRef = useRef<HTMLDivElement>(null);
  const sseRef = useRef<EventSource | null>(null);

  const load = useCallback(async (sym = symbol) => {
    try {
      const [r, c, p, days, comp] = await Promise.all([
        fetchJSON<BacktestResults>("/backtest/results").catch(() => ({})),
        fetchJSON<typeof curves>("/equity/curve").catch(() => ({})),
        fetchJSON<Record<RiskLevel, RiskProfile>>("/risk/profiles").catch(() => null),
        fetchJSON<CandleDateInfo[]>(`/days?symbol=${encodeURIComponent(sym)}`).catch(() => []),
        fetchJSON<KalmanComparisonReport>("/backtest/compare").catch(() => null),
      ]);
      setResults(r);
      setCurves(c);
      if (p) setProfiles(p);
      if (comp && comp.available) setCompareReport(comp);
      const validDays = (days ?? []).filter((d) => d.day && String(d.day) > "1970-01-02");
      setAvailDays(validDays);
      if (validDays.length > 0 && !datesInitRef.current) {
        datesInitRef.current = true;
        setStartDate(String(validDays[0].day));
        setEndDate(String(validDays[validDays.length - 1].day));
      }
    } catch {
      // ignore
    }
  }, [symbol]);

  const loadHistory = useCallback(async () => {
    setHistoryLoading(true);
    setHistoryError("");
    const params = new URLSearchParams({
      limit: String(historyPageSize),
      offset: String(historyOffset),
    });
    if (historyFrom) params.set("from", historyDateToIso(historyFrom));
    if (historyTo) params.set("to", historyDateToIso(historyTo));
    if (historyRisk) params.set("risk", historyRisk);

    try {
      const response = await fetchJSON<BacktestHistoryResponse>(
        `/backtest/history?${params.toString()}`
      );
      setHistory(response.runs ?? []);
      setHistoryTotal(response.total ?? 0);
      if (!selectedRunId && (response.runs ?? []).length > 0) {
        setSelectedRunId(response.runs[0].run_id);
      }
    } catch (err) {
      setHistoryError(err instanceof Error ? err.message : "Unable to load backtest history");
    } finally {
      setHistoryLoading(false);
    }
  }, [historyFrom, historyOffset, historyRisk, historyTo, selectedRunId]);

  const connectSSE = useCallback(() => {
    sseRef.current?.close();
    const sse = new EventSource(`${API_BASE}/backtest/progress`);
    sseRef.current = sse;

    sse.onmessage = (e) => {
      try {
        const payload = JSON.parse(e.data);
        if (payload.log) {
          setLogs((prev) => {
            const next = [...prev, payload.log];
            return next.length > 500 ? next.slice(-500) : next;
          });
        }
        if (payload.progress) {
          setProgress((prev) => ({ ...prev, ...payload.progress }));
        }
        if (payload.status === "done" || payload.status === "error") {
          setRunning(false);
          setStatusText(payload.status);
          sse.close();
          load();
          loadHistory();
        }
      } catch {
        // ignore chunk parse error
      }
    };

    sse.onerror = () => {
      sse.close();
    };
  }, [load, loadHistory]);

  useEffect(() => {
    load();
    fetchJSON<{ running: boolean; status?: string; last_risk?: string; pct_complete?: number }>(
      "/backtest/status"
    )
      .then((s) => {
        if (s.running) {
          setRunning(true);
          setStatusText("running");
          if (s.last_risk) setSelectedRisk(s.last_risk as RiskLevel);
          connectSSE();
        }
      })
      .catch(() => {});
    return () => sseRef.current?.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    loadHistory();
  }, [loadHistory]);

  useEffect(() => {
    if (!selectedRunId) {
      setSelectedRun(null);
      return;
    }
    let active = true;
    setSelectedRunError("");
    fetchJSON<BacktestHistoryDetail>(`/backtest/history/${selectedRunId}`)
      .then((detail) => {
        if (active) setSelectedRun(detail);
      })
      .catch((err) => {
        if (active) {
          setSelectedRun(null);
          setSelectedRunError(err instanceof Error ? err.message : "Unable to load backtest report");
        }
      });
    return () => {
      active = false;
    };
  }, [selectedRunId]);

  useEffect(() => {
    if (!running) return;
    const id = setInterval(async () => {
      try {
        const s = await fetchJSON<{
          running: boolean;
          status: "idle" | "running" | "done" | "error" | "stopping";
          last_risk?: string;
          run_id?: string;
          pct_complete?: number;
          current_day?: number;
          total_days?: number;
          current_date?: string;
          live_trades?: number;
          live_wins?: number;
          live_pnl?: number;
          live_equity?: number;
          elapsed_secs?: number;
          eta_secs?: number;
        }>("/backtest/status");

        setProgress((prev) => ({
          ...prev,
          risk: s.last_risk || prev.risk,
          run_id: s.run_id || prev.run_id,
          pct_complete: s.pct_complete ?? 0,
          current_day: s.current_day ?? 0,
          total_days: s.total_days ?? 0,
          current_date: s.current_date ?? "",
          live_trades: s.live_trades ?? 0,
          live_wins: s.live_wins ?? 0,
          live_pnl: s.live_pnl ?? 0,
          live_equity: s.live_equity ?? 10000,
          elapsed_secs: s.elapsed_secs ?? 0,
          eta_secs: s.eta_secs ?? 0,
        }));
        if (!s.running) {
          setRunning(false);
          setStatusText(s.status === "error" ? "error" : "done");
          await load();
          if (s.run_id) setSelectedRunId(s.run_id);
          await loadHistory();
        }
      } catch {
        // ignore transient poll error
      }
    }, 2000);
    return () => clearInterval(id);
  }, [running, load, loadHistory]);

  useEffect(() => {
    if (termRef.current) {
      const { scrollTop, scrollHeight, clientHeight } = termRef.current;
      if (scrollHeight - scrollTop - clientHeight < 150) {
        termRef.current.scrollTop = termRef.current.scrollHeight;
      }
    }
  }, [logs.length]);

  const runBacktest = async (risk: RiskLevel) => {
    if (running) return;
    setSelectedRisk(risk);
    setRunning(true);
    setStatusText("running");
    setLogs([
      `=== Launching ${symbol} ${risk.toUpperCase()} (${strategyMode === "candidate_15_e1" ? "Candidate 15:00 UTC + E1" : "Baseline 17:00 UTC"}) | ${startDate || "All"} -> ${endDate || "All"} ===`,
    ]);
    setProgress({
      risk,
      run_id: "",
      pct_complete: 0,
      current_day: 0,
      total_days: 0,
      current_date: startDate,
      live_trades: 0,
      live_wins: 0,
      live_pnl: 0,
      live_equity: 10000,
      elapsed_secs: 0,
      eta_secs: 0,
    });
    if (compareKalman) setCompareReport(null);

    try {
      const res = await postJSON<{ run_id?: string; error?: string }>("/backtest/run", {
        risk,
        symbol,
        start_date: startDate,
        end_date: endDate,
        exit_agent: exitAgent,
        compare_kalman: compareKalman,
        strategy_mode: strategyMode,
        commission_per_lot: commissionPerLot,
      });
      if (res.error) {
        setRunning(false);
        setStatusText("error");
        setLogs((prev) => [...prev, `ERROR: ${res.error}`]);
        return;
      }
      connectSSE();
    } catch (err) {
      setRunning(false);
      setStatusText("error");
      setLogs((prev) => [...prev, `ERROR: ${err instanceof Error ? err.message : "Request failed"}`]);
    }
  };

  const stopBacktest = async () => {
    try {
      await postJSON("/backtest/stop", {});
      setStatusText("stopping");
    } catch {
      // ignore
    }
  };

  const handleCopyTerminal = () => {
    if (logs.length === 0) return;
    navigator.clipboard.writeText(logs.join("\n"));
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const toggleCompareRun = (runId: string) => {
    setCompareRunIds((current) =>
      current.includes(runId)
        ? current.filter((id) => id !== runId)
        : current.length >= 3
          ? [...current.slice(1), runId]
          : [...current, runId]
    );
  };

  const daysInRange = availDays.filter((d) => {
    const ds = String(d.day);
    return (!startDate || ds >= startDate) && (!endDate || ds <= endDate);
  }).length;

  const p = results[selectedRisk];
  const selectedMetrics = useMemo(
    () => (selectedRun ? resolveRunMetrics(selectedRun) : null),
    [selectedRun]
  );
  const historyCurve = selectedRun?.equity_curve ?? [];
  const comparedRuns = compareRunIds
    .map((id) => history.find((run) => run.run_id === id))
    .filter((run): run is BacktestHistoryRun => Boolean(run));

  return (
    <div className="flex flex-col md:flex-row min-h-screen">
      <Sidebar />
      <main className="flex-1 p-3 sm:p-5 md:p-6 pb-24 md:pb-6 overflow-y-auto overflow-x-hidden">
        {/* Header */}
        <div className="flex items-center justify-between mb-5">
          <div>
            <h1 className="text-sm font-bold uppercase tracking-wider" style={{ color: "#00e87b" }}>
              Backtest
            </h1>
            <p className="text-[10px] mt-0.5" style={{ color: "#3d4450" }}>
              RUN &amp; COMPARE ACROSS RISK PROFILES · EXECUTION-PRICE SIZING + TICK-MTM DRAWDOWN
            </p>
          </div>
          <button onClick={() => load()} className="t-btn flex items-center gap-1.5">
            <RefreshCw className="w-3 h-3" /> REFRESH
          </button>
        </div>

        {/* Date range & Strategy config bar */}
        <div className="t-panel px-4 py-3 mb-5 flex items-center gap-3 flex-wrap">
          <div className="flex items-center gap-2">
            <Calendar className="w-3.5 h-3.5" style={{ color: "#4da6ff" }} />
            <span className="text-[10px] font-bold uppercase tracking-wider" style={{ color: "#5a6270" }}>
              DATE RANGE
            </span>
          </div>
          <RetroCalendar
            label="FROM"
            align="left"
            dates={availDays.map((d) => ({ day: String(d.day), bars: d.bars ?? 0, ticks: d.ticks }))}
            selectedDate={startDate}
            onSelect={setStartDate}
          />
          <RetroCalendar
            label="TO"
            align="left"
            dates={availDays.map((d) => ({ day: String(d.day), bars: d.bars ?? 0, ticks: d.ticks }))}
            selectedDate={endDate}
            onSelect={setEndDate}
          />
          <span className="text-[9px]" style={{ color: "#5a6270" }}>
            {daysInRange} TICK-DATA DAYS IN RANGE
            {availDays.length > 0 && <span style={{ color: "#3d4450" }}> / {availDays.length} TOTAL</span>}
          </span>
          {availDays.length > 0 && (
            <button
              type="button"
              onClick={() => {
                setStartDate(String(availDays[0].day));
                setEndDate(String(availDays[availDays.length - 1].day));
              }}
              className="text-[9px] font-bold uppercase tracking-wider px-2 py-0.5"
              style={{ background: "#181c24", border: "1px solid #252a33", color: "#4da6ff" }}
            >
              ALL DATES
            </button>
          )}

          <div className="flex items-center gap-2 ml-2">
            <label className="text-[9px] uppercase tracking-wider" style={{ color: "#3d4450" }}>
              PAIR
            </label>
            <select
              value={symbol}
              onChange={(e) => {
                setSymbol(e.target.value);
                datesInitRef.current = false;
                load(e.target.value);
              }}
              className="text-[11px] px-2 py-1 outline-none"
            >
              <option value="XAUUSD">XAUUSD</option>
              <option value="EURUSD">EURUSD</option>
              <option value="GBPUSD">GBPUSD</option>
            </select>
          </div>

          <div className="flex items-center gap-2">
            <label className="text-[9px] uppercase tracking-wider" style={{ color: "#3d4450" }}>
              STRATEGY
            </label>
            <select
              value={strategyMode}
              onChange={(e) => setStrategyMode(e.target.value as StrategyMode)}
              className="text-[11px] px-2 py-1 outline-none"
              style={{
                background: strategyMode === "candidate_15_e1" ? "rgba(0, 232, 123, 0.10)" : "#181c24",
                borderColor: strategyMode === "candidate_15_e1" ? "#00e87b" : "#252a33",
                color: strategyMode === "candidate_15_e1" ? "#00e87b" : "#c8cdd5",
              }}
            >
              <option value="candidate_15_e1">Candidate (15:00 UTC + E1)</option>
              <option value="baseline">Baseline (17:00 UTC)</option>
            </select>
          </div>

          <div className="flex items-center gap-2">
            <label className="text-[9px] uppercase tracking-wider" style={{ color: "#3d4450" }}>
              EXIT AGENT
            </label>
            <select
              value={exitAgent}
              onChange={(e) => setExitAgent(e.target.value as ExitAgentMode)}
              className="text-[11px] px-2 py-1 outline-none"
            >
              <option value="Standard">Standard (Frozen Trail)</option>
              <option value="Kalman">Kalman Velocity</option>
            </select>
          </div>

          <div className="flex items-center gap-2">
            <label className="text-[9px] uppercase tracking-wider" style={{ color: "#3d4450" }}>
              COMM $/LOT
            </label>
            <input
              type="number"
              min={0}
              step={0.5}
              value={commissionPerLot}
              onChange={(e) => setCommissionPerLot(Math.max(0, Number(e.target.value) || 0))}
              className="w-16 text-[11px] px-2 py-1 text-right"
            />
          </div>

          <button
            type="button"
            onClick={() => setCompareKalman((v) => !v)}
            className="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wider px-2.5 py-1 transition-colors"
            style={{
              background: compareKalman ? "rgba(232, 195, 0, 0.14)" : "#181c24",
              border: `1px solid ${compareKalman ? "#e8c300" : "#252a33"}`,
              color: compareKalman ? "#e8c300" : "#7a8494",
            }}
          >
            <span
              className="inline-block w-2 h-2 rounded-full"
              style={{ background: compareKalman ? "#e8c300" : "#3d4450" }}
            />
            COMPARE KALMAN (A/B): {compareKalman ? "ON" : "OFF"}
          </button>
        </div>

        {/* Risk profile cards */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-[1px] mb-5">
          {profiles &&
            (["low", "medium", "high"] as RiskLevel[]).map((r) => (
              <div key={r} className="space-y-[1px]">
                <RiskProfileCard
                  level={r}
                  profile={profiles[r]}
                  active={selectedRisk === r}
                  onSelect={setSelectedRisk}
                  context="backtest"
                  result={results[r]}
                />
                <button
                  type="button"
                  onClick={() => runBacktest(r)}
                  disabled={running}
                  className="w-full flex items-center justify-center gap-2 px-3 py-2 text-[10px] font-bold uppercase tracking-wider transition-all"
                  style={{
                    background: running && progress.risk === r ? "#252a33" : riskColors[r],
                    color: running && progress.risk === r ? "#c8cdd5" : "#000",
                    opacity: running && progress.risk !== r ? 0.4 : 1,
                  }}
                >
                  {running && progress.risk === r ? (
                    <>
                      <RefreshCw className="w-3 h-3 animate-spin" /> RUNNING...
                    </>
                  ) : (
                    <>
                      <Play className="w-3 h-3" /> RUN {r.toUpperCase()}
                    </>
                  )}
                </button>
              </div>
            ))}
        </div>

        {/* Live Snapshot cards (only while running) */}
        {running && (
          <div className="mb-4">
            <div className="mb-2">
              <div className="flex items-center justify-between mb-1">
                <span className="text-[9px] font-bold uppercase tracking-wider" style={{ color: "#e8c300" }}>
                  {progress.total_days
                    ? `Day ${progress.current_day} of ${progress.total_days}${progress.current_date ? ` · ${progress.current_date}` : ""}`
                    : "Initialising..."}
                </span>
                <span className="text-[9px]" style={{ color: "#5a6270" }}>
                  {`${progress.pct_complete.toFixed(1)}%`}
                  {progress.eta_secs > 0 ? (
                    <span style={{ color: "#4da6ff" }}> · ETA {formatEta(progress.eta_secs)}</span>
                  ) : null}
                </span>
              </div>
              <div className="w-full h-1.5 overflow-hidden" style={{ background: "#181c24" }}>
                <div
                  className="h-full transition-all duration-700 ease-out"
                  style={{
                    width: `${progress.pct_complete}%`,
                    background: "linear-gradient(90deg, #00e87b 0%, #4da6ff 100%)",
                  }}
                />
              </div>
            </div>

            <div className="grid grid-cols-2 md:grid-cols-4 gap-[1px]">
              {[
                {
                  label: "LIVE P&L",
                  value: fmtUsd(progress.live_pnl, true),
                  color: progress.live_pnl >= 0 ? "#00e87b" : "#ff3e3e",
                },
                {
                  label: "WIN RATE",
                  value:
                    progress.live_trades > 0
                      ? `${((progress.live_wins / progress.live_trades) * 100).toFixed(1)}%`
                      : "—",
                  color: "#c8cdd5",
                },
                {
                  label: "TRADES",
                  value: progress.live_trades,
                  color: "#4da6ff",
                },
                {
                  label: "EQUITY",
                  value: fmtUsd(progress.live_equity),
                  color: "#c8cdd5",
                },
              ].map(({ label, value, color }) => (
                <div key={label} className="t-panel px-3 py-2">
                  <p className="text-[8px] uppercase tracking-[1.5px] mb-0.5" style={{ color: "#3d4450" }}>
                    {label}
                  </p>
                  <p className="text-base font-bold leading-tight" style={{ color }}>
                    {value}
                  </p>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Backtest progress terminal */}
        {statusText !== "idle" && (
          <div
            className="t-panel mb-5"
            style={{
              borderColor: running ? "#e8c300" : statusText === "done" ? "#00e87b" : "#ff3e3e",
            }}
          >
            <div
              className="flex items-center justify-between px-4 py-2"
              style={{
                background: "#0e1117",
                borderBottom: `1px solid ${running ? "#e8c300" : statusText === "done" ? "#00e87b" : "#ff3e3e"}`,
              }}
            >
              <div className="flex items-center gap-2">
                <Terminal
                  className="w-3 h-3"
                  style={{ color: running ? "#e8c300" : statusText === "done" ? "#00e87b" : "#ff3e3e" }}
                />
                <span
                  className="text-[10px] font-bold uppercase tracking-wider"
                  style={{ color: running ? "#e8c300" : statusText === "done" ? "#00e87b" : "#ff3e3e" }}
                >
                  BACKTEST {progress.risk?.toUpperCase()} ({symbol} · {fmtModeLabel(strategyMode).toUpperCase()}) —{" "}
                  {statusText.toUpperCase()}
                </span>
              </div>
              <div className="flex items-center gap-2 text-[9px]" style={{ color: "#5a6270" }}>
                {running && (
                  <button
                    type="button"
                    onClick={stopBacktest}
                    className="px-2 py-1 bg-[#ff3e3e]/10 text-[#ff3e3e] font-bold tracking-wider"
                  >
                    STOP
                  </button>
                )}
                <button
                  type="button"
                  onClick={handleCopyTerminal}
                  className="flex items-center gap-1 px-2 py-1 bg-[#3d4450]/20 hover:bg-[#3d4450]/40 text-white/70"
                >
                  {copied ? <Check className="w-3 h-3 text-[#00e87b]" /> : <Copy className="w-3 h-3" />}
                  {copied ? "COPIED" : "COPY"}
                </button>
              </div>
            </div>

            {running && (
              <div className="w-full h-0.5" style={{ background: "#181c24" }}>
                <div
                  className="h-full transition-all duration-700 ease-out"
                  style={{
                    width: `${progress.pct_complete}%`,
                    background: "linear-gradient(90deg, #00e87b 0%, #4da6ff 100%)",
                  }}
                />
              </div>
            )}

            <div
              ref={termRef}
              className="px-4 py-3 overflow-y-auto font-mono"
              style={{ background: "#0a0c10", height: 460, fontSize: 10, lineHeight: "1.7" }}
            >
              {logs.length === 0 ? (
                <span style={{ color: "#3d4450" }}>WAITING FOR OUTPUT...</span>
              ) : (
                logs.map((line, i) => {
                  const color =
                    line.includes("ERROR") || line.includes("LOSS") || line.includes("-$")
                      ? "#ff3e3e"
                      : line.includes("ENTRY")
                        ? "#4da6ff"
                        : line.includes("WIN") || line.includes("+$") || line.includes("TARGET")
                          ? "#00e87b"
                          : line.includes("EXIT") || line.includes("FINAL REPORT")
                            ? "#e8c300"
                            : line.includes("SKIP") || line.includes("COOL")
                              ? "#3d4450"
                              : "#7a8290";
                  return (
                    <div key={i} style={{ color, whiteSpace: "pre" }}>
                      <span style={{ color: "#1e2230", marginRight: 8, userSelect: "none" }}>
                        {String(i + 1).padStart(4, " ")}
                      </span>
                      {line}
                    </div>
                  );
                })
              )}
            </div>
          </div>
        )}

        {/* Active session run scorecard */}
        {p && (p.trades ?? 0) > 0 ? (
          <>
            <div className="flex items-center justify-between mb-2">
              <span className="text-[11px] font-bold uppercase tracking-wider" style={{ color: riskColors[selectedRisk] }}>
                CURRENT SESSION RESULT — {selectedRisk.toUpperCase()} ({p.trades} TRADES)
              </span>
              <button
                type="button"
                onClick={() => downloadCSV(p, selectedRisk)}
                className="t-btn flex items-center gap-1.5"
              >
                <Download className="w-3 h-3" /> EXPORT CSV
              </button>
            </div>

            <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-[1px] mb-4">
              {[
                { label: "Total P&L", value: fmtUsd(p.pnl, true), color: p.pnl >= 0 ? "#00e87b" : "#ff3e3e" },
                { label: "Win Rate", value: fmtWinRate(p.win_rate), color: "#c8cdd5" },
                { label: "Profit Factor", value: p.pf.toFixed(2), color: p.pf >= 1.5 ? "#00e87b" : "#e8c300" },
                { label: "Risk-Reward", value: `${p.rr.toFixed(2)}×`, color: "#4da6ff" },
                { label: "Total Trades", value: p.trades, color: "#c8cdd5" },
                { label: "Avg / Trade", value: fmtUsd(p.pnl / Math.max(p.trades, 1), true), color: p.pnl >= 0 ? "#00e87b" : "#ff3e3e" },
                { label: "Required Capital", value: fmtUsd(p.required_capital ?? 10000), color: "#c8cdd5" },
                { label: "Return (ROI)", value: `${(p.roi ?? 0).toFixed(1)}%`, color: (p.roi ?? 0) >= 0 ? "#00e87b" : "#ff3e3e" },
                { label: "Final Equity", value: fmtUsd(p.final_equity ?? 10000 + p.pnl), color: "#4da6ff" },
                { label: "Closed MaxDD", value: `${fmtUsd(p.closed_max_dd ?? p.max_dd)} (${(p.closed_max_dd_pct ?? 0).toFixed(2)}%)`, color: "#e8c300" },
                { label: "Tick-MTM MaxDD", value: `${fmtUsd(p.mtm_max_dd ?? p.max_dd)} (${(p.mtm_max_dd_pct ?? 0).toFixed(2)}%)`, color: "#ff3e3e" },
                { label: "Max Plan Risk", value: `${(p.max_planned_risk_pct ?? 0).toFixed(3)}%`, color: (p.max_planned_risk_pct ?? 0) <= 1.0 ? "#00e87b" : "#ff3e3e" },
              ].map(({ label, value, color }) => (
                <div key={label} className="t-panel p-3">
                  <p className="text-[9px] uppercase tracking-[1.5px] mb-1" style={{ color: "#5a6270" }}>
                    {label}
                  </p>
                  <p className="text-lg font-bold" style={{ color }}>
                    {value}
                  </p>
                </div>
              ))}
            </div>

            <div className="t-panel p-4 mb-4">
              <h3 className="text-[11px] font-semibold mb-3 uppercase tracking-wider" style={{ color: "#5a6270" }}>
                Equity Curve — <span style={{ color: riskColors[selectedRisk] }}>{selectedRisk}</span>
              </h3>
              <EquityChart curves={curves} selected={selectedRisk} />
            </div>

            {compareReport && compareReport.available && (
              <div className="t-panel p-4 mb-4">
                <div className="flex items-center justify-between mb-3">
                  <h3 className="text-[11px] font-semibold uppercase tracking-wider flex items-center gap-2" style={{ color: "#c084fc" }}>
                    <TrendingUp className="w-3.5 h-3.5" /> Kalman Exit A/B Comparison ({compareReport.symbol})
                  </h3>
                  <span
                    className="text-[11px] font-bold"
                    style={{ color: compareReport.net_kalman_difference >= 0 ? "#00e87b" : "#ff3e3e" }}
                  >
                    NET DIFF: {fmtUsd(compareReport.net_kalman_difference, true)}
                  </span>
                </div>
                <table>
                  <thead>
                    <tr>
                      <th>Metric</th>
                      <th>Standard Exit</th>
                      <th>Kalman Exit</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[
                      ["Trades", compareReport.without_kalman.trades, compareReport.with_kalman.trades],
                      ["Win Rate", `${compareReport.without_kalman.win_rate}%`, `${compareReport.with_kalman.win_rate}%`],
                      ["Total P&L", fmtUsd(compareReport.without_kalman.total_pnl, true), fmtUsd(compareReport.with_kalman.total_pnl, true)],
                      ["Closed MaxDD", fmtUsd(compareReport.without_kalman.closed_max_drawdown ?? compareReport.without_kalman.max_drawdown), fmtUsd(compareReport.with_kalman.closed_max_drawdown ?? compareReport.with_kalman.max_drawdown)],
                      ["Tick-MTM MaxDD", fmtUsd(compareReport.without_kalman.mtm_max_drawdown ?? compareReport.without_kalman.max_drawdown), fmtUsd(compareReport.with_kalman.mtm_max_drawdown ?? compareReport.with_kalman.max_drawdown)],
                      ["Profit Factor", compareReport.without_kalman.pf, compareReport.with_kalman.pf],
                    ].map(([lbl, a, b]) => (
                      <tr key={String(lbl)}>
                        <td style={{ color: "#5a6270" }}>{lbl}</td>
                        <td>{a}</td>
                        <td className="font-bold">{b}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {p.trade_list && p.trade_list.length > 0 && (
              <div className="t-panel p-4 mb-4">
                <h3 className="text-[11px] font-semibold mb-3 uppercase tracking-wider" style={{ color: "#5a6270" }}>
                  Trade List — <span style={{ color: riskColors[selectedRisk] }}>{selectedRisk}</span>
                  <span className="ml-2 font-normal" style={{ color: "#3d4450" }}>
                    {p.trade_list.length} trades
                  </span>
                </h3>
                <TradeTable trades={p.trade_list} detailed />
              </div>
            )}
          </>
        ) : (
          <div className="t-panel p-4 mb-4 text-center text-[11px]" style={{ color: "#5a6270" }}>
            NO LIVE SESSION RESULT FOR <span style={{ color: riskColors[selectedRisk] }}>{selectedRisk.toUpperCase()}</span> — CLICK RUN ABOVE OR INSPECT A SAVED REPORT BELOW.
          </div>
        )}

        {/* All-profile comparison */}
        <div className="t-panel p-4 mb-6">
          <h3 className="text-[11px] font-semibold mb-3 uppercase tracking-wider" style={{ color: "#5a6270" }}>
            All Profiles — Equity Curves
          </h3>
          <EquityChart curves={curves} selected="all" />

          <table className="mt-4">
            <thead>
              <tr>
                {["Profile", "Trades", "P&L", "Win Rate", "R:R", "Closed DD", "MTM DD", "Plan Risk Max"].map((h) => (
                  <th key={h}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {(["low", "medium", "high"] as RiskLevel[]).map((r) => {
                const rp = results[r];
                if (!rp) {
                  return (
                    <tr key={r}>
                      <td className="uppercase font-semibold" style={{ color: riskColors[r] }}>
                        {r}
                      </td>
                      <td colSpan={7} style={{ color: "#3d4450" }}>
                        NO DATA
                      </td>
                    </tr>
                  );
                }
                return (
                  <tr key={r} style={{ background: selectedRisk === r ? "#1e222c" : undefined }}>
                    <td className="uppercase font-semibold" style={{ color: riskColors[r] }}>
                      {r}
                    </td>
                    <td>{rp.trades}</td>
                    <td style={{ color: rp.pnl >= 0 ? "#00e87b" : "#ff3e3e", fontWeight: 600 }}>
                      {fmtUsd(rp.pnl, true)}
                    </td>
                    <td>{fmtWinRate(rp.win_rate)}</td>
                    <td>{rp.rr.toFixed(2)}×</td>
                    <td style={{ color: "#e8c300" }}>{fmtUsd(rp.closed_max_dd ?? rp.max_dd)}</td>
                    <td style={{ color: "#ff3e3e" }}>{fmtUsd(rp.mtm_max_dd ?? rp.max_dd)}</td>
                    <td style={{ color: "#00e87b" }}>{(rp.max_planned_risk_pct ?? 0).toFixed(3)}%</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        {/* Persisted reports */}
        <section>
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2">
              <History className="w-4 h-4" style={{ color: "#4da6ff" }} />
              <div>
                <h2 className="text-[11px] font-bold uppercase tracking-wider" style={{ color: "#c8cdd5" }}>
                  Historical Backtest Reports
                </h2>
                <p className="text-[9px] mt-0.5" style={{ color: "#5a6270" }}>
                  SAVED RUNS, DUAL DRAWDOWN METRICS, BREAKDOWNS, AND HEAD-TO-HEAD COMPARISON
                </p>
              </div>
            </div>
            <button onClick={loadHistory} disabled={historyLoading} className="t-btn flex items-center gap-1.5">
              <RefreshCw className={`w-3 h-3 ${historyLoading ? "animate-spin" : ""}`} /> REFRESH HISTORY
            </button>
          </div>

          <div className="t-panel p-3 mb-3 flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1 text-[9px] uppercase tracking-wider" style={{ color: "#5a6270" }}>
              FROM (LOCAL TIME)
              <input
                type="datetime-local"
                value={historyFrom}
                onChange={(e) => {
                  setHistoryOffset(0);
                  setHistoryFrom(e.target.value);
                }}
                className="text-[11px] px-2 py-1"
              />
            </label>
            <label className="flex flex-col gap-1 text-[9px] uppercase tracking-wider" style={{ color: "#5a6270" }}>
              TO (LOCAL TIME)
              <input
                type="datetime-local"
                value={historyTo}
                onChange={(e) => {
                  setHistoryOffset(0);
                  setHistoryTo(e.target.value);
                }}
                className="text-[11px] px-2 py-1"
              />
            </label>
            <label className="flex flex-col gap-1 text-[9px] uppercase tracking-wider" style={{ color: "#5a6270" }}>
              RISK PROFILE
              <select
                value={historyRisk}
                onChange={(e) => {
                  setHistoryOffset(0);
                  setHistoryRisk(e.target.value);
                }}
                className="text-[11px] px-2 py-1"
              >
                <option value="">ALL</option>
                <option value="low">LOW</option>
                <option value="medium">MEDIUM</option>
                <option value="high">HIGH</option>
              </select>
            </label>

            <div className="ml-auto flex items-center gap-2">
              {compareRunIds.length >= 2 && (
                <Link
                  href={`/backtest/compare?runs=${compareRunIds.join(",")}`}
                  className="t-btn px-3 py-1 text-[11px] font-bold flex items-center gap-1.5"
                  style={{ background: "#00e87b22", color: "#00e87b", border: "1px solid #00e87b88" }}
                >
                  COMPARE {compareRunIds.length} RUNS -&gt;
                </Link>
              )}
              {compareRunIds.length === 1 && (
                <span
                  className="text-[10px] px-2 py-0.5"
                  style={{ color: "#e8c300", background: "#e8c30015", border: "1px solid #e8c30044" }}
                >
                  Select 1 more run to compare
                </span>
              )}
              <span className="text-[10px]" style={{ color: "#5a6270" }}>
                {historyTotal} REPORT{historyTotal === 1 ? "" : "S"}
              </span>
            </div>
          </div>

          {historyError && (
            <div className="t-panel p-3 mb-3 text-[10px]" style={{ color: "#ff6b6b" }}>
              Could not load saved reports: {historyError}
            </div>
          )}

          <div className="t-panel p-3 mb-4 overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr>
                  {["Compare", "Created", "Pair / Risk", "Mode", "Exit", "Replay Range", "Trades", "P&L", "Win Rate", "Closed DD", "MTM DD", "PF"].map(
                    (label) => (
                      <th key={label}>{label}</th>
                    )
                  )}
                </tr>
              </thead>
              <tbody>
                {history.map((run) => {
                  const closedDd = run.metrics?.closed_max_dd ?? run.max_dd;
                  const mtmDd = run.metrics?.mtm_max_dd ?? run.max_dd;
                  return (
                    <tr
                      key={run.run_id}
                      onClick={() => setSelectedRunId(run.run_id)}
                      style={{
                        background: selectedRunId === run.run_id ? "#1e222c" : undefined,
                        cursor: "pointer",
                      }}
                    >
                      <td onClick={(e) => e.stopPropagation()}>
                        <input
                          type="checkbox"
                          aria-label={`Compare run ${run.run_id}`}
                          checked={compareRunIds.includes(run.run_id)}
                          onChange={() => toggleCompareRun(run.run_id)}
                        />
                      </td>
                      <td className="whitespace-nowrap">
                        {run.created_at ? new Date(run.created_at).toLocaleString() : "—"}
                      </td>
                      <td className="uppercase whitespace-nowrap">
                        {run.symbol}{" "}
                        <span style={{ color: riskColors[run.risk as RiskLevel] ?? "#c8cdd5" }}>
                          {run.risk}
                        </span>
                      </td>
                      <td style={{ color: (run.strategy_mode ?? "").includes("candidate") ? "#00e87b" : "#4da6ff" }}>
                        {fmtModeLabel(run.strategy_mode)}
                      </td>
                      <td className="uppercase">{run.exit_agent}</td>
                      <td className="whitespace-nowrap">
                        {run.start_date} -&gt; {run.end_date}
                      </td>
                      <td>{run.trades}</td>
                      <td style={{ color: run.pnl >= 0 ? "#00e87b" : "#ff3e3e", fontWeight: 600 }}>
                        {fmtUsd(run.pnl, true)}
                      </td>
                      <td>{fmtWinRate(run.win_rate)}</td>
                      <td style={{ color: "#e8c300" }}>{fmtUsd(closedDd)}</td>
                      <td style={{ color: "#ff3e3e" }}>{fmtUsd(mtmDd)}</td>
                      <td>{run.pf.toFixed(2)}</td>
                    </tr>
                  );
                })}
                {!historyLoading && history.length === 0 && (
                  <tr>
                    <td colSpan={12} className="py-4 text-center" style={{ color: "#5a6270" }}>
                      NO SAVED BACKTEST RUNS
                    </td>
                  </tr>
                )}
              </tbody>
            </table>

            <div className="mt-3 flex items-center justify-end gap-3 text-[10px]" style={{ color: "#5a6270" }}>
              <button
                type="button"
                className="t-btn flex items-center gap-1"
                disabled={historyOffset === 0}
                onClick={() => setHistoryOffset(Math.max(0, historyOffset - historyPageSize))}
              >
                <ChevronLeft className="w-3 h-3" /> PREVIOUS
              </button>
              <span>
                {historyTotal === 0 ? 0 : historyOffset + 1}–{Math.min(historyOffset + historyPageSize, historyTotal)} OF{" "}
                {historyTotal}
              </span>
              <button
                type="button"
                className="t-btn flex items-center gap-1"
                disabled={historyOffset + historyPageSize >= historyTotal}
                onClick={() => setHistoryOffset(historyOffset + historyPageSize)}
              >
                NEXT <ChevronRight className="w-3 h-3" />
              </button>
            </div>
          </div>

          {comparedRuns.length > 0 && (
            <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 bg-[#141820] border border-[#00e87b] shadow-[0_10px_30px_rgba(0,0,0,0.8)] px-5 py-3 flex items-center gap-4 text-xs backdrop-blur-md">
              <div className="flex items-center gap-2.5">
                <span className="w-2 h-2 rounded-full bg-[#00e87b] animate-ping" />
                <span className="font-bold text-[#c8cdd5]">{comparedRuns.length} Runs Selected</span>
                <span className="text-[#3d4450]">|</span>
                <div className="flex items-center gap-1.5 text-[10px]">
                  {comparedRuns.map((r) => (
                    <span
                      key={r.run_id}
                      className="px-2 py-0.5 uppercase font-semibold"
                      style={{
                        background: `${riskColors[r.risk as RiskLevel] || "#4da6ff"}18`,
                        color: riskColors[r.risk as RiskLevel] || "#4da6ff",
                        border: `1px solid ${riskColors[r.risk as RiskLevel] || "#4da6ff"}44`,
                      }}
                    >
                      {r.risk} ({r.start_date})
                    </span>
                  ))}
                </div>
              </div>
              <div className="flex items-center gap-2 border-l border-[#252a33] pl-3 ml-2">
                <button
                  type="button"
                  onClick={() => setCompareRunIds([])}
                  className="px-2 py-1 text-[11px] text-[#5a6270] hover:text-[#c8cdd5]"
                >
                  Clear
                </button>
                {comparedRuns.length >= 2 ? (
                  <Link
                    href={`/backtest/compare?runs=${compareRunIds.join(",")}`}
                    className="t-btn px-4 py-1.5 text-[11px] font-bold flex items-center gap-1.5"
                    style={{ background: "#00e87b", color: "#0d0f14" }}
                  >
                    Compare Head-to-Head -&gt;
                  </Link>
                ) : (
                  <span className="text-[10px] text-[#e8c300] italic">Select 1 more run</span>
                )}
              </div>
            </div>
          )}

          {selectedRunError && (
            <div className="t-panel p-3 mb-3 text-[10px]" style={{ color: "#ff6b6b" }}>
              Could not load selected report: {selectedRunError}
            </div>
          )}

          {/* Auto-loaded Selected Run Details */}
          {selectedRun && selectedMetrics && (
            <div className="t-panel p-4 mb-4">
              <div className="flex flex-wrap items-center justify-between gap-2 mb-4">
                <div>
                  <h3 className="text-[11px] font-bold uppercase tracking-wider" style={{ color: "#c8cdd5" }}>
                    Run Details — {selectedRun.symbol} / {selectedRun.risk.toUpperCase()} /{" "}
                    {fmtModeLabel(selectedRun.strategy_mode).toUpperCase()} ({selectedRun.exit_agent})
                  </h3>
                  <p className="text-[9px] mt-1" style={{ color: "#5a6270" }}>
                    {selectedRun.created_at ? new Date(selectedRun.created_at).toLocaleString() : "—"} ·{" "}
                    {selectedRun.start_date} -&gt; {selectedRun.end_date} · {selectedRun.run_id}
                  </p>
                </div>
                <span
                  className="text-[10px] uppercase font-bold"
                  style={{ color: selectedRun.status === "error" ? "#ff3e3e" : "#00e87b" }}
                >
                  {(selectedRun.status ?? "done").toUpperCase()}
                </span>
              </div>

              {selectedRun.error && (
                <p className="text-[10px] mb-3" style={{ color: "#ff6b6b" }}>
                  {selectedRun.error}
                </p>
              )}

              <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-8 gap-[1px] mb-4">
                {[
                  { label: "Total P&L", value: fmtUsd(selectedRun.pnl, true), color: selectedRun.pnl >= 0 ? "#00e87b" : "#ff3e3e" },
                  { label: "Trades", value: selectedRun.trades, color: "#c8cdd5" },
                  { label: "Win Rate", value: fmtWinRate(selectedRun.win_rate), color: normalizeWinRate(selectedRun.win_rate) >= 50 ? "#00e87b" : "#e8c300" },
                  { label: "PF / R:R", value: `${selectedRun.pf.toFixed(2)} / ${selectedRun.rr.toFixed(2)}×`, color: "#4da6ff" },
                  { label: "Closed MaxDD", value: `${fmtUsd(selectedMetrics.closed_max_dd ?? selectedRun.max_dd)}`, color: "#e8c300" },
                  { label: "Tick-MTM MaxDD", value: `${fmtUsd(selectedMetrics.mtm_max_dd ?? selectedRun.max_dd)}`, color: "#ff3e3e" },
                  { label: "Max Plan Risk", value: `${(selectedMetrics.max_planned_risk_pct ?? 0).toFixed(3)}%`, color: (selectedMetrics.max_planned_risk_pct ?? 0) <= 1.0 ? "#00e87b" : "#ff3e3e" },
                  { label: "ROI", value: `${(selectedMetrics.roi ?? 0).toFixed(1)}%`, color: (selectedMetrics.roi ?? 0) >= 0 ? "#00e87b" : "#ff3e3e" },
                ].map(({ label, value, color }) => (
                  <div key={label} className="bg-[#11141a] p-3">
                    <p className="text-[9px] uppercase mb-1" style={{ color: "#5a6270" }}>
                      {label}
                    </p>
                    <p className="text-sm font-bold" style={{ color }}>
                      {value}
                    </p>
                  </div>
                ))}
              </div>

              {historyCurve.length > 0 && (
                <div className="h-64 mb-4">
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart
                      data={historyCurve.map((point, index) => ({
                        trade: index + 1,
                        equity: point.equity,
                      }))}
                    >
                      <CartesianGrid strokeDasharray="3 3" stroke="#1e222c" />
                      <XAxis dataKey="trade" tick={{ fill: "#5a6270", fontSize: 9 }} />
                      <YAxis
                        domain={["auto", "auto"]}
                        tick={{ fill: "#5a6270", fontSize: 9 }}
                        tickFormatter={(v) => `$${Number(v).toLocaleString("en-US", { maximumFractionDigits: 0 })}`}
                        width={60}
                      />
                      <Tooltip
                        contentStyle={{ background: "#181c24", border: "1px solid #252a33", fontSize: 10 }}
                        formatter={(v: unknown) => [fmtUsd(Number(v)), "Equity"]}
                      />
                      <ReferenceLine y={10000} stroke="#3d4450" strokeDasharray="4 4" />
                      <Line
                        type="monotone"
                        dataKey="equity"
                        name="Equity"
                        stroke={riskColors[selectedRun.risk as RiskLevel] || "#00e87b"}
                        dot={false}
                      />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              )}

              {Object.keys(selectedMetrics.strategy_breakdown ?? {}).length > 0 && (
                <div className="mb-4 overflow-x-auto">
                  <h4 className="text-[10px] uppercase tracking-wider mb-2" style={{ color: "#5a6270" }}>
                    Strategy Performance
                  </h4>
                  <table className="w-full">
                    <thead>
                      <tr>
                        {["Strategy", "Trades", "Win Rate", "PF", "P&L"].map((label) => (
                          <th key={label}>{label}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(selectedMetrics.strategy_breakdown ?? {}).map(([strategy, stats]) => (
                        <tr key={strategy}>
                          <td>{strategy}</td>
                          <td>{stats.trades}</td>
                          <td>{stats.win_rate.toFixed(1)}%</td>
                          <td>{(stats.pf ?? 0).toFixed(2)}</td>
                          <td style={{ color: stats.pnl >= 0 ? "#00e87b" : "#ff3e3e" }}>
                            {fmtUsd(stats.pnl, true)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-4">
                {Object.keys(selectedMetrics.daily_breakdown ?? {}).length > 0 && (
                  <div className="overflow-x-auto max-h-64 overflow-y-auto">
                    <h4 className="text-[10px] uppercase tracking-wider mb-2" style={{ color: "#5a6270" }}>
                      Daily Performance
                    </h4>
                    <table className="w-full">
                      <thead>
                        <tr>
                          {["Date", "Trades", "Win Rate", "P&L"].map((label) => (
                            <th key={label}>{label}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {Object.entries(selectedMetrics.daily_breakdown ?? {}).map(([day, stats]) => (
                          <tr key={day}>
                            <td>{day}</td>
                            <td>{stats.trades}</td>
                            <td>{stats.win_rate.toFixed(1)}%</td>
                            <td style={{ color: stats.pnl >= 0 ? "#00e87b" : "#ff3e3e" }}>
                              {fmtUsd(stats.pnl, true)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}

                {Object.keys(selectedMetrics.direction_breakdown ?? {}).length > 0 && (
                  <div className="overflow-x-auto">
                    <h4 className="text-[10px] uppercase tracking-wider mb-2" style={{ color: "#5a6270" }}>
                      Direction Performance (BUY vs SELL)
                    </h4>
                    <table className="w-full">
                      <thead>
                        <tr>
                          {["Direction", "Trades", "Win Rate", "PF", "P&L"].map((label) => (
                            <th key={label}>{label}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {Object.entries(selectedMetrics.direction_breakdown ?? {}).map(([direction, stats]) => (
                          <tr key={direction}>
                            <td style={{ color: direction === "BUY" ? "#00e87b" : "#ff3e3e", fontWeight: 600 }}>
                              {direction}
                            </td>
                            <td>{stats.trades}</td>
                            <td>{stats.win_rate.toFixed(1)}%</td>
                            <td>{(stats.pf ?? 0).toFixed(2)}</td>
                            <td style={{ color: stats.pnl >= 0 ? "#00e87b" : "#ff3e3e", fontWeight: 600 }}>
                              {fmtUsd(stats.pnl, true)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>

              {(selectedRun.trade_list ?? []).length > 0 && (
                <div className="overflow-x-auto">
                  <h4 className="text-[10px] uppercase tracking-wider mb-2" style={{ color: "#5a6270" }}>
                    Saved Trades ({selectedRun.trade_list.length})
                  </h4>
                  <TradeTable trades={selectedRun.trade_list} detailed />
                </div>
              )}
            </div>
          )}
        </section>
      </main>
    </div>
  );
}
