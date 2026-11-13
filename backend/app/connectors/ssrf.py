"""Outbound HTTP for user-supplied URLs (AC9).

Every hop (first request and each redirect) is checked: http/https only, no userinfo, standard
ports unless the source opts in, and every DNS answer must be a public unicast address. The
request then connects to the validated IP (Host header + TLS SNI keep the real name), so a second
DNS lookup can't swap in a private address (rebinding). Proxies from the environment are ignored
for the same reason.
"""

import asyncio
import ipaddress
import re
import socket
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from app.connectors.base import ConnectorError

USER_AGENT = "RAGPlatformBot/0.1"
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[str]]]
IpCheck = Callable[[IPAddress], bool]

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
# Hosts like 2130706433, 0177.0.0.1, 0x7f.1: resolvers expand these to IPs in surprising ways.
_NUMERIC_HOST = re.compile(r"^(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+))*\.?$", re.I)
_REDIRECTS = {301, 302, 303, 307, 308}


class BlockedURL(Exception):
    """The URL may not be fetched. The message is safe to show users (no resolved IPs)."""


class FetchTooLarge(Exception):
    pass


def _embedded_v4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped or ip.sixtofour:
        return ip.ipv4_mapped or ip.sixtofour
    if ip.teredo:
        return ip.teredo[1]
    if ip in _NAT64 or int(ip) < 2**32:  # NAT64 and deprecated IPv4-compatible ::a.b.c.d
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def ip_allowed(ip: IPAddress) -> bool:
    """Public unicast only. is_global already excludes private, loopback, link-local, reserved,
    unspecified, CGNAT 100.64/10 and IPv6 ULA; multicast and IPv4 hidden in IPv6 need checks."""
    if isinstance(ip, ipaddress.IPv6Address) and (v4 := _embedded_v4(ip)) is not None:
        if not ip_allowed(v4):
            return False
    return ip.is_global and not ip.is_multicast


async def system_resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


# Cloud metadata services stay unreachable even for sources allowed to use private endpoints.
_METADATA = {ipaddress.ip_address("fd00:ec2::254")}  # AWS IPv6 IMDS (IPv4 one is link-local)
# The only extra ranges an opted-in source may reach: RFC1918 and IPv6 ULA.
_PRIVATE_NETS = [
    ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
]


def ip_allowed_private(ip: IPAddress) -> bool:
    """For sources that opted in to private endpoints (storage/DB in the customer's network):
    public, RFC1918 and ULA only. Loopback (the API host itself), link-local/metadata, CGNAT,
    multicast, unspecified and reserved stay blocked, also when hidden in an IPv6 form."""
    if isinstance(ip, ipaddress.IPv6Address) and (v4 := _embedded_v4(ip)) is not None:
        ip = v4
    if ip.is_multicast or ip in _METADATA:
        return False
    return ip.is_global or any(ip in n for n in _PRIVATE_NETS)


def endpoint_check(opted_in: bool) -> IpCheck:
    """IP check for a source's `allow_private_endpoint`. The opt-in only works while the operator
    gate CONNECTORS_ALLOW_PRIVATE_ENDPOINTS is on; it is re-read at every test and sync."""
    if not opted_in:
        return ip_allowed
    from app.core.config import get_settings  # lazy: keeps this module importable without env

    if not get_settings().connectors_allow_private_endpoints:
        raise ConnectorError(PRIVATE_DISABLED)
    return ip_allowed_private


PRIVATE_DISABLED = (
    "Private endpoints are disabled on this server. An operator must set "
    "CONNECTORS_ALLOW_PRIVATE_ENDPOINTS=true (self-hosted/BYOC only) before a source may use "
    "allow_private_endpoint."
)


async def resolve_checked(
    host: str, port: int, ip_check: IpCheck = ip_allowed, resolver: Resolver = system_resolve
) -> str:
    """First address of `host` when every DNS answer passes `ip_check`; connect to it (pinning)."""
    try:
        answers = [host] if _is_canonical_ip(host) else await resolver(host, port)
    except OSError as exc:
        raise BlockedURL(f"The host {host} could not be resolved.") from exc
    ips = [ipaddress.ip_address(a.split("%")[0]) for a in answers]
    # Any blocked answer refuses the host: a mixed answer is a rebinding/split-horizon tell.
    if not ips or not all(ip_check(ip) for ip in ips):
        raise BlockedURL(f"The host {host} points to a private or reserved address.")
    return str(ips[0])


