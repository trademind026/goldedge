import { toNextJsHandler } from "better-auth/next-js";
import { auth } from "@/lib/auth";

const handler = async (request: Request) => {
  const url = new URL(request.url);

  if (url.pathname.endsWith("/sign-up/email")) {
    return Response.json({ error: "Public signup is disabled" }, { status: 404 });
  }

  return auth.handler(request);
};

export const { GET, POST } = toNextJsHandler(handler);
