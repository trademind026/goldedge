"use client";

import { useState, useEffect } from "react";
import Sidebar from "@/components/Sidebar";
import { API_BASE, postJSON } from "@/lib/api";
import { Play, CheckCircle, AlertTriangle, Calendar } from "lucide-react";

export default function BacktestPage() {
  const [startDate, setStartDate] = useState("2023-01-01");
  const [endDate, setEndDate] = useState("2023-12-31");
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState(0);
  const [resultMsg, setResultMsg] = useState<{type: "ok"|"err", text: string} | null>(null);

  useEffect(() => {
    if (!running) return;
    const es = new EventSource(`${API_BASE}/backtest/progress`);
    es.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        if (data.progress !== undefined) {
          setProgress(data.progress);
        }
        if (data.status === "completed" || data.status === "error") {
          setRunning(false);
          setResultMsg({ type: data.status === "completed" ? "ok" : "err", text: data.message || "Backtest finished" });
          es.close();
        }
      } catch(err) {}
    };
    es.onerror = () => {
      setRunning(false);
      setResultMsg({ type: "err", text: "Progress stream disconnected." });
      es.close();
    };
    return () => es.close();
  }, [running]);

  const startBacktest = async () => {
    setRunning(true);
    setProgress(0);
    setResultMsg(null);
    try {
      await postJSON("/backtest/run", { start_date: startDate, end_date: endDate, symbol: "XAUUSD" });
    } catch (e: any) {
      setRunning(false);
      setResultMsg({ type: "err", text: e.message || "Failed to start" });
    }
  };

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 p-5 overflow-y-auto">
        <div className="mb-5">
          <h1 className="text-sm font-bold uppercase tracking-wider text-[#e8c300]">Historical Backtest</h1>
          <p className="text-[10px] mt-0.5 text-[#3d4450]">RUN STRATEGY ON M1 OHLCV DATA · XAUUSD</p>
        </div>

        <div className="t-panel p-5 mb-4 max-w-2xl">
          <h2 className="text-[12px] font-bold uppercase tracking-wider mb-4 text-[#c8cdd5]">Backtest Parameters</h2>
          
          <div className="grid grid-cols-2 gap-4 mb-4">
            <div>
              <label className="block text-[9px] uppercase tracking-widest mb-1 text-[#5a6270]">Start Date</label>
              <div className="relative">
                <Calendar className="w-3.5 h-3.5 absolute left-3 top-2.5 text-[#5a6270]" />
                <input 
                  type="date" 
                  value={startDate} 
                  onChange={e => setStartDate(e.target.value)} 
                  className="w-full bg-[#181c24] border border-[#252a33] text-[11px] p-2 pl-9 outline-none text-[#c8cdd5]" 
                />
              </div>
            </div>
            <div>
              <label className="block text-[9px] uppercase tracking-widest mb-1 text-[#5a6270]">End Date</label>
              <div className="relative">
                <Calendar className="w-3.5 h-3.5 absolute left-3 top-2.5 text-[#5a6270]" />
                <input 
                  type="date" 
                  value={endDate} 
                  onChange={e => setEndDate(e.target.value)} 
                  className="w-full bg-[#181c24] border border-[#252a33] text-[11px] p-2 pl-9 outline-none text-[#c8cdd5]" 
                />
              </div>
            </div>
          </div>

          <button
            onClick={startBacktest}
            disabled={running}
            className="flex items-center gap-2 px-5 py-2.5 text-[11px] font-bold uppercase tracking-wider transition-all disabled:opacity-50 bg-[#0a2a40] text-[#4da6ff] border border-[#4da6ff]"
          >
            {running ? "..." : <Play className="w-3 h-3" />}
            {running ? "BACKTEST RUNNING..." : "START XAU/USD BACKTEST"}
          </button>

          {running && (
            <div className="mt-5">
              <div className="flex justify-between text-[10px] text-[#5a6270] mb-1">
                <span>Progress</span>
                <span>{progress.toFixed(1)}%</span>
              </div>
              <div className="h-1.5 w-full bg-[#181c24] overflow-hidden rounded-sm">
                <div className="h-full bg-[#4da6ff] transition-all duration-300" style={{ width: `${progress}%` }} />
              </div>
            </div>
          )}

          {resultMsg && !running && (
            <div className={`flex items-start gap-2 mt-4 px-3 py-2 text-[10px] ${resultMsg.type === "ok" ? "bg-[#0a3d26] border-[#00e87b] text-[#00e87b]" : "bg-[#3d1414] border-[#ff3e3e] text-[#ff3e3e]"} border`}>
              {resultMsg.type === "ok" ? <CheckCircle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" /> : <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />}
              {resultMsg.text}
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
