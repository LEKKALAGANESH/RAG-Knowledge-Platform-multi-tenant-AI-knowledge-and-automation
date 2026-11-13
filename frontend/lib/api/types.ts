import { z } from "zod";

// ponytail: hand-mirrored from the backend's /openapi.json (A1); regenerate with openapi-typescript when it grows.

export const documentStatus = z.enum(["queued", "processing", "ready", "failed", "deleted"]);
export type DocumentStatus = z.infer<typeof documentStatus>;

export const role = z.enum(["owner", "admin", "editor", "viewer"]);
export type Role = z.infer<typeof role>;

export const knowledgeBase = z.object({
  id: z.string(),
  name: z.string(),
  settings: z.record(z.string(), z.unknown()),
  embedding_index_id: z.string().nullable(),
  created_at: z.string(),
});
export type KnowledgeBase = z.infer<typeof knowledgeBase>;

export const kbDocument = z.object({
  id: z.string(),
  kb_id: z.string(),
  source_id: z.string().nullable(),
  name: z.string(),
  mime: z.string(),
  sha256: z.string(),
  version: z.number(),
  status: documentStatus,
  error: z.string().nullable(),
  warnings: z.array(z.unknown()),
  meta: z.record(z.string(), z.unknown()),
  created_at: z.string(),
});
export type KbDocument = z.infer<typeof kbDocument>;

export const me = z.object({
  user: z.object({ id: z.string(), email: z.string() }),
  org: z.object({ id: z.string(), name: z.string() }),
  role,
});
export type Me = z.infer<typeof me>;

export const page = <T extends z.ZodType>(item: T) =>
  z.object({ items: z.array(item), next_cursor: z.string().nullable() });

export type CreateKbInput = { name: string };

export const chunk = z.object({
  id: z.string(),
  ord: z.number(),
  text: z.string(),
  // Known keys: page, section, table, sheet, range, record. Unknown keys are kept and ignored.
  loc: z.record(z.string(), z.unknown()),
});
export type Chunk = z.infer<typeof chunk>;
export type ChunkLoc = Chunk["loc"];

export const citation = z.object({
  n: z.number(),
  chunk_id: z.string(),
  document_id: z.string(),
  document_name: z.string(),
  kb_id: z.string().optional(), // absent on messages stored before the server sent it
  loc: z.record(z.string(), z.unknown()),
  excerpt: z.string(),
});
export type Citation = z.infer<typeof citation>;

export const conversation = z.object({
  id: z.string(),
  title: z.string(),
  kb_ids: z.array(z.string()),
  created_at: z.string(),
});
export type Conversation = z.infer<typeof conversation>;

export const storedMessage = z.object({
  id: z.string(),
  role: z.string(),
  content: z.string(),
  citations: z.array(citation),
  // Assistant messages only; null on rows stored before the server recorded it.
  abstained: z.boolean().nullable().optional(),
  created_at: z.string(),
});

export const conversationDetail = conversation.extend({ messages: z.array(storedMessage) });

export const searchMode = z.enum(["hybrid", "vector", "keyword"]);
export type SearchMode = z.infer<typeof searchMode>;

export const searchHit = z.object({
  chunk_id: z.string(),
  document_id: z.string(),
  document_name: z.string(),
  kb_id: z.string(),
  text: z.string(),
  loc: z.record(z.string(), z.unknown()),
  score: z.number(),
});
export type SearchHit = z.infer<typeof searchHit>;

export const connectorCategory = z.enum(["storage", "business", "meta", "web", "api", "database"]);
export type ConnectorCategory = z.infer<typeof connectorCategory>;

export const authKind = z.enum(["none", "api_key", "basic", "oauth2", "service_account", "connection_string"]);
export type AuthKind = z.infer<typeof authKind>;

export const connectorMeta = z.object({
  type: z.string(),
  title: z.string(),
  description: z.string(),
  category: connectorCategory,
  auth: authKind,
  config_schema: z.record(z.string(), z.unknown()),
});
export type ConnectorMeta = z.infer<typeof connectorMeta>;

export const sourceStatus = z.enum(["idle", "syncing", "error", "disabled"]);
export type SourceStatus = z.infer<typeof sourceStatus>;

export const source = z.object({
  id: z.string(),
  kb_id: z.string(),
  type: z.string(),
  name: z.string(),
  config: z.record(z.string(), z.unknown()),
  has_secret: z.boolean(),
  status: sourceStatus,
  last_sync_at: z.string().nullable(),
  schedule_minutes: z.number().nullable(),
  created_at: z.string(),
  last_error: z.string().nullable().default(null), // last failed/partial run, plain text
  needs_reconnect: z.boolean().default(false), // OAuth grant revoked or expired
});
export type Source = z.infer<typeof source>;

export const runStatus = z.enum(["running", "succeeded", "failed", "partial"]);
export type RunStatus = z.infer<typeof runStatus>;

export const syncRun = z.object({
  id: z.string(),
  source_id: z.string(),
  status: runStatus,
  started_at: z.string(),
  finished_at: z.string().nullable(),
  stats: z.record(z.string(), z.unknown()),
  error: z.string().nullable(),
});
export type SyncRun = z.infer<typeof syncRun>;

export const testResult = z.object({
  ok: z.boolean(),
  message: z.string(),
  schema: z.object({ tables: z.array(z.object({ name: z.string(), columns: z.array(z.string()) })) }).nullable(),
});
export type TestResult = z.infer<typeof testResult>;

export type SourceInput = {
  type: string;
  name: string;
  config: Record<string, unknown>;
  secret?: string;
  schedule_minutes: number | null;
};
export type SourcePatch = Partial<Omit<SourceInput, "type">> & { enabled?: boolean };

