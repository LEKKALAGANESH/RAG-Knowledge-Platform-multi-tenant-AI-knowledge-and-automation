import { ApiError, networkError, toApiError } from "@/lib/api/errors";
import { kbDocument, type KbDocument } from "@/lib/api/types";

// Mirrors backend ingestion/mime.py + config.py (max_upload_mb default 50). The server re-checks
// both by content, so these only save a pointless upload.
export const ACCEPTED_EXTENSIONS = [".pdf", ".docx", ".xlsx", ".csv", ".txt", ".md", ".markdown", ".html", ".htm", ".json", ".jsonl"];
export const MAX_UPLOAD_MB = 50;

export type UploadResult =
  | { kind: "queued"; doc: KbDocument }
  | { kind: "duplicate"; doc: KbDocument }
  | { kind: "error"; message: string; retryAt?: number };

/** Client-side pre-check; null = send it. */
export function precheck(file: File): string | null {
  const dot = file.name.lastIndexOf(".");
  const ext = dot < 0 ? "" : file.name.slice(dot).toLowerCase();
  if (!ACCEPTED_EXTENSIONS.includes(ext)) return "This file type isn't supported. Upload PDF, DOCX, XLSX, CSV, TXT, MD, HTML, JSON or JSONL.";
  if (file.size === 0) return "The file is empty.";
  if (file.size > MAX_UPLOAD_MB * 2 ** 20) return `The file is larger than ${MAX_UPLOAD_MB} MB.`;
  return null;
}

const OVERRIDE: Partial<Record<number, (serverMessage: string) => string>> = {
  401: () => "Your session has expired. Sign in again, then retry the upload.",
  403: () => "Your role can't upload documents. Ask an owner or admin for editor access.",
  404: () => "This knowledge base no longer exists.",
  413: (m) => `The file is too large. ${m}`,
  415: (m) => `This file type isn't supported. ${m}`,
};

export async function mapUploadResponse(status: number, body: string, retryAfter: string | null = null): Promise<UploadResult> {
  if (status === 0) return { kind: "error", message: networkError().message };
  if (status === 200 || status === 201) {
    let json: unknown = null;
    try {
      json = JSON.parse(body);
    } catch {
      // handled by the failed parse below
    }
    const parsed = kbDocument.safeParse(json);
    if (!parsed.success) return { kind: "error", message: "The server sent an unexpected response. Refresh to check the upload." };
    return { kind: status === 201 ? "queued" : "duplicate", doc: parsed.data };
  }
  const err: ApiError = await toApiError(new Response(body, { status, headers: retryAfter ? { "Retry-After": retryAfter } : {} }));
  const override = OVERRIDE[status];
  const message = override && status < 500 ? override(err.message) : err.message;
  return err.retryAt === undefined ? { kind: "error", message } : { kind: "error", message, retryAt: err.retryAt };
}

/** XHR rather than fetch: fetch has no upload-progress events. */
export function uploadFile(kbId: string, file: File, onProgress: (fraction: number) => void): Promise<UploadResult> {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/v1/kbs/${encodeURIComponent(kbId)}/documents`);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => resolve(mapUploadResponse(xhr.status, xhr.responseText, xhr.getResponseHeader("Retry-After")));
    xhr.onerror = () => resolve(mapUploadResponse(0, ""));
    const form = new FormData();
    form.append("file", file);
    xhr.send(form);
  });
}
