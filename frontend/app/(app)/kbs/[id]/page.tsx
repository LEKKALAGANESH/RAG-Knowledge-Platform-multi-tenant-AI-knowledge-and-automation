import type { Metadata } from "next";
import { Suspense } from "react";
import { KbDetail, KbDetailSkeleton } from "@/components/kb-detail";

export const metadata: Metadata = { title: "Knowledge base" };

export default function KbDetailPage() {
  return (
    <Suspense fallback={<KbDetailSkeleton />}>
      <KbDetail />
    </Suspense>
  );
}
