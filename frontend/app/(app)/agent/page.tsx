import type { Metadata } from "next";
import { Suspense } from "react";
import { AgentRuns, AgentRunsSkeleton } from "@/components/agent-runs";

export const metadata: Metadata = { title: "Agent workflows" };

export default function AgentPage() {
  return (
    <Suspense fallback={<AgentRunsSkeleton />}>
      <AgentRuns />
    </Suspense>
  );
}
