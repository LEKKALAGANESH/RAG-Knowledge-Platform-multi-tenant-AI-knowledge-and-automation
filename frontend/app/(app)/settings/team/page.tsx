import type { Metadata } from "next";
import { TeamScreen } from "@/components/team";
import { SettingsHeader } from "@/components/settings-nav";

export const metadata: Metadata = { title: "Team" };

export default function TeamPage() {
  return (
    <>
      <SettingsHeader current="/settings/team" title="Team" description="Who can use this organization, and what each person can do." />
      <TeamScreen />
    </>
  );
}
