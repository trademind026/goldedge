"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Lock, LogIn, Mail } from "lucide-react";
import { authClient, useSession } from "@/lib/auth-client";
import { hasTwoFactorEnabled } from "@/lib/auth-policy";

export default function LoginPage() {
  const router = useRouter();
  const session = useSession();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!session.data) return;
    router.replace(hasTwoFactorEnabled(session.data.user) ? "/" : "/two-factor/setup");
  }, [router, session.data]);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    setLoading(true);

    const result = await authClient.signIn.email({
      email,
      password,
      callbackURL: "/",
    });

    setLoading(false);

    if (result.error) {
      setError(result.error.message ?? "Login failed");
      return;
    }

    if ((result.data as unknown as { twoFactorRedirect?: boolean })?.twoFactorRedirect) {
      return;
    }

    window.location.href = "/";
  };

  return (
    <main className="min-h-screen grid place-items-center px-4" style={{ background: "#0b0d12" }}>
      <form onSubmit={submit} className="w-full max-w-sm space-y-4 p-5" style={{ border: "1px solid #252a33", background: "#0e1117" }}>
        <div>
          <p className="text-[10px] uppercase tracking-[0.25em]" style={{ color: "#3d4450" }}>Secure access</p>
          <h1 className="text-lg font-bold mt-1" style={{ color: "#00e87b" }}>AI Trader Login</h1>
        </div>

        <label className="block space-y-1">
          <span className="text-[10px] uppercase tracking-wider" style={{ color: "#5a6270" }}>Email</span>
          <div className="flex items-center gap-2 px-3 py-2" style={{ border: "1px solid #252a33", background: "#0b0d12" }}>
            <Mail className="w-4 h-4" style={{ color: "#3d4450" }} />
            <input
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              className="w-full bg-transparent outline-none text-sm"
              autoComplete="email"
              required
            />
          </div>
        </label>

        <label className="block space-y-1">
          <span className="text-[10px] uppercase tracking-wider" style={{ color: "#5a6270" }}>Password</span>
          <div className="flex items-center gap-2 px-3 py-2" style={{ border: "1px solid #252a33", background: "#0b0d12" }}>
            <Lock className="w-4 h-4" style={{ color: "#3d4450" }} />
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              className="w-full bg-transparent outline-none text-sm"
              autoComplete="current-password"
              required
            />
          </div>
        </label>

        {error && <p className="text-[11px]" style={{ color: "#ff3e3e" }}>{error}</p>}

        <button disabled={loading} className="t-btn-green w-full flex items-center justify-center gap-2 py-2 disabled:opacity-50">
          <LogIn className="w-4 h-4" />
          {loading ? "SIGNING IN" : "SIGN IN"}
        </button>
      </form>
    </main>
  );
}