def check_static(url: str, allow_nonstandard_ports: bool = False) -> httpx.URL:
    """Checks that need no network: scheme, credentials, port, numeric host tricks."""
    try:
        u = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise BlockedURL("The URL is not valid.") from exc
    if u.scheme not in ("http", "https") or not u.host:
        raise BlockedURL("Only http and https URLs are allowed.")
    if u.userinfo:
        raise BlockedURL("URLs with embedded credentials are not allowed.")
    if u.port is not None and u.port not in (80, 443) and not allow_nonstandard_ports:
        raise BlockedURL("Only ports 80 and 443 are allowed for this source.")
    if _NUMERIC_HOST.match(u.host) and not _is_canonical_ip(u.host):
        raise BlockedURL("Numeric host forms other than a plain IP address are not allowed.")
    return u


def _is_canonical_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class Fetched:
    url: str  # final URL after redirects
    status: int
    headers: httpx.Headers
    content: bytes


_ENCODINGS = {"identity", "gzip", "x-gzip", "deflate"}
ACCEPT_ENCODING = {"Accept-Encoding": "gzip, deflate"}  # what read_capped can decode


async def read_capped(resp: httpx.Response, max_bytes: int) -> bytes:
    """Body of a streamed response, decompressed and closed. Raises FetchTooLarge past
    `max_bytes` of DECODED output, httpx.DecodingError on an unsupported or corrupt encoding.

    httpx would inflate a whole raw chunk at once (64 KB of gzip can be 64 MB), so this
    decompresses itself with the output bounded per chunk."""
    try:
        enc = resp.headers.get("content-encoding", "identity").strip().lower()
        if enc not in _ENCODINGS:
            raise httpx.DecodingError(f"Unsupported Content-Encoding {enc!r}.")
        inflate = zlib.decompressobj(47) if enc != "identity" else None  # 47: gzip or zlib
        body = bytearray()
        async for part in resp.aiter_raw():
            room = max_bytes + 1 - len(body)
            try:
                body += inflate.decompress(part, room) if inflate else part
            except zlib.error as exc:
                raise httpx.DecodingError(f"Bad {enc} body.") from exc
            if len(body) > max_bytes:
                raise FetchTooLarge(f"Response is larger than {max_bytes} bytes.")
    finally:
        await resp.aclose()
    return bytes(body)


class SafeFetcher:
    """`resolver` and `ip_check` exist so tests can reach a local server; production code never
    passes them."""

    def __init__(
        self,
        *,
        allow_nonstandard_ports: bool = False,
        resolver: Resolver = system_resolve,
        ip_check: IpCheck = ip_allowed,
        max_bytes: int = 5 * 2**20,
        max_redirects: int = 5,
        timeout_s: float = 15.0,
    ) -> None:
        self.allow_ports, self.resolver, self.ip_check = allow_nonstandard_ports, resolver, ip_check
        self.max_bytes, self.max_redirects = max_bytes, max_redirects
        self.client = httpx.AsyncClient(
            follow_redirects=False, trust_env=False, timeout=httpx.Timeout(timeout_s)
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _pinned_ip(self, u: httpx.URL) -> str:
        port, host = u.port or (443 if u.scheme == "https" else 80), u.raw_host.decode("ascii")
        return await resolve_checked(host, port, self.ip_check, self.resolver)

    async def check(self, url: str) -> None:
        """Raises BlockedURL if `url` may not be fetched (no request is sent)."""
        await self._pinned_ip(check_static(url, self.allow_ports))

    async def _send_once(self, url: str) -> tuple[httpx.Response, bytes]:
        u = check_static(url, self.allow_ports)
        ip, host = await self._pinned_ip(u), u.raw_host.decode("ascii")
        req = self.client.build_request(
            "GET",
            u.copy_with(host=ip),
            headers={
                "Host": u.netloc.decode("ascii"),
                "User-Agent": USER_AGENT,
                **ACCEPT_ENCODING,
            },
            # TLS verifies the certificate against the real name, not the pinned IP.
            extensions={} if _is_canonical_ip(host) else {"sni_hostname": host},
        )
        resp = await self.client.send(req, stream=True)
        return resp, await read_capped(resp, self.max_bytes)

    async def get(self, url: str, allow_redirect: Callable[[str], bool] | None = None) -> Fetched:
        """GET with manual redirects; `allow_redirect` can stop a hop (e.g. off-domain)."""
        for _ in range(self.max_redirects + 1):
            resp, body = await self._send_once(url)
            location = resp.headers.get("location")
            if resp.status_code not in _REDIRECTS or not location:
                return Fetched(url, resp.status_code, resp.headers, body)
            url = urljoin(url, location)
            if allow_redirect is not None and not allow_redirect(url):
                raise BlockedURL("The page redirects outside the allowed domains.")
        raise BlockedURL("Too many redirects.")
