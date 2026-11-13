import { NextResponse, type NextRequest } from "next/server";
import { ACCESS_COOKIE, REFRESH_COOKIE } from "@/lib/server/session";

// Optimistic check only: real authorization happens in the backend on every API call.
export function proxy(req: NextRequest) {
  if (req.cookies.has(ACCESS_COOKIE) || req.cookies.has(REFRESH_COOKIE)) return NextResponse.next();
  const login = new URL("/login", req.url);
  if (req.nextUrl.pathname !== "/") login.searchParams.set("next", req.nextUrl.pathname);
  return NextResponse.redirect(login);
}

export const config = {
  matcher: ["/((?!login|register|api|_next|favicon.ico).*)"],
};
