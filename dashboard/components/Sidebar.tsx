"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import {
  LayoutDashboard, TrendingUp, List, BarChart2,
  Settings, Radio, FlaskConical, Power, Maximize2,
  UserPlus, LogOut, Network
} from "lucide-react";
import { fetchJSON, postJSON, type LiveState } from "@/lib/api";
import { useTradingMode } from "@/contexts/TradingModeContext";
import { useFullscreenPnl } from "@/app/providers";
import { authClient, useSession } from "@/lib/auth-client";
import { isAdminRole } from "@/lib/auth-policy";

const nav = [
  { href: "/",         label: "DASHBOARD",   icon: LayoutDashboard },
  { href: "/live",     label: "LIVE",        icon: Radio },
  { href: "/trades",   label: "TRADES",      icon: List },
  { href: "/backtest", label: "BACKTEST",     icon: FlaskConical },
  { href: "/charts",   label: "CHARTS",       icon: BarChart2 },
  { href: "/settings", label: "SETTINGS",     icon: Settings },
];

export default function Sidebar() {
  const pathname = usePathname();
  const session = useSession();
  const [status, setStatus] = useState<string>("connecting");
  const [enabled, setEnabled] = useState<boolean>(true);
  const [toggling, setToggling] = useState(false);
  const { mode, setMode } = useTradingMode();
  const { show: onFullscreenPnl } = useFullscreenPnl();

  // Poll system state every 4s
  useEffect(() => {
    const poll = () => {
      fetchJSON<LiveState>("/system/state").then(d => {
        setStatus(d.status);
        setEnabled(d.scanner_enabled ?? true);
      }).catch(() => setStatus("offline"));
    };
    poll();
    const id = setInterval(poll, 4000);
    return () => clearInterval(id);
  }, []);

  const toggleSystem = async () => {
    setToggling(true);
    try {
      const endpoint = enabled ? "/system/stop" : "/system/start";
      await postJSON(endpoint);
      setEnabled(!enabled);
      setStatus(enabled ? "stopped" : "idle");
    } catch { /* ignore */ }
    setToggling(false);
  };

  const statusDot = status === "scanning" ? "bg-[#e8c300]"
    : status === "idle" ? "bg-[#4da6ff]"
    : status === "stopped" ? "bg-[#ff3e3e]"
    : "bg-[#3d4450]";

  const isAdmin = isAdminRole(session.data?.user.role);

  const logout = async () => {
    await authClient.signOut();
    window.location.href = "/login";
  };

  return (
    <aside className="w-52 min-h-screen flex flex-col" style={{ background: '#0e1117', borderRight: '1px solid #252a33' }}>
      {/* Brand */}
      <div className="px-4 py-4" style={{ borderBottom: '1px solid #252a33' }}>
        <div className="flex items-center gap-2">
          <TrendingUp className="w-4 h-4" style={{ color: '#e8c300' }} />
          <span className="text-sm font-bold tracking-wide" style={{ color: '#e8c300' }}>
            GOLDEDGE
          </span>
        </div>
        <p className="text-[9px] mt-1 tracking-widest" style={{ color: '#3d4450' }}>XAU/USD TRADING SYSTEM</p>
      </div>

      {/* Nav */}
      <nav className="flex-1 px-2 py-3 space-y-[2px]">
        {nav.map(({ href, label, icon: Icon }) => {
          const active = pathname === href;
          return (
            <Link
              key={href}
              href={href}
              className="flex items-center gap-3 px-3 py-[7px] text-[11px] font-medium tracking-wide transition-all"
              style={{
                background: active ? '#181c24' : 'transparent',
                color: active ? '#e8c300' : '#5a6270',
                borderLeft: active ? '2px solid #e8c300' : '2px solid transparent',
              }}
            >
              <Icon className="w-3.5 h-3.5 flex-shrink-0" />
              {label}
            </Link>
          );
        })}

        {isAdmin && (
          <Link
            href="/admin/users"
            className="flex items-center gap-3 px-3 py-[7px] text-[11px] font-medium tracking-wide transition-all"
            style={{
              background: pathname === "/admin/users" ? '#181c24' : 'transparent',
              color: pathname === "/admin/users" ? '#e8c300' : '#5a6270',
              borderLeft: pathname === "/admin/users" ? '2px solid #e8c300' : '2px solid transparent',
            }}
          >
            <UserPlus className="w-3.5 h-3.5 flex-shrink-0" />
            USERS
          </Link>
        )}

        {/* Full P&L shortcut */}
        <button
          onClick={onFullscreenPnl}
          className="flex items-center gap-3 px-3 py-[7px] text-[11px] font-medium tracking-wide w-full text-left mt-4 transition-all"
          style={{ color: '#00e87b', borderLeft: '2px solid transparent' }}
        >
          <Maximize2 className="w-3.5 h-3.5 flex-shrink-0" />
          FULL P&L [Ctrl+K]
        </button>
      </nav>

      {/* Data source badge */}
      <div
        className="px-3 py-2.5 mx-3 mb-1 flex items-center gap-2"
        style={{ background: '#111318', border: '1px solid #1a2a3a' }}
      >
        <Network className="w-3 h-3 flex-shrink-0" style={{ color: '#4da6ff' }} />
        <div className="flex-1 min-w-0">
          <p className="text-[8px] uppercase tracking-widest truncate" style={{ color: '#3d4450' }}>
            DATA & BROKER
          </p>
          <p className="text-[9px] font-bold truncate" style={{ color: '#4da6ff' }}>
            META TRADER 5
          </p>
        </div>
      </div>

      {/* Mode toggle */}
      <div className="px-3 py-3" style={{ borderTop: '1px solid #252a33' }}>
        <p className="text-[8px] uppercase tracking-[2px] mb-2 px-1" style={{ color: '#3d4450' }}>TRADING MODE</p>
        <div className="flex" style={{ border: '1px solid #252a33', background: '#0e1117' }}>
          <button
            onClick={() => setMode("test")}
            className="flex-1 py-[5px] text-[10px] font-bold uppercase tracking-wider text-center transition-all"
            style={{
              background: mode === "test" ? '#4da6ff' : 'transparent',
              color: mode === "test" ? '#000' : '#5a6270',
              borderRight: '1px solid #252a33',
            }}
          >
            DEMO
          </button>
          <button
            onClick={() => setMode("live")}
            className="flex-1 py-[5px] text-[10px] font-bold uppercase tracking-wider text-center transition-all"
            style={{
              background: mode === "live" ? '#ff3e3e' : 'transparent',
              color: mode === "live" ? '#000' : '#5a6270',
            }}
          >
            LIVE
          </button>
        </div>
        <p className="text-[8px] mt-1.5 px-1" style={{ color: mode === "live" ? '#ff3e3e' : '#3d4450' }}>
          {mode === "live" ? "⚠ REAL EXECUTIONS VIA MT5" : "SIMULATED DEMO TRADES ONLY"}
        </p>
      </div>

      {/* System control */}
      <div className="px-3 py-3 space-y-2" style={{ borderTop: '1px solid #252a33' }}>
        <button
          onClick={toggleSystem}
          disabled={toggling}
          className={`flex items-center justify-center gap-2 w-full py-[6px] text-[10px] font-semibold tracking-wider uppercase transition-all disabled:opacity-50 ${enabled ? "bg-[#ff3e3e] text-black" : "bg-[#00e87b] text-black"}`}
        >
          <Power className="w-3 h-3" />
          {toggling ? "..." : enabled ? "STOP SYSTEM" : "START SYSTEM"}
        </button>

        <div className="flex items-center gap-2 text-[10px] px-1">
          <span className={`w-[6px] h-[6px] ${statusDot} ${status === "scanning" ? "t-pulse" : ""}`} />
          <span style={{ color: status === "scanning" ? '#e8c300' : status === "idle" ? '#4da6ff' : status === "stopped" ? '#ff3e3e' : '#3d4450' }}>
            {status === "scanning" ? "SCANNING..." : status.toUpperCase()}
          </span>
        </div>

        <button
          onClick={logout}
          className="flex items-center justify-center gap-2 w-full py-[6px] text-[10px] font-semibold tracking-wider uppercase transition-all mt-2"
          style={{ border: '1px solid #252a33', color: '#5a6270' }}
        >
          <LogOut className="w-3 h-3" />
          SIGN OUT
        </button>
      </div>
    </aside>
  );
}

