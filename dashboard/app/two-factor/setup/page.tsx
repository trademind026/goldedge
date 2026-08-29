"use client";

import { FormEvent, useState } from "react";
import { KeyRound, ShieldCheck } from "lucide-react";
import { authClient } from "@/lib/auth-client";
import { QRCodeSVG } from "qrcode.react";

export default function TwoFactorSetupPage() {
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [totpURI, setTotpURI] = useState<string | null>(null);
  const [backupCodes, setBackupCodes] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const enable = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    setLoading(true);

    const result = await authClient.twoFactor.enable({ password });

    setLoading(false);

    if (result.error) {
      setError(result.error.message ?? "Unable to enable two-factor authentication");
      return;
    }

    setTotpURI(result.data.totpURI);
    setBackupCodes(result.data.backupCodes ?? []);
  };

  const verify = async (event: FormEvent<HTMLFormElement>) => {
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

    // Force a hard reload so that `useSession()` picks up the newly enabled 2FA status
    window.location.href = "/";
  };

  return (
    <main className="min-h-screen grid place-items-center px-4" style={{ background: "#0b0d12" }}>
      <section className="w-full max-w-xl space-y-4 p-5" style={{ border: "1px solid #252a33", background: "#0e1117" }}>
        <div>
          <p className="text-[10px] uppercase tracking-[0.25em]" style={{ color: "#3d4450" }}>Account security</p>
          <h1 className="text-lg font-bold mt-1" style={{ color: "#00e87b" }}>Set Up Two-Factor Auth</h1>
        </div>

        {!totpURI ? (
          <form onSubmit={enable} className="space-y-4">
            <label className="block space-y-1">
              <span className="text-[10px] uppercase tracking-wider" style={{ color: "#5a6270" }}>Confirm password</span>
              <input
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                className="w-full px-3 py-2 bg-transparent outline-none text-sm"
                style={{ border: "1px solid #252a33", background: "#0b0d12" }}
                autoComplete="current-password"
                required
              />
            </label>
            <button disabled={loading} className="t-btn-green flex items-center justify-center gap-2 w-full py-2 disabled:opacity-50">
              <KeyRound className="w-4 h-4" />
              {loading ? "CREATING SECRET" : "CREATE AUTHENTICATOR SECRET"}
            </button>
          </form>
        ) : (
          <form onSubmit={verify} className="space-y-4">
            <div className="space-y-4">
              <p className="text-[10px] uppercase tracking-wider" style={{ color: "#5a6270" }}>Scan this QR Code in your Authenticator App</p>
              <div className="flex justify-center p-4 bg-white rounded-md mx-auto" style={{ width: "fit-content" }}>
                <QRCodeSVG value={totpURI} size={200} />
              </div>
              <div className="space-y-2">
                <p className="text-[10px] uppercase tracking-wider" style={{ color: "#5a6270" }}>Or enter this code manually</p>
                <code className="block break-all p-3 text-[11px]" style={{ border: "1px solid #252a33", background: "#0b0d12", color: "#e8c300" }}>
                  {totpURI}
                </code>
              </div>
            </div>

            {backupCodes.length > 0 && (
              <div className="grid grid-cols-2 gap-2">
                {backupCodes.map((backupCode) => (
                  <code key={backupCode} className="px-2 py-1 text-[11px]" style={{ border: "1px solid #252a33", background: "#0b0d12" }}>
                    {backupCode}
                  </code>
                ))}
              </div>
            )}

            <input
              value={code}
              onChange={(event) => setCode(event.target.value)}
              className="w-full px-3 py-2 bg-transparent outline-none text-center text-lg tracking-[0.3em]"
              style={{ border: "1px solid #252a33", background: "#0b0d12" }}
              inputMode="numeric"
              autoComplete="one-time-code"
              required
            />

            <button disabled={loading} className="t-btn-green flex items-center justify-center gap-2 w-full py-2 disabled:opacity-50">
              <ShieldCheck className="w-4 h-4" />
              {loading ? "VERIFYING" : "VERIFY AND CONTINUE"}
            </button>
          </form>
        )}

        {error && <p className="text-[11px]" style={{ color: "#ff3e3e" }}>{error}</p>}
      </section>
    </main>
  );
}
