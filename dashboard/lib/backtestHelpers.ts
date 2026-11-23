import {
  type BacktestHistoryDetail,
  type BacktestMetrics,
  type BreakdownStats,
  type ExitReasonStats,
  type Trade,
  normalizeWinRate,
} from "./api";

export function fmtUsd(v?: number | null, showPlus = false): string {
  const n = Number(v ?? 0);
  if (!Number.isFinite(n)) return "$0.00";
  const sign = n < 0 ? "-" : showPlus && n > 0 ? "+" : "";
  return `${sign}$${Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function fmtPrice(symbol: string = "XAUUSD", price?: number | null): string {
  if (price === undefined || price === null || Number.isNaN(price)) return "--";
  const sym = symbol.toUpperCase();
  if (sym.includes("JPY") || sym.includes("XAU") || sym.includes("GOLD")) {
    return Number(price).toFixed(2);
  }
  return Number(price).toFixed(5);
}

export function fmtModeLabel(mode?: string): string {
  if (!mode) return "Baseline";
  return mode.includes("candidate") || mode.includes("15") ? "Candidate 15+E1" : "Baseline";
}

export function computeFallbackMetrics(
  trades: Trade[] = [],
  totalPnl: number = 0,
  maxDd: number = 0,
  initialCapital: number = 10000
): BacktestMetrics {
  const strategy_breakdown: Record<string, BreakdownStats> = {};
  const direction_breakdown: Record<string, BreakdownStats> = {};
  const daily_breakdown: Record<string, BreakdownStats> = {};
  const exit_reason_breakdown: Record<string, ExitReasonStats> = {};

  type GroupAcc = {
    trades: number;
    wins: number;
    losses: number;
    pnl: number;
    grossWin: number;
    grossLoss: number;
  };

  const addTo = (map: Record<string, GroupAcc>, key: string, pnl: number) => {
    const g = (map[key] ??= {
      trades: 0,
      wins: 0,
      losses: 0,
      pnl: 0,
      grossWin: 0,
      grossLoss: 0,
    });
    g.trades += 1;
    g.pnl += pnl;
    const pnlCents = Math.round(pnl * 100);
    if (pnlCents > 0) {
      g.wins += 1;
      g.grossWin += pnl;
    } else if (pnlCents < 0) {
      g.losses += 1;
      g.grossLoss += Math.abs(pnl);
    }
  };

  const stratAcc: Record<string, GroupAcc> = {};
  const dirAcc: Record<string, GroupAcc> = {};
  const dailyAcc: Record<string, GroupAcc> = {};

  let maxWin = 0;
  let maxLoss = 0;
  let maxMargin = 0;
  let maxPlannedRisk = 0;

  for (const t of trades) {
    const pnl = Number(t.pnl ?? 0);
    if (pnl > maxWin) maxWin = pnl;
    if (pnl < maxLoss) maxLoss = pnl;
    const lots = Number(t.qty ?? t.lot_size ?? 0);
    const margin = Number(t.entry_price ?? 0) * lots;
    if (margin > maxMargin) maxMargin = margin;
    if (Number(t.planned_risk_pct ?? 0) > maxPlannedRisk) {
      maxPlannedRisk = Number(t.planned_risk_pct ?? 0);
    }

    const strat = t.strategy || "UNKNOWN";
    const dir = (t.direction || "UNKNOWN").toUpperCase();
    const day = (t.entry_time || t.exit_time || "").slice(0, 10) || "UNKNOWN";
    const reason = t.exit_reason || "UNKNOWN";

    addTo(stratAcc, strat, pnl);
    addTo(dirAcc, dir, pnl);
    addTo(dailyAcc, day, pnl);

    const er = (exit_reason_breakdown[reason] ??= { count: 0, wins: 0, win_rate: 0, pnl: 0 });
    er.count += 1;
    if (Math.round(pnl * 100) > 0) er.wins += 1;
    er.pnl += pnl;
  }

  const finalize = (acc: Record<string, GroupAcc>, out: Record<string, BreakdownStats>) => {
    for (const key of Object.keys(acc).sort()) {
      const g = acc[key];
      const pf = g.grossLoss > 0 ? g.grossWin / g.grossLoss : g.grossWin > 0 ? 999 : 0;
      out[key] = {
        trades: g.trades,
        wins: g.wins,
        losses: g.losses,
        win_rate: g.trades > 0 ? Number(((g.wins / g.trades) * 100).toFixed(2)) : 0,
        pnl: Number(g.pnl.toFixed(2)),
        avg_pnl: g.trades > 0 ? Number((g.pnl / g.trades).toFixed(2)) : 0,
        pf: Number(pf.toFixed(2)),
      };
    }
  };

  finalize(stratAcc, strategy_breakdown);
  finalize(dirAcc, direction_breakdown);
  finalize(dailyAcc, daily_breakdown);

  for (const key of Object.keys(exit_reason_breakdown)) {
    const er = exit_reason_breakdown[key];
    er.win_rate = er.count > 0 ? Number(((er.wins / er.count) * 100).toFixed(2)) : 0;
    er.pnl = Number(er.pnl.toFixed(2));
  }

  const reqCap = maxMargin + Math.abs(maxDd) || initialCapital;
  const roi = reqCap > 0 ? (totalPnl / reqCap) * 100 : 0;
  const daysTraded = Object.keys(daily_breakdown).filter((d) => d !== "UNKNOWN").length;

  return {
    initial_capital: initialCapital,
    final_equity: Number((initialCapital + totalPnl).toFixed(2)),
    max_margin: Number(maxMargin.toFixed(2)),
    required_capital: Number(reqCap.toFixed(2)),
    roi: Number(roi.toFixed(2)),
    closed_max_dd: Number(maxDd.toFixed(2)),
    mtm_max_dd: Number(maxDd.toFixed(2)),
    max_planned_risk_pct: Number(maxPlannedRisk.toFixed(3)),
    days_traded: daysTraded,
    avg_pnl_per_trade: trades.length > 0 ? Number((totalPnl / trades.length).toFixed(2)) : 0,
    avg_pnl_per_day: daysTraded > 0 ? Number((totalPnl / daysTraded).toFixed(2)) : 0,
    max_win: Number(maxWin.toFixed(2)),
    max_loss: Number(maxLoss.toFixed(2)),
    strategy_breakdown,
    direction_breakdown,
    exit_reason_breakdown,
    daily_breakdown,
  };
}

export function resolveRunMetrics(run: BacktestHistoryDetail): BacktestMetrics {
  const m = run.metrics;
  if (m && m.strategy_breakdown && Object.keys(m.strategy_breakdown).length > 0) {
    return m;
  }
  const fallback = computeFallbackMetrics(run.trade_list ?? [], run.pnl, run.max_dd);
  return { ...fallback, ...(m ?? {}) };
}

export function fmtWinRate(val?: number | null): string {
  return `${normalizeWinRate(val).toFixed(1)}%`;
}
