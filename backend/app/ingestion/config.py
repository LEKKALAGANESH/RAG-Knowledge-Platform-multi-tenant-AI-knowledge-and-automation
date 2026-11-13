from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class IngestionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file="../.env", extra="ignore")

    blob_backend: Literal["local", "s3"] = "local"
    blob_dir: str = "./data/blobs"
    s3_endpoint_url: str | None = "http://localhost:9000"
    s3_region: str = "us-east-1"
    s3_bucket: str = "rag-documents"
    s3_access_key: SecretStr | None = None
    s3_secret_key: SecretStr | None = None
    max_upload_mb: int = Field(default=50, gt=0)
    chunk_tokens: int = Field(default=400, ge=50)
    chunk_overlap: int = Field(default=50, ge=0)
    table_chunk_cap_tokens: int = Field(default=1200, ge=50)
    # Parser-bomb limits (S4). Exceeding any one fails the document with a readable reason;
    # nothing is ever indexed partially.
    parse_timeout_s: float = Field(default=120, gt=0)
    parse_memory_mb: int = Field(default=2048, ge=256)  # POSIX only (RLIMIT_AS on the child)
    max_unzipped_mb: int = Field(default=256, gt=0)
    max_zip_members: int = Field(default=10_000, gt=0)
    max_zip_ratio: int = Field(default=100, gt=1)
    max_pdf_pages: int = Field(default=2_000, gt=0)
    max_sheet_cells: int = Field(default=1_000_000, gt=0)
    max_text_chars: int = Field(default=20_000_000, gt=0)
    max_chunks_per_doc: int = Field(default=20_000, gt=0)
    # OCR (B8). None = on when the tesseract binary is found (ocr.ocr_enabled decides).
    ocr_enabled: bool | None = None
    ocr_langs: str = Field(default="eng", pattern=r"^[A-Za-z0-9_]+(\+[A-Za-z0-9_]+)*$")
    tesseract_cmd: str = "tesseract"  # full path when it is not on PATH (Windows installer)
    ocr_dpi: int = Field(default=300, ge=72, le=600)
    max_image_pixels: int = Field(default=50_000_000, gt=0)  # decompression-bomb cap per image
    # Archives (B8). Total size and ratio reuse max_unzipped_mb / max_zip_ratio.
    archive_max_members: int = Field(default=1_000, gt=0)
    archive_max_depth: int = Field(default=1, ge=1)  # 1 = archives inside archives are skipped
    # Audio/video (A8). None = on when faster-whisper and the model are present locally.
    transcribe_enabled: bool | None = None
    transcribe_model: str = "small"  # size name (tiny/base/small/medium/large-v3) or a model dir
    transcribe_model_dir: str | None = None  # model cache dir; None = Hugging Face default cache
    transcribe_allow_download: bool = False  # fetch a missing model on first use
    max_media_minutes: float = Field(default=120, gt=0)
    transcribe_timeout_factor: float = Field(default=2.0, gt=0)  # x duration, + parse_timeout_s
    transcribe_memory_mb: int = Field(default=4096, ge=512)  # POSIX only (RLIMIT_AS on the child)
    max_subtitle_cues: int = Field(default=50_000, gt=0)


@lru_cache
def get_ingestion_settings() -> IngestionSettings:
    return IngestionSettings()
