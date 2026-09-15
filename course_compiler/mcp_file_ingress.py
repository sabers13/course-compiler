"""Bounded adapter-only HTTPS ingress for ChatGPT temporary files.

This module is intentionally outside the deterministic Course Compiler core.
It validates one OpenAI-style temporary-file descriptor and downloads bytes
under exactly one of two explicit, non-inferred trust policies:

``"exact_host"`` ("F1"): trusts only an exact, non-empty, operator-curated
hostname allowlist. Its default, byte-for-byte-unchanged historical
behavior downloads through :mod:`urllib.request` with no destination
resolution of its own. An explicit opt-in, ``bind_resolved_destination``,
additionally routes the exact-host-allowlisted request through the same
shared resolve-validate-bind engine "F2" uses — exact-host membership is
checked first, at every hop, and then the surviving hop is subject to the
same connection-bound destination safety F2 provides. This is the mode the
real runtime (`create_runtime_config`, ``--allowed-file-host``) enables
automatically; a directly constructed ``FileDownloadPolicy`` without that
flag remains the original, unhardened historical behavior.

``"connection_bound"`` ("F2"): never pre-enumerates hostnames. It resolves
the caller-supplied hostname exactly once per request/redirect hop,
validates every resolved address against a centralized destination-safety
predicate, and binds the physical TCP connection to one already-validated
numeric IP literal through a custom ``httpcore.NetworkBackend`` — never to
the hostname a second time. TLS/SNI/Host identity is always the original
canonical hostname, owned by httpcore's own connection/request-origin
machinery, never by the custom backend. A Course-Compiler-owned monotonic
overall deadline bounds the complete attempt, including every redirect hop.
Exact-host-allowlisted requests share this same engine when
``bind_resolved_destination`` is set.

Both policies return exact bytes or one of the same four fixed, content-safe
failures. Temporary URLs, resolved addresses, and response bytes are never
logged or persisted here.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Literal, TypeAlias
from urllib.parse import urljoin, urlsplit

import httpcore

MAX_SOURCE_DOWNLOAD_BYTES = 64 * 1024 * 1024
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 20.0
DEFAULT_OVERALL_DEADLINE_SECONDS = 45.0
MAX_OVERALL_DEADLINE_SECONDS = 600.0

# Small, explicit, deterministic redirect-hop limit for the connection-bound
# ("F2") egress path. Not fixed by the accepted ADR/task brief; chosen here
# conservatively (existing accepted F1 redirect coverage exercises only one
# hop) and tested at its exact boundary. Up to this many redirect targets are
# followed after the initial request; a further redirect fails closed.
MAX_REDIRECT_HOPS = 5

# Redirect status codes this module follows itself; httpcore's own
# high-level redirect machinery is never used for F2 (Course Compiler owns
# every hop explicitly).
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})

_HOST_RE = re.compile(
    r"(?=.{1,253}\Z)"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)"
    r"(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*\Z"
)
_FILE_INGRESS_MESSAGES = {
    "invalid_file_reference": "The temporary file reference is invalid.",
    "file_download_not_allowed": "The temporary file download is not allowed.",
    "file_download_failed": "The temporary file download failed.",
    "file_too_large": "The temporary file exceeds the configured size limit.",
}

# Destination-safety predicate ranges not already covered by
# ``ipaddress``'s own classification properties. ``ipaddress.is_global``
# alone is insufficient: some non-unicast/non-CGNAT ranges (e.g. multicast)
# are classified "global" by that property, and RFC 6598 shared address
# space and deprecated IPv6 site-local space are classified neither private
# nor global by the standard library.
_IPV4_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")  # RFC 6598 CGNAT
_IPV6_DEPRECATED_SITE_LOCAL = ipaddress.ip_network("fec0::/10")  # RFC 3879, historical


@dataclass(frozen=True, slots=True)
class OpenAIFileReference:
    """Adapter-only ChatGPT temporary-file descriptor.

    ``file_name`` and ``mime_type`` are untrusted hints and are deliberately
    absent from the downloaded-byte result and every Course Compiler identity.
    """

    download_url: str = field(repr=False)
    file_id: str
    mime_type: str | None = None
    file_name: str | None = None

    def __post_init__(self) -> None:
        if not _valid_file_reference(self):
            raise ValueError("temporary file reference is invalid")


@dataclass(frozen=True, slots=True)
class FileDownloadPolicy:
    """Trusted immutable outbound-download policy.

    ``mode`` selects the ingress trust model explicitly; it is never
    inferred from ``allowed_hosts`` emptiness or any other implicit signal.

    ``"exact_host"`` (the default) is the original accepted "F1" contract,
    byte-for-byte unchanged: a non-empty, exact, operator-curated hostname
    allowlist is the sole required trust decision. Every existing caller
    and accepted test that constructs ``FileDownloadPolicy`` positionally
    or with only ``allowed_hosts``/``timeout_seconds``/``max_bytes`` keeps
    working unchanged.

    ``"connection_bound"`` ("F2") never pre-enumerates hostnames and
    therefore requires ``allowed_hosts`` to be empty — combining F2
    selection with a non-empty host allowlist is rejected as an ambiguous,
    invalid policy rather than silently preferring one meaning.

    ``bind_resolved_destination`` is an explicit, default-``False`` opt-in
    for ``"exact_host"`` policies only: when set, F1's exact-host-allowlisted
    request is handed to the *same* shared resolve-validate-bind engine "F2"
    uses, instead of the original :mod:`urllib.request`-based transport.
    Exact-host membership (no suffix/wildcard reasoning) is still checked
    first, at every hop including every redirect target, before that hop's
    hostname is ever resolved; only a membership-passing hop proceeds to
    destination-safety validation and gets its physical connection bound to
    an already-validated numeric IP literal — never to the hostname a
    second time. Resolution failure fails closed, exactly as it does for F2.
    A directly constructed ``FileDownloadPolicy`` that omits this flag (the
    default) keeps the original, byte-for-byte-unchanged historical F1
    behavior: no destination resolution of its own, downloaded through
    :mod:`urllib.request`. It has no effect in ``"connection_bound"`` mode,
    where destination validation is already unconditional.
    """

    allowed_hosts: tuple[str, ...]
    timeout_seconds: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS
    max_bytes: int = MAX_SOURCE_DOWNLOAD_BYTES
    mode: Literal["exact_host", "connection_bound"] = "exact_host"
    overall_deadline_seconds: float = DEFAULT_OVERALL_DEADLINE_SECONDS
    bind_resolved_destination: bool = False

    def __post_init__(self) -> None:
        if type(self.mode) is not str or self.mode not in (
            "exact_host",
            "connection_bound",
        ):
            raise ValueError("download policy mode is invalid")
        if self.mode == "exact_host":
            if (
                type(self.allowed_hosts) is not tuple
                or not self.allowed_hosts
                or any(
                    type(host) is not str
                    or host != host.lower()
                    or _HOST_RE.fullmatch(host) is None
                    for host in self.allowed_hosts
                )
                or len(set(self.allowed_hosts)) != len(self.allowed_hosts)
            ):
                raise ValueError("download host allowlist is invalid")
        else:
            if type(self.allowed_hosts) is not tuple or self.allowed_hosts:
                raise ValueError(
                    "connection-bound download policy must not declare a "
                    "temporary-host allowlist"
                )
        if (
            type(self.timeout_seconds) is not float
            or not 0.0 < self.timeout_seconds <= 120.0
        ):
            raise ValueError("download timeout is invalid")
        if (
            type(self.max_bytes) is not int
            or not 1 <= self.max_bytes <= MAX_SOURCE_DOWNLOAD_BYTES
        ):
            raise ValueError("download size limit is invalid")
        if type(self.bind_resolved_destination) is not bool:
            raise ValueError("download resolved-destination binding flag is invalid")
        if self.mode == "connection_bound" and self.bind_resolved_destination:
            # "connection_bound" is already unconditionally bound to the
            # shared resolve-validate-bind engine; this flag exists to opt
            # an "exact_host" policy into that same engine. Setting both is
            # redundant/ambiguous, not a stronger request, so it is rejected
            # rather than silently accepted as a no-op.
            raise ValueError(
                "connection-bound download policy must not redundantly set "
                "the exact-host resolved-destination binding flag"
            )
        # The overall deadline only constrains modes that actually use the
        # shared resolve-validate-bind engine: unconditionally for
        # "connection_bound", and only when opted in for "exact_host". A
        # historical, unhardened "exact_host" policy never consults this
        # field at all, so its timeout is never tied to it — see the T033
        # corrective review's backward-compatibility requirement.
        uses_resolved_destination_engine = (
            self.mode == "connection_bound" or self.bind_resolved_destination
        )
        if (
            type(self.overall_deadline_seconds) is not float
            or not 0.0 < self.overall_deadline_seconds <= MAX_OVERALL_DEADLINE_SECONDS
        ):
            raise ValueError("download overall deadline is invalid")
        if (
            uses_resolved_destination_engine
            and self.timeout_seconds > self.overall_deadline_seconds
        ):
            raise ValueError("download overall deadline is invalid")


@dataclass(frozen=True, slots=True)
class FileIngressFailure:
    """One fixed adapter failure with no URL, response, or exception detail."""

    code: Literal[
        "invalid_file_reference",
        "file_download_not_allowed",
        "file_download_failed",
        "file_too_large",
    ]
    message: str

    def __post_init__(self) -> None:
        if (
            type(self.code) is not str
            or self.code not in _FILE_INGRESS_MESSAGES
            or type(self.message) is not str
            or self.message != _FILE_INGRESS_MESSAGES[self.code]
        ):
            raise ValueError("file ingress failure is invalid")


FileIngressResult: TypeAlias = bytes | FileIngressFailure


class _DownloadNotAllowed(Exception):
    """Private control-flow signal; its text is never returned."""


class _DeadlineExceeded(TimeoutError):
    """Private control-flow signal for Course-Compiler-owned deadline exhaustion."""


class _AllowlistedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject every redirect whose destination leaves the trusted allowlist."""

    def __init__(self, policy: FileDownloadPolicy) -> None:
        super().__init__()
        self._policy = policy

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        if not _url_is_allowed(newurl, self._policy):
            raise _DownloadNotAllowed
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_openai_file(
    reference: OpenAIFileReference,
    *,
    policy: FileDownloadPolicy,
) -> FileIngressResult:
    """Download exact bytes under the caller's exact-host or connection-bound policy."""

    if type(reference) is not OpenAIFileReference:
        raise TypeError("reference must be exactly OpenAIFileReference")
    if type(policy) is not FileDownloadPolicy:
        raise TypeError("policy must be exactly FileDownloadPolicy")

    try:
        validated_reference = OpenAIFileReference(
            reference.download_url,
            reference.file_id,
            reference.mime_type,
            reference.file_name,
        )
        validated_policy = FileDownloadPolicy(
            policy.allowed_hosts,
            policy.timeout_seconds,
            policy.max_bytes,
            policy.mode,
            policy.overall_deadline_seconds,
            policy.bind_resolved_destination,
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        return _failure("invalid_file_reference")

    if (
        validated_policy.mode == "connection_bound"
        or validated_policy.bind_resolved_destination
    ):
        return _download_via_connection_bound_egress(validated_reference, validated_policy)
    return _download_via_exact_host(validated_reference, validated_policy)


def _download_via_exact_host(
    reference: OpenAIFileReference,
    policy: FileDownloadPolicy,
) -> FileIngressResult:
    """Original accepted "F1" behavior; byte-for-byte unchanged control flow.

    Only reached when ``policy.bind_resolved_destination`` is ``False``
    (the default): a directly constructed ``FileDownloadPolicy`` that never
    opts into the shared resolve-validate-bind engine performs no
    destination resolution of its own here, exactly as the original
    accepted F1 implementation did.
    """

    if not _url_is_allowed(reference.download_url, policy):
        return _failure("file_download_not_allowed")

    try:
        opener = urllib.request.build_opener(_AllowlistedRedirectHandler(policy))
        request = urllib.request.Request(
            reference.download_url,
            method="GET",
            headers={"Accept": "application/octet-stream"},
        )
        with opener.open(
            request,
            timeout=policy.timeout_seconds,
        ) as response:
            if not _url_is_allowed(response.geturl(), policy):
                return _failure("file_download_not_allowed")
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_size = int(content_length, 10)
                except (TypeError, ValueError):
                    return _failure("file_download_failed")
                if declared_size < 0:
                    return _failure("file_download_failed")
                if declared_size > policy.max_bytes:
                    return _failure("file_too_large")

            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(min(64 * 1024, policy.max_bytes - total + 1))
                if type(chunk) is not bytes:
                    return _failure("file_download_failed")
                if not chunk:
                    break
                total += len(chunk)
                if total > policy.max_bytes:
                    return _failure("file_too_large")
                chunks.append(chunk)
            return b"".join(chunks)
    except _DownloadNotAllowed:
        return _failure("file_download_not_allowed")
    except (
        OSError,
        TimeoutError,
        urllib.error.HTTPError,
        urllib.error.URLError,
    ):
        return _failure("file_download_failed")
    except Exception:
        return _failure("file_download_failed")


def _valid_file_reference(value: object) -> bool:
    try:
        return (
            type(value) is OpenAIFileReference
            and type(value.download_url) is str
            and 1 <= len(value.download_url) <= 8192
            and type(value.file_id) is str
            and 1 <= len(value.file_id) <= 512
            and "\x00" not in value.file_id
            and "\r" not in value.file_id
            and "\n" not in value.file_id
            and _valid_hint(value.mime_type, 255)
            and _valid_hint(value.file_name, 1024)
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        return False


def _valid_hint(value: object, maximum: int) -> bool:
    return value is None or (
        type(value) is str
        and len(value) <= maximum
        and "\x00" not in value
        and "\r" not in value
        and "\n" not in value
    )


def _url_is_allowed(url: object, policy: FileDownloadPolicy) -> bool:
    if type(url) is not str:
        return False
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and parsed.hostname is not None
        and parsed.hostname.lower() in policy.allowed_hosts
        and parsed.hostname == parsed.hostname.lower()
        and parsed.netloc != ""
        and parsed.fragment == ""
        and (port is None or 1 <= port <= 65535)
    )


def _failure(code: str) -> FileIngressFailure:
    return FileIngressFailure(code, _FILE_INGRESS_MESSAGES[code])  # type: ignore[arg-type]


# =====================================================================
# Connection-bound ("F2") egress: resolve -> validate -> bind.
# =====================================================================


@dataclass(frozen=True, slots=True)
class _CanonicalUrl:
    """One syntactically validated, canonicalized F2-eligible URL."""

    hostname: str
    port: int
    target: bytes


def _canonicalize_f2_url(url: object) -> _CanonicalUrl | None:
    """Parse/validate/canonicalize one F2 hop URL, or return ``None``.

    Requires: HTTPS scheme only; no userinfo; no fragment; hostname
    present and not a literal IPv4/IPv6 address; explicit port absent or
    exactly 443; ASCII, deterministically lowercased DNS-label hostname
    syntax (a non-ASCII/IDNA-form or trailing-dot hostname is
    deterministically rejected rather than normalized). The exact
    path/query bytes are preserved unchanged for the request target;
    nothing about the filename, MIME hint, file ID, query-string shape, or
    hostname branding participates in this decision.
    """

    if type(url) is not str or not (1 <= len(url) <= 8192):
        return None
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return None
    if parsed.scheme != "https":
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if parsed.fragment != "":
        return None
    if parsed.netloc == "" or parsed.hostname is None:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port not in (None, 443):
        return None
    hostname = parsed.hostname
    if not hostname.isascii():
        return None
    canonical_hostname = hostname.lower()
    try:
        ipaddress.ip_address(canonical_hostname)
    except ValueError:
        pass
    else:
        return None  # literal IPv4/IPv6 URL host rejected
    if _HOST_RE.fullmatch(canonical_hostname) is None:
        return None
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    try:
        target_bytes = target.encode("ascii")
    except UnicodeEncodeError:
        return None
    return _CanonicalUrl(canonical_hostname, 443, target_bytes)


def _unwrap_ipv4_mapped(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            return mapped
    return address


def _destination_is_safe(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Centralized, deterministic destination-safety predicate.

    Rejects loopback, private, link-local, multicast, unspecified,
    reserved/special-use, carrier-grade NAT (RFC 6598), deprecated IPv6
    site-local space, and IPv4-mapped forms of any denied IPv4 class.
    Deliberately does not rely on ``ipaddress.is_global`` alone: verified
    by direct inspection that some non-unicast/non-CGNAT ranges (e.g.
    multicast) are classified "global" by that property, and RFC 6598/
    deprecated-site-local ranges are classified neither private nor global.
    """

    address = _unwrap_ipv4_mapped(address)
    if isinstance(address, ipaddress.IPv4Address):
        if address in _IPV4_SHARED_ADDRESS_SPACE:
            return False
        return not (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
        )
    if address in _IPV6_DEPRECATED_SITE_LOCAL:
        return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    )


def _resolve_hop_addresses(
    hostname: str, port: int
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...] | None:
    """Resolve ``hostname`` exactly once for TCP; ``None`` on resolution failure.

    ``socket.getaddrinfo`` has no built-in cancellation/timeout; a resolver
    call that hangs is not preemptively killed here (see the module-level
    overall-deadline documentation and the implementation report for the
    exact, disclosed scope of this platform limitation). The resolve-once
    security invariant itself does not depend on resolver latency: no
    second resolution of ``hostname`` ever occurs for a given hop regardless
    of how long this one call takes.
    """

    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return None
    seen: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for _family, _socktype, _proto, _canonname, sockaddr in infos:
        try:
            candidate = ipaddress.ip_address(sockaddr[0])
        except (ValueError, IndexError, TypeError):
            continue
        if candidate not in seen:
            seen.add(candidate)
            addresses.append(candidate)
    if not addresses:
        return None
    return tuple(addresses)


class _BoundNetworkStream(httpcore.NetworkStream):
    """Deadline-aware wrapper around one already-validated, connected stream.

    Every operation is bounded by both its own requested timeout and the
    remaining Course-Compiler-owned overall deadline, whichever is smaller,
    so a server that keeps sending data just inside each individual
    operation timeout cannot keep one attempt alive indefinitely.
    """

    def __init__(self, stream: httpcore.NetworkStream, deadline_at: float) -> None:
        self._stream = stream
        self._deadline_at = deadline_at

    def _bounded_timeout(self, requested: float | None) -> float:
        remaining = self._deadline_at - time.monotonic()
        if remaining <= 0:
            raise _DeadlineExceeded("overall download deadline exceeded")
        if requested is None:
            return remaining
        return requested if requested < remaining else remaining

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._stream.read(max_bytes, timeout=self._bounded_timeout(timeout))

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._stream.write(buffer, timeout=self._bounded_timeout(timeout))

    def close(self) -> None:
        self._stream.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        bounded = self._bounded_timeout(timeout)
        try:
            new_stream = self._stream.start_tls(
                ssl_context, server_hostname=server_hostname, timeout=bounded
            )
        except BaseException:
            # The underlying already-connected TCP stream was successfully
            # bound to the validated numeric IP before this call; a TLS
            # failure must not leak that connected socket. Close it, then
            # re-raise the original failure unchanged (never swallowed,
            # never exposed publicly) for the caller's existing fixed,
            # content-safe failure mapping.
            try:
                self._stream.close()
            except Exception:
                pass
            raise
        return _BoundNetworkStream(new_stream, self._deadline_at)

    def get_extra_info(self, info: str) -> object:
        return self._stream.get_extra_info(info)


class _ValidatedDestinationBackend(httpcore.NetworkBackend):
    """Request/hop-scoped backend binding httpcore's connection to one hop.

    ``connect_tcp`` is httpcore's documented public extension point. This
    backend first verifies the ``(host, port)`` httpcore hands it against
    the exact authorized binding for this hop (an expected-identity check,
    never a resolution step) and only then delegates the physical
    connection to the already-validated numeric IP literal via the real
    :class:`httpcore.SyncBackend` — never to the hostname a second time.
    TLS/SNI/Host identity is not this backend's concern: httpcore's own
    ``HTTPConnection``, using its request ``Origin``, calls ``start_tls``
    on the stream this backend returns.
    """

    def __init__(
        self,
        expected_hostname: str,
        expected_port: int,
        validated_ip: str,
        deadline_at: float,
    ) -> None:
        self._expected_hostname = expected_hostname
        self._expected_port = expected_port
        self._validated_ip = validated_ip
        self._deadline_at = deadline_at
        self._inner = httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> httpcore.NetworkStream:
        if host != self._expected_hostname or port != self._expected_port:
            raise httpcore.ConnectError("destination binding mismatch")
        remaining = self._deadline_at - time.monotonic()
        if remaining <= 0:
            raise _DeadlineExceeded("overall download deadline exceeded")
        bounded_timeout = remaining if timeout is None else min(timeout, remaining)
        stream = self._inner.connect_tcp(
            self._validated_ip,
            port,
            timeout=bounded_timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
        return _BoundNetworkStream(stream, self._deadline_at)

    def sleep(self, seconds: float) -> None:
        remaining = self._deadline_at - time.monotonic()
        if remaining <= 0:
            raise _DeadlineExceeded("overall download deadline exceeded")
        time.sleep(min(seconds, remaining))


def _perform_f2_hop(
    canonical: _CanonicalUrl,
    validated_ip: str,
    policy: FileDownloadPolicy,
    deadline_at: float,
) -> tuple[httpcore.HTTPConnection, httpcore.Response]:
    """Issue exactly one GET over one request/hop-scoped, validated connection.

    Caller owns closing both the returned response and connection.
    """

    hostname_bytes = canonical.hostname.encode("ascii")
    origin = httpcore.Origin(b"https", hostname_bytes, canonical.port)
    url = httpcore.URL(
        scheme=b"https",
        host=hostname_bytes,
        port=canonical.port,
        target=canonical.target,
    )
    request = httpcore.Request(
        method="GET",
        url=url,
        headers=[
            (b"Host", hostname_bytes),
            (b"Accept", b"application/octet-stream"),
        ],
        extensions={
            "timeout": {
                "connect": policy.timeout_seconds,
                "read": policy.timeout_seconds,
                "write": policy.timeout_seconds,
                "pool": policy.timeout_seconds,
            },
        },
    )
    backend = _ValidatedDestinationBackend(
        canonical.hostname, canonical.port, validated_ip, deadline_at
    )
    connection = httpcore.HTTPConnection(
        origin=origin,
        network_backend=backend,
        http2=False,
        retries=0,
    )
    try:
        response = connection.handle_request(request)
    except BaseException:
        # ``handle_request`` failing (connect error, TLS failure, malformed
        # response, ...) means this function never reaches its normal
        # return, so the caller's own response/connection cleanup never
        # runs for this connection. Close it here instead, before
        # re-raising the original failure unchanged, so the caller's
        # existing ``except Exception: return _failure("file_download_failed")``
        # still maps this to the same fixed, content-safe result.
        try:
            connection.close()
        except Exception:
            pass
        raise
    return connection, response


def _extract_header_values(headers: object, name: bytes) -> list[bytes]:
    if type(headers) is not list:
        return []
    values: list[bytes] = []
    for item in headers:
        if type(item) is not tuple or len(item) != 2:
            continue
        header_name, header_value = item
        if type(header_name) is bytes and type(header_value) is bytes and header_name.lower() == name:
            values.append(header_value)
    return values


def _extract_content_length(headers: object) -> int | None:
    """Declared Content-Length, ``None`` if absent, ``-1`` if malformed/conflicting."""

    values = _extract_header_values(headers, b"content-length")
    if not values:
        return None
    if len(set(values)) != 1:
        return -1
    try:
        return int(values[0].decode("ascii"), 10)
    except (UnicodeDecodeError, ValueError):
        return -1


def _extract_redirect_location(headers: object) -> str | None:
    values = _extract_header_values(headers, b"location")
    if len(values) != 1:
        return None
    try:
        return values[0].decode("ascii")
    except UnicodeDecodeError:
        return None


def _stream_f2_body(response: httpcore.Response, policy: FileDownloadPolicy) -> FileIngressResult:
    if response.status != 200:
        return _failure("file_download_failed")
    content_length = _extract_content_length(response.headers)
    if content_length is not None:
        if content_length < 0:
            return _failure("file_download_failed")
        if content_length > policy.max_bytes:
            return _failure("file_too_large")
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_stream():
        if type(chunk) is not bytes:
            return _failure("file_download_failed")
        if not chunk:
            continue
        total += len(chunk)
        if total > policy.max_bytes:
            return _failure("file_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _resolved_destination_hostname_allowed(
    hostname: str, policy: FileDownloadPolicy
) -> bool:
    """Exact-host membership gate for the shared resolve-validate-bind engine.

    Applies only when ``policy.mode == "exact_host"`` — the hardened F1
    path, selected by ``bind_resolved_destination=True``: every hop's
    canonical hostname, the initial request and every redirect target
    alike, must be an exact, case-normalized member of
    ``policy.allowed_hosts`` *before* that hop's hostname is ever resolved.
    No suffix/wildcard reasoning, matching F1's original ``_url_is_allowed``
    semantics. ``"connection_bound"`` ("F2") mode has no allowlist and is
    always allowed here; its emptiness is already enforced by
    ``FileDownloadPolicy.__post_init__``.
    """

    if policy.mode != "exact_host":
        return True
    return hostname in policy.allowed_hosts


def _download_via_connection_bound_egress(
    reference: OpenAIFileReference,
    policy: FileDownloadPolicy,
) -> FileIngressResult:
    """Resolve -> validate -> bind, restarted fully at every redirect hop.

    Shared by both explicit egress modes that need connection-bound
    destination safety: ``"connection_bound"`` ("F2", no hostname-membership
    restriction) and hardened ``"exact_host"`` (F1 opted into
    ``bind_resolved_destination=True``, which additionally requires exact
    allowlist membership at every hop before that hop is resolved).
    """

    deadline_at = time.monotonic() + policy.overall_deadline_seconds
    current_url: str = reference.download_url

    for _hop in range(MAX_REDIRECT_HOPS + 1):
        if time.monotonic() >= deadline_at:
            return _failure("file_download_failed")

        canonical = _canonicalize_f2_url(current_url)
        if canonical is None:
            return _failure("file_download_not_allowed")

        if not _resolved_destination_hostname_allowed(canonical.hostname, policy):
            return _failure("file_download_not_allowed")

        addresses = _resolve_hop_addresses(canonical.hostname, canonical.port)
        # ``socket.getaddrinfo`` has no built-in cancellation; a call that
        # blocks past the overall deadline cannot be interrupted while it is
        # blocked (see ``_resolve_hop_addresses``'s own documentation for the
        # exact, disclosed scope of this platform limitation). Once it
        # returns, though, its elapsed time is immediately charged against
        # the overall deadline, checked here before the returned addresses
        # are ever classified or used for a connection — an attempt that
        # already exhausted its budget while blocked in the resolver stops
        # here rather than proceeding to validate/connect on a stale budget.
        if time.monotonic() >= deadline_at:
            return _failure("file_download_failed")
        if not addresses:
            return _failure("file_download_not_allowed")
        if not all(_destination_is_safe(address) for address in addresses):
            return _failure("file_download_not_allowed")
        validated_ip = str(addresses[0])

        try:
            connection, response = _perform_f2_hop(canonical, validated_ip, policy, deadline_at)
        except Exception:
            return _failure("file_download_failed")

        try:
            if response.status in _REDIRECT_STATUS_CODES:
                location = _extract_redirect_location(response.headers)
                if location is None:
                    return _failure("file_download_failed")
                current_url = urljoin(current_url, location)
                continue
            try:
                return _stream_f2_body(response, policy)
            except Exception:
                return _failure("file_download_failed")
        finally:
            try:
                response.close()
            except Exception:
                pass
            try:
                connection.close()
            except Exception:
                pass

    return _failure("file_download_failed")


__all__ = [
    "DEFAULT_DOWNLOAD_TIMEOUT_SECONDS",
    "DEFAULT_OVERALL_DEADLINE_SECONDS",
    "MAX_OVERALL_DEADLINE_SECONDS",
    "MAX_REDIRECT_HOPS",
    "MAX_SOURCE_DOWNLOAD_BYTES",
    "FileDownloadPolicy",
    "FileIngressFailure",
    "FileIngressResult",
    "OpenAIFileReference",
    "download_openai_file",
]
