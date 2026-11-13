import { describe, expect, it } from "vitest";
import registry from "./fixtures/connectors.json";
import { errorAt, fieldsOf, initialValues, serverFieldErrors, toConfig, type Field } from "@/lib/schema-form";

// Captured from the live GET /v1/connectors (B6a); re-capture when a connector's config changes.
const schemaOf = (type: string) => {
  const c = registry.items.find((i) => i.type === type);
  if (!c) throw new Error(`fixture lacks ${type}`);
  return c.config_schema as Record<string, unknown>;
};
const byKey = (fields: Field[]) => Object.fromEntries(fields.map((f) => [f.key, f]));

describe("fieldsOf (real registry schemas)", () => {
  it("maps the website schema: required list, nullable string, bounded integers, number, boolean", () => {
    const f = byKey(fieldsOf(schemaOf("website")));
    expect(f.start_urls).toMatchObject({ kind: "string-list", required: true, minItems: 1, maxItems: 20, label: "Start URLs" });
    expect(f.sitemap_url).toMatchObject({ kind: "string", nullable: true, required: false, default: null });
    expect(f.max_depth).toMatchObject({ kind: "integer", minimum: 0, maximum: 5, default: 2 });
    expect(f.rate_limit_rps).toMatchObject({ kind: "number", exclusiveMinimum: 0, maximum: 5 });
    expect(f.allow_nonstandard_ports).toMatchObject({ kind: "boolean", default: false });
  });

  it("maps the s3 schema with pattern and length limits", () => {
    const f = byKey(fieldsOf(schemaOf("s3")));
    expect(f.bucket).toMatchObject({ kind: "string", required: true, minLength: 3, maxLength: 63 });
    expect(f.bucket.pattern).toBeDefined();
    expect(f.include_extensions.kind).toBe("string-list");
    expect(f.allow_private_endpoint.kind).toBe("boolean");
  });

  it("maps the postgres schema: enum, and tables as a list of objects resolved through $defs", () => {
    const f = byKey(fieldsOf(schemaOf("postgres")));
    expect(f.sslmode).toMatchObject({ kind: "enum", options: ["disable", "require", "verify-full"], default: "require", label: "SSL mode" });
    expect(f.tables.kind).toBe("object-list");
    const sub = byKey(f.tables.itemFields ?? []);
    expect(sub.text_columns).toMatchObject({ kind: "string-list", minItems: 1, required: true });
    expect(sub.updated_column).toMatchObject({ kind: "string", nullable: true });
  });
});

describe("toConfig", () => {
  it("builds a website config from defaults plus one start URL", () => {
    const fields = fieldsOf(schemaOf("website"));
    const values = { ...initialValues(fields), start_urls: "https://example.com/\nhttps://example.com/docs" };
    const { config, errors } = toConfig(fields, values);
    expect(errors).toEqual({});
    expect(config).toMatchObject({
      start_urls: ["https://example.com/", "https://example.com/docs"],
      sitemap_url: null,
      max_depth: 2,
      rate_limit_rps: 1,
      allow_nonstandard_ports: false,
      allowed_domains: [],
    });
  });

  it("flags required, range, integer, pattern and min-items errors by path", () => {
    const web = fieldsOf(schemaOf("website"));
    expect(toConfig(web, { ...initialValues(web), max_depth: "9", rate_limit_rps: "0" }).errors).toEqual({
      start_urls: "Add at least one.",
      max_depth: "Must be at most 5.",
      rate_limit_rps: "Must be more than 0.",
    });
    const pg = fieldsOf(schemaOf("postgres"));
    const bad = { ...initialValues(pg), host: "db.example.com", database: "app", port: "54.5",
      tables: [{ name: "bad name", id_column: "id", text_columns: "", title_column: "", updated_column: "" }] };
    expect(toConfig(pg, bad).errors).toEqual({
      port: "Enter a whole number.",
      "tables.0.name": "This format isn't allowed here.",
      "tables.0.text_columns": "Add at least one.",
    });
  });

  it("round-trips a saved postgres config through the form values", () => {
    const pg = fieldsOf(schemaOf("postgres"));
    const saved = { host: "db.example.com", port: 5432, database: "app", schema: "public", sslmode: "require",
      max_rows_per_table: 10000, full_sync_every: 12, allow_private_endpoint: false,
      tables: [{ name: "faq", id_column: "id", text_columns: ["q", "a"], title_column: null, updated_column: "updated_at" }] };
    expect(toConfig(pg, initialValues(pg, saved)).config).toEqual(saved);
  });
});

describe("serverFieldErrors", () => {
  it("maps the server's invalid_config message (captured live) onto field paths", () => {
    const msg = "Invalid configuration: tables.0.name: String should match pattern '^[A-Za-z_][A-Za-z0-9_$]{0,62}$'; tables.0.text_columns: List should have at least 1 item after validation, not 0";
    const { fields, general } = serverFieldErrors(msg);
    expect(general).toEqual([]);
    expect(Object.keys(fields)).toEqual(["tables.0.name", "tables.0.text_columns"]);
    expect(fields["tables.0.name"]).toContain("should match pattern");
  });

  it("keeps messages without a field path as general errors", () => {
    expect(serverFieldErrors("Start URL must be public.")).toEqual({ fields: {}, general: ["Start URL must be public."] });
  });

  it("finds an error reported on a list item under its list field", () => {
    expect(errorAt({ "start_urls.1": "bad" }, "start_urls")).toBe("bad");
    expect(errorAt({ "tables.0.name": "bad" }, "tables")).toBeUndefined();
  });
});
