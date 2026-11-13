"use client";

// Authentication globally disabled, AuthGate bypassed
export default function AuthGate({ children }: { children: React.ReactNode }) {
  return children;
}
