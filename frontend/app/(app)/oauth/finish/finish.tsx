"use client";

import { useEffect, useRef } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Loader2 } from "lucide-react";
import { api } from "@/lib/api/client";
import { finishErrorPath, safeFinishRedirect } from "@/lib/oauth";

export function Finishing() {
  return (
    <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
      <Loader2 className="size-4 animate-spin" aria-hidden /> Finishing the sign-in…
    </p>
  );
}

/** Hands the provider's code+state to the API as the signed-in user; the API refuses a flow this browser didn't start. */
export function FinishOAuth() {
  const params = useSearchParams();
  const router = useRouter();
  const sent = useRef(false); // the state is single-use: a second POST (Strict Mode, re-render) would void it

  useEffect(() => {
    if (sent.current) return;
    sent.current = true;
    const body = { state: params.get("state") ?? "", code: params.get("code") ?? "", error: params.get("error") ?? "" };
    window.history.replaceState(null, "", "/oauth/finish"); // the code must not stay in history
    api.finishOAuth(body).then(
      ({ redirect }) => router.replace(safeFinishRedirect(redirect) ?? finishErrorPath(null)),
      (err: unknown) => router.replace(finishErrorPath(err)),
    );
  }, [params, router]);

  return <Finishing />;
}
