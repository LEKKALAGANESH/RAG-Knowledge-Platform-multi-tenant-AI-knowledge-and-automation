// Usage: RAG_API_KEY=rag_... node src/search.ts "refund policy" [kb_id]   (scope: search)
import { fromEnv } from "./client.ts";

const [query, kbId] = process.argv.slice(2);
if (!query) throw new Error('usage: node src/search.ts "<query>" [kb_id]');

const { results } = await fromEnv().search(query, kbId ? [kbId] : undefined);
for (const r of results) {
  console.log(`${r.score.toFixed(3)}  ${r.document_name}  ${r.text.slice(0, 80).replace(/\s+/g, " ")}`);
}
console.log(`${results.length} result(s)`);
