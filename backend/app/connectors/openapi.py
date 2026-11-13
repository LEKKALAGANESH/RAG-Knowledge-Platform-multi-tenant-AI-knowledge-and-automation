"""OpenAPI 3 / Swagger 2 spec -> a SUGGESTED REST connector config (C2).

The spec is untrusted data: it is parsed (JSON, or YAML with the safe loader and no anchors or
aliases), never executed, and the API it describes is never called. Only GET operations without
path parameters become endpoints; everything else is listed as skipped. Any `$ref` that is not
local (`#/...`) refuses the whole spec, so no remote or file reference is ever resolved.
"""

import json
from typing import Any

import yaml

MAX_SPEC_CHARS = 500_000
MAX_NODES = 100_000
MAX_DEPTH = 64
MAX_REF_HOPS = 10
MAX_ENDPOINTS = 20  # RestConfig.endpoints max_length
_METHODS = {"put", "post", "delete", "options", "head", "patch", "trace"}
_TITLES = ("title", "name", "subject", "summary", "label")
_UPDATED = ("updated_at", "updatedAt", "modified_at", "modifiedAt", "last_modified", "updated")


def _dict(v: Any) -> dict[Any, Any]:
    return v if isinstance(v, dict) else {}


class SpecError(Exception):
    """Why the spec was refused. Safe to show (no spec content is echoed)."""