export const agentStatus = z.enum(["planning", "running", "awaiting_approval", "succeeded", "failed", "cancelled"]);
export type AgentStatus = z.infer<typeof agentStatus>;
export const ACTIVE_AGENT_STATUSES: readonly AgentStatus[] = ["planning", "running", "awaiting_approval"];

export const agentRun = z.object({
  id: z.string(),
  goal: z.string(),
  kb_ids: z.array(z.string()),
  status: agentStatus,
  max_steps: z.number(),
  // succeeded: {answer, citations, reports}; failed: {error_code}. Parsed where it is shown.
  result: z.record(z.string(), z.unknown()).nullable(),
  error: z.string().nullable(),
  created_at: z.string(),
  finished_at: z.string().nullable(),
});
export type AgentRun = z.infer<typeof agentRun>;

export const agentStep = z.object({
  id: z.string(),
  n: z.number(),
  kind: z.enum(["plan", "message", "tool_call", "tool_result"]),
  tool: z.string().nullable(),
  input: z.record(z.string(), z.unknown()).nullable(),
  output: z.record(z.string(), z.unknown()).nullable(),
  risk: z.string().nullable(), // read | reversible_write | irreversible_write
  started_at: z.string().nullable(),
  finished_at: z.string().nullable(),
  error: z.string().nullable(),
  created_at: z.string(),
});
export type AgentStep = z.infer<typeof agentStep>;

export const approval = z.object({
  id: z.string(),
  run_id: z.string(),
  step_id: z.string().nullable(),
  tool: z.string(),
  input: z.record(z.string(), z.unknown()),
  status: z.enum(["pending", "approved", "rejected"]),
  decided_by: z.string().nullable(),
  decided_at: z.string().nullable(),
  created_at: z.string(),
});
export type Approval = z.infer<typeof approval>;

export const agentRunDetail = agentRun.extend({ steps: z.array(agentStep), approvals: z.array(approval) });
export type AgentRunDetail = z.infer<typeof agentRunDetail>;

export const agentResult = z.object({
  answer: z.string(),
  citations: z.array(
    z.object({
      chunk_id: z.string(),
      document_name: z.string(),
      loc: z.record(z.string(), z.unknown()),
      // Results stored before the server sent these lack them; null when the chunk was deleted since.
      document_id: z.string().nullish(),
      kb_id: z.string().nullish(),
      excerpt: z.string().nullish(),
    }),
  ),
  reports: z.array(z.string()),
});
export type AgentResult = z.infer<typeof agentResult>;

export const member = z.object({ user_id: z.string(), email: z.string(), role });
export type Member = z.infer<typeof member>;

/** A pending invite as the inviting org's owner/admin sees it. */
export const invite = z.object({ id: z.string(), email: z.string(), role, created_at: z.string(), expires_at: z.string() });
export type Invite = z.infer<typeof invite>;

/** An org the signed-in user belongs to, with their role in it. */
export const orgSummary = z.object({ id: z.string(), name: z.string(), role });
export type OrgSummary = z.infer<typeof orgSummary>;

/** A pending invite addressed to the signed-in user's email. */
export const myInvite = z.object({
  id: z.string(),
  org_id: z.string(),
  org_name: z.string(),
  role,
  created_at: z.string(),
  expires_at: z.string(),
});
export type MyInvite = z.infer<typeof myInvite>;

export const apiKey = z.object({
  id: z.string(),
  name: z.string(),
  prefix: z.string(),
  scopes: z.array(z.string()),
  user_id: z.string(),
  created_at: z.string(),
  expires_at: z.string().nullable(),
  last_used_at: z.string().nullable(),
  revoked_at: z.string().nullable(),
});
export type ApiKey = z.infer<typeof apiKey>;
export const apiKeyCreated = apiKey.extend({ key: z.string() });
export type ApiKeyInput = { name: string; scopes: string[]; expires_at: string | null };

export const auditEntry = z.object({
  id: z.string(),
  action: z.string(),
  actor_user_id: z.string().nullable(),
  actor_api_key_id: z.string().nullable(),
  target_type: z.string().nullable(),
  target_id: z.string().nullable(),
  request_id: z.string().nullable(),
  ip_hash: z.string().nullable(),
  meta: z.record(z.string(), z.unknown()),
  created_at: z.string(),
});
export type AuditEntry = z.infer<typeof auditEntry>;

// Pydantic serializes Decimal as a JSON string.
const money = z.union([z.string(), z.number()]).transform(Number);

const usageTotals = z.object({
  in_tok: z.number(),
  out_tok: z.number(),
  cost_usd: money,
  calls: z.number(),
  unpriced_calls: z.number(),
});
export const usageBucket = usageTotals.extend({ key: z.string().nullable(), label: z.string().nullable().optional() });
export type UsageBucket = z.infer<typeof usageBucket>;
export const usageGroup = z.enum(["day", "kind", "model", "user"]);
export type UsageGroup = z.infer<typeof usageGroup>;
export const usageReport = z.object({
  start: z.string(),
  end: z.string(),
  group_by: usageGroup,
  scope: z.enum(["org", "self"]),
  totals: usageTotals,
  series: z.array(usageBucket),
});
export type UsageReport = z.infer<typeof usageReport>;

export const budgetAction = z.enum(["block", "warn"]);
export type BudgetAction = z.infer<typeof budgetAction>;
export const orgSettings = z.object({
  monthly_budget_usd: z.union([z.string(), z.number()]).nullable().transform((v) => (v === null ? null : String(v))),
  budget_action: budgetAction,
  month_spend_usd: money,
});
export type OrgSettings = z.infer<typeof orgSettings>;
