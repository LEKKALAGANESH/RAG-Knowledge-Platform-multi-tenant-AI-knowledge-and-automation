# Ingestion pipeline

Code: `backend/app/ingestion/`, `backend/app/kb/router.py` (upload), `backend/app/retrieval/indexing.py`
(embedding step).

## Steps

1. **Upload** (`POST /v1/kbs/{kb}/documents`, editor+). Read at most `MAX_UPLOAD_MB` (default 50) →
   `413 file_too_large`; empty → `422 empty_file`.
2. **Type detection from content** (`mime.py`), not from the extension: `%PDF-` → PDF; ZIP with
   `word/document.xml` → DOCX, `xl/workbook.xml` → XLSX, any other ZIP → archive; `ustar` → TAR;
   gzip → TAR.GZ or single-file GZ; PNG/JPEG/TIFF/BMP/WEBP signatures → image; otherwise UTF-8 text
   sniffed as HTML, JSON, JSONL, and only then the extension chooses between CSV, MD and TXT. Anything
   else → `415 unsupported_type`.
3. **Dedupe:** sha256 of the bytes. The same bytes already uploaded to this KB (not deleted) → `200`
   with the existing document; nothing is reprocessed (AC3). Connector documents never dedupe an upload.
4. **Store** the blob at `<org>/<kb>/<sha256>` and insert `documents(status=queued)`; enqueue
   `ingest_document` (queue `ingest`).
