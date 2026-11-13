from functools import lru_cache
from typing import Annotated

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="../.env", extra="ignore")

    database_url: SecretStr
    migration_database_url: SecretStr
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: SecretStr = Field(min_length=32)
    access_token_ttl_minutes: int = Field(default=15, gt=0)
    refresh_token_ttl_days: int = Field(default=7, gt=0)
    # A rotated refresh token presented again within this window returns the same successor
    # pair (parallel refreshes). Later reuse = theft: the whole session family is revoked.
    refresh_reuse_grace_seconds: int = Field(default=30, ge=0, le=300)
    fernet_keys: SecretStr
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]
    log_level: str = "INFO"
    # Owned by retrieval/embedders; read here only to scrub it from logs. Importing
    # app.retrieval from here would register its actors and cycle back into app.worker.
    embedding_api_key: SecretStr | None = None
    # Same: owned by retrieval/qdrant_store and retrieval/rerank, mirrored only for log scrubbing.
    qdrant_api_key: SecretStr | None = None
    rerank_api_key: SecretStr | None = None
    # Operator gate for per-source allow_private_endpoint (self-hosted/BYOC). Not a secret.
    connectors_allow_private_endpoints: bool = False
    # HMAC key for API-key hashes and audit IP hashes. Unset = derived from jwt_secret, so rotating
    # the JWT secret would then also invalidate every API key; set it separately in production.
    api_key_pepper: SecretStr | None = None
    # Requests per minute. auth = per client IP; the others per user or API key.
    rate_limit_auth: int = Field(default=10, gt=0)
    rate_limit_answer: int = Field(default=30, gt=0)
    rate_limit_upload: int = Field(default=60, gt=0)
    rate_limit_search: int = Field(default=120, gt=0)
    # Key prefix so dev/test servers sharing one Redis keep separate buckets; "" = prod keys.
    rate_limit_namespace: str = ""
    # S11: peers whose X-Forwarded-For is believed (comma list of IPs/CIDRs, "*" = any). Set it to
    # the BFF's address; anyone else's XFF is ignored. Same env var uvicorn's own flag reads.
    forwarded_allow_ips: str = "127.0.0.1"
    # --- OAuth connectors. Google Drive stays hidden until the operator sets this AND
    # the client id/secret of a Google Cloud OAuth client ("Web application" type).
    connector_gdrive_enabled: bool = False
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: SecretStr | None = None
    # Public base URL of the WEB APP (FX-1): redirect_uri = <base>/oauth/finish (must be
    # registered on the OAuth client exactly). That page posts code+state to the API's
    # authenticated POST /v1/connectors/oauth/finish; the API has no public callback.
    oauth_redirect_base: str = "http://localhost:3000"
    frontend_url: str = "http://localhost:3000"
    # --- A7b OneDrive/SharePoint (Microsoft identity platform v2) and Dropbox. Hidden until the
    # owner sets the flag AND the app registration's client id/secret (Dropbox: app key/secret).
    connector_onedrive_enabled: bool = False
    microsoft_oauth_client_id: str | None = None
    microsoft_oauth_client_secret: SecretStr | None = None
    # common | organizations | consumers | a tenant id/domain (single-tenant org apps).
    microsoft_oauth_tenant: str = Field(
        default="common", pattern=r"^(common|organizations|consumers|[A-Za-z0-9.-]{1,100})$"
    )
    connector_dropbox_enabled: bool = False
    dropbox_oauth_client_id: str | None = None
    dropbox_oauth_client_secret: SecretStr | None = None
    # --- FX-2 resource limits. Request body cap for every route except document upload, which
    # allows MAX_UPLOAD_MB + 1 MB of multipart overhead (app/core/bodylimit.py).
    max_request_body_kb: int = Field(default=1024, gt=0)
    # --- H2: open SSE streams (answer + agent events) per user or API key (app/core/ratelimit.py).
    max_concurrent_streams: int = Field(default=5, gt=0)
    # --- C2 generic REST connector (+ POST /v1/connectors/rest/openapi). On by default: it needs
    # no provider app; credentials are per source.
    connector_rest_enabled: bool = True

    @field_validator("*", mode="before")
    @classmethod
    def _blank_optional_is_unset(cls, v: object, info: ValidationInfo) -> object:
        # N1/N2: `API_KEY_PEPPER=` in .env arrives as "", which `is None` checks read as configured.
        blank = isinstance(v, str) and not v.strip()
        optional = info.field_name is not None and cls.model_fields[info.field_name].default is None
        return None if blank and optional else v

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        return [o.strip() for o in v.split(",") if o.strip()] if isinstance(v, str) else v

    @field_validator("fernet_keys")
    @classmethod
    def _valid_fernet_keys(cls, v: SecretStr) -> SecretStr:
        keys = [k.strip() for k in v.get_secret_value().split(",") if k.strip()]
        if not keys:
            raise ValueError("at least one Fernet key is required")
        for key in keys:
            Fernet(key)  # raises ValueError on a malformed key
        return v

    def secret_values(self) -> list[str]:
        """Raw secrets, so the log formatter can scrub them if one ever leaks into a message."""
        from app.llm.config import get_llm_settings  # leaf module, no app imports

        llm = get_llm_settings()
        provider_keys = [
            llm.anthropic_api_key,
            llm.openai_api_key,
            self.embedding_api_key,
            self.qdrant_api_key,
            self.rerank_api_key,
            self.api_key_pepper,
            self.google_oauth_client_secret,
            self.microsoft_oauth_client_secret,
            self.dropbox_oauth_client_secret,
        ]
        keys = self.fernet_keys.get_secret_value().split(",")
        return [
            self.jwt_secret.get_secret_value(),
            *(k.strip() for k in keys if k.strip()),
            *(k.get_secret_value() for k in provider_keys if k is not None),
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # values come from env
