"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { LogOut } from "lucide-react";
import { ConfirmDialog } from "@/components/kb-sources";
import { Button } from "@/components/ui/button";
import { api, auth } from "@/lib/api/client";

export function SecuritySettings() {
  const router = useRouter();
  const [confirming, setConfirming] = useState(false);
  return (
    <section aria-labelledby="sessions-heading" className="grid max-w-2xl gap-3 rounded-lg border bg-card p-4 md:p-5">
      <h2 id="sessions-heading" className="text-base font-semibold">Signed-in devices</h2>
      <p className="text-sm text-muted-foreground">
        Lost a laptop or signed in on a shared computer? Sign out everywhere ends every session of your account,
        in every organization, including this one. Other devices lose access within 15 minutes.
      </p>
      <Button variant="outline" className="justify-self-start" onClick={() => setConfirming(true)}>
        <LogOut aria-hidden />
        Sign out everywhere
      </Button>
      <ConfirmDialog
        open={confirming}
        destructive
        title="Sign out everywhere?"
        body="Every browser and device signed in to your account is signed out, and you'll need to sign in again here too. API keys keep working."
        action="Sign out everywhere"
        onClose={() => setConfirming(false)}
        onConfirm={async () => {
          await api.logoutAll();
          await auth.logout().catch(() => undefined); // this browser's token is already revoked; this just clears cookies
          router.replace("/login");
        }}
      />
    </section>
  );
}
