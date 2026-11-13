import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import gdrive from "./fixtures/connector-gdrive.json";
import { Integrations } from "@/components/integrations";
import { KbSources, SourceRow } from "@/components/kb-sources";
import { canConnect, SourceForm } from "@/components/source-form";
import { browserNav } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { ConnectorMeta, Source } from "@/lib/api/types";
import { OAUTH_RETURN_ERRORS, oauthStartMessage, safeAuthorizeUrl } from "@/lib/oauth";

let search = "";
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(search),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

const connector = gdrive as ConnectorMeta;
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const err = (status: number, code: string, message: string) => json(status, { error: { code, message, request_id: "r1" } });
const GOOGLE = "https://accounts.google.com/o/oauth2/v2/auth?client_id=test&code_challenge=abc&code_challenge_method=S256&state=xyz";

const driveSource: Source = {
  id: "11111111-1111-4111-8111-111111111111", kb_id: "kb-1", type: "gdrive", name: "Team Drive", has_secret: true, status: "error",
  config: { folder_ids: [], recursive: true, include_shared_drives: false, mime_allowlist: [], max_files: 10000, full_sync_every: 12 },
  last_sync_at: null, schedule_minutes: null, created_at: "2026-11-12T00:00:00Z", last_error: null, needs_reconnect: false,
};

function setUrl(path: string) {
  window.history.replaceState(null, "", path);
  search = window.location.search;
}

beforeEach(() => setUrl("/"));

describe("safeAuthorizeUrl", () => {
  it("allows https on the connector's allowlisted host only", () => {
    expect(safeAuthorizeUrl("gdrive", GOOGLE)).toBe(GOOGLE);
    for (const bad of [
      GOOGLE.replace("https:", "http:"),
      "https://evil.example.com/o/oauth2/v2/auth",
      "https://accounts.google.com.evil.example.com/auth",
      "https://user:pw@accounts.google.com/auth",
      "https://accounts.google.com:8443/auth",
      "javascript:alert(1)",
      "//accounts.google.com/auth",
      "not a url",
    ])
      expect(safeAuthorizeUrl("gdrive", bad)).toBeNull();
    expect(safeAuthorizeUrl("box", GOOGLE)).toBeNull(); // no allowlist for this type
    expect(safeAuthorizeUrl("dropbox", GOOGLE)).toBeNull(); // another type's host
    expect(safeAuthorizeUrl("toString", GOOGLE)).toBeNull(); // prototype keys aren't providers
  });

  it("enables Connect only for oauth2 types with an allowlist", () => {
    expect(canConnect(connector)).toBe(true);
    expect(canConnect({ ...connector, type: "box" })).toBe(false);
    expect(canConnect({ ...connector, type: "onedrive" })).toBe(true);
    expect(canConnect({ ...connector, type: "dropbox" })).toBe(true);
  });

  it.each([
    ["onedrive", "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id=c&state=s", "login.microsoftonline.com"],
    ["dropbox", "https://www.dropbox.com/oauth2/authorize?client_id=c&state=s", "www.dropbox.com"],
  ])("allows %s only on its exact https host", (type, good, host) => {
    expect(safeAuthorizeUrl(type, good)).toBe(good);
    for (const bad of [
      good.replace("https:", "http:"),
      good.replace(host, `${host}.evil.com`),
      good.replace(host, `${host}@evil.com`),
      good.replace(host, `evil-${host}`),
      good.replace(host, host.split(".").slice(1).join(".")), // parent domain: no suffix matching either way
      GOOGLE,
    ])
      expect(safeAuthorizeUrl(type, bad)).toBeNull();
  });

  it.each([
    ["onedrive", "OneDrive", /operator must set MICROSOFT_OAUTH_CLIENT_ID and MICROSOFT_OAUTH_CLIENT_SECRET/],
    ["dropbox", "Dropbox", /operator must set DROPBOX_OAUTH_CLIENT_ID and DROPBOX_OAUTH_CLIENT_SECRET/],
  ])("names %s's operator settings when not configured", (type, title, text) => {
    expect(oauthStartMessage(new ApiError(422, "oauth_not_configured", "x"), { ...connector, type, title })).toMatch(text);
  });
});

