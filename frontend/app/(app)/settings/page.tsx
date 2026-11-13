import type { Metadata } from "next";
import { SecuritySettings } from "@/components/security-settings";
import { SettingsHeader } from "@/components/settings-nav";

export const metadata: Metadata = { title: "Settings" };

export default function SettingsPage() {
  return (
    <>
      <SettingsHeader current="/settings" title="Settings" description="Your sign-in security, team, API keys and audit log." />
      <SecuritySettings />
    </>
  );
}
