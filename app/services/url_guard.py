"""URL validation and the SSRF guard.

An uploaded CSV is untrusted input that decides what this server connects to. Left
unguarded, a row reading `http://169.254.169.254/latest/meta-data/` turns the
crawler into a way to read a cloud instance's credentials, and `http://127.0.0.1:5432`
turns it into a port scanner for whatever else is on the host.

The guard is deliberately in two halves. The syntactic half - scheme, host present,
no credentials - can reject a value immediately. The resolving half needs DNS,
because a public-looking hostname is free to resolve to a loopback address; that
half is best-effort, since a name that does not resolve is a crawl failure rather
than a security decision.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = ("http", "https")

#: Hostnames that never need resolving to be recognised as local.
BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
    }
)

#: Suffixes that are internal by definition.
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")


def host_is_private(host: str) -> bool:
    """True when a hostname or literal address is not publicly routable."""
    if not host:
        return True

    name = host.strip("[]").lower().rstrip(".")
    if name in BLOCKED_HOSTNAMES or name.endswith(BLOCKED_SUFFIXES):
        return True

    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False  # A name, not an address - the resolving half deals with it.
    return _address_is_private(address)


def _address_is_private(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Every range that is not a public destination.

    `is_global` covers loopback, link-local, private, multicast and reserved in one
    predicate, which is exactly the set to refuse. The explicit checks are kept
    beside it because they name the cases people actually try.
    """
    if address.is_loopback or address.is_private or address.is_link_local:
        return True
    if address.is_multicast or address.is_reserved or address.is_unspecified:
        return True
    # IPv4-mapped IPv6, e.g. ::ffff:127.0.0.1, would otherwise slip past the
    # checks above because the wrapper address is not itself loopback.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return _address_is_private(address.ipv4_mapped)
    return not address.is_global


def resolves_to_private(host: str) -> bool:
    """Resolve a hostname and report whether any answer is non-public.

    Any private answer is enough to refuse: a hostname with both a public and a
    loopback address can be pointed at either, and the crawler does not get to
    choose which.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        # Unresolvable is not a policy decision - let the fetch fail and report it.
        return False

    for info in infos:
        literal = info[4][0]
        try:
            if host_is_private(literal):
                return True
        except ValueError:
            continue
    return False


def validate_url(url: str, allow_private: bool = False, resolve: bool = True) -> tuple[bool, str]:
    """Return (ok, reason). `reason` is empty when the URL is acceptable.

    `resolve=False` skips the DNS half of the guard. Callers that validate many
    URLs at once - an upload of a few hundred rows - use it to keep the check
    instant, and the crawl itself repeats the full check per URL.
    """
    candidate = (url or "").strip()
    if not candidate:
        return False, "empty URL"

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        return False, f"malformed URL ({exc})"

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        # file:// is the one that matters here - it would read the server's disk.
        return False, f"unsupported scheme '{parts.scheme or 'none'}' - only http and https"

    if not parts.hostname:
        return False, "no host in URL"

    if parts.username or parts.password:
        return False, "credentials in URL are not accepted"

    if allow_private:
        return True, ""

    host = parts.hostname
    if host_is_private(host):
        return False, f"'{host}' is a local or private address"

    if resolve and resolves_to_private(host):
        return False, f"'{host}' resolves to a local or private address"

    return True, ""


def domain_of(url: str) -> str:
    """The registrable-ish host for grouping and same-domain checks.

    This is the hostname with a leading `www.` removed, not a public-suffix
    calculation: `news.bbc.co.uk` and `bbc.co.uk` count as different sites here,
    which is the conservative direction for a same-domain crawl limit.
    """
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def is_same_domain(candidate: str, origin: str) -> bool:
    """Whether a discovered link stays on the site it was found on."""
    left, right = domain_of(candidate), domain_of(origin)
    return bool(left) and bool(right) and left == right
