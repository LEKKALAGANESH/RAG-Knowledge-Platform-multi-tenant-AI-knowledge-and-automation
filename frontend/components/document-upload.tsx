"use client";

import { useRef, useState, type DragEvent } from "react";
import { AlertTriangle, CheckCircle2, CopyCheck, Loader2, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";
import { rateLimitMessage } from "@/lib/api/errors";
import { formatBytes } from "@/lib/format";
import { useRetryCountdown } from "@/lib/use-retry-countdown";
import { ACCEPTED_EXTENSIONS, MAX_UPLOAD_MB, precheck, uploadFile, type UploadResult } from "@/lib/upload";
import { cn } from "@/lib/utils";

type Row = { key: string; name: string; size: number; progress: number; result: UploadResult | null };

function ResultLine({ result }: { result: UploadResult }) {
  if (result.kind === "queued")
    return (
      <span className="flex items-center gap-1.5 text-success">
        <CheckCircle2 className="size-3.5 shrink-0" aria-hidden /> Uploaded. Processing has started.
      </span>
    );
  if (result.kind === "duplicate")
    return (
      <span className="flex items-center gap-1.5 text-foreground">
        <CopyCheck className="size-3.5 shrink-0 text-primary" aria-hidden />
        Already uploaded as &ldquo;{result.doc.name}&rdquo;. It was not processed again.
      </span>
    );
  return <ErrorLine message={result.message} retryAt={result.retryAt} />;
}

function ErrorLine({ message, retryAt }: { message: string; retryAt?: number }) {
  const wait = useRetryCountdown(retryAt);
  return (
    <span className="flex items-start gap-1.5 text-destructive">
      <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden /> {retryAt === undefined ? message : rateLimitMessage(wait)}
    </span>
  );
}

export function DocumentUpload({ kbId, onUploaded }: { kbId: string; onUploaded: () => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [rows, setRows] = useState<Row[]>([]);
  const [dragging, setDragging] = useState(false);

  const update = (key: string, patch: Partial<Row>) =>
    setRows((rs) => rs.map((r) => (r.key === key ? { ...r, ...patch } : r)));

  function start(files: FileList | null) {
    if (!files?.length) return;
    const batch = Array.from(files, (file) => ({ file, key: crypto.randomUUID() }));
    setRows((rs) => [
      ...batch.map(({ file, key }) => {
        const problem = precheck(file);
        return { key, name: file.name, size: file.size, progress: 0, result: problem ? ({ kind: "error", message: problem } as const) : null };
      }),
      ...rs,
    ]);
    for (const { file, key } of batch) {
      if (precheck(file)) continue;
      void uploadFile(kbId, file, (progress) => update(key, { progress })).then((result) => {
        update(key, { progress: 1, result });
        if (result.kind !== "error") onUploaded();
      });
    }
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    setDragging(false);
    start(e.dataTransfer.files);
  }

  return (
    <div className="grid gap-3">
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={(e) => {
          if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDragging(false);
        }}
        onDrop={onDrop}
        className={cn(
          "flex flex-col items-center gap-2 rounded-lg border border-dashed px-4 py-8 text-center transition-colors",
          dragging ? "border-primary bg-primary/5" : "bg-muted/30",
        )}
      >
        <Upload className="size-5 text-muted-foreground" aria-hidden />
        <p className="text-sm font-medium">Drag files here</p>
        <Button type="button" variant="outline" onClick={() => input.current?.click()}>
          Choose files
        </Button>
        <p className="text-xs text-muted-foreground">
          PDF, DOCX, XLSX, CSV, TXT, MD, HTML, JSON, JSONL · up to {MAX_UPLOAD_MB} MB each
        </p>
        <input
          ref={input}
          type="file"
          aria-label="Choose files to upload"
          multiple
          accept={ACCEPTED_EXTENSIONS.join(",")}
          className="hidden"
          onChange={(e) => {
            start(e.target.files);
            e.target.value = ""; // allow picking the same file again
          }}
        />
      </div>

      {rows.length > 0 && (
        <ul aria-label="Uploads" aria-live="polite" className="grid gap-2">
          {rows.map((r) => (
            <li key={r.key} className="grid gap-1 rounded-md border px-3 py-2 text-sm">
              <div className="flex items-center justify-between gap-3">
                <span className="truncate font-medium" title={r.name}>{r.name}</span>
                <span className="shrink-0 text-xs text-muted-foreground">{formatBytes(r.size)}</span>
              </div>
              {r.result ? (
                <div className="text-xs"><ResultLine result={r.result} /></div>
              ) : (
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Loader2 className="size-3.5 text-primary motion-safe:animate-spin" aria-hidden />
                  <progress className="h-1.5 flex-1 accent-primary" max={1} value={r.progress} aria-label={`Uploading ${r.name}`} />
                  <span className="w-9 text-right tabular-nums">{Math.round(r.progress * 100)}%</span>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
