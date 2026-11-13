"""Pure number helpers for agent tools: safe Decimal arithmetic and invoice field extraction.
No eval, no floats for money."""

import ast
import re
from collections.abc import Callable
from decimal import Decimal, DecimalException, InvalidOperation, localcontext

# Work bounds (S6). The libmpdec/regex C code holds the GIL, so no timeout can interrupt it; these
# caps keep every call small instead: <= MAX_NODES ops, each on <= 28-digit results (context prec)
# with exponents in +-EXP_LIMIT, literals <= MAX_DIGITS digits, round() places <= MAX_PLACES.
MAX_NODES = 400
MAX_DIGITS, MAX_LITERAL_EXP, EXP_LIMIT, MAX_PLACES = 50, 100, 1000, 28


class CalcError(ValueError):
    pass


def _flatten(values: list[Decimal | list[Decimal]]) -> list[Decimal]:
    return [x for v in values for x in (v if isinstance(v, list) else [v])]


def _round(*args: Decimal) -> Decimal:
    places = args[1] if len(args) > 1 else Decimal(0)
    if places != places.to_integral_value() or abs(places) > MAX_PLACES:
        raise CalcError(f"round() places must be a whole number, at most {MAX_PLACES}.")
    return args[0].quantize(Decimal(1).scaleb(-int(places)))


def _literal(text: str) -> Decimal:
    d = Decimal(text)
    if len(d.as_tuple().digits) > MAX_DIGITS or abs(d.adjusted()) > MAX_LITERAL_EXP:
        raise CalcError(f"Numbers are limited to {MAX_DIGITS} digits, up to 1e{MAX_LITERAL_EXP}.")
    return d


FUNCS: dict[str, Callable[..., Decimal]] = {
    "sum": lambda *a: sum(a, Decimal(0)),
    "min": min,
    "max": max,
    "abs": abs,
    "round": _round,
}
BINOPS: dict[type[ast.operator], Callable[[Decimal, Decimal], Decimal]] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
}


def calculate(expression: str) -> Decimal:
    """Evaluate + - * / ( ), unary minus, and sum/min/max/abs/round([..]) over decimal literals.
    Every other syntax (names, attributes, **, calls to anything else) is rejected."""
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        raise CalcError("The expression is not valid arithmetic.") from exc
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
        raise CalcError(f"The expression is too long (max {MAX_NODES} terms).")

    def ev(node: ast.AST) -> Decimal | list[Decimal]:
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return _literal(ast.get_source_segment(expression, node) or "")  # exact literal text
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
            v = ev(node.operand)
            if isinstance(v, list):
                raise CalcError("A list cannot be negated.")
            return -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.BinOp) and type(node.op) in BINOPS:
            a, b = ev(node.left), ev(node.right)
            if isinstance(a, list) or isinstance(b, list):
                raise CalcError("Use sum(...) to add up a list.")
            return BINOPS[type(node.op)](a, b)
        if isinstance(node, ast.List | ast.Tuple):
            return _flatten([ev(e) for e in node.elts])
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in FUNCS
            and not node.keywords
        ):
            args = [ev(a) for a in node.args]
            flat = (
                _flatten(args)
                if node.func.id != "round"
                else [a for a in args if not isinstance(a, list)]
            )
            if not flat:
                raise CalcError(f"{node.func.id}() needs at least one number.")
            return FUNCS[node.func.id](*flat)
        raise CalcError(f"Unsupported syntax: {type(node).__name__}.")

    try:
        with localcontext(Emax=EXP_LIMIT, Emin=-EXP_LIMIT):  # Overflow traps -> CalcError
            out = ev(tree.body)
    except (DecimalException, InvalidOperation, ZeroDivisionError, RecursionError) as exc:
        raise CalcError("The arithmetic failed (division by zero or an invalid number).") from exc
    if isinstance(out, list):
        raise CalcError("The result is a list; wrap it in sum(), min() or max().")
    return out


SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR"}
# Starts only at the beginning of a digit run and never backtracks (possessive): linear time (S6).
_AMOUNT = r"(?<!\d)(?<!\d[,.' ])-?\d++(?:[,.' ]\d++)*+"
MONEY = re.compile(
    rf"(?:(?P<c1>\b[A-Z]{{3}}\b|[$€£¥₹])\s?(?P<a1>{_AMOUNT}))|(?:(?P<a2>{_AMOUNT})\s?(?P<c2>\b[A-Z]{{3}}\b|[€£¥₹]))"
)
ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
EU_DATE = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b")
# Linear-time (S6): no line-crossing \s at a line start, no lazy value before a trailing \s*$.
FIELD = re.compile(r"^[ \t]*([A-Za-z][A-Za-z #._/-]{0,40}?)[ \t]*[:=]\s*(\S.*)", re.MULTILINE)


def parse_amount(raw: str) -> Decimal | None:
    """'1,234.56', '1.234,56', "1'234.50", '1 234,5' -> Decimal; the last separator followed by
    1-2 digits is the decimal point, every other separator groups thousands."""
    s = raw.replace("'", "").replace(" ", "")
    m = re.search(r"[.,](\d{1,2})$", s)
    whole, frac = (s[: m.start()], m.group(1)) if m else (s, "")
    whole = whole.replace(",", "").replace(".", "")
    try:
        return Decimal(f"{whole}.{frac}" if frac else whole)
    except InvalidOperation:
        return None


def parse_money(text: str) -> tuple[str, Decimal] | None:
    for m in MONEY.finditer(text):
        cur, amt = (m["c1"], m["a1"]) if m["c1"] else (m["c2"], m["a2"])
        value = parse_amount(amt)
        if value is not None:
            return SYMBOLS.get(cur, cur), value
    return None


def parse_date(text: str) -> str | None:
    if m := ISO_DATE.search(text):
        return f"{m[1]}-{m[2]}-{m[3]}"
    if m := EU_DATE.search(text):  # day first: 31.12.2024 / 31/12/2024
        return f"{m[3]}-{int(m[2]):02d}-{int(m[1]):02d}"
    return None


def extract_fields(text: str) -> dict[str, str | None]:
    """Invoice-like fields from `Key: value` lines; the amount prefers a line keyed 'total'."""
    fields = {k.strip().lower(): v.strip() for k, v in FIELD.findall(text)}

    def pick(*words: str, exclude: str = "\0") -> str | None:
        return next(
            (v for k, v in fields.items() if any(w in k for w in words) and exclude not in k), None
        )

    money_line = pick("total", "amount", exclude="sub") or text  # not "Subtotal"
    money = parse_money(money_line) or parse_money(text)
    return {
        "invoice_number": pick("invoice", "number", "no.", exclude="date"),
        "vendor": pick("vendor", "supplier", "from", "seller"),
        "category": pick("category"),
        "date": parse_date(pick("date") or "") or parse_date(text),
        "currency": money[0] if money else None,
        "amount": str(money[1]) if money else None,
    }
