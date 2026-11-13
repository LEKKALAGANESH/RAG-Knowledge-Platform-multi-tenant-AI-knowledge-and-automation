"""Agent tool registry. The list, schemas and risk classes are code constants: nothing a model or
a document says can add a tool, change a risk class or skip an approval (AC10). Every handler runs
in the run owner's RLS session and re-validates its input."""

import asyncio
import io
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Annotated

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.worksheet.worksheet import Worksheet
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.models import XLSX_MIME, AgentRun, Report, Risk
from app.agent.numbers import CalcError, calculate, extract_fields
from app.core.csv_export import FORMULA_START
from app.ingestion.storage import get_blob_store
from app.kb.models import Chunk, Document
from app.llm.providers import ToolSpec
from app.retrieval.embedders import EmbeddingError
from app.retrieval.search import search


class ToolError(Exception):
    """A failure the model is told about (as an error tool_result); the run continues."""


@dataclass
class ToolCtx:
    session: AsyncSession
    run: AgentRun
    # Chunks this run has already received from search/read tools: id -> {document_name, loc, ...}
    seen: dict[str, dict[str, JsonValue]] = field(default_factory=dict)


Handler = Callable[[ToolCtx, dict[str, JsonValue]], Awaitable[dict[str, JsonValue]]]


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: type[Args]
    risk: Risk
    run: Handler

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.args.model_json_schema())


def _chunk_out(c: Chunk, doc_name: str) -> dict[str, JsonValue]:
    return {"chunk_id": str(c.id), "document_name": doc_name, "loc": c.loc, "text": c.text}


def _require_seen(ctx: ToolCtx, ids: list[uuid.UUID]) -> list[uuid.UUID]:
    unknown = [str(i) for i in ids if str(i) not in ctx.seen]
    if unknown:
        raise ToolError(f"Unknown chunk ids (use ids returned by search_knowledge): {unknown[:5]}")
    return ids


class SearchArgs(Args):
    query: str = Field(min_length=1, max_length=500)
    k: int = Field(default=8, ge=1, le=20)


async def search_knowledge(ctx: ToolCtx, raw: dict[str, JsonValue]) -> dict[str, JsonValue]:
    a = SearchArgs.model_validate(raw)
    try:
        hits = await search(ctx.session, a.query, ctx.run.kb_ids, a.k)
    except EmbeddingError as exc:
        raise ToolError("The embedding provider failed; retry the search.") from exc
    chunks: list[JsonValue] = [
        {
            "chunk_id": str(h.chunk_id),
            "document_name": h.document_name,
            "loc": h.loc,
            "text": h.text,
        }
        for h in hits
    ]
    return {"chunks": chunks}


class ChunkIdsArgs(Args):
    chunk_ids: list[uuid.UUID] = Field(min_length=1, max_length=50)


async def _load_chunks(ctx: ToolCtx, ids: list[uuid.UUID]) -> list[tuple[Chunk, str]]:
    rows = await ctx.session.execute(
        select(Chunk, Document.name)
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.id.in_(_require_seen(ctx, ids)), Chunk.kb_id.in_(ctx.run.kb_ids))
    )
    by_id = {c.id: (c, name) for c, name in rows}
    return [by_id[i] for i in ids if i in by_id]


async def read_chunks(ctx: ToolCtx, raw: dict[str, JsonValue]) -> dict[str, JsonValue]:
    a = ChunkIdsArgs.model_validate(raw)
    return {"chunks": [_chunk_out(c, name) for c, name in await _load_chunks(ctx, a.chunk_ids)]}


class CalcArgs(Args):
    expression: str = Field(min_length=1, max_length=4000)


async def calculate_tool(_: ToolCtx, raw: dict[str, JsonValue]) -> dict[str, JsonValue]:
    a = CalcArgs.model_validate(raw)
    try:
        return {"result": str(calculate(a.expression))}
    except CalcError as exc:
        raise ToolError(str(exc)) from exc


async def extract_table(ctx: ToolCtx, raw: dict[str, JsonValue]) -> dict[str, JsonValue]:
    a = ChunkIdsArgs.model_validate(raw)
    rows: list[JsonValue] = []
    first: dict[tuple[str | None, ...], str] = {}
    for c, name in await _load_chunks(ctx, a.chunk_ids):
        f = extract_fields(c.text)
        key = (f["invoice_number"], f["vendor"], f["currency"], f["amount"])
        dup = first.get(key) if f["invoice_number"] else None
        first.setdefault(key, str(c.id))
        rows.append({"chunk_id": str(c.id), "document_name": name, **f, "duplicate_of": dup})
    return {"rows": rows}


SheetTitle = Annotated[
    str, StringConstraints(min_length=1, max_length=31, pattern=r"^[^\[\]:*?/\\]+$")
]
Cell = str | int | Decimal | None
DECIMAL = re.compile(r"-?\d{1,15}\.\d{1,10}")  # needs a fraction: years and ids stay text


class SheetIn(Args):
    title: SheetTitle
    columns: list[Annotated[str, StringConstraints(max_length=100)]] = Field(
        min_length=1, max_length=30
    )
    rows: list[list[Cell]] = Field(max_length=5000)

    @model_validator(mode="after")
    def _widths(self) -> "SheetIn":
        if any(len(r) != len(self.columns) for r in self.rows):
            raise ValueError("every row needs exactly one cell per column")
        return self


