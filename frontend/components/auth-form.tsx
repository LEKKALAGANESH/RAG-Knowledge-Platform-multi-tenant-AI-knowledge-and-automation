"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { auth } from "@/lib/api/client";
import { ApiError, rateLimitMessage } from "@/lib/api/errors";
import { useRetryCountdown } from "@/lib/use-retry-countdown";
import { safeNext } from "@/lib/safe-next";
import { MIN_PASSWORD, validateAuth, type AuthErrors, type AuthMode, type AuthValues } from "@/lib/auth-validation";

const COPY = {
  login: { title: "Sign in", submit: "Sign in", alt: "No account yet?", altLink: "Create one", altHref: "/register" },
  register: {
    title: "Create your account",
    submit: "Create account",
    alt: "Already have an account?",
    altLink: "Sign in",
    altHref: "/login",
  },
} as const;

export function AuthForm({ mode }: { mode: AuthMode }) {
  const copy = COPY[mode];
  const [values, setValues] = useState<AuthValues>({ email: "", password: "", confirm: "" });
  const [errors, setErrors] = useState<AuthErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [retryAt, setRetryAt] = useState<number | undefined>(undefined);
  const [pending, setPending] = useState(false);
  const wait = useRetryCountdown(retryAt);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (wait > 0) return;
    const found = validateAuth(values, mode);
    setErrors(found);
    setFormError(null);
    setRetryAt(undefined);
    if (Object.keys(found).length) return;
    setPending(true);
    try {
      await auth[mode]({ email: values.email.trim(), password: values.password });
      window.location.assign(safeNext(new URLSearchParams(window.location.search).get("next"), window.location.origin));
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "Something went wrong. Try again.");
      setRetryAt(err instanceof ApiError ? err.retryAt : undefined);
      setPending(false);
    }
  }

  const field = (name: keyof AuthValues, label: string, type: string, autoComplete: string, hint?: string) => (
    <div className="grid gap-1.5">
      <Label htmlFor={name}>{label}</Label>
      <Input
        id={name}
        name={name}
        type={type}
        autoComplete={autoComplete}
        value={values[name]}
        onChange={(e) => setValues((v) => ({ ...v, [name]: e.target.value }))}
        aria-invalid={Boolean(errors[name])}
        aria-describedby={errors[name] ? `${name}-error` : hint ? `${name}-hint` : undefined}
        className="h-10"
      />
      {errors[name] ? (
        <p id={`${name}-error`} className="text-[13px] text-destructive">
          {errors[name]}
        </p>
      ) : hint ? (
        <p id={`${name}-hint`} className="text-[13px] text-muted-foreground">
          {hint}
        </p>
      ) : null}
    </div>
  );

  return (
    <form onSubmit={onSubmit} noValidate className="grid gap-5" aria-busy={pending}>
      <h1 className="text-2xl font-semibold tracking-tight">{copy.title}</h1>
      {formError && (
        <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {retryAt !== undefined ? rateLimitMessage(wait) : formError}
        </p>
      )}
      {field("email", "Work email", "email", "email")}
      {field(
        "password",
        "Password",
        "password",
        mode === "login" ? "current-password" : "new-password",
        mode === "register" ? `At least ${MIN_PASSWORD} characters.` : undefined,
      )}
      {mode === "register" && field("confirm", "Confirm password", "password", "new-password")}
      <Button type="submit" size="lg" className="h-10" disabled={pending || wait > 0}>
        {pending && <Loader2 className="animate-spin" aria-hidden />}
        {copy.submit}
      </Button>
      <p className="text-sm text-muted-foreground">
        {copy.alt}{" "}
        <Link href={copy.altHref} className="font-medium text-primary underline-offset-4 hover:underline focus-visible:underline">
          {copy.altLink}
        </Link>
      </p>
    </form>
  );
}
