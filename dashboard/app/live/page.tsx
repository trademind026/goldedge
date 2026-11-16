"use client";

import React, { useEffect, useState, useRef, useCallback } from "react";
import Sidebar from "@/components/Sidebar";
import Badge from "@/components/Badge";
import {
  LIVE_STREAM_URL,
  apiZeroLoss,
  apiLockProfit,
  apiExitPosition,
  apiKillSwitch,
  apiExecuteSuggestion,
  apiSetLiveMode,
  type LiveEnginePayload,
  type ForexPosition,
  type TradeSuggestion,
  type AccountState,
} from "@/lib/api";
import {
  RefreshCw, Zap, TrendingUp, TrendingDown, X, Trash2, Radio,
  Bot, ShieldAlert, Lock, DollarSign, LineChart, AlertTriangle, CheckCircle2
} from "lucide-react";

export default function LiveRoomPage() {
  const [telemetry, setTelemetry] = useState<LiveEnginePayload | null>(null);
  const [mode, setMode] = useState<"paper" | "demo" | "live">("paper");
  const [sseConnected, setSseConnected] = useState<boolean>(false);
  const [reconnectAttempts, setReconnectAttempts] = useState<number>(0);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [selectedJourney, setSelectedJourney] = useState<ForexPosition | null>(null);
  const [confirmModalSuggestion, setConfirmModalSuggestion] = useState<TradeSuggestion | null>(null);
  const [orderLots, setOrderLots] = useState<string>("0.50");
  const [killConfirm, setKillConfirm] = useState<boolean>(false);

  const esRef = useRef<EventSource | null>(null);
  const reconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null);

  // Resilient SSE Connection with Exponential Backoff
  const connectSSE = useCallback(() => {
    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }

    const es = new EventSource(LIVE_STREAM_URL);
    esRef.current = es;

    es.onopen = () => {
      setSseConnected(true);
      setReconnectAttempts(0);
    };

    es.onmessage = (event) => {
      try {
        const payload: LiveEnginePayload = JSON.parse(event.data);
        setTelemetry(payload);
        if (payload.mode) setMode(payload.mode);
      } catch (err) {
        console.error("SSE parse error:", err);
      }
    };

    es.onerror = () => {
      setSseConnected(false);
      es.close();
      esRef.current = null;

      // Exponential backoff reconnect: 1s, 2s, 4s, 8s (max 10s)
      const nextDelay = Math.min(1000 * Math.pow(2, reconnectAttempts), 10000);
      setReconnectAttempts((prev) => prev + 1);

      if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = setTimeout(() => {
        connectSSE();
      }, nextDelay);
    };
  }, [reconnectAttempts]);

  useEffect(() => {
    connectSSE();
    return () => {
      if (esRef.current) esRef.current.close();
      if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current);
    };
  }, [connectSSE]);

  // Actions
  const handleSelectMode = async (targetMode: "paper" | "demo" | "live") => {
    if (targetMode === mode) return;
    setActionLoading(`mode_${targetMode}`);
    try {
      await apiSetLiveMode(targetMode);
      setMode(targetMode);
    } catch (err) {
      console.error("Mode switch error:", err);
    } finally {
      setActionLoading(null);
    }
  };

  const handleZeroLoss = async (ticket: number) => {
    setActionLoading(`zero_${ticket}`);
    try {
      await apiZeroLoss(ticket);
    } catch (err) {
      console.error("Zero loss error:", err);
    } finally {
      setActionLoading(null);
    }
  };

  const handleLockProfit = async (ticket: number) => {
    setActionLoading(`lock_${ticket}`);
    try {
      await apiLockProfit(ticket, 0.60);
    } catch (err) {
      console.error("Lock profit error:", err);
    } finally {
      setActionLoading(null);
    }
  };

  const handleExit = async (ticket: number) => {
    setActionLoading(`exit_${ticket}`);
    try {
      await apiExitPosition(ticket);
    } catch (err) {
      console.error("Exit error:", err);
    } finally {
      setActionLoading(null);
    }
  };

  const handleKillSwitch = async () => {
    setActionLoading("kill");
    try {
      await apiKillSwitch();
      setKillConfirm(false);
    } catch (err) {
      console.error("Kill switch error:", err);
    } finally {
      setActionLoading(null);
    }
  };

  const handleExecuteSuggestion = async () => {
    if (!confirmModalSuggestion) return;
    const lotsNum = parseFloat(orderLots) || confirmModalSuggestion.recommended_lots || 0.5;
    setActionLoading("exec_modal");
    try {
      await apiExecuteSuggestion(confirmModalSuggestion.id || "manual", lotsNum);
      setConfirmModalSuggestion(null);
    } catch (err) {
      console.error("Execution error:", err);
    } finally {
      setActionLoading(null);
    }
  };

  const account: AccountState = telemetry?.account || {
    balance: 10000.0,
    equity: 10000.0,
    used_margin: 0.0,
    free_margin: 10000.0,
    margin_level_pct: 999.0,
    leverage: 100,
    currency: "USD",
  };

  const positions: ForexPosition[] = telemetry?.positions || [];
  const closedPositions: ForexPosition[] = telemetry?.closed_positions || [];
  const suggestions: TradeSuggestion[] = telemetry?.suggestions || [];
  const status = telemetry?.status || "scanning";
  const alerts = telemetry?.system_alerts || [];

  const isRolloverFreeze = status === "rollover_freeze";
  const marginWarning = account.margin_level_pct < 200 && account.used_margin > 0;
  const marginCritical = account.margin_level_pct < 100 && account.used_margin > 0;

  return (
    <div className="flex min-h-screen" style={{ background: "#0b0e14", color: "#e4e8ec", fontFamily: "var(--font-mono, monospace)" }}>
      <Sidebar />
      <main className="flex-1 p-4 md:p-6 overflow-y-auto">

        {/* 1. Rollover Blackout Banner */}
        {isRolloverFreeze && (
          <div className="mb-4 p-3 border-l-4 flex items-center justify-between" style={{ background: "#2a2000", borderColor: "#e8c300", color: "#e8c300" }}>
            <div className="flex items-center gap-2">
              <AlertTriangle className="w-4 h-4 text-[#e8c300] animate-pulse" />
              <div>
                <span className="font-bold text-[11px] uppercase tracking-wider">ROLLOVER SPREAD FREEZE (21:00 UTC)</span>
                <p className="text-[10px] text-[#e8c300aa]">Institutional rollover spread widening in effect. New manual entries temporarily gated.</p>
              </div>
            </div>
            <span className="text-[10px] uppercase font-bold tracking-widest px-2 py-0.5 rounded bg-[#e8c30033]">AUTO-GUARD ACTIVE</span>
          </div>
        )}

        {/* System Alerts */}
        {alerts.length > 0 && (
          <div className="mb-4 space-y-1.5">
            {alerts.slice(0, 2).map((a) => (
              <div
                key={a.id}
                className="flex items-center justify-between p-2.5 border-l-2 text-[11px]"
                style={{
                  background: "#15191e",
                  borderLeftColor: a.level === "critical" ? "#ff3e3e" : a.level === "warning" ? "#e8c300" : "#00e87b",
                  color: "#e4e8ec",
                }}
              >
                <div className="flex items-center gap-2">
                  <span className="font-bold uppercase tracking-wider" style={{ color: a.level === "critical" ? "#ff3e3e" : a.level === "warning" ? "#e8c300" : "#00e87b" }}>
                    [{a.level}]:
                  </span>
                  <span>{a.text}</span>
                </div>
                <span className="text-[9px] text-[#5a6270]">{a.ts}</span>
              </div>
            ))}
          </div>
        )}

        {/* 2. Key Metrics HUD (Header) */}
        <div className="p-4 mb-5 border rounded" style={{ background: "#11151c", borderColor: "#1e2430" }}>
          <div className="flex flex-wrap items-center justify-between gap-4 pb-4 border-b border-[#1e2430]">
            <div className="flex items-center gap-3">
              {/* Status Indicator */}
              <div className="flex items-center gap-2">
                <span
                  className="w-2.5 h-2.5 rounded-full"
                  style={{
                    background: status === "trading" || status === "scanning" ? "#00e87b" : isRolloverFreeze ? "#e8c300" : status === "awaiting_ticks" ? "#00e5ff" : "#ff3e3e",
                    boxShadow: status === "trading" || status === "scanning" ? "0 0 8px #00e87b" : isRolloverFreeze ? "0 0 8px #e8c300" : status === "awaiting_ticks" ? "0 0 8px #00e5ff" : "0 0 8px #ff3e3e",
                  }}
                />
                <span className="text-xs font-bold uppercase tracking-wider">
                  {status === "trading" ? "LIVE TRADING" : status === "scanning" ? "RADAR SCANNING" : status === "rollover_freeze" ? "ROLLOVER FREEZE" : status === "awaiting_ticks" ? "AWAITING MARKET TICKS" : status === "broker_disconnected" ? "MT5 DISCONNECTED" : "IDLE"}
                </span>
              </div>
              <span className="text-[#3d4450]">|</span>
              <span className="text-[10px] text-[#5a6270]">
                LATENCY: <strong className="text-[#00e87b]">{telemetry?.latency_ms ?? 0}ms</strong>
              </span>
              <span className="text-[#3d4450]">|</span>
              <span className="flex items-center gap-1 text-[10px]" style={{ color: sseConnected ? "#00e87b" : "#ff3e3e" }}>
                <Radio className="w-3 h-3" />
                {sseConnected ? "SSE CONNECTED (1s)" : "RECONNECTING..."}
              </span>
            </div>

            {/* Right Header Controls */}
            <div className="flex items-center gap-3">
              {/* 3-Way Mode Switcher (Paper, Demo, Live) */}
              <div className="flex items-center gap-[1px] p-0.5 rounded border border-[#252a33]" style={{ background: "#0e1117" }}>
                {(["paper", "demo", "live"] as const).map((m) => {
                  const isActive = mode === m;
                  const activeColor = m === "live" ? "#ff3e3e" : m === "demo" ? "#00e5ff" : "#00e87b";
                  const activeBg = m === "live" ? "#330a0a" : m === "demo" ? "#002b33" : "#0a2e18";
                  return (
                    <button
                      key={m}
                      onClick={() => handleSelectMode(m)}
                      disabled={actionLoading === `mode_${m}`}
                      className="px-2.5 py-1 text-[9px] font-extrabold uppercase tracking-wider rounded transition-all"
                      style={{
                        background: isActive ? activeBg : "transparent",
                        color: isActive ? activeColor : "#5a6270",
                        border: isActive ? `1px solid ${activeColor}` : "1px solid transparent",
                      }}
                    >
                      {m === "paper" ? "PAPER" : m === "demo" ? "DEMO (MT5)" : "LIVE"}
                    </button>
                  );
                })}
              </div>

              {/* Kill Switch Button */}
              {killConfirm ? (
                <div className="flex items-center gap-1">
                  <button
                    onClick={handleKillSwitch}
                    disabled={actionLoading === "kill"}
                    className="px-3 py-1.5 text-[10px] font-extrabold uppercase tracking-wider bg-[#ff3e3e] text-black rounded animate-pulse"
                  >
                    CONFIRM HALT & EXIT ALL
                  </button>
                  <button onClick={() => setKillConfirm(false)} className="p-1.5 text-[#5a6270] hover:text-white">
                    <X className="w-3.5 h-3.5" />
                  </button>
                </div>
              ) : (
                <button
                  onClick={() => setKillConfirm(true)}
                  className="px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider border border-[#ff3e3e66] text-[#ff3e3e] hover:bg-[#ff3e3e11] rounded transition-all"
                >
                  <ShieldAlert className="w-3 h-3 inline mr-1" />
                  EMERGENCY KILL SWITCH
                </button>
              )}
            </div>
          </div>

          {/* Account Strip */}
          <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 gap-4 pt-4">
            <div>
              <p className="text-[9px] uppercase tracking-wider text-[#5a6270]">Account Balance</p>
              <p className="text-base md:text-lg font-bold text-white">${account.balance.toFixed(2)}</p>
            </div>
            <div>
              <p className="text-[9px] uppercase tracking-wider text-[#5a6270]">Real-Time Equity</p>
              <p className="text-base md:text-lg font-bold" style={{ color: account.equity >= account.balance ? "#00e87b" : "#ff3e3e" }}>
                ${account.equity.toFixed(2)}
              </p>
            </div>
            <div>
              <p className="text-[9px] uppercase tracking-wider text-[#5a6270]">Used Margin</p>
              <p className="text-base md:text-lg font-bold text-[#c8cdd5]">${account.used_margin.toFixed(2)}</p>
            </div>
            <div>
              <p className="text-[9px] uppercase tracking-wider text-[#5a6270]">Free Margin</p>
              <p className="text-base md:text-lg font-bold text-[#00e87b]">${account.free_margin.toFixed(2)}</p>
            </div>
            <div>
              <p className="text-[9px] uppercase tracking-wider text-[#5a6270]">Margin Level %</p>
              <p
                className="text-base md:text-lg font-extrabold"
                style={{
                  color: marginCritical ? "#ff3e3e" : marginWarning ? "#e8c300" : "#00e87b",
                }}
              >
                {account.margin_level_pct >= 999 ? "∞ (HEALTHY)" : `${account.margin_level_pct.toFixed(1)}%`}
              </p>
            </div>
          </div>
        </div>

        {/* 3. Active Positions (Dual View: Desktop Table & Mobile Touch Cards) */}
        <div className="p-4 mb-5 border rounded" style={{ background: "#11151c", borderColor: "#1e2430" }}>
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2">
              <h2 className="text-xs font-bold uppercase tracking-wider text-[#00e87b]">
                Active Positions ({positions.length})
              </h2>
              <span className="text-[10px] text-[#5a6270]">· SUB-BAR INTRA-TICK MONITOR</span>
            </div>
            <span className="text-[10px] text-[#5a6270]">SPOT GOLD (XAUUSD) & FOREX</span>
          </div>

          {positions.length === 0 ? (
            <div className="py-8 text-center text-[#5a6270] text-xs">
              NO OPEN POSITIONS · ENGINE SCANNING AT 1S TELEMETRY
            </div>
          ) : (
            <>
              {/* Desktop Table View (>= 768px) */}
              <div className="hidden md:block overflow-x-auto">
                <table className="w-full text-left text-xs border-collapse">
                  <thead>
                    <tr className="border-b border-[#1e2430] text-[#5a6270] text-[10px] uppercase tracking-wider">
                      <th className="py-2 px-2">Ticket</th>
                      <th className="py-2 px-2">Symbol</th>
                      <th className="py-2 px-2">Dir</th>
                      <th className="py-2 px-2">Lots</th>
                      <th className="py-2 px-2">Entry</th>
                      <th className="py-2 px-2">Current</th>
                      <th className="py-2 px-2">Spread</th>
                      <th className="py-2 px-2">Dynamic SL</th>
                      <th className="py-2 px-2">P&L (Pips)</th>
                      <th className="py-2 px-2">P&L ($)</th>
                      <th className="py-2 px-2 text-right">Risk Overrides</th>
                    </tr>
                  </thead>
                  <tbody>
                    {positions.map((p) => {
                      const pnlColor = p.pnl_usd >= 0 ? "#00e87b" : "#ff3e3e";
                      return (
                        <tr key={p.ticket} className="border-b border-[#1a202c] hover:bg-[#151a24] transition-colors">
                          <td className="py-2.5 px-2 font-mono text-[#5a6270]">#{p.ticket}</td>
                          <td className="py-2.5 px-2 font-bold text-white">{p.symbol}</td>
                          <td className="py-2.5 px-2">
                            <Badge label={p.direction} variant={p.direction === "BUY" ? "green" : "red"} />
                          </td>
                          <td className="py-2.5 px-2 font-semibold text-[#c8cdd5]">{p.lots.toFixed(2)}</td>
                          <td className="py-2.5 px-2 font-mono">${p.entry_price.toFixed(2)}</td>
                          <td className="py-2.5 px-2 font-mono font-bold" style={{ color: pnlColor }}>
                            ${p.current_price.toFixed(2)}
                          </td>
                          <td className="py-2.5 px-2 text-[#5a6270]">{p.current_spread_pips}p</td>
                          <td className="py-2.5 px-2 font-mono text-[#e8c300]">${p.sl.toFixed(2)}</td>
                          <td className="py-2.5 px-2 font-bold font-mono" style={{ color: pnlColor }}>
                            {p.pnl_pips >= 0 ? "+" : ""}{p.pnl_pips} pips
                          </td>
                          <td className="py-2.5 px-2 font-extrabold font-mono" style={{ color: pnlColor }}>
                            {p.pnl_usd >= 0 ? "+" : ""}${p.pnl_usd.toFixed(2)}
                          </td>
                          <td className="py-2.5 px-2 text-right space-x-1.5 whitespace-nowrap">
                            <button
                              onClick={() => handleZeroLoss(p.ticket)}
                              disabled={actionLoading === `zero_${p.ticket}` || p.pnl_pips <= 0}
                              className="px-2 py-1 text-[10px] font-bold border border-[#00e87b44] text-[#00e87b] hover:bg-[#00e87b22] disabled:opacity-30 rounded"
                              title="Move Stop Loss to Breakeven (+ spread buffer)"
                            >
                              🔒 ZERO LOSS
                            </button>
                            <button
                              onClick={() => handleLockProfit(p.ticket)}
                              disabled={actionLoading === `lock_${p.ticket}` || p.pnl_pips < 5}
                              className="px-2 py-1 text-[10px] font-bold border border-[#e8c30044] text-[#e8c300] hover:bg-[#e8c30022] disabled:opacity-30 rounded"
                              title="Lock 60% of current floating profit"
                            >
                              💰 LOCK 60%
                            </button>
                            <button
                              onClick={() => setSelectedJourney(p)}
                              className="px-2 py-1 text-[10px] font-bold border border-[#4da6ff44] text-[#4da6ff] hover:bg-[#4da6ff22] rounded"
                            >
                              📈 JOURNEY
                            </button>
                            <button
                              onClick={() => handleExit(p.ticket)}
                              disabled={actionLoading === `exit_${p.ticket}`}
                              className="px-2.5 py-1 text-[10px] font-bold bg-[#ff3e3e22] text-[#ff3e3e] border border-[#ff3e3e44] hover:bg-[#ff3e3e] hover:text-black rounded"
                            >
                              EXIT
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>

              {/* Mobile Touch Cards (< 768px) */}
              <div className="md:hidden space-y-3">
                {positions.map((p) => {
                  const pnlColor = p.pnl_usd >= 0 ? "#00e87b" : "#ff3e3e";
                  return (
                    <div key={p.ticket} className="p-3 border border-[#1e2430] rounded" style={{ background: "#15191e" }}>
                      <div className="flex items-center justify-between pb-2 border-b border-[#1e2430]">
                        <div className="flex items-center gap-2">
                          <span className="font-bold text-white text-sm">{p.symbol}</span>
                          <Badge label={p.direction} variant={p.direction === "BUY" ? "green" : "red"} />
                          <span className="text-[10px] text-[#5a6270]">#{p.ticket}</span>
                        </div>
                        <span className="text-xs font-semibold text-[#c8cdd5]">{p.lots.toFixed(2)} Lots</span>
                      </div>

                      {/* Big Contrast P&L Display */}
                      <div className="my-3 flex items-baseline justify-between">
                        <div>
                          <p className="text-[9px] uppercase tracking-wider text-[#5a6270]">Floating P&L</p>
                          <p className="text-xl font-black font-mono" style={{ color: pnlColor }}>
                            {p.pnl_pips >= 0 ? "+" : ""}{p.pnl_pips} PIPS
                          </p>
                        </div>
                        <div className="text-right">
                          <p className="text-lg font-black font-mono" style={{ color: pnlColor }}>
                            {p.pnl_usd >= 0 ? "+" : ""}${p.pnl_usd.toFixed(2)}
                          </p>
                        </div>
                      </div>

                      {/* Kalman Velocity & Entropy Indicator Tags */}
                      <div className="flex items-center gap-2 mb-3 text-[10px]">
                        <span className="px-2 py-0.5 rounded bg-[#1e2430] text-[#c8cdd5]">
                          V_z: <strong>{p.kalman_velocity_z >= 0 ? "+" : ""}{p.kalman_velocity_z}</strong>
                        </span>
                        <span className="px-2 py-0.5 rounded bg-[#1e2430] text-[#c8cdd5]">
                          Entropy: <strong>{p.entropy}</strong> ({p.entropy > 2.3 ? "Chop" : "Clean Trend"})
                        </span>
                        <span className="px-2 py-0.5 rounded bg-[#1e2430] text-[#e8c300]">
                          SL: ${p.sl.toFixed(2)}
                        </span>
                      </div>

                      {/* Touch Ergonomic Action Bar (min 44px target) */}
                      <div className="grid grid-cols-3 gap-2 mb-2">
                        <button
                          onClick={() => handleZeroLoss(p.ticket)}
                          disabled={p.pnl_pips <= 0}
                          className="min-h-[44px] flex items-center justify-center text-[11px] font-bold border border-[#00e87b66] text-[#00e87b] rounded bg-[#00e87b11] disabled:opacity-30"
                        >
                          🔒 Zero Loss
                        </button>
                        <button
                          onClick={() => handleLockProfit(p.ticket)}
                          disabled={p.pnl_pips < 5}
                          className="min-h-[44px] flex items-center justify-center text-[11px] font-bold border border-[#e8c30066] text-[#e8c300] rounded bg-[#e8c30011] disabled:opacity-30"
                        >
                          💰 Lock 60%
                        </button>
                        <button
                          onClick={() => setSelectedJourney(p)}
                          className="min-h-[44px] flex items-center justify-center text-[11px] font-bold border border-[#4da6ff66] text-[#4da6ff] rounded bg-[#4da6ff11]"
                        >
                          📈 Journey
                        </button>
                      </div>

                      <button
                        onClick={() => handleExit(p.ticket)}
                        className="w-full min-h-[44px] flex items-center justify-center text-xs font-black tracking-wider bg-[#ff3e3e] text-black rounded"
                      >
                        🚨 EXIT POSITION
                      </button>
                    </div>
                  );
                })}
              </div>
            </>
          )}
        </div>

        {/* 4. Live Suggestion Queue (Enter Trade) */}
        <div className="p-4 mb-5 border rounded" style={{ background: "#11151c", borderColor: "#1e2430" }}>
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2">
              <h2 className="text-xs font-bold uppercase tracking-wider text-[#e8c300]">Live Suggestion Queue</h2>
              <span className="text-[10px] text-[#5a6270]">· QUANT + ML DIRECTIONAL EDGE</span>
            </div>
            <span className="text-[10px] text-[#5a6270]">GATE: SCORE &ge; 0.80 · PROB &ge; 0.62</span>
          </div>

          {suggestions.length === 0 ? (
            <div className="py-6 text-center text-[#5a6270] text-xs">
              NO SIGNALS CURRENTLY PASSING ALL 4 INSTITUTIONAL GATES
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {suggestions.map((s) => (
                <div key={s.id} className="p-3 border border-[#2a303c] rounded bg-[#15191e] flex flex-col justify-between">
                  <div className="flex items-center justify-between mb-2">
                    <div className="flex items-center gap-2">
                      <span className="font-bold text-white text-sm">{s.symbol}</span>
                      <Badge label={s.direction} variant={s.direction === "BUY" ? "green" : "red"} />
                      <span className="text-[10px] text-[#5a6270]">{s.strategy}</span>
                    </div>
                    <span className="text-[10px] font-bold text-[#00e87b] bg-[#00e87b22] px-2 py-0.5 rounded">
                      Edge: {(s.ml_probability ? s.ml_probability * 100 : 65).toFixed(0)}%
                    </span>
                  </div>

                  <div className="grid grid-cols-4 gap-2 my-2 text-[10px] text-[#5a6270]">
                    <div>
                      <p>Entry</p>
                      <p className="font-bold text-white">${s.entry_price.toFixed(2)}</p>
                    </div>
                    <div>
                      <p>SL ({s.sl_pips ?? 0}p)</p>
                      <p className="font-bold text-[#ff3e3e]">${(s.sl ?? s.sl_price ?? 0).toFixed(2)}</p>
                    </div>
                    <div>
                      <p>Target ({s.tp_pips ?? 0}p)</p>
                      <p className="font-bold text-[#00e87b]">${(s.tp ?? s.target_price ?? 0).toFixed(2)}</p>
                    </div>
                    <div>
                      <p>Spread</p>
                      <p className="font-bold text-[#c8cdd5]">{s.spread_pips} pips</p>
                    </div>
                  </div>

                  {!isRolloverFreeze ? (
                    <button
                      onClick={() => {
                        setConfirmModalSuggestion(s);
                        setOrderLots(String(s.recommended_lots || "0.50"));
                      }}
                      className="w-full mt-2 py-2 text-xs font-bold uppercase tracking-wider bg-[#00e87b] text-black hover:bg-[#00ff88] rounded transition-all"
                    >
                      EXECUTE {s.direction} ({s.recommended_lots || 0.5} LOTS)
                    </button>
                  ) : (
                    <div className="w-full mt-2 py-2 text-center text-xs font-bold uppercase tracking-wider rounded bg-[#2a2000] border border-[#e8c30055] text-[#e8c300]">
                      ENTRY GATED (ROLLOVER BLACKOUT)
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>

        {/* 5. Closed Trade History */}
        {closedPositions.length > 0 && (
          <div className="p-4 border rounded" style={{ background: "#11151c", borderColor: "#1e2430" }}>
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-xs font-bold uppercase tracking-wider text-[#5a6270]">
                Closed Positions History ({closedPositions.length})
              </h3>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs border-collapse">
                <thead>
                  <tr className="border-b border-[#1e2430] text-[#5a6270] text-[10px] uppercase tracking-wider">
                    <th className="py-2 px-2">Ticket</th>
                    <th className="py-2 px-2">Symbol</th>
                    <th className="py-2 px-2">Dir</th>
                    <th className="py-2 px-2">Lots</th>
                    <th className="py-2 px-2">Entry</th>
                    <th className="py-2 px-2">Exit</th>
                    <th className="py-2 px-2">Realised (Pips)</th>
                    <th className="py-2 px-2">Realised ($)</th>
                  </tr>
                </thead>
                <tbody>
                  {closedPositions.slice(0, 10).map((cp, idx) => {
                    const pnlColor = cp.pnl_usd >= 0 ? "#00e87b" : "#ff3e3e";
                    return (
                      <tr key={idx} className="border-b border-[#1a202c]">
                        <td className="py-2 px-2 font-mono text-[#5a6270]">#{cp.ticket}</td>
                        <td className="py-2 px-2 font-bold text-white">{cp.symbol}</td>
                        <td className="py-2 px-2"><Badge label={cp.direction} variant={cp.direction === "BUY" ? "green" : "red"} /></td>
                        <td className="py-2 px-2 text-[#c8cdd5]">{cp.lots}</td>
                        <td className="py-2 px-2 font-mono">${cp.entry_price.toFixed(2)}</td>
                        <td className="py-2 px-2 font-mono">${cp.current_price.toFixed(2)}</td>
                        <td className="py-2 px-2 font-bold font-mono" style={{ color: pnlColor }}>
                          {cp.pnl_pips >= 0 ? "+" : ""}{cp.pnl_pips} pips
                        </td>
                        <td className="py-2 px-2 font-bold font-mono" style={{ color: pnlColor }}>
                          {cp.pnl_usd >= 0 ? "+" : ""}${cp.pnl_usd.toFixed(2)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Modal: Order Confirmation */}
        {confirmModalSuggestion && (
          <div className="fixed inset-0 bg-black/70 flex items-center justify-center p-4 z-50">
            <div className="w-full max-w-md p-5 border rounded" style={{ background: "#15191e", borderColor: "#00e87b" }}>
              <div className="flex items-center justify-between pb-3 border-b border-[#1e2430]">
                <h3 className="font-bold text-sm uppercase tracking-wider text-[#00e87b]">Confirm Order Execution</h3>
                <button onClick={() => setConfirmModalSuggestion(null)} className="text-[#5a6270] hover:text-white">
                  <X className="w-4 h-4" />
                </button>
              </div>

              <div className="my-4 space-y-3">
                <div className="flex items-center justify-between text-xs">
                  <span className="text-[#5a6270]">Symbol / Direction:</span>
                  <span className="font-bold text-white">{confirmModalSuggestion.symbol} ({confirmModalSuggestion.direction})</span>
                </div>
                <div className="flex items-center justify-between text-xs">
                  <span className="text-[#5a6270]">Entry Fill (Estimated):</span>
                  <span className="font-mono text-white">${confirmModalSuggestion.entry_price.toFixed(2)}</span>
                </div>
                <div className="flex items-center justify-between text-xs">
                  <span className="text-[#5a6270]">Stop Loss / Target:</span>
                  <span className="font-mono text-[#ff3e3e]">
                    ${(confirmModalSuggestion.sl ?? confirmModalSuggestion.sl_price ?? 0).toFixed(2)}
                  </span>
                  {" / "}
                  <span className="font-mono text-[#00e87b]">
                    ${(confirmModalSuggestion.tp ?? confirmModalSuggestion.target_price ?? 0).toFixed(2)}
                  </span>
                </div>

                {/* Numeric Lot Adjustment Input */}
                <div>
                  <label className="block text-[10px] uppercase tracking-wider text-[#5a6270] mb-1">
                    Order Lot Size (1.00 = 100 oz)
                  </label>
                  <input
                    type="text"
                    inputMode="decimal"
                    value={orderLots}
                    onChange={(e) => setOrderLots(e.target.value)}
                    className="w-full p-2.5 text-base font-bold font-mono bg-[#0b0e14] border border-[#2a303c] rounded text-white focus:outline-none focus:border-[#00e87b]"
                    placeholder="0.50"
                  />
                </div>

                {/* Margin Check Preview */}
                <div className="p-2.5 bg-[#0b0e14] border border-[#1e2430] rounded text-[11px] space-y-1">
                  <div className="flex justify-between">
                    <span className="text-[#5a6270]">Required Margin:</span>
                    <span className="font-bold text-white">
                      ${((parseFloat(orderLots) || 0.5) * 100 * confirmModalSuggestion.entry_price / 100).toFixed(2)}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-[#5a6270]">Usable Free Margin:</span>
                    <span className="font-bold text-[#00e87b]">${account.free_margin.toFixed(2)}</span>
                  </div>
                </div>
              </div>

              <div className="flex gap-2">
                <button
                  onClick={() => setConfirmModalSuggestion(null)}
                  className="flex-1 py-2 text-xs font-bold border border-[#2a303c] text-[#5a6270] hover:text-white rounded"
                >
                  CANCEL
                </button>
                <button
                  onClick={handleExecuteSuggestion}
                  disabled={actionLoading === "exec_modal"}
                  className="flex-1 py-2 text-xs font-bold bg-[#00e87b] text-black hover:bg-[#00ff88] rounded transition-all"
                >
                  {actionLoading === "exec_modal" ? "EXECUTING..." : "EXECUTE ORDER"}
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Modal: Journey Visualizer */}
        {selectedJourney && (
          <div className="fixed inset-0 bg-black/70 flex items-center justify-center p-4 z-50">
            <div className="w-full max-w-lg p-5 border rounded" style={{ background: "#15191e", borderColor: "#4da6ff" }}>
              <div className="flex items-center justify-between pb-3 border-b border-[#1e2430]">
                <div className="flex items-center gap-2">
                  <LineChart className="w-4 h-4 text-[#4da6ff]" />
                  <h3 className="font-bold text-sm uppercase tracking-wider text-[#4da6ff]">
                    Trade Journey #{selectedJourney.ticket} ({selectedJourney.symbol})
                  </h3>
                </div>
                <button onClick={() => setSelectedJourney(null)} className="text-[#5a6270] hover:text-white">
                  <X className="w-4 h-4" />
                </button>
              </div>

              <div className="my-4">
                <div className="grid grid-cols-3 gap-2 mb-3 text-[11px] text-center">
                  <div className="p-2 bg-[#0b0e14] rounded">
                    <p className="text-[#5a6270]">Entry Price</p>
                    <p className="font-bold text-white">${selectedJourney.entry_price.toFixed(2)}</p>
                  </div>
                  <div className="p-2 bg-[#0b0e14] rounded">
                    <p className="text-[#5a6270]">Ratcheted SL</p>
                    <p className="font-bold text-[#e8c300]">${selectedJourney.sl.toFixed(2)}</p>
                  </div>
                  <div className="p-2 bg-[#0b0e14] rounded">
                    <p className="text-[#5a6270]">Floating Gain</p>
                    <p className="font-bold" style={{ color: selectedJourney.pnl_usd >= 0 ? "#00e87b" : "#ff3e3e" }}>
                      {selectedJourney.pnl_pips} pips
                    </p>
                  </div>
                </div>

                <div className="max-h-56 overflow-y-auto border border-[#1e2430] rounded p-2 bg-[#0b0e14] font-mono text-[10px]">
                  <div className="grid grid-cols-4 pb-1 border-b border-[#1e2430] text-[#5a6270] font-sans">
                    <span>Time</span>
                    <span>Price</span>
                    <span>SL</span>
                    <span className="text-right">P&L Pips</span>
                  </div>
                  {selectedJourney.journey && selectedJourney.journey.length > 0 ? (
                    selectedJourney.journey.map((pt, i) => (
                      <div key={i} className="grid grid-cols-4 py-1 border-b border-[#141820]">
                        <span className="text-[#5a6270]">{pt.ts}</span>
                        <span className="text-white">${pt.price.toFixed(2)}</span>
                        <span className="text-[#e8c300]">${pt.sl.toFixed(2)}</span>
                        <span className="text-right font-bold" style={{ color: pt.pnl_pips >= 0 ? "#00e87b" : "#ff3e3e" }}>
                          {pt.pnl_pips >= 0 ? "+" : ""}{pt.pnl_pips}
                        </span>
                      </div>
                    ))
                  ) : (
                    <div className="py-4 text-center text-[#5a6270]">Collecting tick journey points...</div>
                  )}
                </div>
              </div>

              <button
                onClick={() => setSelectedJourney(null)}
                className="w-full py-2 text-xs font-bold border border-[#2a303c] text-white hover:bg-[#1e2430] rounded"
              >
                CLOSE
              </button>
            </div>
          </div>
        )}

      </main>
    </div>
  );
}
