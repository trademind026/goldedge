// Middleware removed to disable authentication redirects globally
import { NextResponse } from "next/server";
export function middleware() {
  return NextResponse.next();
}
export const config = {
  matcher: [],
};
