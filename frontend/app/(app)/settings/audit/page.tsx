import type { Metadata } from "next";
import { AuditScreen } from "@/components/audit-log";
import { SettingsHeader } from "@/components/settings-nav";

export const metadata: Metadata = { title: "Audit log" };

export default function AuditPage() {
  return (
    <>
      <SettingsHeader current="/settings/audit" title="Audit log" description="Who did what in this organization, newest first." />
      <AuditScreen />
    </>
  );
}
