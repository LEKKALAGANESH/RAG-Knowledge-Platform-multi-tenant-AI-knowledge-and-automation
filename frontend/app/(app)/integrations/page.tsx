import type { Metadata } from "next";
import { Suspense } from "react";
import { Integrations, IntegrationsSkeleton } from "@/components/integrations";

export const metadata: Metadata = { title: "Integrations" };

export default function IntegrationsPage() {
  return (
    <Suspense fallback={<IntegrationsSkeleton />}>
      <Integrations />
    </Suspense>
  );
}
