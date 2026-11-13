"""The contract every connector implements. The sync engine (sync.py) owns all DB work."""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, JsonValue

AuthKind = Literal["none", "api_key", "basic", "oauth2", "service_account", "connection_string"]
Config = dict[str, Any]


class ConnectorError(Exception):
    """User-facing failure (bad config, unreachable source). Message must not contain secrets."""


class ConnectorMeta(BaseModel):
    type: str
    title: str
    description: str
    category: Literal["storage", "business", "meta", "web", "api", "database"]
    auth: AuthKind
    config_schema: dict[str, JsonValue]  # JSON Schema; the integrations UI renders forms from it


@dataclass(frozen=True)
class Item:
    """One source object. `fetch` may raise: the run then counts it as failed (transient) and
    keeps the old version, since a failed item is never treated as deleted."""

    external_key: str
    name: str
    fetch: Callable[[], Awaitable[bytes]]
    mime_hint: str | None = None
    content_hash: str | None = None  # connector's change marker; defaults to sha256(bytes)
    meta: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True)
class Checked:
    """Rich test() result. `tables` maps each readable table to its columns (database sources)."""

    message: str
    tables: dict[str, list[str]] | None = None


class Connector(Protocol):
    meta: ConnectorMeta

    def validate_config(self, raw: Config) -> Config:
        """Normalized config, or raises ConnectorError / pydantic.ValidationError."""
        ...

    async def test(self, config: Config, secret: str | None) -> str | Checked:
        """Short success message (or Checked), or raises ConnectorError. The API runs it before
        saving a new source or a changed config/secret."""
        ...

    def discover(self, config: Config, secret: str | None, cursor: Config) -> AsyncIterator[Item]:
        """Yields every current item. Raising aborts the run (failed, nothing deleted). It may
        update `cursor` in place; the engine saves it only after a successful run. An empty
        cursor (first run, `?full=true`, config changed) must mean a full view. Setting
        `cursor["incremental"] = True` marks a run that yields only changed items: absent items
        are then NOT deleted (the engine pops the flag before saving)."""
        ...
