"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import { hasTwoFactorEnabled } from "@/lib/auth-policy";
import { useSession } from "@/lib/auth-client";

const PUBLIC_PATHS = ["/login", "/two-factor"];

export default function AuthGate({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const session = useSession();
  const isPublic = PUBLIC_PATHS.some((path) => pathname === path || pathname.startsWith(`${path}/`));

  useEffect(() => {
    if (isPublic || session.isPending) return;

    if (!session.data) {
      router.replace("/login");
      return;
    }

    if (!hasTwoFactorEnabled(session.data.user)) {
      router.replace("/two-factor/setup");
    }
  }, [isPublic, router, session.data, session.isPending]);

  if (isPublic) return children;

  if (session.isPending || !session.data || !hasTwoFactorEnabled(session.data.user)) {
    return (
      <main className="min-h-screen grid place-items-center" style={{ background: "#0b0d12", color: "#5a6270" }}>
        <span className="text-[11px] uppercase tracking-[0.25em]">Authorizing</span>
      </main>
    );
  }

  return children;
}
