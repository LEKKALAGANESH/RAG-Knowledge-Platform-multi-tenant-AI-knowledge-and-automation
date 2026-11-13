// Usage: RAG_API_KEY=rag_... node src/upload-and-ask.ts <kb_id> "<question>" [file]
// Uploads `file` (if given), waits until it is indexed, then streams the answer with citations.
// Scopes: documents:write + kb:read (upload), answer.
import { readFile } from "node:fs/promises";
import { basename } from "node:path";
import { fromEnv, RagError, type Citation } from "./client.ts";

const [kbId, question, file] = process.argv.slice(2);
if (!kbId || !question) throw new Error('usage: node src/upload-and-ask.ts <kb_id> "<question>" [file]');
const rag = fromEnv();

if (file) {
  const doc = await rag.upload(kbId, new Blob([await readFile(file)]), basename(file));
  console.log(`uploaded ${doc.name} (${doc.status}), waiting for indexing...`);
  await rag.waitUntilReady(doc.id);
}

const citations: Citation[] = [];
try {
  for await (const e of rag.answer(question, [kbId])) {
    if (e.event === "token") process.stdout.write(e.data.text);
    else if (e.event === "citation") citations.push(e.data);
    else if (e.event === "error") throw new Error(`stream ended: ${e.data.code} ${e.data.message}`);
    else if (e.event === "done" && e.data.abstained) console.log("\n(no answer in your sources)");
  }
} catch (err) {
  if (err instanceof RagError && err.status === 429) console.error(`rate limited, retry in ${err.retryAfter}s`);
  throw err;
}
console.log("\n");
for (const c of citations) console.log(`[c${c.n}] ${c.document_name}: ${c.excerpt.slice(0, 100)}`);
