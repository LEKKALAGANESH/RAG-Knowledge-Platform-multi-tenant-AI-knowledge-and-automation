"""SRT / WebVTT: cues grouped into timed sections that never split a cue."""

import re

from app.ingestion.chunking import count_tokens
from app.ingestion.config import IngestionSettings
from app.ingestion.parsers import ParsedDoc, ParseError, Section

Cue = tuple[float, float, str]  # start s, end s, text

_TIME = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")
_TAGS = re.compile(r"<[^>]*>|\{\\[^}]*\}")  # VTT <v Name>/<i>/<00:01.000>, SRT {\an8}


def hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _seconds(m: re.Match[str]) -> float:
    h, mi, s, ms = m.groups()
    return int(h or 0) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_cues(text: str, max_cues: int) -> list[Cue]:
    """Blocks without a `-->` line (WEBVTT header, NOTE, STYLE, REGION, junk) are skipped;
    cue indices are optional; CRLF/CR and a BOM are tolerated."""
    cues: list[Cue] = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿"))
    for block in blocks:
        lines = block.strip().split("\n")
        timing = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if timing is None:
            continue
        stamps = list(_TIME.finditer(lines[timing]))
        if len(stamps) < 2:
            continue
        body = " ".join(_TAGS.sub("", ln).strip() for ln in lines[timing + 1 :])
        if body.strip():
            if len(cues) >= max_cues:
                raise ParseError(f"The subtitles have over {max_cues:,} cues; that is the limit.")
            cues.append((_seconds(stamps[0]), _seconds(stamps[1]), " ".join(body.split())))
    return cues


def timed_sections(cues: list[Cue], size: int) -> list[Section]:
    """Consecutive cues merged up to ~`size` tokens; loc spans the first start to the last end."""
    sections: list[Section] = []
    group: list[Cue] = []

    def flush() -> None:
        if group:
            loc = {"start": hms(group[0][0]), "end": hms(group[-1][1])}
            sections.append(Section("\n".join(c[2] for c in group), dict(loc)))

    for cue in cues:
        if group and count_tokens(" ".join(c[2] for c in group)) + count_tokens(cue[2]) > size:
            flush()
            group = []
        group.append(cue)
    flush()
    return sections


def parse(data: bytes, s: IngestionSettings) -> ParsedDoc:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:  # legacy SRT files are often cp1252 / latin-1
        text = data.decode("latin-1")
    cues = parse_cues(text, s.max_subtitle_cues)
    if not cues:
        return ParsedDoc(warnings=["no subtitle cues with text were found"])
    return ParsedDoc(timed_sections(cues, s.chunk_tokens))