describe("SourceForm (oauth2)", () => {
  it("has no secret fields and starts sign-in, then navigates to the allowlisted URL", async () => {
    const fetchMock = vi.fn().mockResolvedValue(json(200, { authorize_url: GOOGLE }));
    vi.stubGlobal("fetch", fetchMock);
    const assign = vi.spyOn(browserNav, "assign").mockImplementation(() => undefined);
    render(<SourceForm connector={connector} kbId="kb-1" onSaved={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.queryByText("Credentials")).toBeNull();
    expect(document.querySelector('input[type="password"]')).toBeNull();
    expect(screen.queryByLabelText(/secret|token|password|key/i)).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Continue to Google Drive" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith(GOOGLE));
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/kbs/kb-1/sources/oauth/start");
    const body = JSON.parse(init.body as string);
    expect(body).toMatchObject({ type: "gdrive", name: "Google Drive", schedule_minutes: null, config: { recursive: true, max_files: 10000 } });
    expect(body.secret).toBeUndefined();
  });

  it.each(["http://accounts.google.com/auth", "https://evil.example.com/auth", "javascript:alert(document.cookie)"])(
    "refuses to navigate to %s",
    async (bad) => {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json(200, { authorize_url: bad })));
      const assign = vi.spyOn(browserNav, "assign").mockImplementation(() => undefined);
      render(<SourceForm connector={connector} kbId="kb-1" onSaved={vi.fn()} onCancel={vi.fn()} />);
      await userEvent.click(screen.getByRole("button", { name: "Continue to Google Drive" }));
      expect(await screen.findByText(/unexpected sign-in address for Google Drive/)).toBeInTheDocument();
      expect(assign).not.toHaveBeenCalled();
      expect(screen.getByRole("button", { name: "Continue to Google Drive" })).toBeEnabled();
    },
  );

  it.each([
    [422, "oauth_not_configured", /operator must set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET/],
    [503, "oauth_unavailable", /briefly unavailable/],
    [422, "unknown_connector", /isn't enabled on this server/],
    [422, "not_oauth", /doesn't use a sign-in flow/],
    [404, "not_found", /knowledge base no longer exists/],
  ])("shows a readable message for %s %s", async (status, code, text) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(err(status, code, "raw server text")));
    const assign = vi.spyOn(browserNav, "assign").mockImplementation(() => undefined);
    render(<SourceForm connector={connector} kbId="kb-1" onSaved={vi.fn()} onCancel={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Continue to Google Drive" }));
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(assign).not.toHaveBeenCalled();
  });

  it("falls back to the server message for an unknown code", () => {
    expect(oauthStartMessage(new ApiError(422, "something_new", "Server says no."), connector)).toBe("Server says no.");
  });

  it("edits an oauth2 source with a PATCH and no secret", async () => {
    const fetchMock = vi.fn().mockResolvedValue(json(200, driveSource));
    vi.stubGlobal("fetch", fetchMock);
    render(<SourceForm connector={connector} kbId="kb-1" source={driveSource} onSaved={vi.fn()} onCancel={vi.fn()} />);
    expect(document.querySelector('input[type="password"]')).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Test and save changes" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`/api/v1/sources/${driveSource.id}`);
    expect(JSON.parse(init.body as string)).toEqual({ name: "Team Drive", schedule_minutes: null });
  });
});

describe("SourceRow Reconnect", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(json(200, { items: [] })))));
  const revoked: Source = { ...driveSource, needs_reconnect: true, last_error: "Sync failed: Google Drive access was revoked or has expired. Reconnect Google Drive." };

  it("is offered only when the source needs a new sign-in", () => {
    const { rerender } = render(<SourceRow source={revoked} connector={connector} canEdit onChanged={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Reconnect Google Drive" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /as new source/ })).toBeNull();
    rerender(<SourceRow source={{ ...revoked, needs_reconnect: false }} connector={connector} canEdit onChanged={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /Reconnect/ })).toBeNull(); // plain sync error: no sign-in fixes it
    rerender(<SourceRow source={{ ...revoked, type: "box" }} connector={{ ...connector, type: "box" }} canEdit onChanged={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /Reconnect/ })).toBeNull(); // no allowlist entry
    rerender(<SourceRow source={revoked} connector={connector} canEdit={false} onChanged={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /Reconnect/ })).toBeNull();
  });

  it("shows the source's last error as text", () => {
    const html = "<img src=x onerror=alert(1)>";
    const { rerender } = render(<SourceRow source={revoked} connector={connector} canEdit onChanged={vi.fn()} />);
    expect(screen.getByText(/Sign-in needed: .*access was revoked/)).toBeInTheDocument();
    rerender(<SourceRow source={{ ...driveSource, status: "idle", last_error: `2 item(s) failed ${html}` }} connector={connector} canEdit onChanged={vi.fn()} />);
    expect(screen.getByText(/Last sync: 2 item\(s\) failed/)).toHaveTextContent(html);
    expect(document.querySelector("img")).toBeNull();
    rerender(<SourceRow source={{ ...driveSource, status: "idle" }} connector={connector} canEdit onChanged={vi.fn()} />);
    expect(screen.queryByText(/Last sync:|Sign-in needed:/)).toBeNull();
  });

  it("asks the API to reconnect this source in place, then goes to the allowlisted sign-in page", async () => {
    const fetchMock = vi.fn((url: string) => Promise.resolve(url.endsWith("/oauth/reconnect") ? json(200, { authorize_url: GOOGLE }) : json(200, { items: [] })));
    vi.stubGlobal("fetch", fetchMock);
    const assign = vi.spyOn(browserNav, "assign").mockImplementation(() => undefined);
    render(<SourceRow source={revoked} connector={connector} canEdit onChanged={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Reconnect Google Drive" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith(GOOGLE));
    expect(screen.queryByRole("dialog")).toBeNull();
    const call = fetchMock.mock.calls.find((c) => c[0].endsWith("/oauth/reconnect")) as unknown as [string, RequestInit];
    expect(call[0]).toBe(`/api/v1/sources/${driveSource.id}/oauth/reconnect`);
    expect(call[1].method).toBe("POST");
    expect(fetchMock.mock.calls.some((c) => c[0].endsWith("/oauth/start"))).toBe(false); // no second source
  });

  it.each([
    [json(200, { authorize_url: "https://evil.example.com/auth" }), /unexpected sign-in address for Google Drive/],
    [err(422, "oauth_not_configured", "raw"), /operator must set GOOGLE_OAUTH_CLIENT_ID/],
    [err(404, "not_found", "raw"), /This source no longer exists/],
  ])("does not navigate and explains when the reconnect can't start (%#)", async (response, text) => {
    vi.stubGlobal("fetch", vi.fn((url: string) => Promise.resolve(url.endsWith("/oauth/reconnect") ? response : json(200, { items: [] }))));
    const assign = vi.spyOn(browserNav, "assign").mockImplementation(() => undefined);
    render(<SourceRow source={revoked} connector={connector} canEdit onChanged={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Reconnect Google Drive" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(text);
    expect(assign).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Reconnect Google Drive" })).toBeEnabled();
  });
});

describe("OAuth return", () => {
  const sourcesApi = (items: Source[]) =>
    vi.fn((url: string) => Promise.resolve(url.endsWith("/connectors") ? json(200, { items: [connector] }) : json(200, { items: url.endsWith("/sources") ? items : [] })));

  it("connected: success message, highlighted row, params stripped", async () => {
    setUrl(`/kbs/kb-1?tab=sources&connected=${driveSource.id}`);
    const replace = vi.spyOn(window.history, "replaceState");
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll; // jsdom has no layout
    vi.stubGlobal("fetch", sourcesApi([{ ...driveSource, status: "idle" }]));
    render(<KbSources kbId="kb-1" canEdit />);
    expect(await screen.findByRole("status")).toHaveTextContent("“Team Drive” is connected. Its first sync has started.");
    expect(screen.queryByText(/Sync now\./)).toBeNull();
    expect(screen.getByText("New")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Team Drive" }).closest("li")).toHaveClass("ring-2");
    expect(scroll).toHaveBeenCalled();
    expect(replace).toHaveBeenCalled();
    expect(window.location.search).toBe("?tab=sources");
    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText(/is connected/)).toBeNull();
  });

  it("reconnected: success message, highlighted row, params stripped", async () => {
    setUrl(`/kbs/kb-1?tab=sources&reconnected=${driveSource.id}`);
    Element.prototype.scrollIntoView = vi.fn();
    vi.stubGlobal("fetch", sourcesApi([{ ...driveSource, status: "idle" }]));
    render(<KbSources kbId="kb-1" canEdit />);
    expect(await screen.findByRole("status")).toHaveTextContent("“Team Drive” is reconnected.");
    expect(screen.getByText("Reconnected")).toBeInTheDocument();
    expect(screen.queryByText("New")).toBeNull();
    expect(screen.getByRole("heading", { name: "Team Drive" }).closest("li")).toHaveClass("ring-2");
    expect(window.location.search).toBe("?tab=sources");
  });

  it.each(Object.keys(OAUTH_RETURN_ERRORS).filter((k) => k !== "invalid_state"))("oauth_error=%s: readable message and retry link", async (code) => {
    setUrl(`/kbs/kb-1?tab=sources&oauth_error=${code}`);
    vi.stubGlobal("fetch", sourcesApi([]));
    render(<KbSources kbId="kb-1" canEdit />);
    expect(await screen.findByRole("alert")).toHaveTextContent(OAUTH_RETURN_ERRORS[code]);
    expect(screen.getByRole("link", { name: "Connect again" })).toHaveAttribute("href", "/integrations?kb=kb-1");
    expect(window.location.search).toBe("?tab=sources");
  });

  it("an unknown oauth_error value is shown as generic text, never echoed", async () => {
    setUrl("/kbs/kb-1?tab=sources&oauth_error=%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E");
    vi.stubGlobal("fetch", sourcesApi([]));
    render(<KbSources kbId="kb-1" canEdit />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Connecting didn't finish");
    expect(document.body.innerHTML).not.toContain("onerror");
    expect(document.querySelector("img")).toBeNull();
  });

  it("invalid_state on the integrations page", async () => {
    setUrl("/integrations?tab=sources&oauth_error=invalid_state");
    vi.stubGlobal("fetch", vi.fn((url: string) =>
      Promise.resolve(url.endsWith("/connectors") ? json(200, { items: [connector] }) : url.endsWith("/me") ? json(200, { user: { id: "u1", email: "e@example.com" }, org: { id: "o1", name: "Org" }, role: "editor" }) : json(200, { items: [], next_cursor: null })),
    ));
    render(<Integrations />);
    expect(await screen.findByRole("alert")).toHaveTextContent("expired or was already used");
    expect(window.location.search).toBe("?tab=sources");
  });
});
