/**
 * Turns a connector's JSON Schema (pydantic output: properties, $defs/$ref, anyOf [T, null]) into
 * form fields, and form values back into a config. Only the keywords the registry emits are handled.
 */

type Json = Record<string, unknown>;

export type FieldKind = "string" | "integer" | "number" | "boolean" | "enum" | "string-list" | "object-list";

export type Field = {
  key: string;
  label: string;
  description?: string;
  kind: FieldKind;
  required: boolean;
  nullable: boolean;
  default?: unknown;
  options?: string[];
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  minLength?: number;
  maxLength?: number;
  pattern?: string; // for string-list: the item pattern
  minItems?: number;
  maxItems?: number;
  itemFields?: Field[]; // object-list only
};

export type Row = Record<string, string | boolean>;
export type Value = string | boolean | Row[];
export type Values = Record<string, Value>;
export type FieldErrors = Record<string, string>;

const isObj = (v: unknown): v is Json => typeof v === "object" && v !== null && !Array.isArray(v);
const num = (v: unknown) => (typeof v === "number" ? v : undefined);
const str = (v: unknown) => (typeof v === "string" ? v : undefined);

const WORDS: Record<string, string> = { url: "URL", urls: "URLs", rps: "per second", id: "ID", sslmode: "SSL mode" };

