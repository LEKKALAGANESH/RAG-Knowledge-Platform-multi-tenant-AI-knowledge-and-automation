import type { Metadata } from "next";
import { KbList } from "@/components/kb-list";

export const metadata: Metadata = { title: "Knowledge bases" };

export default function KbsPage() {
  return <KbList />;
}
