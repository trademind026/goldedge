"use client";

import { FormEvent, useState } from "react";
import { ShieldCheck } from "lucide-react";
import { authClient } from "@/lib/auth-client";

export default function TwoFactorVerifyPage() {
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    setLoading(true);

    const result = await authClient.twoFactor.verifyTotp({
      code,
      trustDevice: true,
    });

    setLoading(false);

    if (result.error) {
      setError(result.error.message ?? "Invalid verification code");
      return;
    }

    window.location.href = "/";
  };

  return (
    <main className="min-h-screen grid place-items-center px-4" style={{ background: "#0b0d12" }}>
      <form onSubmit={submit} className="w-full max-w-sm space-y-4 p-5" style={{ border: "1px solid #252a33", background: "#0e1117" }}>
        <div>
          <p className="text-[10px] uppercase tracking-[0.25em]" style={{ color: "#3d4450" }}>Two-factor required</p>
          <h1 className="text-lg font-bold mt-1" style={{ color: "#00e87b" }}>Enter Authenticator Code</h1>
        </div>

        <input
          value={code}
          onChange={(event) => setCode(event.target.value)}
          className="w-full px-3 py-2 bg-transparent outline-none text-center text-lg tracking-[0.3em]"
          style={{ border: "1px solid #252a33", background: "#0b0d12" }}
          inputMode="numeric"
          autoComplete="one-time-code"
          required
        />

        {error && <p className="text-[11px]" style={{ color: "#ff3e3e" }}>{error}</p>}

        <button disabled={loading} className="t-btn-green w-full flex items-center justify-center gap-2 py-2 disabled:opacity-50">
          <ShieldCheck className="w-4 h-4" />
          {loading ? "VERIFYING" : "VERIFY"}
        </button>
      </form>
    </main>
  );
}
