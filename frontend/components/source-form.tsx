"use client";

import { useMemo, useState, type FormEvent, type ReactNode } from "react";
import { Loader2, Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, browserNav } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { AuthKind, ConnectorMeta, Source } from "@/lib/api/types";
import {
  emptyRow,
  errorAt,
  fieldsOf,
  initialValues,
  serverFieldErrors,
  toConfig,
  type Field,
  type FieldErrors,
  type Row,
  type Value,
  type Values,
} from "@/lib/schema-form";
import { oauthStartMessage, oauthSupported, safeAuthorizeUrl } from "@/lib/oauth";

type SecretField = { key: string; label: string; type: "text" | "password"; required: boolean };

// Secret JSON shapes the backend parses (s3.S3Secret, postgres.PgSecret).
const SECRET_FIELDS: Partial<Record<AuthKind, SecretField[]>> = {
  none: [],
  oauth2: [], // tokens come only from the provider's sign-in; the API refuses manual secrets (oauth_only)
  api_key: [
    { key: "access_key_id", label: "Access key ID", type: "text", required: true },
    { key: "secret_access_key", label: "Secret access key", type: "password", required: true },
  ],
  basic: [
    { key: "user", label: "Username", type: "text", required: true },
    { key: "password", label: "Password", type: "password", required: false },
  ],
};

/** Service-account / connection-string connectors need a secret shape this UI doesn't have yet. */
export const canConnect = (c: ConnectorMeta) => (c.auth === "oauth2" ? oauthSupported(c.type) : SECRET_FIELDS[c.auth] !== undefined);

export const AUTH_LABEL: Record<AuthKind, string> = {
  none: "No sign-in needed",
  api_key: "Access keys",
  basic: "Username and password",
  oauth2: "Sign in with provider",
  service_account: "Service account",
  connection_string: "Connection string",
};

const MIN_SCHEDULE = 15;
const MAX_NAME = 200;
const textareaClass =
  "min-h-20 w-full rounded-lg border border-input bg-transparent px-2.5 py-1.5 text-base outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20 md:text-sm dark:bg-input/30";
const selectClass =
  "h-8 w-full rounded-lg border border-input bg-background px-2 text-base outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 md:text-sm";

const idOf = (path: string) => `f-${path.replaceAll(".", "-")}`;

function FieldShell({ id, label, hint, error, children }: { id: string; label: string; hint?: string; error?: string; children: ReactNode }) {
  return (
    <div className="grid min-w-0 gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children}
      {hint && <p id={`${id}-hint`} className="text-xs text-muted-foreground">{hint}</p>}
      {error && <p id={`${id}-error`} className="text-xs text-destructive">{error}</p>}
    </div>
  );
}

type InputProps = { field: Field; path: string; value: Value; error?: string; inRow?: boolean; onChange: (v: Value) => void };

