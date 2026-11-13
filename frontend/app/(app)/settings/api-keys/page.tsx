import type { Metadata } from "next";
import { ApiKeysScreen } from "@/components/api-keys";
import { SettingsHeader } from "@/components/settings-nav";

export const metadata: Metadata = { title: "API keys" };

export default function ApiKeysPage() {
  return (
    <>
      <SettingsHeader current="/settings/api-keys" title="API keys" description="Keys for scripts and other apps that call this organization's API." />
      <ApiKeysScreen />
    </>
  );
}
