import type { Metadata } from "next";
import { Suspense } from "react";
import { AgentRunPage, AgentRunSkeleton } from "@/components/agent-run";

export const metadata: Metadata = { title: "Agent run" };

export default function AgentRunRoute() {
  return (
    <Suspense fallback={<AgentRunSkeleton />}>
      <AgentRunPage />
    </Suspense>
  );
}