5. **Worker** (`pipeline.process_document`, in an RLS session for the document's org): status
   `processing`, load blob, parse, chunk, replace the document's chunks.
6. **Finalize:** 0 chunks → `failed` with "No text could be extracted" (+ warnings); never `ready`
   with 0 chunks (AC2). Otherwise embed into the KB's active index (creating version 1 on first use),
   then `ready`.

## Formats

| Format | Parser | Location metadata (`chunks.loc`) | Warnings |
|---|---|---|---|
| PDF | pypdf; pages without a text layer are rendered (pypdfium2) and OCR'd | `page` | `page N: no text, OCR not enabled` / `page N: no text found by OCR` |
| PNG, JPEG, TIFF, BMP, WEBP | tesseract (pytesseract) | `page` (TIFF frame) | `page N: no text found by OCR`; without OCR the document fails: "OCR is not available on this server…" |
| ZIP, TAR, TAR.GZ, GZ | stdlib zipfile / tarfile / gzip | see [Archives](#archives) | one per skipped member |
| DOCX | python-docx | `section` (heading); tables as `section` + `table` | |
| XLSX | openpyxl | `sheet` + `range` | `sheet X: empty` |
| CSV | stdlib csv | `range` | |
| TXT, MD | text | `section` (markdown heading) | |
| HTML | trafilatura | `section` | |
| JSON, JSONL | stdlib json | `record` | |
| SRT, VTT | `parsers/subtitles.py` (pure Python) | `start` + `end` (`HH:MM:SS`) | `no subtitle cues with text were found` |
| MP3, WAV, M4A, AAC, MP4, MOV, WEBM | faster-whisper (PyAV decode) | `start` + `end` (`HH:MM:SS`) | `no speech was detected`; without transcription the document fails: "Transcription is not available on this server…" |

Planned: PPTX, 7Z (no pure-wheel reader installed) (see [roadmap](DEVELOPMENT_ROADMAP.md)).

## OCR

- `parsers/ocr.py`, inside the isolated parse child (timeout and memory limit apply). On when the
  `tesseract` binary is found unless `OCR_ENABLED=false`; the API and worker log at startup whether
  OCR is on, and why not. Languages `OCR_LANGS` (default `eng`).
- Images: each frame (multi-page TIFF) is one `page` section; frames share `MAX_PDF_PAGES`. An image
  over `MAX_IMAGE_PIXELS` fails before decoding (PIL bomb check at open).
- PDF: only pages with no text layer are OCR'd, rendered at `OCR_DPI` (300) and scaled down to fit
  `MAX_IMAGE_PIXELS`. pypdfium2 ships pdfium inside its wheel, so no poppler install is needed
  (pdf2image would need poppler on every host).
- No OCR: images fail with a readable reason; image-only PDF pages keep the `OCR not enabled` warning.
- Install tesseract: Debian/Ubuntu (and Debian-based Docker images) `apt-get install -y
  tesseract-ocr` (+ `tesseract-ocr-deu` etc. per language); Alpine `apk add tesseract-ocr`; macOS
  `brew install tesseract`; Windows `winget install UB-Mannheim.TesseractOCR`, then put its folder on
  `PATH` or set `TESSERACT_CMD` to the full `tesseract.exe` path. Restart API and worker.

## Audio, video and subtitles

- Subtitles (always on): SRT and WebVTT share one tolerant parser (BOM, CRLF/CR, missing cue
  indices, VTT header/NOTE/STYLE/REGION blocks skipped, cue settings and `<v>`/`<i>`/`{\an8}` tags
  stripped, latin-1 fallback for legacy SRT). Detected by content (`WEBVTT` / a cue timing line),
  not the extension. Max `MAX_SUBTITLE_CUES` cues.
- Speech-to-text (`parsers/media.py`): faster-whisper (CTranslate2, CPU, int8) with VAD, language
  auto-detected. Runs in the isolated parse child. PyAV ships its own FFmpeg libraries, so no
  system ffmpeg is needed; for video the first audio track is transcribed.
- Cues / segments are merged into sections of up to `CHUNK_TOKENS` words without splitting a cue;
  each section's `loc` is `{"start": "HH:MM:SS", "end": "HH:MM:SS"}` (first cue start, last cue end).
- Document `meta` gets `language`, `language_probability`, `duration_s` for transcribed media.
- Limits: duration is read from the container header and checked against `MAX_MEDIA_MINUTES`
  (default 120) before any decoding. The child reports the duration first; the parent then waits
  `PARSE_TIMEOUT_S + duration x TRANSCRIBE_TIMEOUT_FACTOR`. Memory cap `TRANSCRIBE_MEMORY_MB`
  (POSIX). Upload size is still `MAX_UPLOAD_MB`.
- Silence / no speech: no text, warning `no speech was detected`, so the document fails
  "No text could be extracted from this file. (no speech was detected)".
- On/off: on when faster-whisper is installed and the model `TRANSCRIBE_MODEL` (default `small`)
  is found locally — a model directory, or the Hugging Face cache under `TRANSCRIBE_MODEL_DIR`
  (default `~/.cache/huggingface/hub`). No implicit download: set `TRANSCRIBE_ALLOW_DOWNLOAD=true`
  to fetch it on first use, or pre-fetch it:
  `uv run python -c "from faster_whisper.utils import download_model; download_model('small', cache_dir='/models')"`.
  `TRANSCRIBE_ENABLED=false` forces it off. Startup log says which. Off = audio/video documents
  fail readably; subtitles still work.
- Model choice: `small` (multilingual, ~480 MB download, roughly 1 GB RAM while transcribing) is the speed/quality
  middle: `tiny`/`base` are fast but much worse outside clean English; `medium`/`large-v3` are 2-6x
  slower on CPU. Each transcription loads the model in its child (a few seconds) and uses all CPU
  cores: keep worker concurrency low on hosts that ingest lots of media.

## Archives

- `archives.py` (reading) + `pipeline.expand_archive` (documents). Nothing is extracted to disk:
  members are read in memory one at a time and stored as content-addressed blobs
  (`<org>/<kb>/<sha256>`, like uploads); member names never become paths.
- Each supported member becomes its own document in the same KB, named `<archive name>/<member
  path>`, with `meta = {archive: <archive doc id>, member: <path>, depth: n}` (no DB column: the link
  lives in `documents.meta`), and is queued like an upload. Members dedupe by sha256 like uploads
  (same content already in the KB, or twice in the archive → skipped, naming the existing copy).
- The archive document has no chunks. It ends `ready` with `warnings` (one per skipped member) and
  `meta.archive_summary = {ingested, skipped, members: [{path, document_id} | {path, skipped}]}`, or
  `failed` when a limit is hit or nothing in it is usable.
- Skipped (reported, archive still `ready`): unsupported type, empty file, over `MAX_UPLOAD_MB`,
  encrypted, unreadable, nested archive beyond `ARCHIVE_MAX_DEPTH`. Refused: absolute paths, drive
  letters, `..`, NUL in the name, symlinks, hard links, devices/FIFOs.
- Whole archive `failed` (no member documents created): more than `ARCHIVE_MAX_MEMBERS` files, total
  declared size over `MAX_UNZIPPED_MB`, compression ratio over `MAX_ZIP_RATIO` (per ZIP member or
  overall, above 1 MB), unreadable archive. Checked while walking headers, before any member is read.
- Reprocess re-reads the archive; members it already created are kept, not duplicated.
- Deleting the archive deletes its member documents (nested ones too) and their unused blobs.
  Deleting one member leaves the archive's summary as it was.
- Limitation: archives from connectors are not unpacked (document `failed`, "upload it directly").

## Chunking (`chunking.py`)

- Chunks never cross a section, page or sheet boundary.
- Size `CHUNK_TOKENS` (400) with `CHUNK_OVERLAP` (50), counted as whitespace words (a `ponytail:`
  approximation; the embedder's tokenizer would be exact). Overlap is capped at half the size.
- Tables render as markdown. A table up to `TABLE_CHUNK_CAP_TOKENS` (1200) stays one chunk; a larger
  one splits into row groups that each repeat the header, with the sheet `range` per group.

## Failures and retries

- A parse error (bad file) is permanent: `failed` with a stored reason, no retry.
- Blob, DB or network errors raise so Dramatiq retries: up to 5 retries, backoff 5 s → 5 min. While
  retrying, the document shows `processing` with `error = "Retrying (attempt n of 5): <category>."`
  (the category never contains raw exception text). After the last retry: `failed` with "Processing
  failed after repeated errors. Use Reprocess to try again."
- Over a `block` budget the embedding step raises `BudgetExceeded`: the document fails once with the
  budget message and is not retried.
- `POST /v1/documents/{id}/reprocess` re-queues a document.

## Embedding and indexes

- Each KB points at one active `embedding_indexes` row (provider, model, dim, version). Writes and
  queries always use the index's own model, never the current env setting (AC8).
- `POST /v1/kbs/{kb}/reindex` builds a new version with the configured embedder (queue `index`),
  switches the KB, retires the old version, and drops its HNSW index.
- Every embedding call writes a `usage_events` row (`kind=embed`); tokens are the provider's count,
  or `ceil(len/4)` per text when the provider reports none.

## Deletion

- `DELETE /v1/documents/{id}` removes the row (chunks and vectors cascade) and the blob when no other
  row uses it. For an archive, its member documents go too.
- `DELETE /v1/kbs/{id}` drops each index's HNSW first (named objects would otherwise be orphaned), then
  cascades.
