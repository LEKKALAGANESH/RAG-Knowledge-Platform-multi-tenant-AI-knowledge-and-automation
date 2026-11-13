"""A8 subtitles: tolerant SRT/WebVTT parsing into timed sections; end-to-end upload + chunks."""

import httpx
import pytest

from app.ingestion import mime
from app.ingestion.config import IngestionSettings
from app.ingestion.parsers import ParseError, parse
from app.ingestion.parsers.subtitles import hms, parse_cues
from tests.conftest import register
from tests.test_ingestion import _chunks, _ingest, stub_broker  # noqa: F401 - autouse fixture

SRT = (
    "﻿1\r\n00:00:01,000 --> 00:00:03,500\r\nWelcome to the <i>quarterly</i> review.\r\n\r\n"
    "2\r\n00:00:04,000 --> 00:00:06,000\r\n{\\an8}Revenue grew\r\nby twelve percent.\r\n\r\n"
    "00:01:05,250 --> 00:01:09,000\r\nNo index on this cue.\r\n\r\n"
    "garbage block without timing\r\n\r\n"
    "4\r\n00:01:10,000 --> 00:01:11,000\r\n\r\n"  # empty cue: skipped
)
VTT = """WEBVTT - Board meeting
Kind: captions

STYLE
::cue { color: yellow }

NOTE this is a comment
spanning lines

intro
00:00.500 --> 00:02.000 align:start position:10%
<v Alice>The zebra migration starts Monday.

01:02:03.004 --> 01:02:05.000
<c.loud>Budget</c> <00:01:02.500>approved.
"""


def settings(**kw: object) -> IngestionSettings:
    return IngestionSettings(**kw)  # type: ignore[arg-type]


def test_srt_cues_tolerate_bom_crlf_tags_and_missing_indices() -> None:
    cues = parse_cues(SRT.encode("utf-8").decode("utf-8-sig"), 100)
    assert cues == [
        (1.0, 3.5, "Welcome to the quarterly review."),
        (4.0, 6.0, "Revenue grew by twelve percent."),
        (65.25, 69.0, "No index on this cue."),
    ]


def test_vtt_skips_header_style_note_and_reads_settings_and_hours() -> None:
    cues = parse_cues(VTT, 100)
    assert cues == [(0.5, 2.0, "The zebra migration starts Monday."),
                    (3723.004, 3725.0, "Budget approved.")]  # fmt: skip


def test_hms() -> None:
    assert (hms(0), hms(65.9), hms(3723.004), hms(360_000)) == (
        "00:00:00", "00:01:05", "01:02:03", "100:00:00")  # fmt: skip


def test_detection_by_content_not_extension() -> None:
    assert mime.detect_mime(SRT.encode(), "a.txt") == mime.SRT
    assert mime.detect_mime(VTT.encode(), "a.txt") == mime.VTT
    legacy = SRT[1:].replace("quarterly", "café").encode("cp1252")  # [1:] drops the BOM
    assert mime.detect_mime(legacy, "a.srt") == mime.SRT
    assert mime.detect_mime(b"Meeting at 10:00 --> moved", "a.txt") == mime.TXT


def test_latin1_srt_is_decoded() -> None:
    doc = parse(mime.SRT, b"1\n00:00:01,000 --> 00:00:02,000\nCaf\xe9 cr\xe8me\n", settings())
    assert doc.sections[0].text == "Café crème"


def test_cues_merge_into_sections_without_splitting_a_cue() -> None:
    srt = "".join(
        f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},900\nword{i} " + "x " * 19 + "\n\n"
        for i in range(1, 8)
    )  # 7 cues x 20 words; 50-token sections hold 2 cues each
    doc = parse(mime.SRT, srt.encode(), settings(chunk_tokens=50))
    assert [s.loc for s in doc.sections] == [
        {"start": "00:00:01", "end": "00:00:02"}, {"start": "00:00:03", "end": "00:00:04"},
        {"start": "00:00:05", "end": "00:00:06"}, {"start": "00:00:07", "end": "00:00:07"},
    ]  # fmt: skip
    assert all(s.text.count("word") in (1, 2) for s in doc.sections)


def test_cue_cap() -> None:
    srt = "".join(f"00:00:0{i},000 --> 00:00:0{i},500\ncue {i}\n\n" for i in range(5))
    with pytest.raises(ParseError, match="over 3 cues"):
        parse(mime.SRT, srt.encode(), settings(max_subtitle_cues=3))


def test_no_cues_is_a_warning() -> None:
    doc = parse(mime.VTT, b"WEBVTT\n\nNOTE nothing here\n", settings())
    assert doc.sections == [] and doc.warnings == ["no subtitle cues with text were found"]


async def test_uploaded_vtt_is_chunked_with_timestamp_locs(client: httpx.AsyncClient) -> None:
    a = await register(client)
    doc = await _ingest(client, a, "board.vtt", VTT.encode())
    assert doc["status"] == "ready" and doc["mime"] == mime.VTT
    chunks = await _chunks(a, str(doc["id"]))
    assert [c.loc for c in chunks] == [{"start": "00:00:00", "end": "01:02:05"}]
    assert "zebra migration" in chunks[0].text