class _NoAliasLoader(yaml.SafeLoader):
    """SafeLoader (plain data types only) that also refuses aliases: a few KB of aliased YAML
    expand into a huge structure when walked (billion laughs)."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("YAML aliases are not accepted.")
        return super().compose_node(parent, index)


def _load(text: str) -> dict[str, Any]:
    try:
        spec = json.loads(text)
    except RecursionError:
        raise SpecError("The spec is nested too deeply.") from None
    except ValueError:
        try:
            spec = yaml.load(text, Loader=_NoAliasLoader)  # noqa: S506 - SafeLoader subclass
        except RecursionError:
            raise SpecError("The spec is nested too deeply.") from None
        except yaml.YAMLError:
            raise SpecError(
                "The spec is neither JSON nor YAML (YAML anchors/aliases are not accepted)."
            ) from None
    if not isinstance(spec, dict) or not isinstance(spec.get("paths"), dict):
        raise SpecError("Not an OpenAPI or Swagger document (no `paths`).")
    return spec


def _scan(spec: dict[str, Any]) -> None:
    """Size/depth limits and the local-$ref-only rule, iteratively (no recursion to exhaust)."""
    stack: list[tuple[Any, int]] = [(spec, 0)]
    nodes = 0
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            raise SpecError(f"The spec is too large (max {MAX_NODES} nodes, depth {MAX_DEPTH}).")
        if isinstance(node, dict):
            ref = node.get("$ref")
            if ref is not None and not (isinstance(ref, str) and ref.startswith("#/")):
                raise SpecError(
                    "Only local $ref (#/...) is accepted; remote/file refs are refused."
                )
            stack.extend((v, depth + 1) for v in node.values())
        elif isinstance(node, list):
            stack.extend((v, depth + 1) for v in node)


def _resolve(spec: dict[str, Any], node: Any) -> dict[str, Any]:
    """Follows local refs with bounded hops (ref cycles end); {} for anything unresolvable."""
    for _ in range(MAX_REF_HOPS):
        if not isinstance(node, dict) or not isinstance(node.get("$ref"), str):
            break
        target: Any = spec
        for part in node["$ref"][2:].split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            target = target.get(key) if isinstance(target, dict) else None
        node = target
    return node if isinstance(node, dict) and "$ref" not in node else {}


def _response_schema(spec: dict[str, Any], op: dict[str, Any]) -> dict[str, Any]:
    responses = _dict(op.get("responses"))
    ok = _resolve(spec, responses.get("200") or responses.get(200))
    if "schema" in ok:  # Swagger 2
        return _resolve(spec, ok["schema"])
    content = _dict(ok.get("content"))
    media = next((v for k, v in content.items() if "json" in str(k)), None)
    return _resolve(spec, media.get("schema") if isinstance(media, dict) else None)


def _records_of(spec: dict[str, Any], schema: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """(items_path, record schema): the body itself, or its first array property."""
    if schema.get("type") == "array":
        return "", _resolve(spec, schema.get("items"))
    props = _dict(schema.get("properties"))
    for name, sub in props.items():
        sub = _resolve(spec, sub)
        if sub.get("type") == "array" and isinstance(name, str) and name.isidentifier():
            return f"{name}[*]", _resolve(spec, sub.get("items"))
    return "", schema


def _endpoint(spec: dict[str, Any], path: str, op: dict[str, Any]) -> dict[str, Any]:
    items_path, record = _records_of(spec, _response_schema(spec, op))
    props = _dict(record.get("properties"))
    names = [n for n in props if isinstance(n, str) and n.isidentifier()]
    id_field = "id" if "id" in names or not names else names[0]
    title = next((n for n in _TITLES if n in names), None)
    texts = [n for n in names if _resolve(spec, props[n]).get("type") == "string" and n != id_field]
    query = {}
    for p in op.get("parameters", []) if isinstance(op.get("parameters"), list) else []:
        p = _resolve(spec, p)
        default = (p.get("schema") or {}).get("default", p.get("default"))
        if p.get("in") == "query" and p.get("required") and isinstance(default, str | int):
            query[str(p.get("name"))[:100]] = str(default)[:500]
    return {
        "path": path[:500],
        "method": "GET",
        "query": query,
        "items_path": items_path,
        "id_field": id_field,
        "title_field": title,
        "text_fields": texts[:10] or [title or id_field],
        "updated_field": next((n for n in _UPDATED if n in names), None),
    }


def _base_url(spec: dict[str, Any]) -> str:
    servers = spec.get("servers")
    if isinstance(servers, list) and servers and isinstance(servers[0], dict):
        url = str(servers[0].get("url", ""))
        return url[:500] if url.startswith(("https://", "http://")) else ""
    host = spec.get("host")
    if isinstance(host, str) and host:
        schemes = spec.get("schemes") or ["https"]
        scheme = "https" if "https" in schemes else str(schemes[0])
        return f"{scheme}://{host}{spec.get('basePath', '')}"[:500]
    return ""


def _auth(spec: dict[str, Any]) -> dict[str, Any]:
    comps = _dict(spec.get("components"))
    schemes = comps.get("securitySchemes") or spec.get("securityDefinitions") or {}
    for raw in schemes.values() if isinstance(schemes, dict) else []:
        s = _resolve(spec, raw)
        kind, scheme = s.get("type"), str(s.get("scheme", "")).lower()
        if kind == "apiKey" and s.get("in") in ("header", "query"):
            return {"kind": "api_key", "api_key_in": s["in"], "api_key_name": str(s.get("name"))}
        if (kind == "http" and scheme == "basic") or kind == "basic":
            return {"kind": "basic"}
        if kind == "http" and scheme == "bearer":
            return {"kind": "bearer"}
        flows = _dict(s.get("flows"))
        cc = flows.get("clientCredentials") or (s if s.get("flow") == "application" else None)
        if kind == "oauth2" and isinstance(cc, dict) and isinstance(cc.get("tokenUrl"), str):
            return {"kind": "oauth2", "token_url": cc["tokenUrl"][:500]}
    return {"kind": "none"}


def suggest(text: str) -> dict[str, Any]:
    """{config, skipped}. `config` is a starting point for the source form, not validated: the
    editor reviews it (and fills base_url if the spec has none) before creating the source."""
    if len(text) > MAX_SPEC_CHARS:
        raise SpecError(f"The spec is larger than {MAX_SPEC_CHARS} characters.")
    spec = _load(text)
    _scan(spec)
    endpoints: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for path, ops in spec["paths"].items():
        if not isinstance(ops, dict):
            continue
        path = str(path)[:500]
        for m in ops:
            if str(m).lower() in _METHODS:
                skipped.append({"method": str(m).upper(), "path": path, "reason": "not GET"})
        if not isinstance(ops.get("get"), dict):
            continue
        reason = "needs path parameters" if "{" in path else None
        reason = reason or ("endpoint limit" if len(endpoints) >= MAX_ENDPOINTS else None)
        if reason:
            skipped.append({"method": "GET", "path": path, "reason": reason})
            continue
        endpoints.append(_endpoint(spec, path, ops["get"]))
    config = {
        "base_url": _base_url(spec),
        "endpoints": endpoints,
        "pagination": {"type": "none"},
        "auth": _auth(spec),
    }
    return {"config": config, "skipped": skipped[:500]}
