"""A8 audio/video: MIME detection, duration cap, transcription (mocked; real run if a model is
cached locally), readable failure when transcription is unavailable."""

import io
import uuid
import wave
from pathlib import Path
from types import SimpleNamespace

import av
import httpx
import numpy as np
import pytest

from app.ingestion import mime
from app.ingestion.config import IngestionSettings, get_ingestion_settings
from app.ingestion.parsers import ParseError, parse, parse_isolated
from app.ingestion.parsers import media as media_mod
from app.ingestion.pipeline import process_document
from tests.conftest import register
from tests.test_ingestion import _kb, _upload, stub_broker  # noqa: F401 - autouse fixture

LOCAL_MODEL = media_mod.model_path(IngestionSettings(transcribe_model="tiny"))
_DTYPES = {"s16": np.int16, "s32": np.int32, "flt": np.float32, "dbl": np.float64}


def wav(seconds: float = 1.0, rate: int = 16_000) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return out.getvalue()


def encoded(fmt: str, codec: str, seconds: float = 1.0, rate: int = 16_000) -> bytes:
    """Real silent media through PyAV's bundled encoders (no ffmpeg binary needed)."""
    out = io.BytesIO()
    with av.open(out, "w", format=fmt) as c:
        st = c.add_stream(codec, rate=rate, layout="mono")
        n = st.codec_context.frame_size or 1024
        fmt_name = st.codec_context.format.name
        for i in range(int(seconds * rate) // n):
            f = av.AudioFrame.from_ndarray(
                np.zeros((1, n), dtype=_DTYPES[fmt_name.rstrip("p")]), format=fmt_name,
                layout="mono",
            )  # fmt: skip
            f.sample_rate, f.pts = rate, i * n
            c.mux(st.encode(f))
        c.mux(st.encode(None))
    return out.getvalue()


def settings(**kw: object) -> IngestionSettings:
    return IngestionSettings(**kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("fmt", "codec", "rate", "kind"),
    [("mp4", "aac", 16_000, mime.MP4), ("ipod", "aac", 16_000, mime.M4A),
     ("mov", "aac", 16_000, mime.MOV), ("webm", "libopus", 48_000, mime.WEBM),
     ("mp3", "libmp3lame", 16_000, mime.MP3), ("adts", "aac", 16_000, mime.AAC)],
)  # fmt: skip
def test_real_media_is_detected_and_its_duration_probed(
    fmt: str, codec: str, rate: int, kind: str
) -> None:
    data = encoded(fmt, codec, 2.0, rate)
    assert mime.detect_mime(data, "no-extension") == kind
    assert 1.0 < media_mod.probe_seconds(data, settings()) < 3.0


@pytest.mark.parametrize(
    ("head", "kind"),
    [(wav(), mime.WAV), (b"ID3\x04\x00" + b"\x00" * 20, mime.MP3),
     (b"\xff\xfb\x90\x00" + b"\x00" * 20, mime.MP3), (b"\xff\xf1\x50\x80" + b"\x00" * 20, mime.AAC),
     (b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 12, mime.MP4),
     (b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 12, mime.M4A),
     (b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 12, mime.MOV),
     (b"\x00\x00\x00\x08wide\x00\x00\x00\x10mdat", mime.MOV),
     (b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01", mime.WEBM),
     (b"\x00\x00\x00\x18ftypheic" + b"\x00" * 12, None)],  # HEIC photo: not media
)  # fmt: skip
def test_magic_bytes(head: bytes, kind: str | None) -> None:
    assert mime.detect_mime(head, "x.bin") == kind


def test_jpeg_is_not_mistaken_for_mpeg_audio() -> None:
    assert mime.detect_mime(b"\xff\xd8\xff\xe0" + b"\x00" * 20, "x") == mime.JPEG


def test_duration_cap_refuses_before_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_decode(*_a: object, **_k: object) -> None:
        raise AssertionError("decoded despite the duration cap")

    import faster_whisper

    monkeypatch.setattr(faster_whisper, "decode_audio", no_decode)
    s = settings(transcribe_allow_download=True, max_media_minutes=0.02)  # 1.2 s
    with pytest.raises(ParseError, match=r"over 0.02 minutes"):
        parse(mime.WAV, wav(3.0), s)


def test_undecodable_media_fails_readably() -> None:
    s = settings(transcribe_allow_download=True)
    with pytest.raises(ParseError, match="could not be read"):
        parse(mime.MP4, b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64, s)


class FakeModel:
    def transcribe(self, audio: np.ndarray, vad_filter: bool) -> tuple[object, object]:
        assert vad_filter and audio.dtype == np.float32 and abs(len(audio) - 32_000) < 4_000
        segs = [SimpleNamespace(start=0.0, end=4.2, text=" Hello and welcome."),
                SimpleNamespace(start=4.2, end=3725.0, text="  "),  # blank: dropped
                SimpleNamespace(start=3725.0, end=3731.5, text=" Budget is approved.")]  # fmt: skip
        return iter(segs), SimpleNamespace(language="en", language_probability=0.98765)


def test_transcription_turns_segments_into_timed_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media_mod, "_model", lambda s: FakeModel())
    s = settings(transcribe_allow_download=True, chunk_tokens=50)
    doc = parse(mime.MP4, encoded("mp4", "aac", 2.0), s)  # video container: audio track used
    assert [x.text for x in doc.sections] == ["Hello and welcome.\nBudget is approved."]
    assert doc.sections[0].loc == {"start": "00:00:00", "end": "01:02:11"}
    assert doc.meta == {"language": "en", "language_probability": 0.988, "duration_s": 2}


def test_no_speech_is_a_warning_not_text(monkeypatch: pytest.MonkeyPatch) -> None:
    info = SimpleNamespace(language="en", language_probability=0.5)
    silent = SimpleNamespace(transcribe=lambda audio, vad_filter: (iter([]), info))
    monkeypatch.setattr(media_mod, "_model", lambda s: silent)
    doc = parse(mime.WAV, wav(2.0), settings(transcribe_allow_download=True))
    assert doc.sections == [] and doc.warnings == ["no speech was detected"]


def test_media_without_audio_track_fails_readably() -> None:
    out = io.BytesIO()
    with av.open(out, "w", format="mp4") as c:
        st = c.add_stream("mpeg4", rate=1)
        st.width = st.height = 16
        frame = av.VideoFrame.from_ndarray(np.zeros((16, 16, 3), np.uint8), format="rgb24")
        c.mux(st.encode(frame.reformat(format="yuv420p")))
        c.mux(st.encode(None))
    with pytest.raises(ParseError, match="no audio track"):
        parse(mime.MP4, out.getvalue(), settings(transcribe_allow_download=True))


def test_enabled_needs_package_and_local_model(tmp_path: Path) -> None:
    assert not media_mod.transcribe_enabled(settings(transcribe_enabled=False))
    missing = settings(transcribe_model="tiny", transcribe_model_dir=str(tmp_path))
    assert media_mod.model_path(missing) is None and not media_mod.transcribe_enabled(missing)
    assert media_mod.transcribe_enabled(settings(transcribe_model=str(tmp_path)))  # a model dir


def test_unavailable_fails_in_the_isolated_child() -> None:
    with pytest.raises(ParseError, match="Transcription is not available"):
        parse_isolated(mime.WAV, wav(), settings(transcribe_enabled=False))


def test_isolated_child_probes_then_reports_a_broken_model(tmp_path: Path) -> None:
    """Duration message first (timeout stretched), then a readable error from the empty dir."""
    with pytest.raises(ParseError, match="could not be read"):
        parse_isolated(mime.WAV, wav(), settings(transcribe_model=str(tmp_path)))


def test_isolated_child_enforces_the_duration_cap() -> None:
    s = settings(transcribe_allow_download=True, max_media_minutes=0.01)
    with pytest.raises(ParseError, match="minutes long"):
        parse_isolated(mime.WAV, wav(2.0), s)


async def test_media_upload_without_transcription_fails_with_reason(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRANSCRIBE_ENABLED", "false")
    get_ingestion_settings.cache_clear()
    a = await register(client)
    r = await _upload(client, a, await _kb(client, a), "call.wav", wav())
    assert r.status_code == 201 and r.json()["mime"] == mime.WAV
    await process_document(uuid.UUID(r.json()["id"]), a.org_id)
    doc = (await client.get(f"/v1/documents/{r.json()['id']}", headers=a.headers)).json()
    assert doc["status"] == "failed" and doc["error"].startswith("Transcription is not available")


@pytest.mark.skipif(LOCAL_MODEL is None, reason="no whisper 'tiny' model cached on this machine")
def test_real_model_finds_no_speech_in_silence() -> None:
    doc = parse_isolated(mime.WAV, wav(2.0), settings(transcribe_model="tiny"))
    assert doc.sections == [] and doc.meta["duration_s"] == 2
