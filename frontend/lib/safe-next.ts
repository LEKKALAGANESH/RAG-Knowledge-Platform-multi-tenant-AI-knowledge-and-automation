const FALLBACK = "/dashboard";

/**
 * Same-origin target for `?next=`, else the dashboard. Resolves with the URL parser instead of
 * string checks because browsers treat `\` as `/`, so `/\evil.com` would leave the site.
 * A same-origin URL can still normalize to a `//host` path (`/.//evil.com`), which is
 * protocol-relative once returned as a relative target, so those are rejected too.
 */
export function safeNext(raw: string | null, origin: string): string {
  if (!raw) return FALLBACK;
  try {
    const url = new URL(raw, origin);
    return url.origin === origin && !url.pathname.startsWith("//") ? url.pathname + url.search + url.hash : FALLBACK;
  } catch {
    return FALLBACK;
  }
}
