"use client";

import { Fragment, Suspense, useEffect, useState, useMemo } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
  ArrowLeft,
  Trophy,
  Shield,
  ChevronDown,
  ChevronRight,
  GitCompare,
  BarChart3,
  CalendarDays,
  Layers,
} from "lucide-react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
  ReferenceLine,
} from "recharts";
import Badge from "@/components/Badge";
import Sidebar from "@/components/Sidebar";
import {
  fetchJSON,
  normalizeWinRate,
  type BacktestHistoryDetail,
  type BacktestMetrics,
  type Trade,
} from "@/lib/api";
import {
  fmtUsd,
  fmtPrice,
  fmtWinRate,
  fmtModeLabel,
  resolveRunMetrics,
} from "@/lib/backtestHelpers";
import { toUTCTime } from "@/lib/time";

type TabId = "summary" | "daily" | "strategy";

const RUN_COLORS = ["#00e87b", "#4da6ff", "#e8c300"];

interface LoadedRun {
  detail: BacktestHistoryDetail;
  metrics: BacktestMetrics;
}

function CompareContent() {
  const searchParams = useSearchParams();
  const runsParam = searchParams.get("runs") ?? "";
  const runIds = useMemo(
    () =>
      runsParam
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean)
        .slice(0, 3),
    [runsParam]
  );

  const [runs, setRuns] = useState<LoadedRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<TabId>("summary");
  const [expandedDates, setExpandedDates] = useState<Record<string, boolean>>({});

  useEffect(() => {
    if (runIds.length === 0) {
      setLoading(false);
      setRuns([]);
      return;
    }
    let active = true;
    setLoading(true);
    setError(null);
    Promise.all(
      runIds.map(async (id) => {
        const detail = await fetchJSON<BacktestHistoryDetail>(`/backtest/history/${id}`);
        return { detail, metrics: resolveRunMetrics(detail) };
      })
    )
      .then((res) => {
        if (active) setRuns(res);
      })
      .catch((err) => {
        if (active) setError(err instanceof Error ? err.message : "Failed to load runs");
      })
      .finally(() => {
        if (active) setLoading(false);
      });

    return () => {
      active = false;
    };
  }, [runIds]);

  const overlayCurveData = useMemo(() => {
    if (runs.length === 0) return [];
    const maxLen = Math.max(...runs.map((r) => r.detail.equity_curve?.length ?? 0));
    return Array.from({ length: maxLen }, (_, idx) => {
      const pt: Record<string, number | string> = { trade: idx + 1 };
      runs.forEach((r, rIdx) => {
        const eqPt = r.detail.equity_curve?.[idx];
        if (eqPt !== undefined) {
          pt[`run_${rIdx}`] = eqPt.equity;
        }
      });
      return pt;
    });
  }, [runs]);

  const allDates = useMemo(() => {
    const s = new Set<string>();
    runs.forEach((r) => {
      Object.keys(r.metrics.daily_breakdown ?? {}).forEach((d) => {
        if (d !== "UNKNOWN") s.add(d);
      });
    });
    return Array.from(s).sort();
  }, [runs]);

  const allStrategies = useMemo(() => {
    const s = new Set<string>();
    runs.forEach((r) => {
      Object.keys(r.metrics.strategy_breakdown ?? {}).forEach((k) => s.add(k));
    });
    return Array.from(s).sort();
  }, [runs]);

  const allDirections = useMemo(() => {
    const s = new Set<string>(["BUY", "SELL"]);
    runs.forEach((r) => {
      Object.keys(r.metrics.direction_breakdown ?? {}).forEach((k) => s.add(k));
    });
    return Array.from(s);
  }, [runs]);

  const tradesByDateForRun = (trades: Trade[], date: string) =>
    trades.filter((t) => (t.entry_time || t.exit_time || "").slice(0, 10) === date);

  const toggleDate = (day: string) => {
    setExpandedDates((prev) => ({ ...prev, [day]: !prev[day] }));
  };

  if (loading) {
    return (
      <div className="p-8 text-center font-mono text-[12px] text-[#5a6270]">
        LOADING COMPARISON MATRIX...
      </div>
    );
  }

  if (error || runs.length < 2) {
    return (
      <div className="space-y-4">
        <Link
          href="/backtest"
          className="t-btn inline-flex items-center gap-1.5 px-3 py-1.5"
          style={{ background: "#181c24", color: "#c8cdd5" }}
        >
          <ArrowLeft className="w-3.5 h-3.5" /> BACK TO BACKTEST
        </Link>
        <div className="t-panel p-8 text-center font-mono space-y-2">
          <div className="text-sm font-bold text-[#e8c300]">
            {error ? `ERROR: ${error}` : "SELECT 2 OR 3 BACKTEST RUNS TO COMPARE"}
          </div>
          <p className="text-[11px] text-[#5a6270]">
            Use the checkboxes in Historical Backtest Reports on the Backtest page and click Compare Head-to-Head.
          </p>
        </div>
      </div>
    );
  }

  // Highlight helpers for Tab 1 Scorecard
  const bestPnlIdx = runs.reduce(
    (best, r, idx) => (r.detail.pnl > runs[best].detail.pnl ? idx : best),
    0
  );
  const lowestClosedDdIdx = runs.reduce(
    (best, r, idx) =>
      Math.abs(r.metrics.closed_max_dd ?? r.detail.max_dd) <
      Math.abs(runs[best].metrics.closed_max_dd ?? runs[best].detail.max_dd)
        ? idx
        : best,
    0
  );
  const lowestMtmDdIdx = runs.reduce(
    (best, r, idx) =>
      Math.abs(r.metrics.mtm_max_dd ?? r.detail.max_dd) <
      Math.abs(runs[best].metrics.mtm_max_dd ?? runs[best].detail.max_dd)
        ? idx
        : best,
    0
  );

  const colsClass =
    runs.length >= 3 ? "md:grid-cols-3" : runs.length === 2 ? "md:grid-cols-2" : "md:grid-cols-1";

  return (
    <div className="space-y-6 pb-12">
      {/* Top Bar */}
      <div className="flex flex-wrap items-center justify-between gap-4 border-b border-[#252a33] pb-4">
        <div className="flex items-center gap-3">
          <Link
            href="/backtest"
            className="t-btn px-3 py-1.5 flex items-center gap-1.5"
            style={{ background: "#181c24", color: "#c8cdd5" }}
          >
            <ArrowLeft className="w-3.5 h-3.5" /> BACK
          </Link>
          <div>
            <h1 className="text-lg font-bold tracking-wider uppercase text-[#e2e8f0] flex items-center gap-2">
              <GitCompare className="w-4 h-4 text-[#00e87b]" />
              HEAD-TO-HEAD BACKTEST COMPARISON
            </h1>
            <p className="text-[10px] font-mono text-[#5a6270] mt-0.5">
              COMPARING {runs.length} SAVED SIMULATIONS SIDE-BY-SIDE
            </p>
          </div>
        </div>

        {/* Navigation Tabs */}
        <div className="flex gap-1.5">
          {[
            { id: "summary" as TabId, label: "SUMMARY & CURVES", icon: BarChart3 },
            { id: "daily" as TabId, label: `DATE-WISE (${allDates.length})`, icon: CalendarDays },
            { id: "strategy" as TabId, label: "STRATEGY & DIRECTION", icon: Layers },
          ].map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              type="button"
              onClick={() => setTab(id)}
              className="t-btn px-3 py-1.5 flex items-center gap-1.5 font-bold"
              style={{
                background: tab === id ? "#00e87b" : "#181c24",
                color: tab === id ? "#0a0c10" : "#5a6270",
              }}
            >
              <Icon className="w-3.5 h-3.5" />
              {label}
            </button>
          ))}
        </div>
      </div>

      {/* Run Identity Cards */}
      <div className={`grid grid-cols-1 ${colsClass} gap-4`}>
        {runs.map((r, idx) => (
          <div
            key={r.detail.run_id}
            className="t-panel p-4"
            style={{ borderTop: `3px solid ${RUN_COLORS[idx]}` }}
          >
            <div className="flex items-center justify-between mb-1">
              <span
                className="text-[11px] font-bold uppercase tracking-wider"
                style={{ color: RUN_COLORS[idx] }}
              >
                RUN {idx + 1} · {fmtModeLabel(r.detail.strategy_mode)}
              </span>
              <Badge
                label={r.detail.risk.toUpperCase()}
                variant={idx === 0 ? "green" : idx === 1 ? "blue" : "yellow"}
              />
            </div>
            <div className="text-[11px] font-mono text-[#e2e8f0] break-all">
              {r.detail.run_id}
            </div>
            <div className="text-[10px] font-mono text-[#5a6270] mt-1">
              {r.detail.symbol} · Exit: {r.detail.exit_agent} · {r.detail.start_date} -&gt;{" "}
              {r.detail.end_date}
            </div>
          </div>
        ))}
      </div>

      {/* TAB 1: SUMMARY & OVERLAY CURVES */}
      {tab === "summary" && (
        <div className="space-y-6">
          {/* Performance Scorecard */}
          <div className="t-panel">
            <div className="px-4 py-2.5 border-b border-[#252a33] bg-[#0e1117] text-[11px] font-bold uppercase tracking-wider text-[#c8cdd5]">
              SIDE-BY-SIDE PERFORMANCE SCORECARD
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-[11px] font-mono border-collapse">
                <thead>
                  <tr className="border-b border-[#252a33] text-[9px] uppercase text-[#5a6270]">
                    <th className="py-2.5 px-4 text-left">Metric</th>
                    {runs.map((r, idx) => (
                      <th
                        key={r.detail.run_id}
                        className="py-2.5 px-4 text-right"
                        style={{ color: RUN_COLORS[idx] }}
                      >
                        Run {idx + 1} ({fmtModeLabel(r.detail.strategy_mode)})
                      </th>
                    ))}
                    {runs.length === 2 && <th className="py-2.5 px-4 text-right">Delta (R2 - R1)</th>}
                  </tr>
                </thead>
                <tbody className="divide-y divide-[#1e222c]">
                  {/* Net P&L */}
                  <tr>
                    <td className="py-2.5 px-4 text-[#c8cdd5] font-semibold">Total Net P&amp;L</td>
                    {runs.map((r, idx) => (
                      <td
                        key={r.detail.run_id}
                        className="py-2.5 px-4 text-right font-bold"
                        style={{ color: r.detail.pnl >= 0 ? "#00e87b" : "#ff3e3e" }}
                      >
                        <span className="inline-flex items-center gap-1 justify-end">
                          {idx === bestPnlIdx && <Trophy className="w-3.5 h-3.5 text-[#e8c300]" />}
                          {fmtUsd(r.detail.pnl, true)}
                        </span>
                      </td>
                    ))}
                    {runs.length === 2 && (
                      <td
                        className="py-2.5 px-4 text-right font-bold"
                        style={{
                          color: runs[1].detail.pnl - runs[0].detail.pnl >= 0 ? "#00e87b" : "#ff3e3e",
                        }}
                      >
                        {fmtUsd(runs[1].detail.pnl - runs[0].detail.pnl, true)}
                      </td>
                    )}
                  </tr>

                  {/* Win Rate */}
                  <tr>
                    <td className="py-2 px-4 text-[#5a6270]">Win Rate</td>
                    {runs.map((r) => (
                      <td key={r.detail.run_id} className="py-2 px-4 text-right text-[#c8cdd5]">
                        {fmtWinRate(r.detail.win_rate)} ({r.detail.trades} trades)
                      </td>
                    ))}
                    {runs.length === 2 && (
                      <td className="py-2 px-4 text-right text-[#c8cdd5]">
                        {(
                          normalizeWinRate(runs[1].detail.win_rate) -
                          normalizeWinRate(runs[0].detail.win_rate)
                        ).toFixed(1)}
                        %
                      </td>
                    )}
                  </tr>

                  {/* Profit Factor */}
                  <tr>
                    <td className="py-2 px-4 text-[#5a6270]">Profit Factor</td>
                    {runs.map((r) => (
                      <td key={r.detail.run_id} className="py-2 px-4 text-right text-[#c8cdd5]">
                        {r.detail.pf.toFixed(2)}
                      </td>
                    ))}
                    {runs.length === 2 && (
                      <td className="py-2 px-4 text-right text-[#c8cdd5]">
                        {(runs[1].detail.pf - runs[0].detail.pf) >= 0 ? "+" : ""}
                        {(runs[1].detail.pf - runs[0].detail.pf).toFixed(2)}
                      </td>
                    )}
                  </tr>

                  {/* Risk:Reward */}
                  <tr>
                    <td className="py-2 px-4 text-[#5a6270]">Risk : Reward Ratio</td>
                    {runs.map((r) => (
                      <td key={r.detail.run_id} className="py-2 px-4 text-right text-[#c8cdd5]">
                        1 : {r.detail.rr.toFixed(2)}
                      </td>
                    ))}
                    {runs.length === 2 && (
                      <td className="py-2 px-4 text-right text-[#5a6270]">
                        {(runs[1].detail.rr - runs[0].detail.rr).toFixed(2)}
                      </td>
                    )}
                  </tr>

                  {/* Closed Max DD */}
                  <tr>
                    <td className="py-2 px-4 text-[#e8c300]">Closed-Trade MaxDD ($ / % HWM)</td>
                    {runs.map((r, idx) => {
                      const dd = r.metrics.closed_max_dd ?? r.detail.max_dd;
                      const pct = r.metrics.closed_max_dd_pct ?? 0;
                      return (
                        <td key={r.detail.run_id} className="py-2 px-4 text-right text-[#e8c300]">
                          <span className="inline-flex items-center gap-1 justify-end">
                            {idx === lowestClosedDdIdx && <Shield className="w-3.5 h-3.5 text-[#00e87b]" />}
                            {fmtUsd(dd)} ({pct.toFixed(2)}%)
                          </span>
                        </td>
                      );
                    })}
                    {runs.length === 2 && (
                      <td className="py-2 px-4 text-right text-[#c8cdd5]">
                        {fmtUsd(
                          (runs[1].metrics.closed_max_dd ?? runs[1].detail.max_dd) -
                            (runs[0].metrics.closed_max_dd ?? runs[0].detail.max_dd),
                          true
                        )}
                      </td>
                    )}
                  </tr>

                  {/* Tick-MTM Max DD */}
                  <tr>
                    <td className="py-2 px-4 text-[#ff3e3e] font-semibold">
                      Tick-Level MTM MaxDD ($ / % HWM)
                    </td>
                    {runs.map((r, idx) => {
                      const dd = r.metrics.mtm_max_dd ?? r.detail.max_dd;
                      const pct = r.metrics.mtm_max_dd_pct ?? 0;
                      return (
                        <td
                          key={r.detail.run_id}
                          className="py-2 px-4 text-right font-bold text-[#ff3e3e]"
                        >
                          <span className="inline-flex items-center gap-1 justify-end">
                            {idx === lowestMtmDdIdx && <Shield className="w-3.5 h-3.5 text-[#00e87b]" />}
                            {fmtUsd(dd)} ({pct.toFixed(2)}%)
                          </span>
                        </td>
                      );
                    })}
                    {runs.length === 2 && (
                      <td className="py-2 px-4 text-right text-[#c8cdd5]">
                        {fmtUsd(
                          (runs[1].metrics.mtm_max_dd ?? runs[1].detail.max_dd) -
                            (runs[0].metrics.mtm_max_dd ?? runs[0].detail.max_dd),
                          true
                        )}
                      </td>
                    )}
                  </tr>

                  {/* Max Planned Risk % */}
                  <tr>
                    <td className="py-2 px-4 text-[#5a6270]">Max Planned Risk %</td>
                    {runs.map((r) => {
                      const v = r.metrics.max_planned_risk_pct ?? 0;
                      return (
                        <td
                          key={r.detail.run_id}
                          className="py-2 px-4 text-right"
                          style={{ color: v <= 1.0 ? "#00e87b" : "#ff3e3e" }}
                        >
                          {v.toFixed(3)}%
                        </td>
                      );
                    })}
                    {runs.length === 2 && <td className="py-2 px-4 text-right text-[#5a6270]">--</td>}
                  </tr>

                  {/* Avg Win / Avg Loss */}
                  <tr>
                    <td className="py-2 px-4 text-[#5a6270]">Avg Winner / Avg Loser</td>
                    {runs.map((r) => (
                      <td key={r.detail.run_id} className="py-2 px-4 text-right">
                        <span className="text-[#00e87b]">{fmtUsd(r.detail.avg_win, true)}</span>
                        {" / "}
                        <span className="text-[#ff3e3e]">{fmtUsd(r.detail.avg_loss)}</span>
                      </td>
                    ))}
                    {runs.length === 2 && <td className="py-2 px-4 text-right text-[#5a6270]">--</td>}
                  </tr>

                  {/* Required Capital -> Final Equity & ROI */}
                  <tr>
                    <td className="py-2 px-4 text-[#5a6270]">Required Capital -&gt; Final Equity (ROI)</td>
                    {runs.map((r) => (
                      <td key={r.detail.run_id} className="py-2 px-4 text-right text-[#c8cdd5]">
                        {fmtUsd(r.metrics.required_capital ?? 10000)} -&gt;{" "}
                        {fmtUsd(r.metrics.final_equity ?? 10000 + r.detail.pnl)} (
                        {(r.metrics.roi ?? 0).toFixed(1)}%)
                      </td>
                    ))}
                    {runs.length === 2 && <td className="py-2 px-4 text-right text-[#5a6270]">--</td>}
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          {/* Overlay Equity Curve */}
          <div className="t-panel p-4">
            <div className="flex items-center justify-between mb-3">
              <span className="text-[11px] font-bold uppercase tracking-wider text-[#c8cdd5]">
                OVERLAY EQUITY TRAJECTORY (BY TRADE #)
              </span>
              <span className="text-[10px] font-mono text-[#5a6270]">
                $10,000 STARTING CAPITAL REFERENCE
              </span>
            </div>
            <ResponsiveContainer width="100%" height={340}>
              <LineChart data={overlayCurveData} margin={{ top: 10, right: 20, bottom: 5, left: 10 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1e222c" />
                <XAxis dataKey="trade" tick={{ fill: "#5a6270", fontSize: 10 }} />
                <YAxis
                  domain={["auto", "auto"]}
                  tick={{ fill: "#5a6270", fontSize: 10 }}
                  tickFormatter={(v) => `$${Number(v).toLocaleString("en-US", { maximumFractionDigits: 0 })}`}
                  width={68}
                />
                <Tooltip
                  contentStyle={{
                    background: "#181c24",
                    border: "1px solid #252a33",
                    fontSize: 11,
                    fontFamily: "JetBrains Mono",
                  }}
                  formatter={(v: unknown) => [fmtUsd(Number(v)), "Equity"]}
                />
                <ReferenceLine y={10000} stroke="#252a33" strokeDasharray="4 4" />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                {runs.map((r, idx) => (
                  <Line
                    key={r.detail.run_id}
                    type="monotone"
                    dataKey={`run_${idx}`}
                    name={`Run ${idx + 1}: ${fmtModeLabel(r.detail.strategy_mode)} (${r.detail.risk.toUpperCase()})`}
                    stroke={RUN_COLORS[idx]}
                    strokeWidth={2}
                    dot={false}
                    connectNulls
                  />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {/* TAB 2: DATE-WISE COMPARISON & TRADE DRILL-DOWN */}
      {tab === "daily" && (
        <div className="t-panel">
          <div className="px-4 py-2.5 border-b border-[#252a33] bg-[#0e1117] flex items-center justify-between">
            <span className="text-[11px] font-bold uppercase tracking-wider text-[#c8cdd5]">
              DATE-WISE PERFORMANCE &amp; EXACT TRADE DRILL-DOWN
            </span>
            <span className="text-[10px] font-mono text-[#5a6270]">
              CLICK ANY ROW TO INSPECT INDIVIDUAL TRADES
            </span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-[11px] font-mono border-collapse">
              <thead>
                <tr className="border-b border-[#252a33] text-[9px] uppercase text-[#5a6270]">
                  <th className="py-2 px-3 text-left">Date</th>
                  {runs.map((r, idx) => (
                    <th
                      key={r.detail.run_id}
                      className="py-2 px-3 text-right"
                      style={{ color: RUN_COLORS[idx] }}
                    >
                      Run {idx + 1} P&amp;L (Trades)
                    </th>
                  ))}
                  {runs.length >= 2 && <th className="py-2 px-3 text-right">Diff (R2 - R1)</th>}
                  <th className="py-2 px-3 text-right">Daily Winner</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#1e222c]">
                {allDates.map((day) => {
                  const isOpen = Boolean(expandedDates[day]);
                  const pnls = runs.map((r) => r.metrics.daily_breakdown?.[day]?.pnl ?? 0);
                  const maxDayPnl = Math.max(...pnls);
                  const winnerIdx = pnls.filter((v) => v === maxDayPnl).length === 1 ? pnls.indexOf(maxDayPnl) : -1;
                  const diff = runs.length >= 2 ? pnls[1] - pnls[0] : 0;

                  return (
                    <Fragment key={day}>
                      <tr
                        onClick={() => toggleDate(day)}
                        className="hover:bg-[#181c24]/70 cursor-pointer transition-colors"
                      >
                        <td className="py-2 px-3 text-[#c8cdd5] flex items-center gap-1.5">
                          {isOpen ? (
                            <ChevronDown className="w-3.5 h-3.5 text-[#4da6ff]" />
                          ) : (
                            <ChevronRight className="w-3.5 h-3.5 text-[#5a6270]" />
                          )}
                          {day}
                        </td>
                        {runs.map((r) => {
                          const d = r.metrics.daily_breakdown?.[day];
                          const p = d?.pnl ?? 0;
                          return (
                            <td
                              key={r.detail.run_id}
                              className="py-2 px-3 text-right"
                              style={{ color: p > 0 ? "#00e87b" : p < 0 ? "#ff3e3e" : "#5a6270" }}
                            >
                              {d ? `${fmtUsd(p, true)} (${d.trades}T)` : "--"}
                            </td>
                          );
                        })}
                        {runs.length >= 2 && (
                          <td
                            className="py-2 px-3 text-right"
                            style={{ color: diff >= 0 ? "#00e87b" : "#ff3e3e" }}
                          >
                            {fmtUsd(diff, true)}
                          </td>
                        )}
                        <td className="py-2 px-3 text-right">
                          {winnerIdx >= 0 ? (
                            <Badge
                              label={`RUN ${winnerIdx + 1}`}
                              variant={winnerIdx === 0 ? "green" : winnerIdx === 1 ? "blue" : "yellow"}
                            />
                          ) : (
                            <span className="text-[#5a6270]">TIE</span>
                          )}
                        </td>
                      </tr>
                      {isOpen && (
                        <tr className="bg-[#0b0d12]">
                          <td colSpan={runs.length + 3} className="p-3">
                            <div className={`grid grid-cols-1 ${colsClass} gap-3`}>
                              {runs.map((r, idx) => {
                                const dayTrades = tradesByDateForRun(r.detail.trade_list ?? [], day);
                                return (
                                  <div key={r.detail.run_id} className="border border-[#252a33] bg-[#111318]">
                                    <div
                                      className="px-3 py-1.5 text-[10px] font-bold uppercase border-b border-[#252a33]"
                                      style={{ color: RUN_COLORS[idx] }}
                                    >
                                      Run {idx + 1} Trades ({dayTrades.length})
                                    </div>
                                    {dayTrades.length === 0 ? (
                                      <div className="p-3 text-[10px] text-[#3d4450]">No trades on {day}</div>
                                    ) : (
                                      <div className="divide-y divide-[#1e222c] text-[10px]">
                                        {dayTrades.map((t, ti) => (
                                          <div key={ti} className="px-3 py-2 flex items-center justify-between gap-2">
                                            <div>
                                              <span className="text-[#5a6270]">
                                                {toUTCTime(t.entry_time)}-&gt;{toUTCTime(t.exit_time)}
                                              </span>{" "}
                                              <span
                                                className="font-bold"
                                                style={{ color: t.direction === "BUY" ? "#00e87b" : "#ff3e3e" }}
                                              >
                                                {t.direction}
                                              </span>{" "}
                                              <span className="text-[#c8cdd5]">{t.strategy}</span>
                                              <div className="text-[#5a6270]">
                                                {fmtPrice(t.symbol, t.entry_price)} -&gt;{" "}
                                                {fmtPrice(t.symbol, t.exit_price)} · {t.exit_reason}
                                              </div>
                                            </div>
                                            <span
                                              className="font-bold whitespace-nowrap"
                                              style={{ color: t.pnl >= 0 ? "#00e87b" : "#ff3e3e" }}
                                            >
                                              {fmtUsd(t.pnl, true)}
                                            </span>
                                          </div>
                                        ))}
                                      </div>
                                    )}
                                  </div>
                                );
                              })}
                            </div>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* TAB 3: STRATEGY & DIRECTION BREAKDOWN */}
      {tab === "strategy" && (
        <div className="space-y-6">
          {/* Direction Comparison */}
          <div className="t-panel">
            <div className="px-4 py-2.5 border-b border-[#252a33] bg-[#0e1117] text-[11px] font-bold uppercase tracking-wider text-[#c8cdd5]">
              DIRECTIONAL EDGE COMPARISON (BUY VS SELL)
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-[11px] font-mono border-collapse">
                <thead>
                  <tr className="border-b border-[#252a33] text-[9px] uppercase text-[#5a6270]">
                    <th className="py-2 px-4 text-left">Direction</th>
                    {runs.map((r, idx) => (
                      <th
                        key={r.detail.run_id}
                        className="py-2 px-4 text-right"
                        style={{ color: RUN_COLORS[idx] }}
                      >
                        Run {idx + 1} (Trades / WR / PF / P&amp;L)
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-[#1e222c]">
                  {allDirections.map((dir) => (
                    <tr key={dir}>
                      <td className="py-2.5 px-4">
                        <Badge label={dir} variant={dir === "BUY" ? "green" : "red"} />
                      </td>
                      {runs.map((r) => {
                        const s = r.metrics.direction_breakdown?.[dir];
                        if (!s) {
                          return (
                            <td key={r.detail.run_id} className="py-2.5 px-4 text-right text-[#3d4450]">
                              --
                            </td>
                          );
                        }
                        return (
                          <td key={r.detail.run_id} className="py-2.5 px-4 text-right">
                            <span className="text-[#c8cdd5]">
                              {s.trades}T · {s.win_rate.toFixed(1)}% · PF {(s.pf ?? 0).toFixed(2)} ·{" "}
                            </span>
                            <span
                              className="font-bold"
                              style={{ color: s.pnl >= 0 ? "#00e87b" : "#ff3e3e" }}
                            >
                              {fmtUsd(s.pnl, true)}
                            </span>
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Strategy Breakdown Comparison */}
          <div className="t-panel">
            <div className="px-4 py-2.5 border-b border-[#252a33] bg-[#0e1117] text-[11px] font-bold uppercase tracking-wider text-[#c8cdd5]">
              PER-STRATEGY BREAKDOWN ACROSS RUNS
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-[11px] font-mono border-collapse">
                <thead>
                  <tr className="border-b border-[#252a33] text-[9px] uppercase text-[#5a6270]">
                    <th className="py-2 px-4 text-left">Strategy</th>
                    {runs.map((r, idx) => (
                      <th
                        key={r.detail.run_id}
                        className="py-2 px-4 text-right"
                        style={{ color: RUN_COLORS[idx] }}
                      >
                        Run {idx + 1}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-[#1e222c]">
                  {allStrategies.map((strat) => (
                    <tr key={strat}>
                      <td className="py-2.5 px-4 text-[#e2e8f0] font-semibold">{strat}</td>
                      {runs.map((r) => {
                        const s = r.metrics.strategy_breakdown?.[strat];
                        if (!s) {
                          return (
                            <td key={r.detail.run_id} className="py-2.5 px-4 text-right text-[#3d4450]">
                              --
                            </td>
                          );
                        }
                        return (
                          <td key={r.detail.run_id} className="py-2.5 px-4 text-right">
                            <span className="text-[#c8cdd5]">
                              {s.trades}T ({s.win_rate.toFixed(1)}% WR) ·{" "}
                            </span>
                            <span
                              className="font-bold"
                              style={{ color: s.pnl >= 0 ? "#00e87b" : "#ff3e3e" }}
                            >
                              {fmtUsd(s.pnl, true)}
                            </span>
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default function ComparePage() {
  return (
    <div className="flex flex-col md:flex-row min-h-screen">
      <Sidebar />
      <main className="flex-1 p-3 sm:p-5 md:p-6 pb-24 md:pb-6 overflow-y-auto overflow-x-hidden">
        <Suspense
          fallback={
            <div className="p-8 text-center font-mono text-[12px] text-[#5a6270]">
              INITIALIZING COMPARISON VIEW...
            </div>
          }
        >
          <CompareContent />
        </Suspense>
      </main>
    </div>
  );
}
