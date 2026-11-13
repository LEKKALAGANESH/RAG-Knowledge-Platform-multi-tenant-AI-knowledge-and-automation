import { api } from "@/lib/api/client";
import type { Role } from "@/lib/api/types";
import { useResource } from "@/lib/use-resource";

/**
 * Who may do what in the UI. Mirrors backend `tests/test_rbac.py` POLICY (the server still enforces it):
 * - edit: create/rename/delete knowledge bases, upload/delete/reprocess documents, connect/edit/sync sources.
 * - manage: team, invites, API keys, org settings/budget, audit log, org-wide usage.
 * Agent runs, chat and search are open to every role; approvals are decided by the run's owner or an admin.
 */
const ALLOWED = {
  edit: ["owner", "admin", "editor"],
  manage: ["owner", "admin"],
} as const satisfies Record<string, readonly Role[]>;

export type Action = keyof typeof ALLOWED;

export const can = (role: Role | null | undefined, action: Action): boolean =>
  role != null && (ALLOWED[action] as readonly Role[]).includes(role);

/** False until /me loads (and on a failed load), so a viewer never sees a write control flash in. */
export function useCan(action: Action): boolean {
  const { state } = useResource(api.me);
  return state.status === "ready" && can(state.data.role, action);
}
