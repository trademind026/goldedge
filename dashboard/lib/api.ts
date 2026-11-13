const configuredApiBase = process.env.NEXT_PUBLIC_API_URL || "http://localhost:5000/api";

export const API_BASE = configuredApiBase.replace(/\/$/, "");
export const APP_BASE = process.env.NEXT_PUBLIC_APP_URL
  ? process.env.NEXT_PUBLIC_APP_URL.replace(/\/$/, "")
  : undefined;

export const SSE_STREAM_URL = `${API_BASE}/stream`;

export interface StreamPayload {
  state: {
    last_price: number;
    regime: string;
    status: string;
    last_scan: string | null;
    scan_count: number;
    trade_suggestions: TradeSuggestion[];
    auto_trade_enabled?: boolean;
    alerts?: { level: string; title: string; message: string; time: string }[];
  };
  positions_by_mode: { test: PaperPosition[]; live: PaperPosition[] };
  tick_cache: Record<string, { price: number; ts: string }>;
  tick_cache_age: number | null;
  total_open_pnl: number;
  total_closed_pnl: number;
  total_pnl: number;
  total_open_pnl_test: number;
  total_closed_pnl_test: number;
  total_pnl_test: number;
  total_open_pnl_live: number;
  total_closed_pnl_live: number;
  total_pnl_live: number;
}

export async function fetchJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`API ${path} -> ${res.status}`);
  return res.json();
}

export async function postJSON<T>(path: string, body?: object): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`API ${path} -> ${res.status}`);
  return res.json();
}

export interface MT5Status {
  connected: boolean;
  account: string;
  server: string;
  error?: string;
}

export type RiskLevel = "low" | "medium" | "high";

export interface LiveState {
  status: string;
  last_scan: string | null;
  last_price: number;
  regime: string;
  db_connected: boolean;
  trade_suggestions: TradeSuggestion[];
  scan_count: number;
  signals_checked?: number;
  trades_today?: number;
  news_sentiment?: { score: number; label: string };
  scanner_enabled?: boolean;
  auto_trade_enabled?: boolean;
  alerts?: { level: string; title: string; message: string; time: string }[];
}

export interface TradeSuggestion {
  time: string;
  symbol: string;
  direction: "BUY" | "SELL";
  strategy: string;
  entry_price: number;
  sl_price: number;
  target_price: number;
  risk_label: "LOW" | "MEDIUM" | "HIGH" | null;
  session: string;
  entry_atr: number;
  ml_prob: number;
  final_score: number;
  regime: string;
  lots?: number;
}

export interface Trade {
  id?: number;
  entry_time: string;
  exit_time: string;
  symbol: string;
  direction: "BUY" | "SELL";
  strategy: string;
  entry_price: number;
  exit_price: number;
  current_price?: number;
  initial_sl?: number;
  sl: number;
  target: number;
  lot_size: number;
  pnl: number;
  exit_reason: string;
  final_score: number;
  regime: string;
  session: string;
  entry_atr: number;
}

export interface PaperPosition {
  id: number;
  entry_time: string;
  symbol: string;
  direction: "BUY" | "SELL";
  strategy: string;
  entry_price: number;
  sl: number;
  initial_sl: number;
  target: number;
  trailing_active: boolean;
  lot_size: number;
  final_score: number;
  session: string;
  entry_atr: number;
  status: "OPEN" | "CLOSED";
  current_price: number;
  unrealised_pnl: number;
  exit_time: string | null;
  exit_price: number | null;
  realised_pnl: number | null;
  exit_reason: string | null;
  order_id?: string;
  breakeven_locked?: boolean;
}

export interface PaperPositionsResponse {
  positions: PaperPosition[];
  total_open_pnl: number;
  total_closed_pnl: number;
  total_pnl: number;
}

export type TradingMode = "test" | "live";

export async function enterPaperTrade(suggestion: TradeSuggestion, mode: TradingMode = "test"): Promise<PaperPosition> {
  return postJSON<PaperPosition>("/paper/enter", {
    ...suggestion,
    mode,
  });
}

export async function exitPaperTrade(id: number, mode: TradingMode = "test"): Promise<PaperPosition> {
  return postJSON<PaperPosition>("/paper/exit", { id, mode });
}

export async function getPaperPositions(mode: TradingMode = "test"): Promise<PaperPositionsResponse> {
  return fetchJSON<PaperPositionsResponse>(`/paper/positions?mode=${mode}`);
}

export async function clearClosedPositions(mode: TradingMode = "test"): Promise<void> {
  await postJSON("/paper/clear", { mode });
}

export async function setAutoTrade(enabled: boolean): Promise<{ auto_trade_enabled: boolean }> {
  return postJSON<{ auto_trade_enabled: boolean }>("/auto_trade", { enabled });
}

export interface RiskProfile {
  name: string;
  context: string;
  risk_per_trade_pct: number;
  max_daily_loss_pct: number;
  max_consecutive_losses: number;
  max_trades_per_day: number;
  sl_atr_multiplier: number;
  tp_atr_multiplier: number;
  min_score_threshold: number;
}

export interface BacktestProfile {
  trades: number;
  win_rate: number;
  pnl: number;
  max_dd: number;
  pf: number;
  avg_win: number;
  avg_loss: number;
  rr: number;
  equity_curve: number[];
  trade_list: Trade[];
}

export interface BacktestResults {
  low?: BacktestProfile;
  medium?: BacktestProfile;
  high?: BacktestProfile;
}

export interface EquityCurvePoint {
  time: string;
  equity: number;
}

export type CandleDateInfo = { day: string; bars: number; ticks?: number; [key: string]: any; };
export type RLStatus = Record<string, any>;
export type LiveTrade = any;
export type JourneyPoint = any;
export type Candle = any;
export type MarketDepthRow = any;
export type DepthTickData = any;

export type OptionChainRow = any;
export type OptionTickData = any;

