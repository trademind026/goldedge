import { NextRequest } from "next/server";
import { auth } from "@/lib/auth";
import { canTrade, hasTwoFactorEnabled, isAdminRole } from "@/lib/auth-policy";
import { requiredEnv } from "@/lib/server-env";

const TRADER_MUTATION_PREFIXES = [
  "/api/paper/",
  "/api/broker/",
  "/api/kite/auth/",
  "/api/auto_trade",
];

const ADMIN_MUTATION_PREFIXES = [
  "/api/system/",
  "/api/risk/",
];

function isMutation(method: string): boolean {
  return !["GET", "HEAD", "OPTIONS"].includes(method);
}

function requiresTrader(path: string, method: string): boolean {
  return isMutation(method) && TRADER_MUTATION_PREFIXES.some((prefix) => path.startsWith(prefix));
}

function requiresAdmin(path: string, method: string): boolean {
  return isMutation(method) && ADMIN_MUTATION_PREFIXES.some((prefix) => path.startsWith(prefix));
}

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const session = await auth.api.getSession({ headers: request.headers });

  if (!session) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }

  if (!hasTwoFactorEnabled(session.user)) {
    return Response.json({ error: "Two-factor authentication is required" }, { status: 403 });
  }

  const params = await context.params;
  const upstreamPath = `/${params.path.join("/")}`;
  const role = session.user.role;

  if (requiresAdmin(upstreamPath, request.method) && !isAdminRole(role)) {
    return Response.json({ error: "Admin role required" }, { status: 403 });
  }

  if (requiresTrader(upstreamPath, request.method) && !canTrade(role)) {
    return Response.json({ error: "Trader role required" }, { status: 403 });
  }

  const upstreamBase = requiredEnv("FLASK_API_URL").replace(/\/$/, "");
  const upstreamUrl = new URL(`${upstreamBase}${upstreamPath}`);
  upstreamUrl.search = request.nextUrl.search;

  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("cookie");
  headers.set("x-internal-api-secret", requiredEnv("INTERNAL_API_SECRET"));
  headers.set("x-auth-user-id", session.user.id);
  headers.set("x-auth-user-role", String(role ?? ""));

  const body = isMutation(request.method) ? await request.arrayBuffer() : undefined;
  const upstreamResponse = await fetch(upstreamUrl, {
    method: request.method,
    headers,
    body,
    cache: "no-store",
  });

  return new Response(upstreamResponse.body, {
    status: upstreamResponse.status,
    statusText: upstreamResponse.statusText,
    headers: upstreamResponse.headers,
  });
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
