import json

from app.ingestion.parsers import ParsedDoc, Section, decode


def _record(value: object, n: int) -> Section:
    return Section(json.dumps(value, ensure_ascii=False, indent=2), {"record": n})


def parse_json(data: bytes) -> ParsedDoc:
    value = json.loads(decode(data))
    items = value if isinstance(value, list) else [value]
    return ParsedDoc([_record(v, n) for n, v in enumerate(items, start=1)])


def parse_jsonl(data: bytes) -> ParsedDoc:
    lines = decode(data).splitlines()
    return ParsedDoc([_record(json.loads(ln), n) for n, ln in enumerate(lines, 1) if ln.strip()])
