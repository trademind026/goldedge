import { NextRequest } from "next/server";
import { requiredEnv } from "@/lib/server-env";

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const params = await context.params;
  const upstreamPath = `/api/${params.path.join("/")}`;

  const upstreamBase = requiredEnv("FLASK_API_URL").replace(/\/$/, "");
  const upstreamUrl = new URL(`${upstreamBase}${upstreamPath}`);
  upstreamUrl.search = request.nextUrl.search;

  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("cookie");
  headers.set("connection", "close");
  headers.set("x-internal-api-secret", requiredEnv("INTERNAL_API_SECRET"));
  headers.set("x-auth-user-id", "admin_bypass");
  headers.set("x-auth-user-role", "admin");

  const isMutation = !["GET", "HEAD", "OPTIONS"].includes(request.method);
  const body = isMutation ? await request.arrayBuffer() : undefined;

  const doFetch = () =>
    fetch(upstreamUrl, {
      method: request.method,
      headers,
      body,
      cache: "no-store",
    });

  let upstreamResponse: Response;
  try {
    upstreamResponse = await doFetch();
  } catch (err) {
    const code = (err as { cause?: { code?: string } })?.cause?.code;
    if (!isMutation && code === "UND_ERR_SOCKET") {
      upstreamResponse = await doFetch();
    } else {
      throw err;
    }
  }

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
export const maxDuration = 3600;
