"use client";

import { createContext, useContext, useState, useCallback, useEffect, type ReactNode } from "react";
import { fetchJSON, postJSON } from "@/lib/api";

export type TradingMode = "test" | "live";

interface TradingModeContextValue {
  mode: TradingMode;
  setMode: (mode: TradingMode) => void;
  dialogError: string | null;
  setDialogError: (msg: string | null) => void;
  showDialog: boolean;
  setShowDialog: (v: boolean) => void;
}

const TradingModeContext = createContext<TradingModeContextValue | null>(null);

export function TradingModeProvider({ children }: { children: ReactNode }) {
  const [mode, setModeInternal] = useState<TradingMode>("test");
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [showDialog, setShowDialog] = useState(false);
  // Holds the mode we want to apply after the user confirms the dialog
  const [pendingLive, setPendingLive] = useState(false);

  // Load persisted mode from DB on mount
  useEffect(() => {
    fetchJSON<{ trading_mode: string }>("/api/config")
      .then((data) => {
        if (data.trading_mode === "live" || data.trading_mode === "test") {
          setModeInternal(data.trading_mode as TradingMode);
        }
      })
      .catch((err) => console.error("Failed to load trading mode:", err));
  }, []);

  /** Apply a mode change — saves to DB which instantly calls sync_mode() on Flask */
  function _applyMode(newMode: TradingMode) {
    setModeInternal(newMode);
    postJSON("/api/config", { trading_mode: newMode })
      .catch((err) => console.error("Failed to save trading mode:", err));
  }

  const setMode = useCallback((newMode: TradingMode) => {
    if (newMode === "test") {
      // Switching back to TEST is always safe — no dialog
      _applyMode("test");
      return;
    }

    // Switching to LIVE — verify Kite token server-side (no secrets in browser bundle)
    fetchJSON<{ valid: boolean; authenticated: boolean; has_token: boolean }>(
      "/api/kite/auth/status"
    )
      .then((status) => {
        if (!status.valid || !status.authenticated) {
          setDialogError(
            "ZERODHA KITE NOT CONNECTED\n\nYour Kite access token is missing or expired.\n\nGo to Settings → Connect Kite and complete the daily login before switching to LIVE mode."
          );
          setPendingLive(false);
          setShowDialog(true);
        } else {
          // Token OK — show safety confirmation dialog
          setDialogError(null);
          setPendingLive(true);
          setShowDialog(true);
        }
      })
      .catch(() => {
        setDialogError(
          "CANNOT REACH FLASK API\n\nEnsure the backend is running before switching to LIVE mode."
        );
        setPendingLive(false);
        setShowDialog(true);
      });
  }, []);

  const dismissDialog = () => {
    setShowDialog(false);
    setDialogError(null);
    setPendingLive(false);
  };

  const confirmGoLive = () => {
    dismissDialog();
    _applyMode("live");
  };

  return (
    <TradingModeContext.Provider
      value={{ mode, setMode, dialogError, setDialogError, showDialog, setShowDialog }}
    >
      {children}

      {/* ── Live-mode gate dialog ─────────────────────────────────────── */}
      {showDialog && (
        <div
          style={{
            position: "fixed", inset: 0, zIndex: 9999,
            background: "rgba(0,0,0,0.8)",
            display: "flex", alignItems: "center", justifyContent: "center",
          }}
          onClick={dismissDialog}
        >
          <div
            style={{
              background: "#0e1117",
              border: `1px solid ${dialogError ? "#ff3e3e" : "#ff3e3e"}`,
              padding: "28px 32px", maxWidth: 440, width: "90%",
              boxShadow: "0 0 40px rgba(255,62,62,0.15)",
            }}
            onClick={(e) => e.stopPropagation()}
          >
            {dialogError ? (
              /* ── Error: Kite not connected ─────────────────────────── */
              <>
                <p
                  style={{
                    fontSize: 11, fontWeight: 700, letterSpacing: "0.12em",
                    textTransform: "uppercase", color: "#ff3e3e", marginBottom: 12,
                  }}
                >
                  ⛔ LIVE MODE BLOCKED
                </p>
                <p
                  style={{
                    fontSize: 11, whiteSpace: "pre-wrap", color: "#c8cdd5",
                    lineHeight: 1.7, marginBottom: 20,
                  }}
                >
                  {dialogError}
                </p>
                <button
                  onClick={dismissDialog}
                  style={{
                    width: "100%", padding: "8px 0",
                    fontSize: 10, fontWeight: 700, letterSpacing: "0.1em",
                    textTransform: "uppercase",
                    background: "#111318", border: "1px solid #252a33", color: "#5a6270",
                    cursor: "pointer",
                  }}
                >
                  CLOSE
                </button>
              </>
            ) : pendingLive ? (
              /* ── Safety confirmation ───────────────────────────────── */
              <>
                <p
                  style={{
                    fontSize: 11, fontWeight: 700, letterSpacing: "0.12em",
                    textTransform: "uppercase", color: "#ff3e3e", marginBottom: 6,
                  }}
                >
                  ⚠ SWITCH TO LIVE MODE?
                </p>
                <p style={{ fontSize: 10, color: "#5a6270", marginBottom: 12 }}>
                  Scanner signals will route to real Zerodha Kite orders using real money.
                </p>
                <div
                  style={{
                    marginBottom: 20, padding: "10px 14px", fontSize: 10,
                    background: "#2a0a0a", border: "1px solid #5c1a1a", color: "#ffb3b3",
                    lineHeight: 1.6,
                  }}
                >
                  Ensure your Kite session is fresh, risk limits are set in Settings, and you
                  understand that AUTO confirmation will place orders immediately without
                  further prompts.
                </div>
                <div style={{ display: "flex", gap: 12 }}>
                  <button
                    onClick={dismissDialog}
                    style={{
                      flex: 1, padding: "8px 0",
                      fontSize: 10, fontWeight: 700, letterSpacing: "0.1em",
                      textTransform: "uppercase",
                      border: "1px solid #252a33", color: "#5a6270",
                      background: "transparent", cursor: "pointer",
                    }}
                  >
                    CANCEL
                  </button>
                  <button
                    onClick={confirmGoLive}
                    style={{
                      flex: 1, padding: "8px 0",
                      fontSize: 10, fontWeight: 700, letterSpacing: "0.1em",
                      textTransform: "uppercase",
                      background: "#3d1414", border: "1px solid #ff3e3e",
                      color: "#ff3e3e", cursor: "pointer",
                    }}
                  >
                    YES, GO LIVE
                  </button>
                </div>
              </>
            ) : null}
          </div>
        </div>
      )}
    </TradingModeContext.Provider>
  );
}

export function useTradingMode() {
  const ctx = useContext(TradingModeContext);
  if (!ctx) throw new Error("useTradingMode must be used within TradingModeProvider");
  return ctx;
}
