import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AgentRunPage } from "@/components/agent-run";
import { Timeline } from "@/components/agent-steps";
import { toCited } from "@/lib/agent";
import type { AgentRunDetail, AgentStep, Approval } from "@/lib/api/types";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useParams: () => ({ id: "r1" }), useRouter: () => ({ push }) }));

const T = "2026-11-12T10:00:00Z";
const json = (status: number, b: unknown) => new Response(JSON.stringify(b), { status, headers: { "Content-Type": "application/json" } });
const sse = (frames: [string, unknown][]) =>
  new Response(frames.map(([e, d]) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`).join(""), { status: 200, headers: { "Content-Type": "text/event-stream" } });
/** An open stream that never sends anything: the run is idle (e.g. waiting for approval). */
const idle = () => new Response(new ReadableStream(), { status: 200, headers: { "Content-Type": "text/event-stream" } });

const step = (n: number, kind: AgentStep["kind"], over: Partial<AgentStep> = {}): AgentStep => ({
  id: `s${n}`, n, kind, tool: null, input: null, output: null, risk: null, started_at: T, finished_at: T, error: null, created_at: T, ...over,
});
const plan = (n: number, text: string) => step(n, "plan", { output: { content: [{ type: "text", text }, { type: "tool_use", id: "tu" }] } });
const call = (n: number, tool: string, risk: string, tu: string, args: unknown) => step(n, "tool_call", { tool, risk, input: { tool_use_id: tu, args } });
const result = (n: number, tool: string, tu: string, data: unknown, error: string | null = null) =>
  step(n, "tool_result", { tool, input: { tool_use_id: tu }, output: error ? null : { data }, error });

const CHUNK = "11111111-2222-3333-4444-555555555555";
const run = (over: Partial<AgentRunDetail> = {}): AgentRunDetail => ({
  id: "r1", goal: "Total the 2025 invoices", kb_ids: [], status: "running", max_steps: 12, result: null, error: null,
  created_at: T, finished_at: null, steps: [], approvals: [], ...over,
});
const approval = (over: Partial<Approval> = {}): Approval => ({
  id: "a1", run_id: "r1", step_id: "s2", tool: "delete_report", input: { report_id: "rep1" }, status: "pending",
  decided_by: null, decided_at: null, created_at: T, ...over,
});

type Handler = (url: string, init?: RequestInit) => Response | Promise<Response>;
function stub(routes: [string, Handler][]) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const hit = routes.find(([prefix]) => url.startsWith(prefix));
    if (!hit) throw new Error(`unexpected fetch ${init?.method ?? "GET"} ${url}`);
    return hit[1](url, init);
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}
const urls = (f: ReturnType<typeof stub>) => f.mock.calls.map((c) => String(c[0]));
const detailReads = (f: ReturnType<typeof stub>) => urls(f).filter((u) => u === "/api/v1/agent/runs/r1").length;

beforeEach(() => push.mockReset());

describe("live run stream", () => {
  it("reconnects with ?after after a timeout and stops on end", async () => {
    const searchOut = { chunks: [{ chunk_id: CHUNK, document_name: "inv-7.pdf", loc: { page: 1 }, text: "not the excerpt source" }] };
    const cite = { chunk_id: CHUNK, document_name: "inv-7.pdf", loc: { page: 1 }, document_id: "d7", kb_id: "k1", excerpt: "Invoice 7: EUR 1,200.50" };
    const done = run({
      status: "succeeded",
      finished_at: T,
      result: { answer: `Total is EUR 1,200.50 [chunk:${CHUNK}].`, citations: [cite], reports: ["rep9"] },
    });
    const streams = [
      sse([["step", plan(1, "Search the invoices first.")], ["timeout", { after: 1 }]]),
      sse([["step", call(2, "search_knowledge", "read", "tu1", { query: "invoice 2025" })], ["step", result(3, "search_knowledge", "tu1", searchOut)], ["status", done], ["end", { status: "succeeded" }]]),
    ];
    const f = stub([
      ["/api/v1/agent/runs/r1/events", () => streams.shift() ?? idle()],
      ["/api/v1/agent/runs/r1", () => json(200, run())],
    ]);
    render(<AgentRunPage />);
    expect(await screen.findByText("Search the invoices first.")).toBeInTheDocument();
    await screen.findByRole("heading", { name: "Answer" });
    const events = urls(f).filter((u) => u.includes("/events"));
    expect(events).toEqual(["/api/v1/agent/runs/r1/events?after=0", "/api/v1/agent/runs/r1/events?after=1"]);
    expect(screen.getByRole("link", { name: /Download report/ })).toHaveAttribute("href", "/api/v1/reports/rep9");
    expect(screen.queryByRole("button", { name: "Cancel run" })).not.toBeInTheDocument();
    expect(detailReads(f)).toBe(1); // the status event carried the result: no re-read on end
    // chunk marker became a chip; the panel shows the excerpt from result.citations and links into the KB
    await userEvent.click(screen.getByRole("button", { name: "Source 1: inv-7.pdf" }));
    expect(await screen.findByText("Invoice 7: EUR 1,200.50")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open document in its knowledge base" })).toHaveAttribute("href", "/kbs/k1?doc=d7");
  });

  it("reconnects after a network drop from the last step it saw", async () => {
    const streams = [sse([["step", plan(1, "Plan A")]]), sse([["status", run({ status: "cancelled", error: "Cancelled by the user." })], ["end", { status: "cancelled" }]])];
    const f = stub([
      ["/api/v1/agent/runs/r1/events", () => streams.shift() ?? idle()],
      ["/api/v1/agent/runs/r1", () => json(200, run())],
    ]);
    render(<AgentRunPage />);
    expect(await screen.findByText("Plan A")).toBeInTheDocument();
    expect(await screen.findByText("Connection dropped. Reconnecting…")).toBeInTheDocument();
    await waitFor(() => expect(urls(f)).toContain("/api/v1/agent/runs/r1/events?after=1"), { timeout: 3000 });
  });
});

describe("approval and step_update events", () => {
  it("an approval event shows the card without re-reading the run", async () => {
    const streams = [sse([["step", plan(1, "p")], ["step", call(2, "delete_report", "irreversible_write", "tu1", { report_id: "rep1" })], ["approval", approval()], ["status", run({ status: "awaiting_approval" })]])];
    const f = stub([
      ["/api/v1/agent/runs/r1/events", () => streams.shift() ?? idle()],
      ["/api/v1/agent/runs/r1", () => json(200, run())],
    ]);
    render(<AgentRunPage />);
    expect(await screen.findByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByText("Waiting for approval")).toBeInTheDocument();
    expect(detailReads(f)).toBe(1); // only the initial load
  });

  it("a server without approval events: pausing with no known approval re-reads the run once", async () => {
    let reads = 0;
    const f = stub([
      ["/api/v1/agent/runs/r1/events", () => (reads > 1 ? idle() : sse([["step", call(2, "delete_report", "irreversible_write", "tu1", {})], ["status", run({ status: "awaiting_approval" })]]))],
      ["/api/v1/agent/runs/r1", () => json(200, reads++ === 0 ? run() : run({ status: "awaiting_approval", steps: [call(2, "delete_report", "irreversible_write", "tu1", {})], approvals: [approval()] }))],
    ]);
    render(<AgentRunPage />);
    expect(await screen.findByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(detailReads(f)).toBe(2);
  });

  it("step_update replaces the step by n", async () => {
    const c = call(2, "calculate", "read", "tu1", { expression: "1+1" });
    const streams = [sse([["step", plan(1, "p")], ["step", { ...c, started_at: null, finished_at: null }], ["step_update", { ...c, error: "This call was interrupted and was not retried." }], ["status", run({ status: "running" })]])];
    stub([
      ["/api/v1/agent/runs/r1/events", () => streams.shift() ?? idle()],
      ["/api/v1/agent/runs/r1", () => json(200, run())],
    ]);
    render(<AgentRunPage />);
    expect(await screen.findByText("This call was interrupted and was not retried.")).toBeInTheDocument();
    expect(within(screen.getByRole("list", { name: "Run steps" })).getAllByRole("listitem")).toHaveLength(2);
  });

  it("updates re-sent on reconnect are idempotent", { timeout: 10_000 }, async () => {
    const c = call(2, "delete_report", "irreversible_write", "tu1", { report_id: "rep1" });
    const waiting = run({ status: "awaiting_approval" });
    const streams = [
      sse([["step", plan(1, "p")], ["step", c], ["approval", approval()], ["status", waiting]]), // then the connection drops
      sse([["step_update", c], ["approval", approval()], ["status", waiting]]),
    ];
    const f = stub([
      ["/api/v1/agent/runs/r1/events", () => streams.shift() ?? idle()],
      ["/api/v1/agent/runs/r1", () => json(200, run())],
    ]);
    render(<AgentRunPage />);
    // a third connect means the re-sent second stream was fully applied
    await waitFor(() => expect(urls(f).filter((u) => u === "/api/v1/agent/runs/r1/events?after=2")).toHaveLength(2), { timeout: 5000 });
    expect(screen.getAllByRole("button", { name: "Approve" })).toHaveLength(1);
    expect(within(screen.getByRole("list", { name: "Run steps" })).getAllByRole("listitem")).toHaveLength(2);
    expect(detailReads(f)).toBe(1);
  });
});

describe("timeline", () => {
  it("renders each kind with its risk, state and collapsed JSON", () => {
    const steps = [
      plan(1, "Find invoices, then report."),
      call(2, "search_knowledge", "read", "tu1", { query: "invoices" }),
      result(3, "search_knowledge", "tu1", { chunks: [{ chunk_id: "c", text: "SECRET CHUNK TEXT" }] }),
      call(4, "calculate", "read", "tu2", { expression: "1/0" }),
      result(5, "calculate", "tu2", null, "Division by zero."),
      call(6, "create_xlsx_report", "reversible_write", "tu3", {}),
      call(7, "delete_report", "irreversible_write", "tu4", { report_id: "rep1" }),
    ];
    render(<Timeline steps={steps} approvals={[approval({ step_id: "s7" })]} runActive />);
    const rows = within(screen.getByRole("list", { name: "Run steps" })).getAllByRole("listitem");
    expect(rows).toHaveLength(5); // results fold into their calls
    expect(rows[0]).toHaveTextContent("Plan");
    expect(rows[1]).toHaveTextContent("Read only");
    expect(rows[1]).toHaveTextContent("Done");
    const output = within(rows[1]).getByText("Output").closest("details");
    expect(output).not.toHaveAttribute("open");
    expect(rows[2]).toHaveTextContent("Failed");
    expect(rows[2]).toHaveTextContent("Division by zero.");
    expect(rows[3]).toHaveTextContent("Reversible write");
    expect(rows[3]).toHaveTextContent("Running");
    expect(rows[4]).toHaveTextContent("Irreversible");
    expect(rows[4]).toHaveTextContent("Waiting for approval");
  });

  it("toCited maps known chunk markers to numbered chips and drops unknown ones", () => {
    const out = toCited({ answer: `A [chunk:${CHUNK}] B [chunk:deadbeef]`, citations: [{ chunk_id: CHUNK, document_name: "x.pdf", loc: {} }], reports: [] });
    expect(out.text).toBe("A [c1] B ");
    // a result stored before the server sent document_id/kb_id/excerpt still renders, without a KB link
    expect(out.citations[0]).toMatchObject({ n: 1, chunk_id: CHUNK, document_name: "x.pdf", document_id: "", kb_id: undefined });
  });
});

describe("approvals and cancel", () => {
  const waiting = (risk: string) =>
    run({ status: "awaiting_approval", steps: [plan(1, "p"), call(2, "delete_report", risk, "tu1", { report_id: "rep1" })], approvals: [approval()] });

  function setup(risk: string, decide: Handler) {
    let decided = false;
    return stub([
      ["/api/v1/agent/runs/r1/events", () => idle()],
      ["/api/v1/approvals/a1", (u, init) => ((decided = true), decide(u, init))],
      ["/api/v1/agent/runs/r1", () => json(200, decided ? { ...waiting(risk), status: "running", approvals: [approval({ status: "approved" })] } : waiting(risk))],
    ]);
  }

  it("approving an irreversible action asks for confirmation first", async () => {
    const f = setup("irreversible_write", () => json(200, approval({ status: "approved" })));
    render(<AgentRunPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
    expect(f.mock.calls.some((c) => String(c[0]).startsWith("/api/v1/approvals"))).toBe(false);
    await userEvent.click(await screen.findByRole("button", { name: "Approve and run" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument());
    const post = f.mock.calls.find((c) => String(c[0]) === "/api/v1/approvals/a1");
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({ decision: "approve" });
    expect(detailReads(f)).toBe(1); // the returned approval replaced the pending one
  });

  it("reject sends the decision without a confirm", async () => {
    const f = setup("reversible_write", () => json(200, approval({ status: "rejected" })));
    render(<AgentRunPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Reject" }));
    await waitFor(() => expect(f.mock.calls.some((c) => String(c[0]) === "/api/v1/approvals/a1")).toBe(true));
    const post = f.mock.calls.find((c) => String(c[0]) === "/api/v1/approvals/a1");
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({ decision: "reject" });
  });

  it("409 already_decided shows a notice and reloads the run", async () => {
    setup("reversible_write", () => json(409, { error: { code: "already_decided", message: "Already decided.", request_id: "r" } }));
    render(<AgentRunPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
    expect(await screen.findByText(/Someone already decided this request/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument());
  });

  it("cancel updates the run; 409 run_finished says so", async () => {
    let first = true;
    stub([
      ["/api/v1/agent/runs/r1/events", () => idle()],
      ["/api/v1/agent/runs/r1/cancel", () => {
        if (first) return (first = false), json(409, { error: { code: "run_finished", message: "x", request_id: "r" } });
        return json(200, run({ status: "cancelled", error: "Cancelled by the user." }));
      }],
      ["/api/v1/agent/runs/r1", () => json(200, run())],
    ]);
    render(<AgentRunPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Cancel run" }));
    expect(await screen.findByText("The run had already finished.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Cancel run" }));
    expect(await screen.findByText("Cancelled by the user.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel run" })).not.toBeInTheDocument();
  });

  it("llm_not_configured shows the admin explanation and Try again starts a new run", async () => {
    const failed = run({ status: "failed", result: { error_code: "llm_not_configured" }, error: "No AI model is configured." });
    stub([
      ["/api/v1/agent/runs/r1", () => json(200, failed)],
      ["/api/v1/agent/runs", () => json(202, run({ id: "r2", status: "planning", steps: undefined, approvals: undefined }))],
    ]);
    render(<AgentRunPage />);
    expect(await screen.findByText("Agent runs need a language model")).toBeInTheDocument();
    expect(screen.getByText("ANTHROPIC_API_KEY")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/agent/r2"));
  });
});
