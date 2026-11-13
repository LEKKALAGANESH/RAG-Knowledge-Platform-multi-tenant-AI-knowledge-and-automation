from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file="../.env", extra="ignore")

    llm_provider: Literal["anthropic", "openai_compat"] = "anthropic"
    llm_model: str = "claude-sonnet-5-5"
    llm_fallback_model: str | None = None
    # Cheap model for follow-up rewrites; default: claude-haiku-5-5 (anthropic) or LLM_MODEL.
    llm_rewrite_model: str | None = None
    llm_base_url: str | None = None  # OpenAI-compatible endpoint (Ollama, vLLM, LM Studio, Azure)
    llm_timeout_s: float = Field(default=60.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None


@lru_cache
def get_llm_settings() -> LLMSettings:
    return LLMSettings()
