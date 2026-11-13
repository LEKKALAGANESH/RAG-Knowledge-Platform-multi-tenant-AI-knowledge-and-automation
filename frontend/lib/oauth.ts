import { ApiError } from "@/lib/api/errors";
import type { ConnectorMeta } from "@/lib/api/types";

/**
 * Per connector type: the hosts its sign-in page may live on, and the operator settings it needs.
 * The browser only leaves for an https URL on one of these hosts, so a compromised or misconfigured
 * backend can't send people to a look-alike sign-in page. Add an entry when a new OAuth connector ships.
 */
export const OAUTH_PROVIDERS: Record<string, { hosts: string[]; env: string }> = {
  gdrive: { hosts: ["accounts.google.com"], env: "GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET" },
  onedrive: { hosts: ["login.microsoftonline.com"], env: "MICROSOFT_OAUTH_CLIENT_ID and MICROSOFT_OAUTH_CLIENT_SECRET" },
  dropbox: { hosts: ["www.dropbox.com"], env: "DROPBOX_OAUTH_CLIENT_ID and DROPBOX_OAUTH_CLIENT_SECRET" },
};

export const oauthSupported = (type: string) => Object.hasOwn(OAUTH_PROVIDERS, type);

/** The authorize URL normalised, or null when it isn't https on an allowlisted host. */
export function safeAuthorizeUrl(type: string, raw: string): string | null {
  const provider = oauthSupported(type) ? OAUTH_PROVIDERS[type] : undefined;
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  const ok = provider && url.protocol === "https:" && !url.username && !url.password && !url.port && provider.hosts.includes(url.hostname);
  return ok ? url.href : null;
}

/** Readable text for a failed `POST …/sources/oauth/start`. */
export function oauthStartMessage(err: unknown, connector: ConnectorMeta): string {
  if (!(err instanceof ApiError)) return "Couldn't start the sign-in. Try again.";
  switch (err.code) {
    case "oauth_not_configured": {
      const env = OAUTH_PROVIDERS[connector.type]?.env ?? "the OAuth client settings";
      return `${connector.title} sign-in isn't set up on this server. An operator must set ${env} (and register this web app's /oauth/finish address as the redirect URL with the provider), then restart the API.`;
    }
    case "oauth_unavailable":
      return "Connecting is briefly unavailable on the server. Wait a moment and try again.";
    case "unknown_connector":
      return `${connector.title} isn't enabled on this server. Ask an operator to turn it on.`;
    case "not_oauth":
      return `${connector.title} doesn't use a sign-in flow. Reload the page and try again.`;
    case "not_found":
      return "That knowledge base no longer exists. Pick another one.";
    default:
      return err.message;
  }
}

/** Messages for the `oauth_error` the API callback sends back. Unknown values get a generic text. */
export const OAUTH_RETURN_ERRORS: Record<string, string> = {
  denied: "Sign-in was cancelled or access wasn't allowed, so nothing was connected.",
  exchange_failed: "The provider didn't confirm the sign-in, so nothing was connected. Try again.",
  scope_denied: "Some requested access was unticked on the consent screen. Allow all requested access to connect.",
  forbidden: "You no longer have editor access to this knowledge base, so nothing was connected.",
  unavailable: "This integration is no longer enabled on the server, so nothing was connected.",
  invalid_state: "That sign-in link expired or was already used (links last 10 minutes). Start the connection again.",
  source_gone: "That source was removed or moved while you were signing in, so nothing was changed.",
  oauth_not_yours: "That sign-in was started from another account or browser, so nothing was connected. Start the connection again here.",
};
const GENERIC_RETURN_ERROR = "Connecting didn't finish, so nothing was connected. Try again.";

export type OAuthReturn = { connected: string | null; reconnected: string | null; error: string | null };
export const NO_OAUTH_RETURN: OAuthReturn = { connected: null, reconnected: null, error: null };
const RETURN_PARAMS = ["connected", "reconnected", "oauth_error"];

/** Reads `connected` / `reconnected` / `oauth_error` from the return URL. Values are only ever rendered as text. */
export function readOAuthReturn(params: URLSearchParams): OAuthReturn {
  const code = params.get("oauth_error");
  return {
    connected: params.get("connected"),
    reconnected: params.get("reconnected"),
    error: code === null ? null : Object.hasOwn(OAUTH_RETURN_ERRORS, code) ? OAUTH_RETURN_ERRORS[code] : GENERIC_RETURN_ERROR,
  };
}

/** The finish call's redirect when it is one of the API's own return paths; null for anything else. */
export function safeFinishRedirect(path: string): string | null {
  return /^\/kbs\/[0-9a-f-]{36}\?tab=sources&(connected|reconnected|oauth_error)=[A-Za-z0-9_-]{1,64}$/.test(path) ? path : null;
}

/** Where a failed finish call sends the browser: the integrations page shows the matching text. */
export function finishErrorPath(err: unknown): string {
  const code = err instanceof ApiError && Object.hasOwn(OAUTH_RETURN_ERRORS, err.code) ? err.code : "failed";
  return `/integrations?oauth_error=${code}`;
}

/** Drops the one-shot return params so a reload or Back doesn't show the message again. */
export function stripOAuthParams() {
  const url = new URL(window.location.href);
  if (!RETURN_PARAMS.some((p) => url.searchParams.has(p))) return;
  for (const p of RETURN_PARAMS) url.searchParams.delete(p);
  window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash);
}