function ScalarInput({ field, path, value, error, inRow, onChange }: InputProps) {
  const id = idOf(path);
  const label = field.label + (field.required ? "" : " (optional)");
  const hint = field.kind === "string-list" ? [field.description, inRow ? "Separate with commas." : "One per line."].filter(Boolean).join(". ") : field.description;
  const aria = {
    "aria-invalid": Boolean(error),
    "aria-describedby": [hint && `${id}-hint`, error && `${id}-error`].filter(Boolean).join(" ") || undefined,
  };
  if (field.kind === "boolean")
    return (
      <div className="grid gap-1">
        <label htmlFor={id} className="flex items-start gap-2 text-sm">
          <input id={id} type="checkbox" className="mt-0.5 size-4 accent-primary" checked={value === true} onChange={(e) => onChange(e.target.checked)} {...aria} />
          <span>
            {field.label}
            {field.description && <span id={`${id}-hint`} className="block text-xs text-muted-foreground">{field.description}</span>}
          </span>
        </label>
        {error && <p id={`${id}-error`} className="text-xs text-destructive">{error}</p>}
      </div>
    );
  const text = typeof value === "string" ? value : "";
  let control: ReactNode;
  if (field.kind === "enum")
    control = (
      <select id={id} className={selectClass} value={text} onChange={(e) => onChange(e.target.value)} {...aria}>
        {!field.required && field.default === undefined && <option value="">—</option>}
        {field.options?.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    );
  else if (field.kind === "string-list" && !inRow)
    control = <textarea id={id} className={textareaClass} value={text} onChange={(e) => onChange(e.target.value)} rows={3} {...aria} />;
  else {
    const numeric = field.kind === "integer" || field.kind === "number";
    control = (
      <Input
        id={id}
        value={text}
        onChange={(e) => onChange(e.target.value)}
        inputMode={field.kind === "integer" ? "numeric" : numeric ? "decimal" : undefined}
        placeholder={field.default !== undefined && field.default !== null && !Array.isArray(field.default) ? String(field.default) : undefined}
        {...aria}
      />
    );
  }
  return <FieldShell id={id} label={label} hint={hint} error={error}>{control}</FieldShell>;
}

function RowsInput({ field, value, errors, onChange }: { field: Field; value: Value; errors: FieldErrors; onChange: (v: Row[]) => void }) {
  const rows = Array.isArray(value) ? value : [];
  const noun = field.label.replace(/s$/, "").toLowerCase();
  const error = errors[field.key];
  return (
    <fieldset className="grid gap-3">
      <legend className="mb-1.5 text-sm font-medium">{field.label}</legend>
      {rows.map((row, i) => (
        <div key={i} className="grid gap-3 rounded-lg border p-3 sm:grid-cols-2">
          {(field.itemFields ?? []).map((sub) => (
            <ScalarInput
              key={sub.key}
              field={sub}
              path={`${field.key}.${i}.${sub.key}`}
              value={row[sub.key]}
              inRow
              error={errorAt(errors, `${field.key}.${i}.${sub.key}`)}
              onChange={(v) => onChange(rows.map((r, j) => (j === i ? { ...r, [sub.key]: v as string | boolean } : r)))}
            />
          ))}
          <div className="sm:col-span-2">
            <Button type="button" variant="ghost" size="sm" onClick={() => onChange(rows.filter((_, j) => j !== i))}>
              <Trash2 aria-hidden />
              Remove {noun} {i + 1}
            </Button>
          </div>
        </div>
      ))}
      {error && <p className="text-xs text-destructive">{error}</p>}
      {(field.maxItems === undefined || rows.length < field.maxItems) && (
        <Button type="button" variant="outline" size="sm" className="w-fit" onClick={() => onChange([...rows, emptyRow(field)])}>
          <Plus aria-hidden />
          Add {noun}
        </Button>
      )}
    </fieldset>
  );
}

type Props = { connector: ConnectorMeta; kbId: string; source?: Source; onSaved: (s: Source) => void; onCancel: () => void };

type Draft = { name: string; values: Values; secret: Record<string, string>; schedule: string };

function validate(draft: Draft, fields: Field[], secretFields: SecretField[], secretNeeded: boolean) {
  const { config, errors } = toConfig(fields, draft.values);
  const name = draft.name.trim();
  if (!name) errors.name = "Give the source a name.";
  else if (name.length > MAX_NAME) errors.name = `Use at most ${MAX_NAME} characters.`;
  const anySecret = secretFields.some((f) => draft.secret[f.key]);
  if (secretNeeded || anySecret)
    for (const f of secretFields) if (f.required && !draft.secret[f.key]) errors[`secret.${f.key}`] = "Required.";
  let schedule: number | null = null;
  if (draft.schedule.trim()) {
    const n = Number(draft.schedule);
    if (!Number.isInteger(n) || n < MIN_SCHEDULE) errors.schedule = `Use a whole number of minutes, at least ${MIN_SCHEDULE}.`;
    else schedule = n;
  }
  const secret = anySecret ? JSON.stringify(Object.fromEntries(secretFields.map((f) => [f.key, draft.secret[f.key] ?? ""]))) : undefined;
  return { errors, config, name, schedule, secret };
}

/** Create or edit a source. The secret is write-only: never pre-filled, and blank on edit keeps the saved one. */
export function SourceForm({ connector, kbId, source, onSaved, onCancel }: Props) {
  const signIn = connector.auth === "oauth2" && !source;
  const fields = useMemo(() => fieldsOf(connector.config_schema), [connector]);
  const secretFields = SECRET_FIELDS[connector.auth] ?? [];
  const [draft, setDraft] = useState<Draft>(() => ({
    name: source?.name ?? connector.title,
    values: initialValues(fields, source?.config),
    secret: {},
    schedule: source?.schedule_minutes ? String(source.schedule_minutes) : "",
  }));
  const [errors, setErrors] = useState<FieldErrors>({});
  const [general, setGeneral] = useState<string[]>([]);
  const [pending, setPending] = useState(false);

  const setValue = (key: string, v: Value) => setDraft((d) => ({ ...d, values: { ...d.values, [key]: v } }));

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const out = validate(draft, fields, secretFields, !source && secretFields.length > 0);
    setErrors(out.errors);
    setGeneral(Object.keys(out.errors).length ? ["Fix the highlighted fields."] : []);
    if (Object.keys(out.errors).length) return;
    setPending(true);
    try {
      if (signIn) {
        await startSignIn(out);
        return;
      }
      let saved: Source;
      if (source) {
        const before = toConfig(fields, initialValues(fields, source.config)).config;
        const changed = JSON.stringify(before) !== JSON.stringify(out.config);
        saved = await api.updateSource(source.id, {
          name: out.name,
          schedule_minutes: out.schedule,
          ...(changed && { config: out.config }),
          ...(out.secret !== undefined && { secret: out.secret }),
        });
      } else {
        saved = await api.createSource(kbId, { type: connector.type, name: out.name, config: out.config, secret: out.secret, schedule_minutes: out.schedule });
      }
      setDraft((d) => ({ ...d, secret: {} }));
      setPending(false);
      onSaved(saved);
    } catch (err) {
      if (err instanceof ApiError && err.code === "invalid_config") {
        const mapped = serverFieldErrors(err.message);
        setErrors(mapped.fields);
        setGeneral(mapped.general.length ? mapped.general : ["Fix the highlighted fields."]);
      } else {
        setGeneral([signIn ? oauthStartMessage(err, connector) : err instanceof ApiError ? err.message : "Couldn't save the source. Try again."]);
      }
      setPending(false);
    }
  }

  // Leaves the page on success, so `pending` stays on until the browser navigates.
  async function startSignIn(out: { name: string; config: Record<string, unknown>; schedule: number | null }) {
    const { authorize_url } = await api.startOAuth(kbId, { type: connector.type, name: out.name, config: out.config, schedule_minutes: out.schedule });
    const target = safeAuthorizeUrl(connector.type, authorize_url);
    if (!target) {
      setGeneral([`The server returned an unexpected sign-in address for ${connector.title}, so it wasn't opened. Ask an operator to check the server's OAuth settings.`]);
      setPending(false);
      return;
    }
    browserNav.assign(target);
  }

  return (
    <form onSubmit={onSubmit} noValidate className="grid gap-5">
      <FieldShell id="f-name" label="Source name" error={errors.name}>
        <Input id="f-name" value={draft.name} maxLength={MAX_NAME + 1} aria-invalid={Boolean(errors.name)} onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))} />
      </FieldShell>

      {fields.map((f) =>
        f.kind === "object-list" ? (
          <RowsInput key={f.key} field={f} value={draft.values[f.key]} errors={errors} onChange={(v) => setValue(f.key, v)} />
        ) : (
          <ScalarInput key={f.key} field={f} path={f.key} value={draft.values[f.key]} error={errorAt(errors, f.key)} onChange={(v) => setValue(f.key, v)} />
        ),
      )}

      {signIn && (
        <p className="rounded-md border bg-muted/40 px-3 py-2 text-sm text-muted-foreground">
          Next you sign in with {connector.title} and allow access. The source is saved when you come back here, and its first sync starts.
        </p>
      )}

      {secretFields.length > 0 && (
        <fieldset className="grid gap-3 rounded-lg border p-3">
          <legend className="px-1 text-sm font-medium">Credentials</legend>
          <p className="text-xs text-muted-foreground">
            {source?.has_secret
              ? "Credentials are saved and never shown again. Leave these blank to keep them, or fill every field to replace them."
              : "Stored encrypted and never shown again. Use a read-only account."}
          </p>
          {secretFields.map((f) => (
            <FieldShell key={f.key} id={`secret-${f.key}`} label={f.label + (f.required ? "" : " (optional)")} error={errors[`secret.${f.key}`]}>
              <Input
                id={`secret-${f.key}`}
                type={f.type}
                autoComplete={f.type === "password" ? "new-password" : "off"}
                value={draft.secret[f.key] ?? ""}
                aria-invalid={Boolean(errors[`secret.${f.key}`])}
                onChange={(e) => setDraft((d) => ({ ...d, secret: { ...d.secret, [f.key]: e.target.value } }))}
              />
            </FieldShell>
          ))}
        </fieldset>
      )}

      <FieldShell id="f-schedule" label="Sync every (minutes, optional)" hint={`Leave blank to sync only when you start it. Minimum ${MIN_SCHEDULE}.`} error={errors.schedule}>
        <Input id="f-schedule" inputMode="numeric" value={draft.schedule} aria-invalid={Boolean(errors.schedule)} aria-describedby="f-schedule-hint" onChange={(e) => setDraft((d) => ({ ...d, schedule: e.target.value }))} />
      </FieldShell>

      {general.length > 0 && (
        <div role="alert" className="grid gap-1 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {general.map((g) => <p key={g} className="break-words">{g}</p>)}
        </div>
      )}

      <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" disabled={pending}>
          {pending && <Loader2 className="animate-spin" aria-hidden />}
          {signIn ? `Continue to ${connector.title}` : source ? "Test and save changes" : "Test and connect"}
        </Button>
      </div>
    </form>
  );
}