export function humanize(key: string): string {
  const words = key.split("_").map((w) => WORDS[w] ?? w);
  const text = words.join(" ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function resolve(node: unknown, root: Json): Json {
  if (!isObj(node)) return {};
  const ref = str(node.$ref);
  if (ref?.startsWith("#/$defs/")) {
    const defs = isObj(root.$defs) ? root.$defs : {};
    return resolve(defs[ref.slice("#/$defs/".length)], root);
  }
  return node;
}

function toField(key: string, raw: unknown, required: boolean, root: Json): Field {
  let node = resolve(raw, root);
  let nullable = false;
  if (Array.isArray(node.anyOf)) {
    const nonNull = node.anyOf.filter((o) => !(isObj(o) && o.type === "null"));
    nullable = nonNull.length < node.anyOf.length;
    node = { ...resolve(nonNull[0], root), ...node, anyOf: undefined };
  }
  const base = {
    key,
    label: humanize(key),
    description: str(node.description),
    required,
    nullable,
    default: node.default,
  };
  if (Array.isArray(node.enum)) return { ...base, kind: "enum", options: node.enum.map(String) };
  if (node.type === "array") {
    const item = resolve(node.items, root);
    const counts = { minItems: num(node.minItems), maxItems: num(node.maxItems) };
    if (item.type === "object") return { ...base, ...counts, kind: "object-list", itemFields: fieldsOf(item, root) };
    return { ...base, ...counts, kind: "string-list", pattern: str(item.pattern) };
  }
  if (node.type === "boolean") return { ...base, kind: "boolean" };
  const bounds = {
    minimum: num(node.minimum),
    maximum: num(node.maximum),
    exclusiveMinimum: num(node.exclusiveMinimum),
  };
  if (node.type === "integer" || node.type === "number") return { ...base, ...bounds, kind: node.type };
  return {
    ...base,
    kind: "string",
    minLength: num(node.minLength),
    maxLength: num(node.maxLength),
    pattern: str(node.pattern),
  };
}

export function fieldsOf(schema: Json, root: Json = schema): Field[] {
  const props = isObj(schema.properties) ? schema.properties : {};
  const required = new Set(Array.isArray(schema.required) ? schema.required.map(String) : []);
  return Object.entries(props).map(([key, raw]) => toField(key, raw, required.has(key), root));
}

/** Lists are edited as one item per line (or comma-separated inside a table row). */
export const splitList = (text: string) =>
  text
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter(Boolean);

function initialValue(f: Field, v: unknown, inRow: boolean): Value {
  const src = v === undefined ? f.default : v;
  if (f.kind === "boolean") return src === true;
  if (f.kind === "string-list") return Array.isArray(src) ? src.map(String).join(inRow ? ", " : "\n") : "";
  if (f.kind === "object-list") {
    const rows = Array.isArray(src) ? src.filter(isObj) : [];
    return rows.map((r) => initialRow(f.itemFields ?? [], r));
  }
  return src === null || src === undefined ? "" : String(src);
}

function initialRow(fields: Field[], data: Json = {}): Row {
  const row: Row = {};
  for (const f of fields) {
    const v = initialValue(f, data[f.key], true);
    row[f.key] = Array.isArray(v) ? "" : v;
  }
  return row;
}

export const emptyRow = (f: Field): Row => initialRow(f.itemFields ?? []);

export function initialValues(fields: Field[], config: Json = {}): Values {
  return Object.fromEntries(fields.map((f) => [f.key, initialValue(f, config[f.key], false)]));
}

type Parsed = { value?: unknown; error?: string; omit?: boolean };

function parseScalar(f: Field, raw: string): Parsed {
  const text = raw.trim();
  if (text === "") {
    if (f.required) return { error: "Required." };
    return f.nullable ? { value: null } : { omit: true }; // omitted = the server's default
  }
  if (f.kind === "integer" || f.kind === "number") {
    const n = Number(text);
    if (!Number.isFinite(n) || (f.kind === "integer" && !Number.isInteger(n)))
      return { error: f.kind === "integer" ? "Enter a whole number." : "Enter a number." };
    if (f.minimum !== undefined && n < f.minimum) return { error: `Must be at least ${f.minimum}.` };
    if (f.exclusiveMinimum !== undefined && n <= f.exclusiveMinimum) return { error: `Must be more than ${f.exclusiveMinimum}.` };
    if (f.maximum !== undefined && n > f.maximum) return { error: `Must be at most ${f.maximum}.` };
    return { value: n };
  }
  if (f.minLength !== undefined && text.length < f.minLength) return { error: `Use at least ${f.minLength} characters.` };
  if (f.maxLength !== undefined && text.length > f.maxLength) return { error: `Use at most ${f.maxLength} characters.` };
  if (f.pattern && !new RegExp(f.pattern, "u").test(text)) return { error: "This format isn't allowed here." };
  return { value: text };
}

function parseList(f: Field, raw: string): Parsed {
  const items = splitList(raw);
  if (f.required && items.length === 0) return { error: "Add at least one." };
  if (f.minItems !== undefined && items.length < f.minItems) return { error: `Add at least ${f.minItems}.` };
  if (f.maxItems !== undefined && items.length > f.maxItems) return { error: `Use at most ${f.maxItems}.` };
  const bad = f.pattern ? items.find((i) => !new RegExp(f.pattern as string, "u").test(i)) : undefined;
  if (bad !== undefined) return { error: `"${bad}" isn't allowed here.` };
  return { value: items };
}

function parseFields(fields: Field[], values: Record<string, Value>, prefix: string, errors: FieldErrors): Json {
  const out: Json = {};
  for (const f of fields) {
    const v = values[f.key];
    const path = prefix + f.key;
    let parsed: Parsed;
    if (f.kind === "boolean") parsed = { value: v === true };
    else if (f.kind === "object-list") {
      const rows = Array.isArray(v) ? v : [];
      parsed = rows.length < (f.minItems ?? (f.required ? 1 : 0)) ? { error: "Add at least one." } : { value: rows.map((r, i) => parseFields(f.itemFields ?? [], r, `${path}.${i}.`, errors)) };
      if (f.maxItems !== undefined && rows.length > f.maxItems) parsed = { error: `Use at most ${f.maxItems}.` };
    } else if (f.kind === "string-list") parsed = parseList(f, typeof v === "string" ? v : "");
    else parsed = parseScalar(f, typeof v === "string" ? v : "");
    if (parsed.error) errors[path] = parsed.error;
    else if (!parsed.omit) out[f.key] = parsed.value;
  }
  return out;
}

/** Client-side check mirroring the schema's limits. The server re-validates; its errors map back via `serverFieldErrors`. */
export function toConfig(fields: Field[], values: Values): { config: Json; errors: FieldErrors } {
  const errors: FieldErrors = {};
  const config = parseFields(fields, values, "", errors);
  return { config, errors };
}

const PREFIX = "Invalid configuration: ";

/**
 * Splits the server's `invalid_config` message ("Invalid configuration: tables.0.name: msg; port: msg")
 * into per-field errors. Parts without a field path are returned in `general`.
 */
export function serverFieldErrors(message: string): { fields: FieldErrors; general: string[] } {
  const fields: FieldErrors = {};
  const general: string[] = [];
  if (!message.startsWith(PREFIX)) return { fields, general: [message] };
  for (const part of message.slice(PREFIX.length).split("; ")) {
    const m = /^([A-Za-z_][\w.]*): ([^]+)$/.exec(part);
    if (m && m[1] !== "config") fields[m[1]] = m[2];
    else general.push(m ? m[2] : part);
  }
  return { fields, general };
}

/** The error for a field, including errors the server reports on one of its list items (`start_urls.0`). */
export function errorAt(errors: FieldErrors, path: string): string | undefined {
  if (errors[path]) return errors[path];
  const child = Object.keys(errors).find((k) => k.startsWith(`${path}.`) && /^\d+$/.test(k.slice(path.length + 1)));
  return child ? errors[child] : undefined;
}
