"use client";

import { useEffect, useState } from "react";
import Sidebar from "@/components/Sidebar";
import {
  fetchJSON,
  postJSON,
  type RiskLevel,
} from "@/lib/api";
import {
  Zap, Play,
  RefreshCw,
  CheckCircle,
  XCircle,
  AlertTriangle,
  Network,
  Shield,
  Bot,
  Hand,
} from "lucide-react";

// ── MT5 Connection Panel ─────────────────────────────────

function MT5AuthPanel() {
  const [status, setStatus] = useState<{connected: boolean; account: string; server: string} | null>(null);
  const [loading, setLoading] = useState(true);
  const [launching, setLaunching] = useState(false);
  const [msg, setMsg] = useState<{ type: "ok" | "err" | "warn"; text: string } | null>(null);

  const [account, setAccount] = useState("");
  const [password, setPassword] = useState("");
  const [server, setServer] = useState("");

  const loadStatus = async () => {
    try {
      // Stub endpoint for MT5 status
      const s = await fetchJSON<{connected: boolean; account: string; server: string}>("/system/mt5/status");
      setStatus(s);
    } catch {
      setStatus(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadStatus();
  }, []);

  const connectMT5 = async () => {
    setLaunching(true);
    setMsg(null);
    try {
      await postJSON("/system/mt5/connect", { account: parseInt(account), password, server });
      setMsg({ type: "ok", text: "✓ MT5 Connected Successfully!" });
      loadStatus();
    } catch (e: unknown) {
      setMsg({ type: "err", text: String(e) });
    } finally {
      setLaunching(false);
    }
  };

  const statusBadge = () => {
    if (loading) return <span style={{ color: "#3d4450" }}>CHECKING...</span>;
    if (status?.connected)
      return (
        <span className="flex items-center gap-1.5" style={{ color: "#00e87b" }}>
          <CheckCircle className="w-3 h-3" /> CONNECTED
        </span>
      );
    return (
      <span className="flex items-center gap-1.5" style={{ color: "#ff3e3e" }}>
        <XCircle className="w-3 h-3" /> DISCONNECTED
      </span>
    );
  };

  return (
    <div className="t-panel p-5 mb-4">
      {/* Header */}
      <div className="flex items-start justify-between mb-4">
        <div>
          <div className="flex items-center gap-2 mb-1">
            <Network className="w-3.5 h-3.5" style={{ color: "#4da6ff" }} />
            <h2 className="text-[12px] font-bold uppercase tracking-wider" style={{ color: "#c8cdd5" }}>
              MetaTrader 5 Integration
            </h2>
          </div>
          <p className="text-[10px]" style={{ color: "#5a6270" }}>
            Connect to your broker MT5 account for live tick data, historical rates, and trade execution.
          </p>
        </div>
        <button onClick={() => loadStatus()} title="Refresh status" className="p-1 transition-opacity hover:opacity-70">
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} style={{ color: "#5a6270" }} />
        </button>
      </div>

      {/* Status grid */}
      <div className="grid grid-cols-2 md:grid-cols-3 gap-px mb-4" style={{ background: "#252a33" }}>
        {[
          { label: "CONNECTION STATUS",  value: statusBadge() },
          { label: "CONNECTED ACCOUNT",  value: status?.account ? <span style={{ color: "#c8cdd5" }}>{status.account}</span> : <span style={{ color: "#3d4450" }}>—</span> },
          { label: "BROKER SERVER",      value: status?.server ? <span style={{ color: "#c8cdd5" }}>{status.server}</span> : <span style={{ color: "#3d4450" }}>—</span> },
        ].map(({ label, value }) => (
          <div key={label} className="px-4 py-3" style={{ background: "#181c24" }}>
            <p className="text-[9px] uppercase tracking-widest mb-1" style={{ color: "#3d4450" }}>{label}</p>
            <div className="text-[11px] font-semibold">{value}</div>
          </div>
        ))}
      </div>

      {/* Connection Form */}
      <div className="p-4" style={{ background: "#111318", border: "1px solid #252a33" }}>
        <div className="grid grid-cols-3 gap-3 mb-3">
          <div>
            <label className="block text-[9px] uppercase tracking-widest mb-1 text-[#5a6270]">Account Login</label>
            <input type="text" value={account} onChange={e => setAccount(e.target.value)} className="w-full bg-[#181c24] border border-[#252a33] text-[11px] p-2 outline-none text-[#c8cdd5]" placeholder="12345678" />
          </div>
          <div>
            <label className="block text-[9px] uppercase tracking-widest mb-1 text-[#5a6270]">Password</label>
            <input type="password" value={password} onChange={e => setPassword(e.target.value)} className="w-full bg-[#181c24] border border-[#252a33] text-[11px] p-2 outline-none text-[#c8cdd5]" placeholder="••••••••" />
          </div>
          <div>
            <label className="block text-[9px] uppercase tracking-widest mb-1 text-[#5a6270]">Server Name</label>
            <input type="text" value={server} onChange={e => setServer(e.target.value)} className="w-full bg-[#181c24] border border-[#252a33] text-[11px] p-2 outline-none text-[#c8cdd5]" placeholder="Broker-Server" />
          </div>
        </div>

        <div className="flex items-center gap-3 flex-wrap">
          <button
            onClick={connectMT5}
            disabled={launching || !account || !password || !server}
            className="flex items-center gap-2 px-5 py-2.5 text-[11px] font-bold uppercase tracking-wider transition-all hover:opacity-90 disabled:opacity-50"
            style={{
              background: "#0a2a40",
              color: "#4da6ff",
              border: "1px solid #4da6ff",
            }}
          >
            {launching ? <><RefreshCw className="w-3 h-3 animate-spin" /> CONNECTING...</> : "CONNECT / RESTART MT5"}
          </button>
        </div>

        {msg && (
          <div
            className="flex items-start gap-2 mt-3 px-3 py-2 text-[10px]"
            style={{
              background: msg.type === "ok" ? "#0a3d26" : "#3d1414",
              border: `1px solid ${msg.type === "ok" ? "#00e87b" : "#ff3e3e"}`,
              color: msg.type === "ok" ? "#00e87b" : "#ff3e3e",
            }}
          >
            {msg.type === "ok" ? <CheckCircle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" /> : <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />}
            {msg.text}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Main Settings Page ────────────────────────────────────────────────────────

export default function SettingsPage() {
  const [tradingMode, setTradingMode] = useState<"test" | "live">("test");
  const [orderConfirmation, setOrderConfirmation] = useState<"manual" | "auto">("manual");
  const [riskLevel, setRiskLevel] = useState<RiskLevel>("medium");
  const [execMsg, setExecMsg] = useState<{ type: "ok" | "err"; text: string } | null>(null);

  useEffect(() => {
    fetchJSON<{ trading_mode?: string; order_confirmation?: string; risk_level?: string; }>("/config")
      .then(cfg => {
        setTradingMode(cfg.trading_mode === "live" ? "live" : "test");
        setOrderConfirmation(cfg.order_confirmation === "auto" ? "auto" : "manual");
        const lvl = cfg.risk_level?.toLowerCase();
        if (lvl === "low" || lvl === "medium" || lvl === "high") setRiskLevel(lvl as RiskLevel);
      })
      .catch(() => {});
  }, []);

  const saveExecutionConfig = async (
    nextMode: "test" | "live" = tradingMode,
    nextConfirmation: "manual" | "auto" = orderConfirmation,
    nextRisk: RiskLevel = riskLevel,
  ) => {
    setExecMsg(null);
    try {
      await postJSON("/config", {
        trading_mode: nextMode,
        order_confirmation: nextConfirmation,
        risk_level: nextRisk,
      });
      setTradingMode(nextMode);
      setOrderConfirmation(nextConfirmation);
      setRiskLevel(nextRisk);
      setExecMsg({ type: "ok", text: "SETTINGS SAVED" });
    } catch {
      setExecMsg({ type: "err", text: "FAILED TO SAVE SETTINGS" });
    }
  };

  const riskColors: Record<string, string> = { low: "#4da6ff", medium: "#e8c300", high: "#00e87b" };

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 p-5 overflow-y-auto">
        <div className="mb-5">
          <h1 className="text-sm font-bold uppercase tracking-wider" style={{ color: "#e8c300" }}>Settings</h1>
          <p className="text-[10px] mt-0.5" style={{ color: "#3d4450" }}>SYSTEM CONFIG · RISK PROFILES · MT5 CONNECTION</p>
        </div>

        {/* ── MT5 Auth ── */}
        <MT5AuthPanel />

        <div className="t-panel p-5 mb-4">
          <h2 className="text-[12px] font-bold uppercase tracking-wider mb-1" style={{ color: "#c8cdd5" }}>Live Execution</h2>
          <p className="text-[10px] mb-4" style={{ color: "#5a6270" }}>Controls whether scanner signals stay simulated, queue for confirmation, or execute automatically to MT5.</p>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {/* Risk Level */}
            <div>
              <p className="text-[9px] uppercase tracking-widest mb-2" style={{ color: "#3d4450" }}>Active Risk Profile</p>
              <div className="flex gap-2">
                {(["low", "medium", "high"] as RiskLevel[]).map(r => (
                  <button
                    key={r}
                    onClick={() => saveExecutionConfig(tradingMode, orderConfirmation, r)}
                    className="flex-1 flex items-center justify-center gap-1.5 px-3 py-2 text-[10px] font-bold uppercase tracking-wider"
                    style={{
                      background: riskLevel === r ? (r === "low" ? "#0d2240" : r === "medium" ? "#2a2200" : "#0d2b14") : "#111318",
                      color: riskLevel === r ? riskColors[r] : "#5a6270",
                      border: `1px solid ${riskLevel === r ? riskColors[r] : "#252a33"}`,
                    }}
                  >
                    <Shield className="w-3 h-3" />
                    {r}
                  </button>
                ))}
              </div>
            </div>

            {/* Trading Mode */}
            <div>
              <p className="text-[9px] uppercase tracking-widest mb-2" style={{ color: "#3d4450" }}>Trading Mode</p>
              <div className="flex gap-2">
                {(["test", "live"] as const).map(m => (
                  <button
                    key={m}
                    onClick={() => saveExecutionConfig(m, orderConfirmation, riskLevel)}
                    className="flex items-center justify-center gap-2 px-4 py-2 text-[10px] font-bold uppercase tracking-wider w-full"
                    style={{
                      background: tradingMode === m ? (m === "live" ? "#3d1414" : "#0a3d26") : "#111318",
                      color: tradingMode === m ? (m === "live" ? "#ff3e3e" : "#00e87b") : "#5a6270",
                      border: `1px solid ${tradingMode === m ? (m === "live" ? "#ff3e3e" : "#00e87b") : "#252a33"}`,
                    }}
                  >
                    {m === "live" ? <Zap className="w-3 h-3" /> : <Shield className="w-3 h-3" />}
                    {m === "live" ? "LIVE" : "DEMO"}
                  </button>
                ))}
              </div>
            </div>

            {/* Order Confirmation */}
            <div>
              <p className="text-[9px] uppercase tracking-widest mb-2" style={{ color: "#3d4450" }}>Live Confirmation</p>
              <div className="flex gap-2">
                {(["manual", "auto"] as const).map(c => (
                  <button
                    key={c}
                    onClick={() => saveExecutionConfig(tradingMode, c, riskLevel)}
                    className="flex items-center justify-center gap-2 px-4 py-2 text-[10px] font-bold uppercase tracking-wider w-full"
                    style={{
                      background: orderConfirmation === c ? (c === "auto" ? "#3d1414" : "#1a1a0a") : "#111318",
                      color: orderConfirmation === c ? (c === "auto" ? "#ff3e3e" : "#e8c300") : "#5a6270",
                      border: `1px solid ${orderConfirmation === c ? (c === "auto" ? "#ff3e3e" : "#e8c300") : "#252a33"}`,
                    }}
                  >
                    {c === "auto" ? <Bot className="w-3 h-3" /> : <Hand className="w-3 h-3" />}
                    {c}
                  </button>
                ))}
              </div>
            </div>
          </div>
          {execMsg && (
            <p className="text-[10px] mt-3 font-semibold" style={{ color: execMsg.type === "ok" ? "#00e87b" : "#ff3e3e" }}>{execMsg.text}</p>
          )}
        </div>
      </main>
    </div>
  );
}

