"""Speech-to-text for audio/video (faster-whisper, CPU int8); runs inside the isolated parse child.
PyAV ships its own FFmpeg libraries in the wheel, so hosts need no system ffmpeg."""

import io
import logging
import os
from typing import Any

from app.ingestion.config import IngestionSettings
from app.ingestion.parsers import TOO_BIG, ParsedDoc, ParseError
from app.ingestion.parsers.subtitles import timed_sections

log = logging.getLogger("app.ingestion")
UNAVAILABLE = "Transcription is not available on this server, so audio and video cannot be read."


def model_path(s: IngestionSettings) -> str | None:
    """Local model dir, or None when the package or the model is missing (no download here)."""
    try:
        from faster_whisper.utils import download_model
    except ImportError:
        return None
    if os.path.isdir(s.transcribe_model):
        return s.transcribe_model
    try:  # local_files_only: a cache lookup, never a network call
        return str(download_model(s.transcribe_model, local_files_only=True,
                                  cache_dir=s.transcribe_model_dir))  # fmt: skip
    except Exception:  # not cached / unknown size name
        return None


def transcribe_enabled(s: IngestionSettings) -> bool:
    if s.transcribe_enabled is False:
        return False
    if s.transcribe_allow_download:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return False
        return True
    return model_path(s) is not None


def log_transcribe_status(s: IngestionSettings) -> None:
    if transcribe_enabled(s):
        log.info("Transcription enabled (model %s)", s.transcribe_model)
    elif s.transcribe_enabled is False:
        log.info("Transcription disabled (TRANSCRIBE_ENABLED=false): audio/video uploads fail")
    else:
        log.warning(
            "Transcription disabled: whisper model %r not found locally; set TRANSCRIBE_MODEL_DIR "
            "or TRANSCRIBE_ALLOW_DOWNLOAD=true. Audio/video uploads fail; SRT/VTT still work.",
            s.transcribe_model,
        )


def probe_seconds(data: bytes, s: IngestionSettings) -> float:
    """Duration from the container header, checked against the cap BEFORE any decoding."""
    import av

    try:
        with av.open(io.BytesIO(data), mode="r") as c:
            if not c.streams.audio:
                raise ParseError("The file has no audio track to transcribe.")
            seconds = c.duration / 1_000_000 if c.duration else 0.0  # AV_TIME_BASE units
    except av.FFmpegError as exc:
        raise ParseError(f"The audio/video file could not be read ({exc})"[:300]) from None
    if seconds <= 0:
        raise ParseError("The length of this audio/video file could not be determined.")
    if seconds > s.max_media_minutes * 60:
        raise ParseError(f"{TOO_BIG} (media over {s.max_media_minutes:g} minutes long).")
    return seconds


def _model(s: IngestionSettings) -> Any:
    from faster_whisper import WhisperModel

    path = model_path(s)
    if path is None and not s.transcribe_allow_download:
        raise ParseError(UNAVAILABLE)
    return WhisperModel(path or s.transcribe_model, device="cpu", compute_type="int8",
                        download_root=s.transcribe_model_dir)  # fmt: skip


def parse_media(data: bytes, s: IngestionSettings) -> ParsedDoc:
    if not transcribe_enabled(s):
        raise ParseError(UNAVAILABLE)
    seconds = probe_seconds(data, s)
    from faster_whisper import decode_audio

    audio = decode_audio(io.BytesIO(data))  # first audio track (video too), 16 kHz mono
    segments, info = _model(s).transcribe(audio, vad_filter=True)  # VAD: no text from silence
    cues = [(seg.start, seg.end, seg.text.strip()) for seg in segments if seg.text.strip()]
    meta = {"language": info.language, "language_probability": round(info.language_probability, 3),
            "duration_s": round(seconds)}  # fmt: skip
    if not cues:
        return ParsedDoc(warnings=["no speech was detected"], meta=meta)
    return ParsedDoc(timed_sections(cues, s.chunk_tokens), meta=meta)
