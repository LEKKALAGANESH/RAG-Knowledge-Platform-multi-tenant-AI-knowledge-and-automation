import { z } from "zod";
import { reportBudget } from "@/lib/budget";
import { ApiError, networkError, toApiError } from "./errors";
import {
  agentRun,
  apiKey,
  apiKeyCreated,
  auditEntry,
  member,
  invite,
  myInvite,
  orgSummary,
  orgSettings,
  usageReport,
  type ApiKeyInput,
  type BudgetAction,
  role,
  type Role,
  type UsageGroup,
  agentRunDetail,
  approval,
  chunk,
  connectorMeta,
  conversation,
  conversationDetail,
  knowledgeBase,
  kbDocument,
  me,
  page,
  searchHit,
  source,
  syncRun,
  testResult,
  type CreateKbInput,
  type SearchMode,
  type SourceInput,
  type SourcePatch,
} from "./types";

/** Sign-in URL that comes back to `here`; the login form reads `next` through safeNext. */
export const loginPath = (here: { pathname: string; search: string }) =>
  `/login?next=${encodeURIComponent(here.pathname + here.search)}`;

/** Full-page navigation, kept on an object so tests can observe it (jsdom can't navigate). */
export const browserNav = { assign: (url: string) => window.location.assign(url) };

export function redirectToLogin() {
  // Outside React (no router here); a full load also drops any in-memory state of the old session.
  browserNav.assign(loginPath(window.location));
}

/**
 * Browser-side client. Calls go through the Next.js BFF (`/api/v1/*`), which attaches the
 * access token from an httpOnly cookie, so no token is ever readable by page JavaScript.
 */
async function request<T extends z.ZodType>(
  path: string,
  schema: T,
  init?: RequestInit,
): Promise<z.infer<T>> {
  let res: Response;
  try {
    res = await fetch(path, {
      ...init,
      headers: { "Content-Type": "application/json", ...init?.headers },
    });
  } catch {
    throw networkError();
  }
  if (!res.ok) {
    const err = await toApiError(res);
    if (res.status === 401 && typeof window !== "undefined") redirectToLogin();
    throw err;
  }
  if (res.headers.get("X-Budget-Warning")) reportBudget({ kind: "warning" });
  const parsed = schema.safeParse(await res.json().catch(() => null));
  if (!parsed.success) {
    throw new ApiError(res.status, "invalid_response", "The server sent an unexpected response. Try again.");
  }
  return parsed.data;
}

