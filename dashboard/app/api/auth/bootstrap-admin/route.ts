import { NextRequest } from "next/server";
import { auth } from "@/lib/auth";
import { authDb } from "@/lib/auth-db";
import { AUTH_ROLES } from "@/lib/auth-policy";
import { requiredEnv } from "@/lib/server-env";

export async function POST(request: NextRequest) {
  const expectedToken = requiredEnv("AUTH_BOOTSTRAP_TOKEN");
  const providedToken = request.headers.get("x-bootstrap-token");

  if (!providedToken || providedToken !== expectedToken) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }

  const existing = await authDb.query<{ count: string }>('select count(*) from "user"');

  if (Number(existing.rows[0]?.count ?? 0) > 0) {
    return Response.json({ error: "Bootstrap is closed" }, { status: 409 });
  }

  const body = await request.json();
  const email = String(body.email ?? "");
  const password = String(body.password ?? "");
  const name = String(body.name ?? "");

  if (!email || !password || !name) {
    return Response.json({ error: "Name, email, and password are required" }, { status: 400 });
  }

  const signUpUrl = new URL("/api/auth/sign-up/email", requiredEnv("BETTER_AUTH_URL"));
  const signUpResponse = await auth.handler(
    new Request(signUpUrl, {
      method: "POST",
      headers: {
        "content-type": "application/json",
      },
      body: JSON.stringify({ email, password, name }),
    }),
  );

  if (!signUpResponse.ok) {
    const payload = await signUpResponse.json().catch(() => null);
    return Response.json(payload ?? { error: "Unable to bootstrap admin" }, { status: signUpResponse.status });
  }

  await authDb.query('update "user" set role = $1 where email = $2', [AUTH_ROLES.admin, email]);

  return Response.json({ ok: true });
}