class ReportArgs(Args):
    name: str = Field(min_length=1, max_length=100)
    sheets: list[SheetIn] = Field(min_length=1, max_length=10)
    source_chunk_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _titles(self) -> "ReportArgs":
        titles = [s.title.lower() for s in self.sheets]
        if len(set(titles)) != len(titles) or "sources" in titles:
            raise ValueError("sheet titles must be unique and not 'Sources'")
        return self


def _write_sheet(ws: Worksheet, columns: list[str], rows: list[list[Cell]]) -> None:
    ws.append(columns)
    for cell in ws[1]:
        cell.font = Font(name="Arial", bold=True)
    for row in rows:
        ws.append(row)
    for line in ws.iter_rows():  # row 1 too: a header is model/document text as well (S7)
        for cell in line:
            if cell.row > 1:
                cell.font = Font(name="Arial")
                if isinstance(cell.value, str) and DECIMAL.fullmatch(cell.value):
                    cell.value = Decimal(cell.value)  # money arrives as decimal strings
            if isinstance(cell.value, Decimal):
                cell.number_format = "#,##0.00"
            elif isinstance(cell.value, str) and cell.value.startswith(FORMULA_START):
                cell.data_type = "s"  # document text is data, never a formula
                cell.quotePrefix = True  # and stays text if a person edits the cell


def build_xlsx(a: ReportArgs, sources: list[dict[str, JsonValue]]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for sheet in a.sheets:
        _write_sheet(wb.create_sheet(sheet.title), sheet.columns, sheet.rows)
    src_rows: list[list[Cell]] = [
        [str(s["chunk_id"]), str(s["document_name"]), str(s["loc"]), str(s.get("text", ""))[:300]]
        for s in sources
    ]
    _write_sheet(
        wb.create_sheet("Sources"), ["chunk_id", "document", "location", "excerpt"], src_rows
    )
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def create_xlsx_report(ctx: ToolCtx, raw: dict[str, JsonValue]) -> dict[str, JsonValue]:
    a = ReportArgs.model_validate(raw)
    ids = _require_seen(ctx, list(dict.fromkeys(a.source_chunk_ids)))
    sources = [{"chunk_id": str(i), **ctx.seen[str(i)]} for i in ids]
    data = build_xlsx(a, sources)
    name = re.sub(
        r"[^A-Za-z0-9 ._-]", "_", a.name
    )  # ASCII: it goes into a header.strip() or "report"
    report = Report(org_id=ctx.run.org_id, run_id=ctx.run.id, name=f"{name}.xlsx", mime=XLSX_MIME)
    report.id = uuid.uuid4()
    report.blob_key, report.size = f"{ctx.run.org_id}/reports/{report.id}.xlsx", len(data)
    await asyncio.to_thread(get_blob_store().put, report.blob_key, data)
    ctx.session.add(report)
    await ctx.session.flush()
    return {
        "report_id": str(report.id),
        "name": report.name,
        "size": report.size,
        "download": f"/v1/reports/{report.id}",
    }


class DeleteReportArgs(Args):
    report_id: uuid.UUID


async def delete_report(ctx: ToolCtx, raw: dict[str, JsonValue]) -> dict[str, JsonValue]:
    a = DeleteReportArgs.model_validate(raw)
    report = await ctx.session.get(Report, a.report_id)
    if report is None or report.run_id != ctx.run.id:
        raise ToolError("Only a report this run created can be deleted.")
    await ctx.session.delete(report)
    await ctx.session.flush()
    await asyncio.to_thread(get_blob_store().delete, report.blob_key)
    return {"deleted": str(a.report_id)}


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool(
            "search_knowledge",
            "Hybrid search over the run's knowledge bases. Returns chunks with chunk_id, "
            "document_name, loc and text.",
            SearchArgs,
            "read",
            search_knowledge,
        ),
        Tool(
            "read_chunks",
            "Re-read chunks by chunk_id (ids returned earlier in this run).",
            ChunkIdsArgs,
            "read",
            read_chunks,
        ),
        Tool(
            "calculate",
            "Exact decimal arithmetic: + - * / ( ) and sum/min/max/abs/round([...]). "
            "Use it for every total or difference.",
            CalcArgs,
            "read",
            calculate_tool,
        ),
        Tool(
            "extract_table",
            "Parse invoice-like fields (invoice_number, vendor, category, date, currency, amount) "
            "from chunks; flags duplicates (duplicate_of) of the same invoice.",
            ChunkIdsArgs,
            "read",
            extract_table,
        ),
        Tool(
            "create_xlsx_report",
            "Create a downloadable Excel report from sheets of rows, plus a Sources sheet listing "
            "source_chunk_ids. Money cells: decimal strings, never converted between currencies.",
            ReportArgs,
            "reversible_write",
            create_xlsx_report,
        ),
        Tool(
            "delete_report",
            "Delete a report this run created. Needs a person's approval.",
            DeleteReportArgs,
            "irreversible_write",
            delete_report,
        ),
    )
}