// ponytail: first page only (limit 100); add cursor paging when a list can exceed it.
export const api = {
  me: () => request("/api/v1/me", me),
  listKbs: () => request("/api/v1/kbs?limit=100", page(knowledgeBase)),
  getKb: (id: string) => request(`/api/v1/kbs/${encodeURIComponent(id)}`, knowledgeBase),
  createKb: (input: CreateKbInput) =>
    request("/api/v1/kbs", knowledgeBase, { method: "POST", body: JSON.stringify(input) }),
  listDocuments: (kbId: string) =>
    request(`/api/v1/kbs/${encodeURIComponent(kbId)}/documents?limit=100`, page(kbDocument)),
  getDocument: (id: string) => request(`/api/v1/documents/${encodeURIComponent(id)}`, kbDocument),
  reprocessDocument: (id: string) =>
    request(`/api/v1/documents/${encodeURIComponent(id)}/reprocess`, kbDocument, { method: "POST" }),
  listChunks: (id: string, cursor: string | null) =>
    request(
      `/api/v1/documents/${encodeURIComponent(id)}/chunks?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
      page(chunk),
    ),
  listConversations: () => request("/api/v1/conversations?limit=50", z.object({ items: z.array(conversation) })),
  getConversation: (id: string) => request(`/api/v1/conversations/${encodeURIComponent(id)}`, conversationDetail),
  search: (body: { query: string; mode: SearchMode; kb_ids?: string[] }) =>
    request("/api/v1/search", z.object({ results: z.array(searchHit) }), { method: "POST", body: JSON.stringify(body) }),
  // 204 has no body; `request` reads it as null.
  deleteDocument: (id: string) => request(`/api/v1/documents/${encodeURIComponent(id)}`, z.null(), { method: "DELETE" }),
  listConnectors: () => request("/api/v1/connectors", z.object({ items: z.array(connectorMeta) })),
  listSources: (kbId: string) =>
    request(`/api/v1/kbs/${encodeURIComponent(kbId)}/sources`, z.object({ items: z.array(source) })),
  createSource: (kbId: string, input: SourceInput) =>
    request(`/api/v1/kbs/${encodeURIComponent(kbId)}/sources`, source, { method: "POST", body: JSON.stringify(input) }),
  updateSource: (id: string, patch: SourcePatch) =>
    request(`/api/v1/sources/${encodeURIComponent(id)}`, source, { method: "PATCH", body: JSON.stringify(patch) }),
  deleteSource: (id: string) => request(`/api/v1/sources/${encodeURIComponent(id)}`, z.null(), { method: "DELETE" }),
  reconnectOAuth: (sourceId: string) =>
    request(`/api/v1/sources/${encodeURIComponent(sourceId)}/oauth/reconnect`, z.object({ authorize_url: z.string() }), { method: "POST" }),
  startOAuth: (kbId: string, input: Omit<SourceInput, "secret">) =>
    request(`/api/v1/kbs/${encodeURIComponent(kbId)}/sources/oauth/start`, z.object({ authorize_url: z.string() }), {
      method: "POST",
      body: JSON.stringify(input),
    }),
  // The BFF adds the browser-binding cookie's value; the API checks it, the user and the org (S2).
  finishOAuth: (body: { state: string; code: string; error: string }) =>
    request("/api/v1/connectors/oauth/finish", z.object({ redirect: z.string() }), { method: "POST", body: JSON.stringify(body) }),
  testSource: (id: string) => request(`/api/v1/sources/${encodeURIComponent(id)}/test`, testResult, { method: "POST" }),
  syncSource: (id: string, full: boolean) =>
    request(`/api/v1/sources/${encodeURIComponent(id)}/sync${full ? "?full=true" : ""}`, syncRun, { method: "POST" }),
  listAgentRuns: () => request("/api/v1/agent/runs?limit=50", z.object({ items: z.array(agentRun) })),
  getAgentRun: (id: string) => request(`/api/v1/agent/runs/${encodeURIComponent(id)}`, agentRunDetail),
  createAgentRun: (body: { goal: string; kb_ids?: string[]; max_steps: number }) =>
    request("/api/v1/agent/runs", agentRun, { method: "POST", body: JSON.stringify(body) }),
  cancelAgentRun: (id: string) => request(`/api/v1/agent/runs/${encodeURIComponent(id)}/cancel`, agentRun, { method: "POST" }),
  decideApproval: (id: string, decision: "approve" | "reject") =>
    request(`/api/v1/approvals/${encodeURIComponent(id)}`, approval, { method: "POST", body: JSON.stringify({ decision }) }),
  listMembers: (orgId: string) => request(`/api/v1/orgs/${encodeURIComponent(orgId)}/members`, z.array(member)),
  // Inviting never adds anyone: the person joins when they accept, signed in with that email.
  inviteMember: (orgId: string, body: { email: string; role: Role }) =>
    request(`/api/v1/orgs/${encodeURIComponent(orgId)}/members`, invite, { method: "POST", body: JSON.stringify(body) }),
  listInvites: (orgId: string) => request(`/api/v1/orgs/${encodeURIComponent(orgId)}/invites`, z.array(invite)),
  revokeInvite: (orgId: string, id: string) =>
    request(`/api/v1/orgs/${encodeURIComponent(orgId)}/invites/${encodeURIComponent(id)}`, z.null(), { method: "DELETE" }),
  listOrgs: () => request("/api/v1/orgs", z.array(orgSummary)),
  myInvites: () => request("/api/v1/invites", z.array(myInvite)),
  acceptInvite: (id: string) =>
    request(`/api/v1/invites/${encodeURIComponent(id)}/accept`, z.object({ org_id: z.string(), role }), { method: "POST" }),
  declineInvite: (id: string) => request(`/api/v1/invites/${encodeURIComponent(id)}/decline`, z.null(), { method: "POST" }),
  changeRole: (orgId: string, userId: string, role: Role) =>
    request(`/api/v1/orgs/${encodeURIComponent(orgId)}/members/${encodeURIComponent(userId)}`, member, {
      method: "PATCH",
      body: JSON.stringify({ role }),
    }),
  removeMember: (orgId: string, userId: string) =>
    request(`/api/v1/orgs/${encodeURIComponent(orgId)}/members/${encodeURIComponent(userId)}`, z.null(), { method: "DELETE" }),
  listApiKeys: () => request("/api/v1/api-keys", z.array(apiKey)),
  createApiKey: (input: ApiKeyInput) =>
    request("/api/v1/api-keys", apiKeyCreated, { method: "POST", body: JSON.stringify(input) }),
  revokeApiKey: (id: string) => request(`/api/v1/api-keys/${encodeURIComponent(id)}`, z.null(), { method: "DELETE" }),
  listAudit: (action: string, cursor: string | null) => {
    const q = new URLSearchParams({ limit: "50" });
    if (action) q.set("action", action);
    if (cursor) q.set("cursor", cursor);
    return request(`/api/v1/audit?${q}`, page(auditEntry));
  },
  usage: (range: string, groupBy: UsageGroup) => request(`/api/v1/usage?${range}&group_by=${groupBy}`, usageReport),
  getSettings: (orgId: string) => request(`/api/v1/orgs/${encodeURIComponent(orgId)}/settings`, orgSettings),
  updateSettings: (orgId: string, body: { monthly_budget_usd: string | null; budget_action: BudgetAction }) =>
    request(`/api/v1/orgs/${encodeURIComponent(orgId)}/settings`, orgSettings, { method: "PATCH", body: JSON.stringify(body) }),
  logoutAll: () => request("/api/v1/auth/logout-all", z.object({ revoked: z.number() }), { method: "POST" }),
  listRuns: (id: string) => request(`/api/v1/sources/${encodeURIComponent(id)}/runs`, z.object({ items: z.array(syncRun) })),
};

export type Credentials = { email: string; password: string };

async function session(path: string, body?: Credentials | { org_id: string }): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`/api/session/${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw networkError();
  }
  if (!res.ok) throw await toApiError(res);
}

export const auth = {
  login: (c: Credentials) => session("login", c),
  register: (c: Credentials) => session("register", c),
  logout: () => session("logout"),
  /** Re-issues the session cookies for another org the user belongs to. */
  switchOrg: (orgId: string) => session("switch-org", { org_id: orgId }),
};
