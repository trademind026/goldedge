"use client";

import React, { useEffect, useState, useRef } from "react";
import Sidebar from "@/components/Sidebar";
import Badge from "@/components/Badge";
import {
  fetchJSON, postJSON,
  enterPaperTrade, exitPaperTrade, clearClosedPositions, setAutoTrade,
  SSE_STREAM_URL, API_BASE,
  type LiveState, type TradeSuggestion, type PaperPosition, type StreamPayload,
} from "@/lib/api";
import { RefreshCw, Zap, TrendingUp, TrendingDown, X, Trash2, Radio, Bot, Hand, Activity } from "lucide-react";
import { useTradingMode } from "@/contexts/TradingModeContext";

export default function LivePage() {
  const { mode } = useTradingMode();
  const [state, setState] = useState<LiveState | null>(null);
  const [scanning, setScanning] = useState(false);
  const [lastUpdate, setLastUpdate] = useState("--");
  const [positionsByMode, setPositionsByMode] = useState<{ test: PaperPosition[]; live: PaperPosition[] }>({ test: [], live: [] });
  const [pnlByMode, setPnlByMode] = useState<Record<string, { open: number; closed: number; total: number }>>({
    test: { open: 0, closed: 0, total: 0 },
    live: { open: 0, closed: 0, total: 0 },
  });
  const [autoTrade, setAutoTradeState] = useState<boolean>(true);
  const [togglingAuto, setTogglingAuto] = useState(false);
  const [entering, setEntering] = useState<string | null>(null);
  const [exiting, setExiting] = useState<number | null>(null);
  const [exitingBrokerOrder, setExitingBrokerOrder] = useState<string | null>(null);
  const [enterError, setEnterError] = useState<string | null>(null);
  const [tickCacheAge, setTickCacheAge] = useState<number | null>(null);
  const [livePrices, setLivePrices] = useState<Record<string, { price: number; ts: string }>>({});
  const [sseConnected, setSseConnected] = useState(false);
  const esRef = useRef<EventSource | null>(null);

  // Broker Status removed for brevity / merged into general UI flow
  useEffect(() => {
    const es = new EventSource(SSE_STREAM_URL);
    esRef.current = es;

    es.onopen = () => setSseConnected(true);
    es.onerror = () => setSseConnected(false);

    es.onmessage = (event) => {
      try {
        const d: StreamPayload = JSON.parse(event.data);
        if (d.state) {
          setState(prev => ({
            ...(prev ?? {} as LiveState),
            ...d.state,
          } as LiveState));
          setLastUpdate(new Date().toLocaleTimeString("en-IN"));
          if (d.state.auto_trade_enabled !== undefined) {
            setAutoTradeState(d.state.auto_trade_enabled);
          }
        }
        if (d.positions_by_mode) setPositionsByMode(d.positions_by_mode);
        if (d.tick_cache) setLivePrices(d.tick_cache);
        if (d.tick_cache_age !== undefined) setTickCacheAge(d.tick_cache_age);
        setPnlByMode({
          test: { open: d.total_open_pnl_test ?? 0, closed: d.total_closed_pnl_test ?? 0, total: d.total_pnl_test ?? 0 },
          live: { open: d.total_open_pnl_live ?? 0, closed: d.total_closed_pnl_live ?? 0, total: d.total_pnl_live ?? 0 },
        });
      } catch {}
    };

    fetchJSON<LiveState>("/system/state").then(d => { if (d) setState(d); }).catch(() => {});
    fetchJSON<{auto_trade: boolean}>("/auto_trade").then(d => { if (d) setAutoTradeState(d.auto_trade); }).catch(() => {});

    return () => { es.close(); esRef.current = null; };
  }, []);

  const positions = positionsByMode[mode] ?? [];
  const totalOpenPnl = pnlByMode[mode]?.open ?? 0;
  const totalClosedPnl = pnlByMode[mode]?.closed ?? 0;
  const totalPnl = pnlByMode[mode]?.total ?? 0;

  const triggerScan = async () => {
    setScanning(true);
    try { await postJSON("/scan"); }
    finally { setScanning(false); }
  };

  const handleToggleAutoTrade = async () => {
    setTogglingAuto(true);
    try {
      const res = await setAutoTrade(!autoTrade);
      setAutoTradeState(res.auto_trade_enabled);
    } finally {
      setTogglingAuto(false);
    }
  };

  const handleEnter = async (t: TradeSuggestion) => {
    setEntering(t.symbol + t.direction);
    setEnterError(null);
    try {
      await enterPaperTrade(t, mode);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setEnterError(msg);
    } finally {
      setEntering(null);
    }
  };

  const handleExit = async (id: number) => {
    setExiting(id);
    try { await exitPaperTrade(id, mode); }
    finally { setExiting(null); }
  };

  const handleBrokerExit = async (orderId: string) => {
    setExitingBrokerOrder(orderId);
    try {
      await postJSON("/trade/exit", { ticket: orderId });
    } finally {
      setExitingBrokerOrder(null);
    }
  };

  const handleClear = async () => {
    await clearClosedPositions(mode);
  };

  const openPositions = positions.filter(p => p.status === "OPEN");
  const closedPositions = positions.filter(p => p.status === "CLOSED");

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 p-5 overflow-y-auto">

        {/* System Alerts */}
        {state?.alerts && state.alerts.length > 0 && (
          <div className="mb-5 space-y-2">
            {state.alerts.map((alert, idx) => (
              <div 
                key={idx} 
                className="flex items-center justify-between p-3 border-l-2" 
                style={{
                  background: '#15191e', 
                  borderLeftColor: alert.level === "CRITICAL" ? '#ff3e3e' : alert.level === "WARNING" ? '#e8c300' : '#4da6ff',
                  color: '#e4e8ec'
                }}
              >
                <div>
                  <h4 className="text-[11px] font-bold uppercase tracking-wider mb-1" style={{ color: alert.level === "CRITICAL" ? '#ff3e3e' : alert.level === "WARNING" ? '#e8c300' : '#4da6ff' }}>
                    {alert.level}: {alert.title}
                  </h4>
                  <p className="text-[10px]" style={{ color: '#8a93a2' }}>{alert.message}</p>
                </div>
                <span className="text-[9px]" style={{ color: '#3d4450' }}>{alert.time}</span>
              </div>
            ))}
          </div>
        )}

        {/* Header */}
        <div className="flex items-center justify-between mb-5">
          <div>
            <h1 className="text-sm font-bold uppercase tracking-wider" style={{ color: '#e8c300' }}>Live Trading</h1>
            <p className="text-[10px] mt-0.5" style={{ color: mode === "live" ? '#ff3e3e' : '#3d4450' }}>
              {mode === "live" ? "LIVE MODE — REAL MT5 EXECUTIONS" : "DEMO MODE — SIMULATED ORDERS"}
            </p>
          </div>
          <div className="flex items-center gap-3">
            <span className="flex items-center gap-1.5 text-[10px]" style={{ color: sseConnected ? '#00e87b' : '#ff3e3e' }}>
              <Radio className="w-3 h-3" />
              {sseConnected ? "STREAM LIVE" : "RECONNECTING..."}
            </span>
            <span className="text-[10px]" style={{ color: '#3d4450' }}>{lastUpdate}</span>

            <button
              onClick={handleToggleAutoTrade}
              disabled={togglingAuto}
              className="flex items-center gap-1.5 text-[10px] font-semibold px-3 py-1.5 disabled:opacity-50 transition-all"
              style={{
                border: `1px solid ${autoTrade ? '#00e87b' : '#4da6ff'}`,
                color: autoTrade ? '#00e87b' : '#4da6ff',
                background: autoTrade ? '#0a2a18' : '#0a1a2a',
                letterSpacing: '0.08em',
              }}
            >
              {autoTrade ? <Bot className="w-3 h-3" /> : <Hand className="w-3 h-3" />}
              {autoTrade ? "AUTO" : "MANUAL"}
            </button>

            <button onClick={triggerScan} disabled={scanning} className="t-btn flex items-center gap-1.5 disabled:opacity-50 border-[#4da6ff] text-[#4da6ff]">
              {scanning ? <RefreshCw className="w-3 h-3 animate-spin" /> : <Zap className="w-3 h-3" />}
              {scanning ? "SCANNING..." : "SCAN NOW"}
            </button>
          </div>
        </div>

        {/* Status bar */}
        <div className="t-panel p-4 mb-4">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div>
              <p className="text-[9px] uppercase tracking-[1.5px] mb-1" style={{ color: '#5a6270' }}>System Status</p>
              <div className="flex items-center gap-2">
                <span className={`w-[6px] h-[6px] ${state?.status === "scanning" ? "bg-[#e8c300] t-pulse" : "bg-[#4da6ff]"}`} />
                <span className="font-semibold text-sm uppercase">{state?.status ?? "..."}</span>
              </div>
            </div>
            <div>
              <p className="text-[9px] uppercase tracking-[1.5px] mb-1" style={{ color: '#5a6270' }}>
                XAUUSD <span style={{ color: '#3d4450' }}>· SPOT</span>
              </p>
              <p className="text-xl font-bold" style={{ color: '#e8c300' }}>
                ${state?.last_price ? state.last_price.toFixed(2) : "--"}
              </p>
            </div>
            <div>
              <p className="text-[9px] uppercase tracking-[1.5px] mb-1" style={{ color: '#5a6270' }}>Market Regime</p>
              <p className="text-xl font-bold" style={{
                color: state?.regime?.includes("BULL") ? '#00e87b' : state?.regime?.includes("BEAR") ? '#ff3e3e' : '#4da6ff'
              }}>
                {state?.regime?.replace(/_/g, " ") ?? "--"}
              </p>
            </div>
            <div>
              <p className="text-[9px] uppercase tracking-[1.5px] mb-1" style={{ color: '#5a6270' }}>Session P&L</p>
              <p className="text-xl font-bold" style={{ color: totalPnl >= 0 ? '#00e87b' : '#ff3e3e' }}>
                ${totalPnl.toFixed(2)}
              </p>
            </div>
          </div>
        </div>

        {/* Open Positions */}
        {openPositions.length > 0 && (
          <div className="t-panel p-4 mb-4" style={{ borderColor: '#1a3a1a' }}>
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: '#00e87b' }}>
                Open Positions ({openPositions.length})
              </h3>
              <span className="text-xs font-bold" style={{ color: totalOpenPnl >= 0 ? '#00e87b' : '#ff3e3e' }}>
                Unrealised: ${totalOpenPnl.toFixed(2)}
              </span>
            </div>
            <div className="overflow-x-auto">
              <table>
                <thead>
                  <tr>
                    {["Time", "Session", "Dir", "Entry $", "Current $", "SL $", "Target $", "Lots", "Unrealised P&L", ""].map(h => (
                      <th key={h}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {openPositions.map(p => {
                    const tickEntry = livePrices[p.symbol];
                    const currentPrice = (tickEntry && tickCacheAge !== null && tickCacheAge < 30)
                      ? tickEntry.price
                      : p.current_price;
                    const unrealisedPnl = p.direction === "BUY" ? (currentPrice - p.entry_price) * p.lot_size * 100 : (p.entry_price - currentPrice) * p.lot_size * 100;
                    const pnlColor = unrealisedPnl >= 0 ? '#00e87b' : '#ff3e3e';
                    return (
                      <tr key={p.id}>
                        <td style={{ color: '#5a6270' }}>{p.entry_time}</td>
                        <td style={{ color: '#c8cdd5', fontWeight: 600 }}>{p.session}</td>
                        <td><Badge label={p.direction} variant={p.direction === "BUY" ? "green" : "red"} /></td>
                        <td>${p.entry_price.toFixed(2)}</td>
                        <td style={{ color: pnlColor, fontWeight: 600 }}>${currentPrice.toFixed(2)}</td>
                        <td style={{ color: '#ff3e3e' }}>${p.sl.toFixed(2)}</td>
                        <td style={{ color: '#00e87b' }}>${p.target.toFixed(2)}</td>
                        <td style={{ color: '#c8cdd5', fontWeight: 600 }}>{p.lot_size}</td>
                        <td style={{ color: pnlColor, fontWeight: 700 }}>
                          {unrealisedPnl >= 0 ? "+" : ""}${unrealisedPnl.toFixed(2)}
                        </td>
                        <td>
                          <button
                            onClick={() => mode === "live" && p.order_id ? handleBrokerExit(p.order_id) : handleExit(p.id)}
                            disabled={exiting === p.id || exitingBrokerOrder === p.order_id}
                            className="t-btn flex items-center gap-1 text-[10px] disabled:opacity-50"
                            style={{ borderColor: '#ff3e3e', color: '#ff3e3e', padding: '2px 8px' }}
                          >
                            <X className="w-3 h-3" />
                            {(exiting === p.id || exitingBrokerOrder === p.order_id) ? "..." : "EXIT"}
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Signal feed */}
        <div className="t-panel p-4 mb-4">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: '#5a6270' }}>Trade Suggestions</h3>
            <span className="text-[10px]" style={{ color: '#3d4450' }}>1 MINUTE BARS · XAU/USD ONLY</span>
          </div>
          {enterError && (
            <div className="mb-3 px-3 py-2 text-[11px]" style={{ background: '#2a0a0a', border: '1px solid #5c1a1a', color: '#ff3e3e' }}>
              ⚠ {enterError}
            </div>
          )}
          {!state?.trade_suggestions?.length ? (
            <div className="py-10 text-center">
              <p className="text-[11px]" style={{ color: '#3d4450' }}>NO SIGNALS YET</p>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table>
                <thead>
                  <tr>
                    {["Time", "Session", "Dir", "Strategy", "Entry ATR", "Risk", "Entry $", "SL $", "Target $", "Lots", "Score", "Regime", ""].map(h => (
                      <th key={h}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {[...state.trade_suggestions].reverse().map((t, i) => {
                    const key = t.symbol + t.direction;
                    const isEntering = entering === key;
                    return (
                      <tr key={i}>
                        <td style={{ color: '#5a6270' }}>{t.time}</td>
                        <td style={{ color: '#c8cdd5', fontWeight: 600 }}>{t.session}</td>
                        <td><Badge label={t.direction} variant={t.direction === "BUY" ? "green" : "red"} /></td>
                        <td style={{ color: '#5a6270' }}>{t.strategy?.replace(/_/g, " ")}</td>
                        <td style={{ color: '#5a6270' }}>${t.entry_atr?.toFixed(2) ?? "--"}</td>
                        <td>
                          <span style={{
                            fontSize: '10px', fontWeight: 700, padding: '2px 6px',
                            borderRadius: '3px', letterSpacing: '0.05em',
                            background: t.risk_label === 'LOW' ? '#0d2b1a' : t.risk_label === 'MEDIUM' ? '#2a2000' : '#2a0a0a',
                            color: t.risk_label === 'LOW' ? '#00e87b' : t.risk_label === 'MEDIUM' ? '#e8c300' : '#ff3e3e',
                            border: `1px solid ${t.risk_label === 'LOW' ? '#00e87b33' : t.risk_label === 'MEDIUM' ? '#e8c30033' : '#ff3e3e33'}`,
                          }}>
                            {t.risk_label ?? '--'}
                          </span>
                        </td>
                        <td>{t.entry_price ? `$${t.entry_price.toFixed(2)}` : "--"}</td>
                        <td style={{ color: '#ff3e3e' }}>{t.sl_price ? `$${t.sl_price.toFixed(2)}` : "--"}</td>
                        <td style={{ color: '#00e87b' }}>{t.target_price ? `$${t.target_price.toFixed(2)}` : "--"}</td>
                        <td style={{ color: '#c8cdd5', fontWeight: 600 }}>{t.lots ?? "--"}</td>
                        <td>
                          <div className="flex items-center gap-2">
                            <div className="h-1 w-14 overflow-hidden" style={{ background: '#1e222c' }}>
                              <div className="h-full" style={{ width: `${t.final_score * 100}%`, background: '#e8c300' }} />
                            </div>
                            <span className="text-[10px]">{(t.final_score * 100).toFixed(0)}%</span>
                          </div>
                        </td>
                        <td style={{ color: '#5a6270' }}>{t.regime}</td>
                        <td>
                          {autoTrade ? (
                            <span
                              className="flex items-center gap-1 text-[9px] font-semibold px-2 py-1"
                              style={{ color: '#00e87b', background: '#0a2a18', border: '1px solid #00e87b33' }}
                            >
                              <Bot className="w-2.5 h-2.5" /> AUTO
                            </span>
                          ) : (
                            <button
                              onClick={() => handleEnter(t)}
                              disabled={isEntering}
                              className="t-btn flex items-center gap-1 text-[10px] disabled:opacity-50"
                              style={{ borderColor: '#e8c300', color: '#e8c300', padding: '2px 8px', whiteSpace: 'nowrap' }}
                            >
                              {t.direction === "BUY"
                                ? <TrendingUp className="w-3 h-3" />
                                : <TrendingDown className="w-3 h-3" />}
                              {isEntering ? "..." : "ENTER"}
                            </button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Closed Positions */}
        {closedPositions.length > 0 && (
          <div className="t-panel p-4">
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: '#5a6270' }}>
                Closed Positions ({closedPositions.length})
              </h3>
              <div className="flex items-center gap-3">
                <span className="text-xs font-bold" style={{ color: totalClosedPnl >= 0 ? '#00e87b' : '#ff3e3e' }}>
                  Realised: ${totalClosedPnl.toFixed(2)}
                </span>
                <button onClick={handleClear} className="t-btn flex items-center gap-1 text-[10px]" style={{ padding: '2px 8px' }}>
                  <Trash2 className="w-3 h-3" /> CLEAR
                </button>
              </div>
            </div>
            <div className="overflow-x-auto">
              <table>
                <thead>
                  <tr>
                    {["Entry", "Exit", "Session", "Dir", "Entry $", "Exit $", "Realised P&L", "Reason"].map(h => (
                      <th key={h}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {[...closedPositions].reverse().map(p => {
                    const pnlColor = (p.realised_pnl ?? 0) >= 0 ? '#00e87b' : '#ff3e3e';
                    const reasonColor = p.exit_reason === "TARGET_HIT" ? '#00e87b'
                      : p.exit_reason === "SL_HIT" ? '#ff3e3e'
                      : '#e8c300';
                    return (
                      <tr key={p.id}>
                        <td style={{ color: '#5a6270' }}>{p.entry_time}</td>
                        <td style={{ color: '#5a6270' }}>{p.exit_time}</td>
                        <td style={{ fontWeight: 600 }}>{p.session}</td>
                        <td><Badge label={p.direction} variant={p.direction === "BUY" ? "green" : "red"} /></td>
                        <td>${p.entry_price.toFixed(2)}</td>
                        <td>${p.exit_price?.toFixed(2) ?? "--"}</td>
                        <td style={{ color: pnlColor, fontWeight: 700 }}>
                          {(p.realised_pnl ?? 0) >= 0 ? "+" : ""}${(p.realised_pnl ?? 0).toFixed(2)}
                        </td>
                        <td style={{ color: reasonColor, fontSize: '10px', fontWeight: 600 }}>
                          {p.exit_reason?.replace(/_/g, " ")}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

      </main>
    </div>
  );
}
