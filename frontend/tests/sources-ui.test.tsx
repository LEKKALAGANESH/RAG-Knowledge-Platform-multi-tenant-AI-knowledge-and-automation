import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import registry from "./fixtures/connectors.json";
import { SourceRow } from "@/components/kb-sources";
import { SourceForm } from "@/components/source-form";
import type { ConnectorMeta, Source, SyncRun } from "@/lib/api/types";
import { POLL_MIN_MS } from "@/lib/use-poll";

const connector = (type: string) => registry.items.find((c) => c.type === type) as ConnectorMeta;
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const err = (status: number, code: string, message: string) => json(status, { error: { code, message, request_id: "r1" } });
const bodyOf = (call: unknown[]) => JSON.parse((call[1] as RequestInit).body as string);
const deleted = (calls: unknown[][]) => calls.some((c) => (c[1] as RequestInit | undefined)?.method === "DELETE");

const s3Source: Source = {
  id: "s-1", kb_id: "kb-1", type: "s3", name: "Team bucket", has_secret: true, status: "idle",
  config: { bucket: "team-docs", prefix: "", region: "us-east-1", endpoint_url: null, include_extensions: [], max_objects: 10000, allow_private_endpoint: false },
  last_sync_at: null, schedule_minutes: null, created_at: "2026-11-12T00:00:00Z", last_error: null, needs_reconnect: false,
};
const run = (status: SyncRun["status"]): SyncRun => ({
  id: "r-1", source_id: "s-1", status, started_at: "2026-11-12T00:00:00Z", finished_at: null,
  stats: { discovered: 3, added: 3 }, error: null,
});

describe("SourceForm", () => {
  it("maps the server's 422 field errors back onto the matching inputs", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      err(422, "invalid_config", "Invalid configuration: start_urls: Value error, Only http and https URLs are allowed."),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<SourceForm connector={connector("website")} kbId="kb-1" onSaved={vi.fn()} onCancel={vi.fn()} />);
    await userEvent.type(screen.getByLabelText("Start URLs"), "ftp://example.com");
    await userEvent.click(screen.getByRole("button", { name: "Test and connect" }));
    await waitFor(() => expect(screen.getByLabelText("Start URLs")).toHaveAttribute("aria-invalid", "true"));
    expect(screen.getByText("Value error, Only http and https URLs are allowed.")).toBeInTheDocument();
    const body = bodyOf(fetchMock.mock.calls[0]);
    expect(body).toMatchObject({ type: "website", config: { start_urls: ["ftp://example.com"] } });
    expect(body.secret).toBeUndefined();
  });

  it("shows the server's readable test failure inline and saves nothing", async () => {
    const msg = "Private endpoints are disabled on this server. An operator must set CONNECTORS_ALLOW_PRIVATE_ENDPOINTS=true (self-hosted/BYOC only) before a source may use allow_private_endpoint.";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(err(422, "private_endpoints_disabled", msg)));
    const onSaved = vi.fn();
    render(<SourceForm connector={connector("s3")} kbId="kb-1" onSaved={onSaved} onCancel={vi.fn()} />);
    await userEvent.type(screen.getByLabelText("Bucket"), "rag-documents");
    await userEvent.type(screen.getByLabelText("Access key ID"), "AKIA1");
    await userEvent.type(screen.getByLabelText("Secret access key"), "s3cr3t-value");
    await userEvent.click(screen.getByRole("button", { name: "Test and connect" }));
    expect(await screen.findByText(msg)).toBeInTheDocument();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("never renders a saved secret, keeps it when left blank, and clears typed secrets after save", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(json(200, s3Source)));
    vi.stubGlobal("fetch", fetchMock);
    const onSaved = vi.fn();
    render(<SourceForm connector={connector("s3")} kbId="kb-1" source={s3Source} onSaved={onSaved} onCancel={vi.fn()} />);
    expect(screen.getByText(/Credentials are saved and never shown again/)).toBeInTheDocument();
    expect(screen.getByLabelText("Secret access key")).toHaveValue("");

    await userEvent.click(screen.getByRole("button", { name: "Test and save changes" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    // Unchanged config and a blank secret: neither is sent, so the saved secret is kept and nothing is re-tested.
    expect(bodyOf(fetchMock.mock.calls[0])).toEqual({ name: "Team bucket", schedule_minutes: null });

    await userEvent.type(screen.getByLabelText("Access key ID"), "AKIA2");
    await userEvent.type(screen.getByLabelText("Secret access key"), "new-s3cr3t");
    await userEvent.click(screen.getByRole("button", { name: "Test and save changes" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(2));
    expect(JSON.parse(bodyOf(fetchMock.mock.calls[1]).secret)).toEqual({ access_key_id: "AKIA2", secret_access_key: "new-s3cr3t" });
    expect(screen.getByLabelText("Secret access key")).toHaveValue("");
    expect(document.body.innerHTML).not.toContain("new-s3cr3t");
  });
});

describe("SourceRow", () => {
  it("polls run history while a run is running and stops once it finishes", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const queue = [run("running"), run("running"), run("succeeded")];
      const fetchMock = vi.fn(() => Promise.resolve(json(200, { items: [queue.shift() ?? run("succeeded")] })));
      vi.stubGlobal("fetch", fetchMock);
      const onChanged = vi.fn();
      render(<SourceRow source={{ ...s3Source, type: "website" }} connector={connector("website")} canEdit onChanged={onChanged} />);
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
      expect(await screen.findByText("Syncing")).toBeInTheDocument();
      await act(() => vi.advanceTimersByTimeAsync(POLL_MIN_MS));
      await act(() => vi.advanceTimersByTimeAsync(POLL_MIN_MS * 2));
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
      await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1)); // the finished run refreshes the source list once
      await act(() => vi.advanceTimersByTimeAsync(60_000));
      expect(fetchMock).toHaveBeenCalledTimes(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it("deletes only after a confirm dialog that names the source and explains document removal", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) =>
      Promise.resolve(init?.method === "DELETE" ? new Response(null, { status: 204 }) : json(200, { items: [] })),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onChanged = vi.fn();
    render(<SourceRow source={s3Source} connector={connector("s3")} canEdit onChanged={onChanged} />);
    await userEvent.click(screen.getByRole("button", { name: "Disconnect" }));
    const dialog = await screen.findByRole("dialog", { name: "Disconnect “Team bucket”?" });
    expect(dialog).toHaveTextContent("every document it brought into this knowledge base is removed");
    expect(deleted(fetchMock.mock.calls)).toBe(false);

    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(deleted(fetchMock.mock.calls)).toBe(false);

    await userEvent.click(screen.getByRole("button", { name: "Disconnect" }));
    await userEvent.click(await screen.findByRole("button", { name: "Disconnect source" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/sources/s-1", expect.objectContaining({ method: "DELETE" }));
  });

  it("shows the postgres schema browser from a test result", async () => {
    vi.stubGlobal("fetch", vi.fn((url: string) => Promise.resolve(
      url.endsWith("/test")
        ? json(200, { ok: true, message: "Tables: faq (id, q, a)", schema: { tables: [{ name: "faq", columns: ["id", "q", "a"] }] } })
        : json(200, { items: [] }),
    )));
    render(<SourceRow source={{ ...s3Source, type: "postgres" }} connector={connector("postgres")} canEdit onChanged={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Test connection" }));
    expect(await screen.findByText(/Connection works/)).toBeInTheDocument();
    expect(screen.getByText("faq")).toBeInTheDocument();
    expect(screen.getByText("id, q, a")).toBeInTheDocument();
  });
});
