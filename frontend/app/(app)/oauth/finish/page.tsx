import type { Metadata } from "next";
import { Suspense } from "react";
import { FinishOAuth, Finishing } from "./finish";

export const metadata: Metadata = { title: "Finishing sign-in" };

/** The provider's redirect URI (OAUTH_REDIRECT_BASE + /oauth/finish). The API has no public callback (S2). */
export default function OAuthFinishPage() {
  return (
    <Suspense fallback={<Finishing />}>
      <FinishOAuth />
    </Suspense>
  );
}
