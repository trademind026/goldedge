"use client";

import { useEffect, useState, useRef, useCallback, useMemo } from "react";
import Sidebar from "@/components/Sidebar";
import {
  apiGetChartData,
  type ForexChartResponse,
  type ChartBar,
} from "@/lib/api";
import {
  ResponsiveContainer,
  ComposedChart,
  Line,
  Bar,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ReferenceLine,
  Cell,
} from "recharts";
import {
  Activity,
  TrendingUp,
  TrendingDown,
  Clock,
  AlertTriangle,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";

/* ── Constants & Formats ──────────────────────────────────────────────── */

const PAIRS = [
  { id: "EURUSD", label: "EUR/USD", pip: 0.0001 },
  { id: "GBPUSD", label: "GBP/USD", pip: 0.0001 },
  { id: "USDJPY", label: "USD/JPY", pip: 0.01 },
  { id: "AUDUSD", label: "AUD/USD", pip: 0.0001 },
  { id: "USDCAD", label: "USD/CAD", pip: 0.0001 },
  { id: "XAUUSD", label: "XAU/USD (GOLD)", pip: 0.10 },
];

const TIMEFRAMES = [
  { id: "1m", label: "M1" },
  { id: "5m", label: "M5" },
  { id: "15m", label: "M15" },
  { id: "1h", label: "H1" },
  { id: "4h", label: "H4" },
  { id: "1d", label: "D1" },
];

const TIP_STYLE = {
  background: "#10141d",
  border: "1px solid #1e2430",
  borderRadius: "2px",
  fontSize: "11px",
  fontFamily: "var(--font-mono, monospace)",
  color: "#e4e8ec",
  padding: "8px 12px",
  boxShadow: "0 8px 24px rgba(0,0,0,0.6)",
};

const AXIS_STYLE = {
  fill: "#5a6270",
  fontSize: 10,
  fontFamily: "var(--font-mono, monospace)",
};

/* ── Custom Candlestick Shape for Recharts ────────────────────────────── */

interface CandlestickProps {
  x?: number;
  width?: number;
  y?: number;
  height?: number;
  payload?: any;
  yAxis?: any;
}

const CandlestickShape = (props: CandlestickProps) => {
  const { x = 0, width = 6, payload, yAxis } = props;
  if (!payload || !yAxis) return null;

  const { open, close, high, low } = payload;
  const isBull = close >= open;
  const strokeColor = isBull ? "#00e87b" : "#ff3e3e";
  const fillColor = isBull ? "#00e87b" : "#ff3e3e";

  const scale = yAxis.scale;
  if (!scale) return null;

  const yOpen = scale(open);
  const yClose = scale(close);
  const yHigh = scale(high);
  const yLow = scale(low);

  const candleTop = Math.min(yOpen, yClose);
  const candleBottom = Math.max(yOpen, yClose);
  const bodyHeight = Math.max(candleBottom - candleTop, 1.5);
  const centerX = x + width / 2;

  return (
    <g>
      {/* Upper and Lower Wick */}
      <line
        x1={centerX}
        y1={yHigh}
        x2={centerX}
        y2={yLow}
        stroke={strokeColor}
        strokeWidth={1.2}
      />
      {/* Real Body */}
      <rect
        x={x}
        y={candleTop}
        width={width}
        height={bodyHeight}
        fill={fillColor}
        stroke={strokeColor}
        strokeWidth={1}
        rx={0.5}
      />
    </g>
  );
};

/* ── Main Component ───────────────────────────────────────────────────── */

export default function ChartsPage() {
  const [selectedPair, setSelectedPair] = useState("EURUSD");
  const [selectedTf, setSelectedTf] = useState("1m");
  const [mobileSubTab, setMobileSubTab] = useState<"velocity" | "entropy">("velocity");
  const [chartData, setChartData] = useState<ForexChartResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(true);

  const pollIntervalRef = useRef<NodeJS.Timeout | null>(null);

  // Fetch chart data
  const loadData = useCallback(async (isPolling = false) => {
    if (!isPolling) setLoading(true);
    try {
      const resp = await apiGetChartData(selectedPair, selectedTf, 150);
      setChartData(resp);
      setError(null);
    } catch (err: any) {
      console.error("Charts fetch error:", err);
      if (!isPolling) setError(err.message || "Failed to load chart telemetry");
    } finally {
      if (!isPolling) setLoading(false);
    }
  }, [selectedPair, selectedTf]);

  useEffect(() => {
    loadData(false);
  }, [loadData]);

  // 2s Auto-Refresh Interval
  useEffect(() => {
    if (!autoRefresh) {
      if (pollIntervalRef.current) clearInterval(pollIntervalRef.current);
      return;
    }

    pollIntervalRef.current = setInterval(() => {
      loadData(true);
    }, 2000);

    return () => {
      if (pollIntervalRef.current) clearInterval(pollIntervalRef.current);
    };
  }, [autoRefresh, loadData]);

  // Formatted Bars for Charting
  const formattedBars = useMemo(() => {
    if (!chartData || !chartData.bars) return [];
    return chartData.bars.map((b) => {
      const dt = new Date(b.timestamp);
      const timeStr = dt.toLocaleTimeString("en-US", {
        timeZone: "UTC",
        hour12: false,
        hour: "2-digit",
        minute: "2-digit",
      });
      return {
        ...b,
        timeStr,
        candleRange: [b.low, b.high],
      };
    });
  }, [chartData]);

  // Price Domain calculation
  const priceDomain = useMemo(() => {
    if (!formattedBars.length) return ["auto", "auto"];
    let minP = Infinity;
    let maxP = -Infinity;
    for (const b of formattedBars) {
      if (b.low < minP) minP = b.low;
      if (b.high > maxP) maxP = b.high;
    }
    const padding = (maxP - minP) * 0.08 || 0.0005;
    return [minP - padding, maxP + padding];
  }, [formattedBars]);

  // Quote and Spread Analysis
  const currentQuote = chartData?.current_quote;
  const spreadPips = currentQuote?.spread_pips ?? 0;
  const spreadStatus = spreadPips > 3.5 ? "critical" : spreadPips > 2.0 ? "warning" : "normal";

  const sessionStatus = chartData?.statistics?.session_status ?? "LONDON";
  const currentRegime = chartData?.statistics?.current_regime ?? "SIDEWAYS";
  const currentVelocity = chartData?.statistics?.current_kalman_velocity ?? 0;
  const currentEntropy = chartData?.statistics?.current_entropy ?? 1.5;
  const isRollover = sessionStatus === "ROLLOVER";

  const digits = selectedPair.includes("JPY") || selectedPair.includes("XAU") ? 2 : 5;

  return (
    <div
      className="flex min-h-screen"
      style={{
        background: "#0a0d13",
        color: "#e4e8ec",
        fontFamily: "var(--font-mono, monospace)",
      }}
    >
      <Sidebar />

      <main className="flex-1 flex flex-col h-screen overflow-hidden">
        {/* ── 1. Interactive Control Header ──────────────────────────── */}
        <header
          className="border-b shrink-0 px-4 py-3 flex flex-col gap-3"
          style={{ background: "#0e1219", borderColor: "#1a202c" }}
        >
          {/* Top Line: Pair Selector & Live Telemetry HUD */}
          <div className="flex flex-wrap items-center justify-between gap-3">
            {/* Currency Pair Segmented Controls */}
            <div className="flex items-center gap-1.5 overflow-x-auto py-1 scrollbar-none">
              {PAIRS.map((p) => {
                const active = p.id === selectedPair;
                return (
                  <button
                    key={p.id}
                    onClick={() => setSelectedPair(p.id)}
                    className="px-3 py-1.5 text-xs font-bold rounded transition-all shrink-0 cursor-pointer"
                    style={{
                      background: active ? "#00e87b" : "#141922",
                      color: active ? "#000000" : "#8a94a6",
                      border: active ? "1px solid #00e87b" : "1px solid #1f2735",
                      boxShadow: active ? "0 0 10px rgba(0,232,123,0.3)" : "none",
                    }}
                  >
                    {p.label}
                  </button>
                );
              })}
            </div>

            {/* Live Spread & Telemetry HUD */}
            <div className="flex items-center gap-3 ml-auto text-xs">
              {/* Spread Indicator */}
              <div
                className="px-2.5 py-1 rounded border flex items-center gap-1.5"
                style={{
                  background:
                    spreadStatus === "critical"
                      ? "rgba(255,62,62,0.15)"
                      : spreadStatus === "warning"
                      ? "rgba(232,195,0,0.15)"
                      : "rgba(0,232,123,0.12)",
                  borderColor:
                    spreadStatus === "critical"
                      ? "#ff3e3e"
                      : spreadStatus === "warning"
                      ? "#e8c300"
                      : "#00e87b",
                  color:
                    spreadStatus === "critical"
                      ? "#ff3e3e"
                      : spreadStatus === "warning"
                      ? "#e8c300"
                      : "#00e87b",
                }}
              >
                <span className="w-1.5 h-1.5 rounded-full animate-ping" style={{ background: "currentColor" }} />
                <span>SPREAD:</span>
                <strong>{spreadPips.toFixed(1)} PIPS</strong>
              </div>

              {/* Bid / Ask HUD */}
              <div className="flex items-center gap-2 px-2.5 py-1 rounded bg-[#141922] border border-[#1f2735] text-[11px]">
                <span className="text-[#8a94a6]">BID:</span>
                <strong className="text-[#ff5252]">{currentQuote?.bid.toFixed(digits) ?? "--"}</strong>
                <span className="text-[#3d4554]">/</span>
                <span className="text-[#8a94a6]">ASK:</span>
                <strong className="text-[#00e87b]">{currentQuote?.ask.toFixed(digits) ?? "--"}</strong>
              </div>

              {/* Auto Refresh Toggle */}
              <button
                onClick={() => setAutoRefresh(!autoRefresh)}
                className="p-1.5 rounded bg-[#141922] border border-[#1f2735] text-[#8a94a6] hover:text-white transition-colors"
                title={autoRefresh ? "Pause auto-refresh (2s)" : "Resume auto-refresh (2s)"}
              >
                <RefreshCw className={`w-3.5 h-3.5 ${autoRefresh ? "text-[#00e87b] animate-spin" : "text-[#5a6270]"}`} style={{ animationDuration: "6s" }} />
              </button>
            </div>
          </div>

          {/* Bottom Line: Timeframes & Session / Regime Status */}
          <div className="flex flex-wrap items-center justify-between gap-3 text-xs border-t border-[#1a202c] pt-2">
            {/* Timeframe Selector */}
            <div className="flex items-center gap-1">
              <span className="text-[10px] text-[#5a6270] mr-1 uppercase">TF:</span>
              {TIMEFRAMES.map((tf) => {
                const active = tf.id === selectedTf;
                return (
                  <button
                    key={tf.id}
                    onClick={() => setSelectedTf(tf.id)}
                    className="px-2.5 py-1 text-[11px] font-bold rounded transition-all cursor-pointer"
                    style={{
                      background: active ? "#1e293b" : "transparent",
                      color: active ? "#00e5ff" : "#5a6270",
                      border: active ? "1px solid #00e5ff66" : "1px solid transparent",
                    }}
                  >
                    {tf.label}
                  </button>
                );
              })}
            </div>

            {/* Global Session & Regime HUD */}
            <div className="flex items-center gap-2.5 flex-wrap">
              {/* Session Tag */}
              <div
                className="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider flex items-center gap-1"
                style={{
                  background: isRollover
                    ? "rgba(255,62,62,0.15)"
                    : sessionStatus.includes("OVERLAP")
                    ? "rgba(232,195,0,0.15)"
                    : "rgba(77,166,255,0.12)",
                  color: isRollover
                    ? "#ff3e3e"
                    : sessionStatus.includes("OVERLAP")
                    ? "#e8c300"
                    : "#4da6ff",
                  border: isRollover
                    ? "1px solid #ff3e3e55"
                    : sessionStatus.includes("OVERLAP")
                    ? "1px solid #e8c30055"
                    : "1px solid #4da6ff55",
                }}
              >
                <Clock className="w-3 h-3" />
                SESSION: {sessionStatus.replace(/_/g, " ")}
              </div>

              {/* Regime Tag */}
              <div
                className="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider flex items-center gap-1"
                style={{
                  background:
                    currentRegime === "TRENDING_BULL"
                      ? "rgba(0,232,123,0.12)"
                      : currentRegime === "TRENDING_BEAR"
                      ? "rgba(255,62,62,0.12)"
                      : currentRegime === "HIGH_VOLATILITY"
                      ? "rgba(232,195,0,0.15)"
                      : "rgba(138,148,166,0.12)",
                  color:
                    currentRegime === "TRENDING_BULL"
                      ? "#00e87b"
                      : currentRegime === "TRENDING_BEAR"
                      ? "#ff3e3e"
                      : currentRegime === "HIGH_VOLATILITY"
                      ? "#e8c300"
                      : "#8a94a6",
                }}
              >
                {currentRegime === "TRENDING_BULL" ? (
                  <TrendingUp className="w-3 h-3" />
                ) : currentRegime === "TRENDING_BEAR" ? (
                  <TrendingDown className="w-3 h-3" />
                ) : (
                  <Activity className="w-3 h-3" />
                )}
                REGIME: {currentRegime.replace(/_/g, " ")}
              </div>

              {/* Kalman Velocity & Entropy Telemetry */}
              <div className="hidden lg:flex items-center gap-2 text-[10px] text-[#8a94a6]">
                <span>
                  VEL Z:{" "}
                  <strong
                    style={{
                      color:
                        currentVelocity > 0.15
                          ? "#00e87b"
                          : currentVelocity < -0.15
                          ? "#ff3e3e"
                          : "#8a94a6",
                    }}
                  >
                    {currentVelocity > 0 ? `+${currentVelocity}` : currentVelocity}
                  </strong>
                </span>
                <span>|</span>
                <span>
                  ENTROPY:{" "}
                  <strong
                    style={{
                      color:
                        currentEntropy < 1.8
                          ? "#00e87b"
                          : currentEntropy > 2.3
                          ? "#ff3e3e"
                          : "#e8c300",
                    }}
                  >
                    {currentEntropy.toFixed(2)}
                  </strong>
                </span>
                <span>|</span>
                <span>
                  ATR: <strong>{chartData?.statistics?.volatility_atr_pips ?? 0} pips</strong>
                </span>
              </div>
            </div>
          </div>
        </header>

        {/* ── 2. Error Banner ────────────────────────────────────────── */}
        {error && (
          <div className="p-3 bg-[#2a1215] border-l-4 border-[#ff3e3e] text-[#ff8080] text-xs flex items-center justify-between shrink-0">
            <div className="flex items-center gap-2">
              <AlertTriangle className="w-4 h-4 text-[#ff3e3e]" />
              <span>{error}</span>
            </div>
            <button
              onClick={() => loadData(false)}
              className="px-2 py-0.5 bg-[#ff3e3e22] text-[#ff3e3e] rounded border border-[#ff3e3e44] hover:bg-[#ff3e3e44]"
            >
              Retry
            </button>
          </div>
        )}

        {/* ── 3. Rollover Warning Banner (21:00 UTC) ─────────────────── */}
        {isRollover && (
          <div className="p-2.5 bg-[#2a2000] border-l-4 border-[#e8c300] text-[#e8c300] text-[11px] flex items-center justify-between shrink-0">
            <div className="flex items-center gap-2">
              <ShieldAlert className="w-4 h-4 text-[#e8c300] animate-pulse" />
              <span>
                <strong>ROLLOVER SPREAD FREEZE (21:00 - 22:00 UTC):</strong> Liquidity rollover widening in effect across global banking hubs.
              </span>
            </div>
            <span className="text-[9px] uppercase px-2 py-0.5 bg-[#e8c30033] rounded font-bold">WIDE SPREADS EXPECTED</span>
          </div>
        )}

        {/* ── 4. Chart Canvas Body ───────────────────────────────────── */}
        <div className="flex-1 flex flex-col min-h-0 overflow-hidden p-3 gap-3 bg-[#0a0d13]">
          {loading && !chartData ? (
            <div className="flex-1 flex flex-col items-center justify-center gap-3 text-[#5a6270]">
              <RefreshCw className="w-6 h-6 animate-spin text-[#00e5ff]" />
              <span className="text-xs tracking-wider uppercase">Loading 2D Kinematic Kalman Telemetry...</span>
            </div>
          ) : (
            <>
              {/* PRIMARY CANDLESTICK & KALMAN OVERLAY CHART */}
              <div
                className="flex-1 min-h-[220px] rounded border relative flex flex-col overflow-hidden"
                style={{ background: "#0e1219", borderColor: "#1a202c" }}
              >
                {/* Chart Header Legend */}
                <div className="px-3 py-1.5 border-b border-[#1a202c] flex items-center justify-between text-[10px] text-[#5a6270] bg-[#0c1016]">
                  <div className="flex items-center gap-3">
                    <span className="font-bold text-[#e4e8ec] uppercase">
                      {selectedPair} [{selectedTf.toUpperCase()}]
                    </span>
                    <span className="flex items-center gap-1">
                      <span className="w-2.5 h-0.5 bg-[#00e5ff]" />
                      <strong className="text-[#00e5ff]">2D KALMAN FILTER ($x_1$)</strong>
                    </span>
                    <span className="hidden sm:flex items-center gap-1">
                      <span className="w-2.5 h-0.5 bg-[#ffb700]" />
                      <span className="text-[#ffb700]">EMA 20</span>
                    </span>
                    <span className="hidden sm:flex items-center gap-1">
                      <span className="w-2.5 h-0.5 bg-[#a855f7]" />
                      <span className="text-[#a855f7]">EMA 50</span>
                    </span>
                  </div>

                  <div className="flex items-center gap-2">
                    <span className="hidden md:inline text-[9px] text-[#3d4554]">
                      SHADING: GOLD = LONDON/NY OVERLAP | BLUE = LONDON | RED = ROLLOVER
                    </span>
                  </div>
                </div>

                {/* Primary Chart Viewport */}
                <div className="flex-1 w-full h-full p-1">
                  <ResponsiveContainer width="100%" height="100%">
                    <ComposedChart data={formattedBars} margin={{ top: 10, right: 15, left: 0, bottom: 0 }}>
                      <CartesianGrid strokeDasharray="2 2" stroke="#161c26" vertical={false} />
                      <XAxis dataKey="timeStr" tick={AXIS_STYLE} minTickGap={30} tickLine={false} axisLine={{ stroke: "#1a202c" }} />
                      <YAxis
                        yAxisId="price"
                        domain={priceDomain}
                        tick={AXIS_STYLE}
                        orientation="right"
                        width={65}
                        tickFormatter={(v) => v.toFixed(digits)}
                        tickLine={false}
                        axisLine={{ stroke: "#1a202c" }}
                      />

                      <Tooltip
                        contentStyle={TIP_STYLE}
                        formatter={(val: any, name: any) => {
                          if (name === "close") return [Number(val).toFixed(digits), "CLOSE"];
                          if (name === "kalman_price") return [Number(val).toFixed(digits), "KALMAN"];
                          if (name === "ema20") return [Number(val).toFixed(digits), "EMA 20"];
                          if (name === "ema50") return [Number(val).toFixed(digits), "EMA 50"];
                          return [val, name];
                        }}
                        labelFormatter={(lbl, items) => {
                          const item = items?.[0]?.payload;
                          if (!item) return lbl;
                          return `${lbl} UTC | O: ${item.open.toFixed(digits)} H: ${item.high.toFixed(digits)} L: ${item.low.toFixed(digits)} C: ${item.close.toFixed(digits)}`;
                        }}
                      />

                      {/* Custom Candlestick Bars */}
                      <Bar
                        yAxisId="price"
                        dataKey="close"
                        shape={<CandlestickShape />}
                        isAnimationActive={false}
                      />

                      {/* Neon Cyan 2D Kinematic Kalman Line (Price State x_1) */}
                      <Line
                        yAxisId="price"
                        type="monotone"
                        dataKey="kalman_price"
                        stroke="#00e5ff"
                        strokeWidth={2}
                        dot={false}
                        isAnimationActive={false}
                        name="kalman_price"
                      />

                      {/* Dynamic EMA 20 & EMA 50 Lines */}
                      <Line
                        yAxisId="price"
                        type="monotone"
                        dataKey="ema20"
                        stroke="#ffb700"
                        strokeWidth={1.2}
                        strokeDasharray="3 3"
                        dot={false}
                        isAnimationActive={false}
                        name="ema20"
                      />
                      <Line
                        yAxisId="price"
                        type="monotone"
                        dataKey="ema50"
                        stroke="#a855f7"
                        strokeWidth={1.2}
                        strokeDasharray="4 4"
                        dot={false}
                        isAnimationActive={false}
                        name="ema50"
                      />
                    </ComposedChart>
                  </ResponsiveContainer>
                </div>
              </div>

              {/* ── SUB-CHARTS CONTAINER ─────────────────────────────── */}
              <div className="h-[180px] shrink-0 flex flex-col md:flex-row gap-3">
                {/* Mobile Sub-chart Tab Switcher (Visible only on <768px) */}
                <div className="md:hidden flex items-center justify-between px-2 py-1 bg-[#0e1219] rounded border border-[#1a202c]">
                  <span className="text-[10px] text-[#5a6270] uppercase">Sub-Chart Telemetry:</span>
                  <div className="flex items-center gap-1">
                    <button
                      onClick={() => setMobileSubTab("velocity")}
                      className={`px-2 py-0.5 text-[10px] font-bold rounded ${
                        mobileSubTab === "velocity" ? "bg-[#00e87b22] text-[#00e87b] border border-[#00e87b55]" : "text-[#5a6270]"
                      }`}
                    >
                      KALMAN VELOCITY
                    </button>
                    <button
                      onClick={() => setMobileSubTab("entropy")}
                      className={`px-2 py-0.5 text-[10px] font-bold rounded ${
                        mobileSubTab === "entropy" ? "bg-[#e8c30022] text-[#e8c300] border border-[#e8c30055]" : "text-[#5a6270]"
                      }`}
                    >
                      SHANNON ENTROPY
                    </button>
                  </div>
                </div>

                {/* SUB-CHART 1: 2D KALMAN PIP VELOCITY (Z-Score) */}
                <div
                  className={`flex-1 rounded border flex-col overflow-hidden ${
                    mobileSubTab === "velocity" ? "flex" : "hidden md:flex"
                  }`}
                  style={{ background: "#0e1219", borderColor: "#1a202c" }}
                >
                  <div className="px-3 py-1 border-b border-[#1a202c] flex items-center justify-between text-[10px] bg-[#0c1016]">
                    <div className="flex items-center gap-2">
                      <span className="font-bold text-[#e4e8ec]">2D KALMAN VELOCITY (Z-SCORE)</span>
                      <span className="text-[#5a6270]">|</span>
                      <span className="text-[#00e87b]">+0.15 EXHAUSTION</span>
                      <span className="text-[#5a6270]">/</span>
                      <span className="text-[#ff3e3e]">-0.15 BLEED</span>
                    </div>
                    <span className="text-[#8a94a6] font-bold">
                      CURRENT: {currentVelocity > 0 ? `+${currentVelocity}` : currentVelocity}
                    </span>
                  </div>

                  <div className="flex-1 w-full p-1">
                    <ResponsiveContainer width="100%" height="100%">
                      <ComposedChart data={formattedBars} margin={{ top: 5, right: 15, left: 0, bottom: 0 }}>
                        <CartesianGrid strokeDasharray="2 2" stroke="#161c26" vertical={false} />
                        <XAxis dataKey="timeStr" tick={AXIS_STYLE} minTickGap={30} tickLine={false} axisLine={{ stroke: "#1a202c" }} />
                        <YAxis
                          domain={[-3.0, 3.0]}
                          tick={AXIS_STYLE}
                          orientation="right"
                          width={45}
                          tickFormatter={(v) => v.toFixed(1)}
                          tickLine={false}
                          axisLine={{ stroke: "#1a202c" }}
                        />

                        {/* Center Zero Line */}
                        <ReferenceLine y={0} stroke="#3d4450" strokeWidth={1} />
                        {/* Upper Exhaustion Threshold (+0.15) */}
                        <ReferenceLine y={0.15} stroke="#00e87b" strokeDasharray="3 3" strokeWidth={1} />
                        {/* Lower Bleed Threshold (-0.15) */}
                        <ReferenceLine y={-0.15} stroke="#ff3e3e" strokeDasharray="3 3" strokeWidth={1} />

                        <Tooltip
                          contentStyle={TIP_STYLE}
                          formatter={(val: any) => [`${Number(val).toFixed(2)} σ`, "VELOCITY Z"]}
                        />

                        <Bar dataKey="kalman_velocity_z" isAnimationActive={false}>
                          {formattedBars.map((entry, index) => (
                            <Cell
                              key={`cell-${index}`}
                              fill={entry.kalman_velocity_z >= 0 ? "#00e87b" : "#ff3e3e"}
                              opacity={Math.abs(entry.kalman_velocity_z) >= 0.15 ? 0.9 : 0.4}
                            />
                          ))}
                        </Bar>
                      </ComposedChart>
                    </ResponsiveContainer>
                  </div>
                </div>

                {/* SUB-CHART 2: ROLLING SHANNON ENTROPY (CHOP VS TREND) */}
                <div
                  className={`flex-1 rounded border flex-col overflow-hidden ${
                    mobileSubTab === "entropy" ? "flex" : "hidden md:flex"
                  }`}
                  style={{ background: "#0e1219", borderColor: "#1a202c" }}
                >
                  <div className="px-3 py-1 border-b border-[#1a202c] flex items-center justify-between text-[10px] bg-[#0c1016]">
                    <div className="flex items-center gap-2">
                      <span className="font-bold text-[#e4e8ec]">SHANNON ENTROPY (CHOP METER)</span>
                      <span className="text-[#5a6270]">|</span>
                      <span className="text-[#00e87b]">&lt;1.8 TREND</span>
                      <span className="text-[#5a6270]">/</span>
                      <span className="text-[#ff3e3e]">&gt;2.3 CHOP</span>
                    </div>
                    <span
                      className="font-bold"
                      style={{
                        color: currentEntropy < 1.8 ? "#00e87b" : currentEntropy > 2.3 ? "#ff3e3e" : "#e8c300",
                      }}
                    >
                      {currentEntropy < 1.8 ? "TREND EDGE" : currentEntropy > 2.3 ? "NOISE REGIME" : "MODERATE"}
                    </span>
                  </div>

                  <div className="flex-1 w-full p-1">
                    <ResponsiveContainer width="100%" height="100%">
                      <ComposedChart data={formattedBars} margin={{ top: 5, right: 15, left: 0, bottom: 0 }}>
                        <CartesianGrid strokeDasharray="2 2" stroke="#161c26" vertical={false} />
                        <XAxis dataKey="timeStr" tick={AXIS_STYLE} minTickGap={30} tickLine={false} axisLine={{ stroke: "#1a202c" }} />
                        <YAxis
                          domain={[0.0, 3.0]}
                          tick={AXIS_STYLE}
                          orientation="right"
                          width={40}
                          tickFormatter={(v) => v.toFixed(1)}
                          tickLine={false}
                          axisLine={{ stroke: "#1a202c" }}
                        />

                        {/* Clean Trending Threshold (< 1.8) */}
                        <ReferenceLine y={1.8} stroke="#00e87b" strokeDasharray="3 3" strokeWidth={1} />
                        {/* Noise / Chop Warning Threshold (> 2.3) */}
                        <ReferenceLine y={2.3} stroke="#ff3e3e" strokeDasharray="3 3" strokeWidth={1} />

                        <Tooltip
                          contentStyle={TIP_STYLE}
                          formatter={(val: any) => [`${Number(val).toFixed(2)} bits`, "SHANNON ENTROPY"]}
                        />

                        <Area
                          type="monotone"
                          dataKey="entropy"
                          stroke="#e8c300"
                          fill="#e8c30033"
                          strokeWidth={1.5}
                          isAnimationActive={false}
                        />
                      </ComposedChart>
                    </ResponsiveContainer>
                  </div>
                </div>
              </div>
            </>
          )}
        </div>
      </main>
    </div>
  );
}