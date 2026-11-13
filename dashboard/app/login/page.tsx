"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function LoginPage() {
  const router = useRouter();

  useEffect(() => {
    // Automatically log in by redirecting to dashboard since auth is disabled
    router.replace("/");
  }, [router]);

  return (
    <main style={{ background: "#050505", minHeight: "100vh", display: "flex", justifyContent: "center", alignItems: "center" }}>
      <p style={{ color: "#fff" }}>Logging in...</p>
    </main>
  );
}
