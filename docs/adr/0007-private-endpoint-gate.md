# ADR 0007: Private connector endpoints need a server gate

Status: Accepted (B5.1)

## Context

The SSRF guard allows only public unicast addresses. Self-hosted and BYOC customers keep S3-compatible
storage or Postgres inside their own network (RFC1918), so a per-source opt-in
(`allow_private_endpoint`) was added. On a shared SaaS deployment that opt-in alone would let any
editor probe the platform's internal network.

## Decision

A per-source `allow_private_endpoint` works only while the operator sets
`CONNECTORS_ALLOW_PRIVATE_ENDPOINTS=true` (default false). The gate is checked on save (`422
private_endpoints_disabled`) and re-read on every test and sync. When allowed, only RFC1918 and IPv6
ULA ranges are added; loopback (the API host itself, including IPv6-embedded forms), link-local and
cloud metadata, CGNAT, multicast, reserved and unspecified stay blocked.

## Consequences

- SaaS stays public-only by default; BYOC can reach in-network sources.
- Turning the gate off later fails tests and syncs of opted-in sources with a readable message.
- Tests against compose services opt in through a fixture that also allows loopback, for tests only.

## Alternatives considered

- **Per-source opt-in alone:** rejected: tenant-controlled access to the operator's network.
- **Operator allowlist of CIDRs:** more precise; can replace the boolean if needed.
- **No private endpoints:** blocks the self-hosted use case.
