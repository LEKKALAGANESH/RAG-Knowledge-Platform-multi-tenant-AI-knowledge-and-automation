// Usage: RAG_API_KEY=rag_... node src/agent-run-with-approval.ts <kb_id> "<goal>" [--reject]
// Starts an agent run, streams its steps, answers the approval prompt, then saves any XLSX report.
// Scope: agent:run.
import { writeFile } from "node:fs/promises";
import { fromEnv, type Run } from "./client.ts";

const [kbId, goal, flag] = process.argv.slice(2);
if (!kbId || !goal) throw new Error('usage: node src/agent-run-with-approval.ts <kb_id> "<goal>" [--reject]');
const rag = fromEnv();

const run = await rag.json<Run>("/v1/agent/runs", "POST", { goal, kb_ids: [kbId] });
console.log(`run ${run.id} started`);

let after = 0;
let finished = false;
while (!finished) {
  // Reconnect loop: the server ends a stream with `timeout` after 15 min, a network drop just ends it.
  try {
    for await (const e of rag.agentEvents(run.id, after)) {
      if (e.event === "step") {
        after = Math.max(after, e.data.n);
        console.log(`step ${e.data.n} ${e.data.kind}${e.data.tool ? ` ${e.data.tool}` : ""}`);
      } else if (e.event === "approval" && e.data.status === "pending") {
        const decision = flag === "--reject" ? "reject" : "approve";
        console.log(`approval needed for ${e.data.tool} ${JSON.stringify(e.data.input)} -> ${decision}`);
        await rag.json(`/v1/approvals/${e.data.id}`, "POST", { decision });
      } else if (e.event === "end") {
        finished = true;
      }
    }
  } catch (err) {
    if (!(err instanceof TypeError)) throw err; // fetch network errors are TypeErrors: reconnect
  }
}

const done = await rag.json<Run>(`/v1/agent/runs/${run.id}`);
console.log(`\n${done.status}: ${done.result?.answer ?? done.result?.error_code ?? ""}`);
for (const reportId of done.result?.reports ?? []) {
  const res = await rag.raw(`/v1/reports/${reportId}`);
  await writeFile(`report-${reportId}.xlsx`, Buffer.from(await res.arrayBuffer()));
  console.log(`saved report-${reportId}.xlsx`);
}
