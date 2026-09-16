"""Deterministic security/adversarial suite for connection-bound egress.

This is the security proof for the accepted connection-bound egress contract. It never
requires the public Internet: DNS resolution, the physical connector, TLS,
the response stream, and the monotonic clock are all injected/mocked.

Existing accepted "F1" (exact-host) tests live in ``tests/test_mcp_adapter.py``
and are intentionally left untouched; this module covers only the new
connection-bound ("F2") path, the destination-safety predicate, URL
canonicalization, and F1's new opt-in ``bind_resolved_destination``
extension.
"""

from __future__ import annotations

import ipaddress
import ssl
import time
import unittest
from unittest import mock

from tests.toolchain_support import (
    HTTPCORE_MISSING_REASON,
    require_optional_packages,
)

require_optional_packages("httpcore", reason=HTTPCORE_MISSING_REASON)

import httpcore

from course_compiler import mcp_file_ingress as ingress
from course_compiler.mcp_file_ingress import (
    FileDownloadPolicy,
    FileIngressFailure,
    OpenAIFileReference,
    download_openai_file,
)


PRIVATE_URL = "https://files.example.test/private/attachment.pdf"
PRIVATE_BYTES = b"%PDF-1.4 private synthetic bytes for T033 tests only"
PRIVATE_EXCEPTION = "sentinel-exception-text-that-must-never-be-returned"


# =====================================================================
# Shared deterministic test doubles (no real network/DNS/TLS anywhere).
# =====================================================================


def _raw_http_response(
    status: int,
    headers: list[tuple[bytes, bytes]],
    body: bytes,
    *,
    reason: bytes = b"OK",
) -> bytes:
    header_lines = b"".join(name + b": " + value + b"\r\n" for name, value in headers)
    status_line = b"HTTP/1.1 " + str(status).encode("ascii") + b" " + reason + b"\r\n"
    return status_line + header_lines + b"\r\n" + body


def _chunked(data: bytes, size: int = 7) -> list[bytes]:
    """Split into small chunks so streaming genuinely exercises multiple reads."""

    return [data[i : i + size] for i in range(0, len(data), size)] or [b""]


class _FakeStream(httpcore.NetworkStream):
    """Scripted, instrumented stand-in for a real connected/TLS stream."""

    def __init__(
        self,
        chunks: list[bytes],
        *,
        read_error: BaseException | None = None,
        tls_stream: "_FakeStream | None" = None,
        tls_error: BaseException | None = None,
    ) -> None:
        self._chunks = list(chunks)
        self._read_error = read_error
        self._tls_stream = tls_stream
        self._tls_error = tls_error
        self.write_calls: list[tuple[bytes, float | None]] = []
        self.read_calls: list[float | None] = []
        self.start_tls_calls: list[dict[str, object]] = []
        self.closed = False

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        self.read_calls.append(timeout)
        if self._read_error is not None:
            raise self._read_error
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.write_calls.append((buffer, timeout))

    def close(self) -> None:
        self.closed = True

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        self.start_tls_calls.append(
            {
                "ssl_context": ssl_context,
                "server_hostname": server_hostname,
                "timeout": timeout,
            }
        )
        if self._tls_error is not None:
            raise self._tls_error
        return self._tls_stream if self._tls_stream is not None else self

    def get_extra_info(self, info: str) -> object:
        return None


class _FakeInnerBackend(httpcore.NetworkBackend):
    """Stand-in for the real ``httpcore.SyncBackend`` the custom backend delegates to.

    Records every ``connect_tcp`` call so tests can assert exactly what
    destination the physical connector received.
    """

    def __init__(
        self,
        response_bytes: bytes | None = None,
        *,
        connect_error: BaseException | None = None,
        stream_factory=None,
    ) -> None:
        self.connect_calls: list[dict[str, object]] = []
        self._response_bytes = response_bytes
        self._connect_error = connect_error
        self._stream_factory = stream_factory

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> httpcore.NetworkStream:
        self.connect_calls.append(
            {"host": host, "port": port, "timeout": timeout, "local_address": local_address}
        )
        if self._connect_error is not None:
            raise self._connect_error
        if self._stream_factory is not None:
            return self._stream_factory()
        assert self._response_bytes is not None
        return _FakeStream(_chunked(self._response_bytes))


def _install_inner_backend(test: unittest.TestCase, backend: httpcore.NetworkBackend) -> None:
    patcher = mock.patch.object(ingress.httpcore, "SyncBackend", return_value=backend)
    patcher.start()
    test.addCleanup(patcher.stop)


def _install_resolver(
    test: unittest.TestCase,
    addresses_by_hostname: dict[str, tuple[str, ...] | None],
    *,
    call_log: list[str] | None = None,
) -> None:
    def fake_resolve(hostname: str, port: int):
        if call_log is not None:
            call_log.append(hostname)
        answer = addresses_by_hostname.get(hostname)
        if answer is None:
            return None
        return tuple(ipaddress.ip_address(a) for a in answer)

    patcher = mock.patch.object(ingress, "_resolve_hop_addresses", side_effect=fake_resolve)
    patcher.start()
    test.addCleanup(patcher.stop)


def _f2_policy(
    *,
    timeout_seconds: float = 5.0,
    overall_deadline_seconds: float = 10.0,
    max_bytes: int = 1024,
) -> FileDownloadPolicy:
    return FileDownloadPolicy(
        (),
        timeout_seconds,
        max_bytes,
        "connection_bound",
        overall_deadline_seconds,
    )


def _ref(url: str = "https://safe.example.test/attachment.pdf") -> OpenAIFileReference:
    return OpenAIFileReference(url, "file-1", "application/pdf", "attachment.pdf")


# =====================================================================
# Destination-safety predicate.
# =====================================================================


class DestinationSafetyPredicateTests(unittest.TestCase):
    SAFE = (
        "8.8.8.8",
        "1.1.1.1",
        "2001:4860:4860::8888",
        "::ffff:8.8.8.8",
        "100.63.255.255",
        "100.128.0.0",
    )
    UNSAFE = (
        "127.0.0.1",
        "::1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "ff02::1",
        "fc00::1",
        "fe80::1",
        "::ffff:10.0.0.1",
        "::ffff:169.254.169.254",
        "240.0.0.1",
        "255.255.255.255",
        "fec0::1",
    )

    def test_safe_public_addresses_are_accepted(self) -> None:
        for addr in self.SAFE:
            with self.subTest(addr=addr):
                self.assertTrue(ingress._destination_is_safe(ipaddress.ip_address(addr)))

    def test_unsafe_addresses_are_rejected(self) -> None:
        for addr in self.UNSAFE:
            with self.subTest(addr=addr):
                self.assertFalse(ingress._destination_is_safe(ipaddress.ip_address(addr)))

    def test_ipv4_mapped_ipv6_is_unwrapped_before_classification(self) -> None:
        mapped_private = ipaddress.ip_address("::ffff:10.0.0.1")
        mapped_public = ipaddress.ip_address("::ffff:8.8.8.8")
        self.assertFalse(ingress._destination_is_safe(mapped_private))
        self.assertTrue(ingress._destination_is_safe(mapped_public))


# =====================================================================
# URL canonicalization / F2 eligibility.
# =====================================================================


class UrlCanonicalizationTests(unittest.TestCase):
    def test_valid_public_https_hostname_accepted(self) -> None:
        result = ingress._canonicalize_f2_url("https://safe.example.test/a/b?c=1")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.hostname, "safe.example.test")
        self.assertEqual(result.port, 443)
        self.assertEqual(result.target, b"/a/b?c=1")

    def test_http_scheme_rejected(self) -> None:
        self.assertIsNone(ingress._canonicalize_f2_url("http://safe.example.test/file"))

    def test_userinfo_rejected(self) -> None:
        self.assertIsNone(
            ingress._canonicalize_f2_url("https://user:secret@safe.example.test/file")
        )

    def test_fragment_rejected(self) -> None:
        self.assertIsNone(ingress._canonicalize_f2_url("https://safe.example.test/file#frag"))

    def test_explicit_non_443_port_rejected(self) -> None:
        self.assertIsNone(ingress._canonicalize_f2_url("https://safe.example.test:8443/file"))

    def test_explicit_443_port_accepted(self) -> None:
        self.assertIsNotNone(ingress._canonicalize_f2_url("https://safe.example.test:443/file"))

    def test_literal_ipv4_host_rejected(self) -> None:
        self.assertIsNone(ingress._canonicalize_f2_url("https://93.184.216.34/file"))

    def test_literal_ipv6_host_rejected(self) -> None:
        self.assertIsNone(ingress._canonicalize_f2_url("https://[2001:db8::1]/file"))

    def test_malformed_host_rejected(self) -> None:
        self.assertIsNone(ingress._canonicalize_f2_url("https://not_a_valid_host!/file"))

    def test_empty_netloc_rejected(self) -> None:
        self.assertIsNone(ingress._canonicalize_f2_url("https:///file"))

    def test_canonical_hostname_case_is_deterministic(self) -> None:
        lower = ingress._canonicalize_f2_url("https://Safe.Example.TEST/file")
        self.assertIsNotNone(lower)
        assert lower is not None
        self.assertEqual(lower.hostname, "safe.example.test")

    def test_canonical_hostname_trailing_dot_is_deterministically_rejected(self) -> None:
        self.assertIsNone(ingress._canonicalize_f2_url("https://safe.example.test./file"))

    def test_canonical_hostname_non_ascii_idna_form_is_deterministically_rejected(self) -> None:
        self.assertIsNone(ingress._canonicalize_f2_url("https://exämple.test/file"))

    def test_oversize_url_rejected(self) -> None:
        self.assertIsNone(ingress._canonicalize_f2_url("https://safe.example.test/" + "a" * 9000))

    def test_query_and_path_bytes_are_preserved_exactly(self) -> None:
        result = ingress._canonicalize_f2_url(
            "https://safe.example.test/download?sig=AbC%2F123&exp=999"
        )
        assert result is not None
        self.assertEqual(result.target, b"/download?sig=AbC%2F123&exp=999")

    def test_empty_path_becomes_root_target(self) -> None:
        result = ingress._canonicalize_f2_url("https://safe.example.test?x=1")
        assert result is not None
        self.assertEqual(result.target, b"/?x=1")


# =====================================================================
# FileDownloadPolicy mode semantics (F1 unchanged, F2 explicit).
# =====================================================================


class FileDownloadPolicyModeTests(unittest.TestCase):
    def test_default_mode_is_exact_host_and_matches_original_f1_contract(self) -> None:
        policy = FileDownloadPolicy(("files.example.test",))
        self.assertEqual(policy.mode, "exact_host")
        self.assertEqual(policy.allowed_hosts, ("files.example.test",))
        self.assertFalse(policy.bind_resolved_destination)

    def test_existing_three_positional_argument_construction_still_works(self) -> None:
        policy = FileDownloadPolicy(("files.example.test",), 3.5, 1024)
        self.assertEqual(policy.mode, "exact_host")
        self.assertEqual(policy.timeout_seconds, 3.5)
        self.assertEqual(policy.max_bytes, 1024)

    def test_connection_bound_mode_requires_empty_allowed_hosts(self) -> None:
        policy = FileDownloadPolicy((), mode="connection_bound")
        self.assertEqual(policy.allowed_hosts, ())

    def test_connection_bound_mode_with_nonempty_allowed_hosts_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            FileDownloadPolicy(("files.example.test",), mode="connection_bound")

    def test_connection_bound_mode_with_bind_resolved_destination_is_rejected(
        self,
    ) -> None:
        """"connection_bound" is already unconditionally bound to the shared
        engine; redundantly setting the exact-host opt-in flag alongside it
        is ambiguous and rejected, not silently accepted as a no-op."""

        with self.assertRaises(ValueError):
            FileDownloadPolicy((), mode="connection_bound", bind_resolved_destination=True)

    def test_connection_bound_mode_without_the_flag_remains_valid(self) -> None:
        # Normal runtime-created F2 construction is unaffected.
        policy = FileDownloadPolicy((), mode="connection_bound")
        self.assertFalse(policy.bind_resolved_destination)

    def test_f2_is_never_inferred_from_empty_allowed_hosts_alone(self) -> None:
        with self.assertRaises(ValueError):
            FileDownloadPolicy(())  # mode defaults to exact_host; empty hosts invalid for F1

    def test_invalid_mode_value_rejected(self) -> None:
        with self.assertRaises(ValueError):
            FileDownloadPolicy(("files.example.test",), mode="wildcard")  # type: ignore[arg-type]

    def test_overall_deadline_must_be_at_least_timeout_seconds(self) -> None:
        with self.assertRaises(ValueError):
            FileDownloadPolicy((), mode="connection_bound", timeout_seconds=30.0, overall_deadline_seconds=5.0)

    def test_overall_deadline_upper_bound_enforced(self) -> None:
        with self.assertRaises(ValueError):
            FileDownloadPolicy((), mode="connection_bound", overall_deadline_seconds=10_000.0)

    def test_bind_resolved_destination_must_be_bool(self) -> None:
        with self.assertRaises(ValueError):
            FileDownloadPolicy(("files.example.test",), bind_resolved_destination="yes")  # type: ignore[arg-type]

    def test_hardened_exact_host_overall_deadline_must_be_at_least_timeout_seconds(
        self,
    ) -> None:
        """Unlike the historical/unhardened default, a hardened
        (``bind_resolved_destination=True``) exact-host policy uses the
        shared resolve-validate-bind engine and is therefore subject to the
        same overall-deadline-vs-timeout invariant F2 has."""

        with self.assertRaises(ValueError):
            FileDownloadPolicy(
                ("files.example.test",),
                timeout_seconds=30.0,
                overall_deadline_seconds=5.0,
                bind_resolved_destination=True,
            )

    def test_legacy_unhardened_exact_host_timeout_60_seconds_is_backward_compatible(
        self,
    ) -> None:
        """A previously valid F1 construction using a timeout above the new
        F2-only default overall deadline (45s) must still construct, because
        an unhardened exact-host policy never consults that deadline field."""

        policy = FileDownloadPolicy(("files.example.test",), 60.0)
        self.assertEqual(policy.timeout_seconds, 60.0)
        self.assertFalse(policy.bind_resolved_destination)

    def test_legacy_unhardened_exact_host_timeout_120_seconds_is_backward_compatible(
        self,
    ) -> None:
        """The original F1 contract's upper timeout bound (120s) must still
        construct even though it exceeds the new default overall deadline."""

        policy = FileDownloadPolicy(("files.example.test",), 120.0)
        self.assertEqual(policy.timeout_seconds, 120.0)
        self.assertFalse(policy.bind_resolved_destination)


# =====================================================================
# F2 integration: download_openai_file() end to end, fully mocked.
# =====================================================================


class ConnectionBoundSuccessTests(unittest.TestCase):
    def test_valid_public_https_download_succeeds_and_uses_validated_ip(self) -> None:
        _install_resolver(self, {"safe.example.test": ("93.184.216.34",)})
        backend = _FakeInnerBackend(
            _raw_http_response(
                200,
                [(b"Content-Length", b"11")],
                b"hello world",
            )
        )
        _install_inner_backend(self, backend)

        result = download_openai_file(_ref(), policy=_f2_policy())

        self.assertEqual(result, b"hello world")
        self.assertEqual(len(backend.connect_calls), 1)
        self.assertEqual(backend.connect_calls[0]["host"], "93.184.216.34")
        self.assertEqual(backend.connect_calls[0]["port"], 443)


class ConnectionBoundDnsTests(unittest.TestCase):
    def test_all_safe_resolution_accepted(self) -> None:
        _install_resolver(self, {"safe.example.test": ("8.8.8.8", "1.1.1.1")})
        backend = _FakeInnerBackend(_raw_http_response(200, [(b"Content-Length", b"2")], b"ok"))
        _install_inner_backend(self, backend)
        result = download_openai_file(_ref(), policy=_f2_policy())
        self.assertEqual(result, b"ok")

    def test_all_unsafe_resolution_rejected(self) -> None:
        _install_resolver(self, {"safe.example.test": ("127.0.0.1",)})
        result = download_openai_file(_ref(), policy=_f2_policy())
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        self.assertEqual(result.code, "file_download_not_allowed")

    def test_mixed_safe_and_unsafe_resolution_rejected_as_a_whole(self) -> None:
        _install_resolver(self, {"safe.example.test": ("8.8.8.8", "169.254.169.254")})
        result = download_openai_file(_ref(), policy=_f2_policy())
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        self.assertEqual(result.code, "file_download_not_allowed")

    def test_resolver_is_invoked_exactly_once_per_hop(self) -> None:
        calls: list[str] = []
        _install_resolver(self, {"safe.example.test": ("8.8.8.8",)}, call_log=calls)
        backend = _FakeInnerBackend(_raw_http_response(200, [(b"Content-Length", b"2")], b"ok"))
        _install_inner_backend(self, backend)
        download_openai_file(_ref(), policy=_f2_policy())
        self.assertEqual(calls, ["safe.example.test"])

    def test_resolution_failure_fails_closed(self) -> None:
        _install_resolver(self, {"safe.example.test": None})
        result = download_openai_file(_ref(), policy=_f2_policy())
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        self.assertEqual(result.code, "file_download_not_allowed")

    def test_dns_rebinding_simulation_cannot_change_the_connection_target(self) -> None:
        """A resolver that would answer differently on a second call must never get one."""

        answers = iter([("8.8.8.8",), ("127.0.0.1",)])
        call_count = 0

        def fake_resolve(hostname, port):
            nonlocal call_count
            call_count += 1
            return tuple(ipaddress.ip_address(a) for a in next(answers))

        patcher = mock.patch.object(ingress, "_resolve_hop_addresses", side_effect=fake_resolve)
        patcher.start()
        self.addCleanup(patcher.stop)
        backend = _FakeInnerBackend(_raw_http_response(200, [(b"Content-Length", b"2")], b"ok"))
        _install_inner_backend(self, backend)

        result = download_openai_file(_ref(), policy=_f2_policy())

        self.assertEqual(result, b"ok")
        self.assertEqual(call_count, 1)  # never asked a second time for this hop
        self.assertEqual(backend.connect_calls[0]["host"], "8.8.8.8")  # first answer, not "rebound"

    def test_lower_layer_never_observes_the_canonical_hostname_after_validation(self) -> None:
        _install_resolver(self, {"safe.example.test": ("93.184.216.34",)})
        backend = _FakeInnerBackend(_raw_http_response(200, [(b"Content-Length", b"2")], b"ok"))
        _install_inner_backend(self, backend)
        download_openai_file(_ref(), policy=_f2_policy())
        for call in backend.connect_calls:
            self.assertNotEqual(call["host"], "safe.example.test")
            self.assertEqual(call["host"], "93.184.216.34")


class ConnectionBoundBackendBindingTests(unittest.TestCase):
    def test_backend_receives_canonical_hostname_and_443_as_identity_check_only(self) -> None:
        _install_resolver(self, {"safe.example.test": ("93.184.216.34",)})
        backend = _FakeInnerBackend(_raw_http_response(200, [(b"Content-Length", b"2")], b"ok"))
        _install_inner_backend(self, backend)
        download_openai_file(_ref(), policy=_f2_policy())
        # httpcore itself is what calls the *custom* backend with (host, port);
        # this test proves what that custom backend then hands the recorded
        # inner backend is the *numeric* destination, never the hostname.
        self.assertEqual(backend.connect_calls[0]["host"], "93.184.216.34")
        self.assertEqual(backend.connect_calls[0]["port"], 443)

    def test_unexpected_host_argument_fails_closed_without_connecting(self) -> None:
        inner = _FakeInnerBackend(_raw_http_response(200, [], b""))
        _install_inner_backend(self, inner)
        custom = ingress._ValidatedDestinationBackend(
            "safe.example.test", 443, "93.184.216.34", time.monotonic() + 5.0
        )
        with self.assertRaises(httpcore.ConnectError):
            custom.connect_tcp("attacker.example.test", 443)
        self.assertEqual(inner.connect_calls, [])

    def test_unexpected_port_argument_fails_closed_without_connecting(self) -> None:
        inner = _FakeInnerBackend(_raw_http_response(200, [], b""))
        _install_inner_backend(self, inner)
        custom = ingress._ValidatedDestinationBackend(
            "safe.example.test", 443, "93.184.216.34", time.monotonic() + 5.0
        )
        with self.assertRaises(httpcore.ConnectError):
            custom.connect_tcp("safe.example.test", 8443)
        self.assertEqual(inner.connect_calls, [])

    def test_backend_never_resolves_the_host_it_receives(self) -> None:
        with mock.patch.object(ingress.socket, "getaddrinfo") as fake_getaddrinfo:
            inner = _FakeInnerBackend(stream_factory=lambda: _FakeStream([]))
            _install_inner_backend(self, inner)
            custom = ingress._ValidatedDestinationBackend(
                "safe.example.test", 443, "93.184.216.34", time.monotonic() + 5.0
            )
            custom.connect_tcp("safe.example.test", 443)
            fake_getaddrinfo.assert_not_called()


class ConnectionBoundTlsIdentityTests(unittest.TestCase):
    def test_start_tls_receives_canonical_hostname_not_the_backend(self) -> None:
        _install_resolver(self, {"safe.example.test": ("93.184.216.34",)})
        recorded_stream = _FakeStream(_chunked(_raw_http_response(200, [(b"Content-Length", b"2")], b"ok")))
        connect_stream = _FakeStream([], tls_stream=recorded_stream)
        backend = _FakeInnerBackend(stream_factory=lambda: connect_stream)
        _install_inner_backend(self, backend)

        result = download_openai_file(_ref(), policy=_f2_policy())

        self.assertEqual(result, b"ok")
        self.assertEqual(len(connect_stream.start_tls_calls), 1)
        self.assertEqual(connect_stream.start_tls_calls[0]["server_hostname"], "safe.example.test")

    def test_certificate_verification_context_is_not_disabled(self) -> None:
        _install_resolver(self, {"safe.example.test": ("93.184.216.34",)})
        recorded_stream = _FakeStream(_chunked(_raw_http_response(200, [(b"Content-Length", b"2")], b"ok")))
        connect_stream = _FakeStream([], tls_stream=recorded_stream)
        backend = _FakeInnerBackend(stream_factory=lambda: connect_stream)
        _install_inner_backend(self, backend)

        download_openai_file(_ref(), policy=_f2_policy())

        context = connect_stream.start_tls_calls[0]["ssl_context"]
        self.assertIsInstance(context, ssl.SSLContext)
        assert isinstance(context, ssl.SSLContext)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_request_authority_remains_canonical_hostname(self) -> None:
        _install_resolver(self, {"safe.example.test": ("93.184.216.34",)})
        captured: dict[str, list[tuple[bytes, bytes]]] = {}
        real_request_cls = httpcore.Request

        def capturing_request(*args, **kwargs):
            request = real_request_cls(*args, **kwargs)
            captured["headers"] = list(request.headers)
            return request

        backend = _FakeInnerBackend(_raw_http_response(200, [(b"Content-Length", b"2")], b"ok"))
        _install_inner_backend(self, backend)
        with mock.patch.object(ingress.httpcore, "Request", side_effect=capturing_request):
            download_openai_file(_ref(), policy=_f2_policy())
        host_headers = [v for k, v in captured["headers"] if k.lower() == b"host"]
        self.assertEqual(host_headers, [b"safe.example.test"])


class ConnectionBoundCleanupTests(unittest.TestCase):
    """Resource cleanup on every failure exit after the physical TCP
    connection to the validated numeric IP has already been established —
    no garbage-collection reliance, no swallowed/leaked exception text."""

    def test_tls_failure_after_successful_connect_closes_the_underlying_stream(
        self,
    ) -> None:
        _install_resolver(self, {"safe.example.test": ("93.184.216.34",)})
        connect_stream = _FakeStream([], tls_error=RuntimeError(PRIVATE_EXCEPTION))
        backend = _FakeInnerBackend(stream_factory=lambda: connect_stream)
        _install_inner_backend(self, backend)

        result = download_openai_file(_ref(), policy=_f2_policy())

        self.assertEqual(len(backend.connect_calls), 1)  # physical connect occurred
        self.assertTrue(connect_stream.closed)
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        self.assertEqual(result.code, "file_download_failed")
        self.assertNotIn(PRIVATE_EXCEPTION, repr(result))

    def test_post_connect_protocol_failure_closes_the_underlying_stream(self) -> None:
        """Smallest deterministic post-connect failure: TLS succeeds, but the
        stream raises while HTTPCore's own protocol machinery reads the
        response."""

        _install_resolver(self, {"safe.example.test": ("93.184.216.34",)})
        connect_stream = _FakeStream([], read_error=OSError(PRIVATE_EXCEPTION))
        backend = _FakeInnerBackend(stream_factory=lambda: connect_stream)
        _install_inner_backend(self, backend)

        result = download_openai_file(_ref(), policy=_f2_policy())

        self.assertEqual(len(backend.connect_calls), 1)  # physical connect occurred
        self.assertTrue(connect_stream.closed)
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        self.assertEqual(result.code, "file_download_failed")
        self.assertNotIn(PRIVATE_EXCEPTION, repr(result))

    def test_hardened_f1_tls_failure_closes_the_underlying_stream(self) -> None:
        """Same cleanup guarantee applies to the hardened exact-host path,
        since it shares the identical engine."""

        _install_resolver(self, {"files.example.test": ("93.184.216.34",)})
        connect_stream = _FakeStream([], tls_error=RuntimeError(PRIVATE_EXCEPTION))
        backend = _FakeInnerBackend(stream_factory=lambda: connect_stream)
        _install_inner_backend(self, backend)

        result = download_openai_file(_hardened_ref(), policy=_hardened_f1_policy())

        self.assertEqual(len(backend.connect_calls), 1)
        self.assertTrue(connect_stream.closed)
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        self.assertEqual(result.code, "file_download_failed")
        self.assertNotIn(PRIVATE_EXCEPTION, repr(result))

    def test_successful_redirect_and_download_cleanup_still_pass(self) -> None:
        """Existing accepted success/redirect cleanup behavior is unaffected
        by the new failure-path cleanup (see also
        ConnectionBoundRedirectTests.test_old_hop_connection_closed_before_new_hop)."""

        _install_resolver(self, {"safe.example.test": ("93.184.216.34",)})
        backend = _FakeInnerBackend(_raw_http_response(200, [(b"Content-Length", b"2")], b"ok"))
        _install_inner_backend(self, backend)
        result = download_openai_file(_ref(), policy=_f2_policy())
        self.assertEqual(result, b"ok")


class ConnectionBoundRedirectTests(unittest.TestCase):
    def test_safe_redirect_is_followed(self) -> None:
        _install_resolver(
            self,
            {
                "safe.example.test": ("93.184.216.34",),
                "safe-2.example.test": ("93.184.216.35",),
            },
        )
        first = _FakeInnerBackend(
            _raw_http_response(302, [(b"Location", b"https://safe-2.example.test/next")], b"")
        )
        second = _FakeInnerBackend(_raw_http_response(200, [(b"Content-Length", b"2")], b"ok"))
        backends = iter([first, second])
        _install_inner_backend_sequence(self, backends)

        result = download_openai_file(_ref(), policy=_f2_policy())
        self.assertEqual(result, b"ok")

    def test_relative_redirect_is_resolved_against_current_url(self) -> None:
        _install_resolver(self, {"safe.example.test": ("93.184.216.34",)})
        first = _FakeInnerBackend(_raw_http_response(302, [(b"Location", b"/next-path")], b""))
        second = _FakeInnerBackend(_raw_http_response(200, [(b"Content-Length", b"2")], b"ok"))
        _install_inner_backend_sequence(self, iter([first, second]))
        result = download_openai_file(_ref(), policy=_f2_policy())
        self.assertEqual(result, b"ok")

    def test_redirect_to_unsafe_ip_derived_destination_rejected(self) -> None:
        _install_resolver(
            self,
            {"safe.example.test": ("93.184.216.34",), "internal.example.test": ("127.0.0.1",)},
        )
        first = _FakeInnerBackend(
            _raw_http_response(302, [(b"Location", b"https://internal.example.test/x")], b"")
        )
        _install_inner_backend_sequence(self, iter([first]))
        result = download_openai_file(_ref(), policy=_f2_policy())
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        self.assertEqual(result.code, "file_download_not_allowed")

    def test_https_to_http_downgrade_redirect_rejected(self) -> None:
        _install_resolver(self, {"safe.example.test": ("93.184.216.34",)})
        first = _FakeInnerBackend(
            _raw_http_response(302, [(b"Location", b"http://safe.example.test/x")], b"")
        )
        _install_inner_backend_sequence(self, iter([first]))
        result = download_openai_file(_ref(), policy=_f2_policy())
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        self.assertEqual(result.code, "file_download_not_allowed")

    def test_multi_hop_safe_redirect_chain_succeeds_at_the_boundary(self) -> None:
        hosts = {f"hop{i}.example.test": (f"10.10.10.{i}",) for i in range(1, 6)}
        # Every hop resolves "unsafe" per the private-range test IP on purpose
        # would fail; use public-looking loopback-free synthetic addresses via
        # an explicit safe override instead.
        hosts = {f"hop{i}.example.test": ("93.184.216.34",) for i in range(0, 6)}
        hosts["safe.example.test"] = ("93.184.216.34",)
        _install_resolver(self, hosts)

        backends = []
        for i in range(1, 6):
            backends.append(
                _FakeInnerBackend(
                    _raw_http_response(
                        302, [(b"Location", f"https://hop{i}.example.test/n".encode())], b""
                    )
                )
            )
        backends.append(_FakeInnerBackend(_raw_http_response(200, [(b"Content-Length", b"2")], b"ok")))
        _install_inner_backend_sequence(self, iter(backends))

        result = download_openai_file(_ref(), policy=_f2_policy())
        self.assertEqual(result, b"ok")  # exactly MAX_REDIRECT_HOPS (5) redirects followed

    def test_redirect_limit_exceeded_fails_closed(self) -> None:
        hosts = {f"hop{i}.example.test": ("93.184.216.34",) for i in range(0, 7)}
        hosts["safe.example.test"] = ("93.184.216.34",)
        _install_resolver(self, hosts)

        backends = []
        for i in range(1, 8):
            backends.append(
                _FakeInnerBackend(
                    _raw_http_response(
                        302, [(b"Location", f"https://hop{i}.example.test/n".encode())], b""
                    )
                )
            )
        _install_inner_backend_sequence(self, iter(backends))

        result = download_openai_file(_ref(), policy=_f2_policy())
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        self.assertEqual(result.code, "file_download_failed")

    def test_old_hop_connection_closed_before_new_hop(self) -> None:
        _install_resolver(
            self, {"safe.example.test": ("93.184.216.34",), "safe-2.example.test": ("93.184.216.35",)}
        )
        first_stream = _FakeStream(
            _chunked(_raw_http_response(302, [(b"Location", b"https://safe-2.example.test/n")], b""))
        )
        first = _FakeInnerBackend(stream_factory=lambda: first_stream)
        second = _FakeInnerBackend(_raw_http_response(200, [(b"Content-Length", b"2")], b"ok"))
        _install_inner_backend_sequence(self, iter([first, second]))

        download_openai_file(_ref(), policy=_f2_policy())
        self.assertTrue(first_stream.closed)


def _install_inner_backend_sequence(test: unittest.TestCase, backends) -> None:
    patcher = mock.patch.object(ingress.httpcore, "SyncBackend", side_effect=lambda: next(backends))
    patcher.start()
    test.addCleanup(patcher.stop)


class ConnectionBoundProxyAndReuseTests(unittest.TestCase):
    def test_environment_proxy_variables_have_no_effect_on_destination(self) -> None:
        _install_resolver(self, {"safe.example.test": ("93.184.216.34",)})
        backend = _FakeInnerBackend(_raw_http_response(200, [(b"Content-Length", b"2")], b"ok"))
        _install_inner_backend(self, backend)
        with mock.patch.dict(
            "os.environ",
            {"HTTPS_PROXY": "http://proxy.invalid:3128", "HTTP_PROXY": "http://proxy.invalid:3128"},
        ):
            result = download_openai_file(_ref(), policy=_f2_policy())
        self.assertEqual(result, b"ok")
        self.assertEqual(backend.connect_calls[0]["host"], "93.184.216.34")

    def test_no_backend_or_connection_state_is_reused_across_independent_calls(self) -> None:
        _install_resolver(self, {"safe.example.test": ("93.184.216.34",)})
        backend_a = _FakeInnerBackend(_raw_http_response(200, [(b"Content-Length", b"2")], b"ok"))
        backend_b = _FakeInnerBackend(_raw_http_response(200, [(b"Content-Length", b"2")], b"ok"))
        _install_inner_backend_sequence(self, iter([backend_a, backend_b]))
        download_openai_file(_ref(), policy=_f2_policy())
        download_openai_file(_ref(), policy=_f2_policy())
        self.assertEqual(len(backend_a.connect_calls), 1)
        self.assertEqual(len(backend_b.connect_calls), 1)

    def test_no_backend_or_connection_state_is_reused_across_a_redirect_hop(self) -> None:
        _install_resolver(
            self, {"safe.example.test": ("93.184.216.34",), "safe-2.example.test": ("93.184.216.35",)}
        )
        first = _FakeInnerBackend(
            _raw_http_response(302, [(b"Location", b"https://safe-2.example.test/n")], b"")
        )
        second = _FakeInnerBackend(_raw_http_response(200, [(b"Content-Length", b"2")], b"ok"))
        _install_inner_backend_sequence(self, iter([first, second]))
        download_openai_file(_ref(), policy=_f2_policy())
        self.assertEqual(len(first.connect_calls), 1)
        self.assertEqual(len(second.connect_calls), 1)


class ConnectionBoundDeadlineTests(unittest.TestCase):
    def test_connect_timeout_bounded_by_remaining_overall_budget(self) -> None:
        base = 1_000.0
        deadline_at = base + 2.0
        inner = _FakeInnerBackend(stream_factory=lambda: _FakeStream([]))
        with mock.patch.object(ingress.httpcore, "SyncBackend", return_value=inner):
            with mock.patch.object(ingress.time, "monotonic", return_value=base):
                custom = ingress._ValidatedDestinationBackend(
                    "safe.example.test", 443, "93.184.216.34", deadline_at
                )
                custom.connect_tcp("safe.example.test", 443, timeout=50.0)
        # remaining budget (2.0s) is smaller than the caller's requested 50s.
        self.assertEqual(inner.connect_calls[0]["timeout"], 2.0)

    def test_read_bounded_by_remaining_overall_budget(self) -> None:
        base = 2_000.0
        deadline_at = base + 3.0
        real_stream = _FakeStream([b"data"])
        wrapped = ingress._BoundNetworkStream(real_stream, deadline_at)
        with mock.patch.object(ingress.time, "monotonic", return_value=base):
            wrapped.read(1024, timeout=100.0)
        # remaining budget (3.0s) is smaller than the caller's requested 100s.
        self.assertEqual(real_stream.read_calls, [3.0])

    def test_deadline_already_exhausted_before_connect_fails_closed_without_connecting(
        self,
    ) -> None:
        _install_resolver(self, {"safe.example.test": ("93.184.216.34",)})
        backend = _FakeInnerBackend(stream_factory=lambda: _FakeStream([]))
        _install_inner_backend(self, backend)
        policy = _f2_policy(overall_deadline_seconds=5.0, timeout_seconds=5.0)
        base = 9_000.0
        # First observation establishes the deadline (base + 5.0); every
        # later observation is already past it.
        clock = iter([base])
        with mock.patch.object(
            ingress.time, "monotonic", side_effect=lambda: next(clock, base + 1000.0)
        ):
            result = download_openai_file(_ref(), policy=policy)
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        self.assertEqual(result.code, "file_download_failed")
        self.assertEqual(backend.connect_calls, [])

    def test_slowloris_repeated_small_reads_cannot_exceed_overall_deadline(self) -> None:
        """A server sending one small chunk just under each read timeout must
        still be cut off once the *overall* deadline — not the per-read
        timeout — is exhausted."""

        _install_resolver(self, {"safe.example.test": ("93.184.216.34",)})
        # Many chunks, each individually "fine", but the wall clock below
        # advances past the overall deadline partway through.
        chunks = [b"x"] * 50
        body = _raw_http_response(200, [(b"Content-Length", b"5000")], b"") + b"".join(chunks)

        real_monotonic = time.monotonic
        start = real_monotonic()
        call_count = 0

        def advancing_clock():
            nonlocal call_count
            call_count += 1
            # Jump straight past the deadline on the 6th observation, well
            # before all 50 chunks could be consumed at "real" speed.
            if call_count > 6:
                return start + 1000.0
            return start

        backend = _FakeInnerBackend(stream_factory=lambda: _FakeStream(_chunked(body, size=1)))
        _install_inner_backend(self, backend)
        policy = _f2_policy(overall_deadline_seconds=5.0, timeout_seconds=5.0)
        with mock.patch.object(ingress.time, "monotonic", side_effect=advancing_clock):
            result = download_openai_file(_ref(), policy=policy)
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        self.assertEqual(result.code, "file_download_failed")

    def test_deadline_budget_spans_redirect_hops(self) -> None:
        _install_resolver(
            self, {"safe.example.test": ("93.184.216.34",), "safe-2.example.test": ("93.184.216.35",)}
        )
        first = _FakeInnerBackend(
            _raw_http_response(302, [(b"Location", b"https://safe-2.example.test/n")], b"")
        )
        second_stream = _FakeStream([])  # would hang forever if asked to read
        second = _FakeInnerBackend(stream_factory=lambda: second_stream)
        _install_inner_backend_sequence(self, iter([first, second]))

        real_monotonic = time.monotonic
        start = real_monotonic()
        calls = {"n": 0}

        def clock():
            calls["n"] += 1
            if calls["n"] > 3:
                return start + 1000.0  # deadline already exhausted by hop 2
            return start

        policy = _f2_policy(overall_deadline_seconds=5.0, timeout_seconds=5.0)
        with mock.patch.object(ingress.time, "monotonic", side_effect=clock):
            result = download_openai_file(_ref(), policy=policy)
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        self.assertEqual(result.code, "file_download_failed")

    def test_resolver_elapsed_time_is_charged_against_overall_budget(self) -> None:
        """Proves the *actual* required sequence: the resolver genuinely
        runs (exactly once), and only the *immediate post-resolution*
        deadline check — not some earlier check that never lets the
        resolver run at all — is what stops this attempt."""

        base = 5_000.0
        resolve_calls = 0

        def slow_resolve(hostname: str, port: int):
            nonlocal resolve_calls
            resolve_calls += 1
            # Simulate a resolver call that itself takes real wall-clock
            # time: the *next* monotonic() observation, taken immediately
            # after this call returns, has jumped past the deadline.
            return (ipaddress.ip_address("93.184.216.34"),)

        # 1) establish deadline; 2) pre-resolution check (still in budget);
        # 3) resolver runs; 4) immediate post-resolution check (expired).
        clock_values = iter([base, base, base + 100.0])
        with mock.patch.object(
            ingress.time, "monotonic", side_effect=lambda: next(clock_values, base + 100.0)
        ):
            with mock.patch.object(ingress, "_resolve_hop_addresses", side_effect=slow_resolve):
                backend = _FakeInnerBackend(stream_factory=lambda: _FakeStream([]))
                _install_inner_backend(self, backend)
                policy = _f2_policy(overall_deadline_seconds=5.0, timeout_seconds=5.0)
                result = download_openai_file(_ref(), policy=policy)
        self.assertEqual(resolve_calls, 1)  # the resolver genuinely ran
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        self.assertEqual(result.code, "file_download_failed")
        self.assertEqual(backend.connect_calls, [])  # never reached connect: budget already gone

    def test_hardened_f1_resolver_elapsed_time_is_charged_against_overall_budget(
        self,
    ) -> None:
        """Same proof, for the hardened exact-host path: the shared engine
        applies the identical immediate post-resolution deadline check."""

        base = 6_000.0
        resolve_calls = 0

        def slow_resolve(hostname: str, port: int):
            nonlocal resolve_calls
            resolve_calls += 1
            return (ipaddress.ip_address("93.184.216.34"),)

        clock_values = iter([base, base, base + 100.0])
        with mock.patch.object(
            ingress.time, "monotonic", side_effect=lambda: next(clock_values, base + 100.0)
        ):
            with mock.patch.object(ingress, "_resolve_hop_addresses", side_effect=slow_resolve):
                backend = _FakeInnerBackend(stream_factory=lambda: _FakeStream([]))
                _install_inner_backend(self, backend)
                policy = _hardened_f1_policy(overall_deadline_seconds=5.0, timeout_seconds=5.0)
                result = download_openai_file(_hardened_ref(), policy=policy)
        self.assertEqual(resolve_calls, 1)
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        self.assertEqual(result.code, "file_download_failed")
        self.assertEqual(backend.connect_calls, [])


class ConnectionBoundSizeStreamingTests(unittest.TestCase):
    def test_declared_oversize_content_length_rejected_before_body_read(self) -> None:
        _install_resolver(self, {"safe.example.test": ("93.184.216.34",)})
        backend = _FakeInnerBackend(
            _raw_http_response(200, [(b"Content-Length", b"999999999")], b"short")
        )
        _install_inner_backend(self, backend)
        result = download_openai_file(_ref(), policy=_f2_policy(max_bytes=1024))
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        self.assertEqual(result.code, "file_too_large")

    def test_missing_content_length_with_actual_oversize_rejected_by_streamed_counter(self) -> None:
        _install_resolver(self, {"safe.example.test": ("93.184.216.34",)})
        body = b"x" * 2000
        backend = _FakeInnerBackend(_raw_http_response(200, [], body))
        _install_inner_backend(self, backend)
        result = download_openai_file(_ref(), policy=_f2_policy(max_bytes=1024))
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        self.assertEqual(result.code, "file_too_large")

    def test_short_content_length_correctly_frames_the_body_and_ignores_trailing_bytes(
        self,
    ) -> None:
        """A short, *correct* ``Content-Length`` is honored as HTTP/1.1
        message framing (via h11): only the declared bytes are ever part of
        this response's body, regardless of what other bytes happen to sit
        in the stream afterward (e.g. a would-be pipelined next response).
        This is safe, standard framing, not a truncation bug — a genuinely
        misleading declaration is instead covered by the oversize-declared,
        oversize-actual-without-declaration, and conflicting-header cases
        above, which are the exploitable "lying Content-Length" shapes."""

        _install_resolver(self, {"safe.example.test": ("93.184.216.34",)})
        trailing_bytes_outside_this_message = b"x" * 2000
        backend = _FakeInnerBackend(
            _raw_http_response(200, [(b"Content-Length", b"5")], b"hello")
            + trailing_bytes_outside_this_message
        )
        _install_inner_backend(self, backend)
        result = download_openai_file(_ref(), policy=_f2_policy(max_bytes=1024))
        self.assertEqual(result, b"hello")

    def test_exact_boundary_size_accepted(self) -> None:
        _install_resolver(self, {"safe.example.test": ("93.184.216.34",)})
        body = b"y" * 1024
        backend = _FakeInnerBackend(_raw_http_response(200, [(b"Content-Length", b"1024")], body))
        _install_inner_backend(self, backend)
        result = download_openai_file(_ref(), policy=_f2_policy(max_bytes=1024))
        self.assertEqual(result, body)

    def test_one_byte_over_boundary_rejected(self) -> None:
        _install_resolver(self, {"safe.example.test": ("93.184.216.34",)})
        body = b"y" * 1025
        backend = _FakeInnerBackend(_raw_http_response(200, [], body))
        _install_inner_backend(self, backend)
        result = download_openai_file(_ref(), policy=_f2_policy(max_bytes=1024))
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        self.assertEqual(result.code, "file_too_large")

    def test_conflicting_content_length_headers_are_treated_as_malformed(self) -> None:
        _install_resolver(self, {"safe.example.test": ("93.184.216.34",)})
        backend = _FakeInnerBackend(
            _raw_http_response(
                200, [(b"Content-Length", b"5"), (b"Content-Length", b"9999")], b"hello"
            )
        )
        _install_inner_backend(self, backend)
        result = download_openai_file(_ref(), policy=_f2_policy())
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        self.assertEqual(result.code, "file_download_failed")

    def test_non_200_status_is_a_safe_download_failure(self) -> None:
        _install_resolver(self, {"safe.example.test": ("93.184.216.34",)})
        backend = _FakeInnerBackend(_raw_http_response(404, [], b"not found"))
        _install_inner_backend(self, backend)
        result = download_openai_file(_ref(), policy=_f2_policy())
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        self.assertEqual(result.code, "file_download_failed")


class ConnectionBoundPrivacyTests(unittest.TestCase):
    def test_url_and_hostname_never_appear_in_any_failure_output(self) -> None:
        _install_resolver(self, {"safe.example.test": ("127.0.0.1",)})
        result = download_openai_file(
            OpenAIFileReference(PRIVATE_URL.replace("files.example.test", "safe.example.test"), "f1"),
            policy=_f2_policy(),
        )
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        serialized = repr(result)
        self.assertNotIn("safe.example.test", serialized)
        self.assertNotIn("private", serialized)

    def test_no_credential_or_auth_header_is_ever_forwarded(self) -> None:
        _install_resolver(self, {"safe.example.test": ("93.184.216.34",)})
        captured_headers: list[list[tuple[bytes, bytes]]] = []
        real_request_cls = httpcore.Request

        def capturing_request(*args, **kwargs):
            request = real_request_cls(*args, **kwargs)
            captured_headers.append(list(request.headers))
            return request

        backend = _FakeInnerBackend(_raw_http_response(200, [(b"Content-Length", b"2")], b"ok"))
        _install_inner_backend(self, backend)
        with mock.patch.object(ingress.httpcore, "Request", side_effect=capturing_request):
            download_openai_file(_ref(), policy=_f2_policy())
        for headers in captured_headers:
            names = {name.lower() for name, _ in headers}
            self.assertNotIn(b"authorization", names)
            self.assertNotIn(b"cookie", names)
        self.assertEqual(captured_headers[0], [(b"Host", b"safe.example.test"), (b"Accept", b"application/octet-stream")])

    def test_persisted_source_evidence_ingestion_is_content_addressed_only(self) -> None:
        # The ingress module itself never persists anything; this asserts the
        # downloaded bytes contract it hands upstream carries no URL/host
        # metadata alongside them.
        _install_resolver(self, {"safe.example.test": ("93.184.216.34",)})
        backend = _FakeInnerBackend(_raw_http_response(200, [(b"Content-Length", b"2")], b"ok"))
        _install_inner_backend(self, backend)
        result = download_openai_file(_ref(), policy=_f2_policy())
        self.assertEqual(result, b"ok")
        self.assertIsInstance(result, bytes)  # exact bytes only, no envelope/metadata


# =====================================================================
# F1 historical/legacy backward compatibility (unhardened default).
# =====================================================================


def _hardened_f1_policy(
    hosts: tuple[str, ...] = ("files.example.test",),
    *,
    timeout_seconds: float = 5.0,
    overall_deadline_seconds: float = 10.0,
    max_bytes: int = 1024,
) -> FileDownloadPolicy:
    return FileDownloadPolicy(
        hosts,
        timeout_seconds,
        max_bytes,
        "exact_host",
        overall_deadline_seconds,
        True,
    )


def _hardened_ref(url: str = "https://files.example.test/attachment.pdf") -> OpenAIFileReference:
    return OpenAIFileReference(url, "file-1", "application/pdf", "attachment.pdf")


def _mocked_urllib_success(build_opener: mock.MagicMock) -> None:
    opener = mock.MagicMock()
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    response.geturl.return_value = PRIVATE_URL
    response.headers.get.return_value = None
    response.read.side_effect = [PRIVATE_BYTES, b""]
    opener.open.return_value = response
    build_opener.return_value = opener


class LegacyExactHostBackwardCompatibilityTests(unittest.TestCase):
    """The historical, unhardened "exact_host" default
    (``bind_resolved_destination=False``): byte-for-byte-unchanged behavior,
    no destination resolution of its own, downloaded through
    :mod:`urllib.request`. Frozen default-mode regression coverage also
    lives in tests/test_mcp_adapter.py::FileIngressBoundaryTests (not
    modified by this pass)."""

    def test_default_policy_never_performs_a_resolve_precheck(self) -> None:
        with mock.patch.object(ingress, "_resolve_hop_addresses") as fake_resolve:
            policy = FileDownloadPolicy(("files.example.test",))
            self.assertFalse(policy.bind_resolved_destination)
            fake_resolve.assert_not_called()

    def test_legacy_construction_uses_urllib_not_the_shared_engine(self) -> None:
        with mock.patch.object(ingress.httpcore, "SyncBackend") as fake_sync_backend:
            with mock.patch.object(ingress.urllib.request, "build_opener") as build_opener:
                _mocked_urllib_success(build_opener)
                policy = FileDownloadPolicy(("files.example.test",))
                result = download_openai_file(
                    OpenAIFileReference(PRIVATE_URL, "f1"), policy=policy
                )
        self.assertEqual(result, PRIVATE_BYTES)
        fake_sync_backend.assert_not_called()

    def test_legacy_timeout_60_seconds_still_constructs_and_downloads(self) -> None:
        with mock.patch.object(ingress.urllib.request, "build_opener") as build_opener:
            _mocked_urllib_success(build_opener)
            policy = FileDownloadPolicy(("files.example.test",), 60.0)
            result = download_openai_file(
                OpenAIFileReference(PRIVATE_URL, "f1"), policy=policy
            )
        self.assertEqual(result, PRIVATE_BYTES)

    def test_legacy_timeout_120_seconds_still_constructs_and_downloads(self) -> None:
        with mock.patch.object(ingress.urllib.request, "build_opener") as build_opener:
            _mocked_urllib_success(build_opener)
            policy = FileDownloadPolicy(("files.example.test",), 120.0)
            result = download_openai_file(
                OpenAIFileReference(PRIVATE_URL, "f1"), policy=policy
            )
        self.assertEqual(result, PRIVATE_BYTES)


# =====================================================================
# F1 hardened ("bind_resolved_destination=True") destination-bound safety.
#
# This is the policy shape ``create_runtime_config`` builds automatically
# for ``--allowed-file-host`` (tests/test_mcp_runtime.py); it shares the
# exact resolve-validate-bind engine "F2" uses, gated by an additional
# exact-host-membership check at every hop.
# =====================================================================


class HardenedExactHostDestinationBindingTests(unittest.TestCase):
    def test_membership_gate_rejects_before_any_resolution(self) -> None:
        with mock.patch.object(ingress, "_resolve_hop_addresses") as fake_resolve:
            result = download_openai_file(
                OpenAIFileReference("https://not-allowlisted.example.test/x", "f1"),
                policy=_hardened_f1_policy(),
            )
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        self.assertEqual(result.code, "file_download_not_allowed")
        fake_resolve.assert_not_called()

    def test_safe_allowlisted_host_resolves_once_and_connects_to_validated_ip(
        self,
    ) -> None:
        calls: list[str] = []
        _install_resolver(self, {"files.example.test": ("93.184.216.34",)}, call_log=calls)
        backend = _FakeInnerBackend(_raw_http_response(200, [(b"Content-Length", b"2")], b"ok"))
        _install_inner_backend(self, backend)
        result = download_openai_file(_hardened_ref(), policy=_hardened_f1_policy())
        self.assertEqual(result, b"ok")
        self.assertEqual(calls, ["files.example.test"])
        self.assertEqual(backend.connect_calls[0]["host"], "93.184.216.34")

    def test_unsafe_resolved_destination_rejected(self) -> None:
        _install_resolver(self, {"files.example.test": ("127.0.0.1",)})
        result = download_openai_file(_hardened_ref(), policy=_hardened_f1_policy())
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        self.assertEqual(result.code, "file_download_not_allowed")

    def test_mixed_safe_and_unsafe_resolved_destination_rejected(self) -> None:
        _install_resolver(self, {"files.example.test": ("8.8.8.8", "169.254.169.254")})
        result = download_openai_file(_hardened_ref(), policy=_hardened_f1_policy())
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        self.assertEqual(result.code, "file_download_not_allowed")

    def test_dns_resolution_failure_is_rejected_not_deferred(self) -> None:
        """Unlike the removed "defer to transport" precheck behavior, the
        hardened path fails closed on resolution failure, exactly like F2."""

        _install_resolver(self, {"files.example.test": None})
        result = download_openai_file(_hardened_ref(), policy=_hardened_f1_policy())
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        self.assertEqual(result.code, "file_download_not_allowed")

    def test_physical_connector_never_receives_the_original_hostname(self) -> None:
        _install_resolver(self, {"files.example.test": ("93.184.216.34",)})
        backend = _FakeInnerBackend(_raw_http_response(200, [(b"Content-Length", b"2")], b"ok"))
        _install_inner_backend(self, backend)
        download_openai_file(_hardened_ref(), policy=_hardened_f1_policy())
        for call in backend.connect_calls:
            self.assertNotEqual(call["host"], "files.example.test")
            self.assertEqual(call["host"], "93.184.216.34")

    def test_dns_rebinding_simulation_cannot_alter_the_physical_target(self) -> None:
        """A resolver that would answer differently on a second call must
        never get one, for the hardened exact-host path either."""

        answers = iter([("93.184.216.34",), ("127.0.0.1",)])
        call_count = 0

        def fake_resolve(hostname: str, port: int):
            nonlocal call_count
            call_count += 1
            return tuple(ipaddress.ip_address(a) for a in next(answers))

        patcher = mock.patch.object(ingress, "_resolve_hop_addresses", side_effect=fake_resolve)
        patcher.start()
        self.addCleanup(patcher.stop)
        backend = _FakeInnerBackend(_raw_http_response(200, [(b"Content-Length", b"2")], b"ok"))
        _install_inner_backend(self, backend)

        result = download_openai_file(_hardened_ref(), policy=_hardened_f1_policy())

        self.assertEqual(result, b"ok")
        self.assertEqual(call_count, 1)
        self.assertEqual(backend.connect_calls[0]["host"], "93.184.216.34")

    def test_tls_sni_and_host_remain_the_original_allowlisted_hostname(self) -> None:
        _install_resolver(self, {"files.example.test": ("93.184.216.34",)})
        recorded_stream = _FakeStream(
            _chunked(_raw_http_response(200, [(b"Content-Length", b"2")], b"ok"))
        )
        connect_stream = _FakeStream([], tls_stream=recorded_stream)
        backend = _FakeInnerBackend(stream_factory=lambda: connect_stream)
        _install_inner_backend(self, backend)

        result = download_openai_file(_hardened_ref(), policy=_hardened_f1_policy())

        self.assertEqual(result, b"ok")
        self.assertEqual(len(connect_stream.start_tls_calls), 1)
        self.assertEqual(
            connect_stream.start_tls_calls[0]["server_hostname"], "files.example.test"
        )


class HardenedExactHostRedirectTests(unittest.TestCase):
    def test_redirect_to_same_allowlisted_host_succeeds(self) -> None:
        _install_resolver(self, {"files.example.test": ("93.184.216.34",)})
        first = _FakeInnerBackend(_raw_http_response(302, [(b"Location", b"/next")], b""))
        second = _FakeInnerBackend(_raw_http_response(200, [(b"Content-Length", b"2")], b"ok"))
        _install_inner_backend_sequence(self, iter([first, second]))
        result = download_openai_file(_hardened_ref(), policy=_hardened_f1_policy())
        self.assertEqual(result, b"ok")

    def test_redirect_to_a_second_allowlisted_host_succeeds(self) -> None:
        _install_resolver(
            self,
            {
                "files.example.test": ("93.184.216.34",),
                "files-2.example.test": ("93.184.216.35",),
            },
        )
        first = _FakeInnerBackend(
            _raw_http_response(302, [(b"Location", b"https://files-2.example.test/next")], b"")
        )
        second = _FakeInnerBackend(_raw_http_response(200, [(b"Content-Length", b"2")], b"ok"))
        _install_inner_backend_sequence(self, iter([first, second]))
        policy = _hardened_f1_policy(hosts=("files.example.test", "files-2.example.test"))
        result = download_openai_file(_hardened_ref(), policy=policy)
        self.assertEqual(result, b"ok")

    def test_redirect_to_nonallowlisted_host_rejected_before_connection(self) -> None:
        _install_resolver(
            self,
            {
                "files.example.test": ("93.184.216.34",),
                "attacker.example.test": ("93.184.216.99",),
            },
        )
        first = _FakeInnerBackend(
            _raw_http_response(302, [(b"Location", b"https://attacker.example.test/x")], b"")
        )
        second = _FakeInnerBackend(_raw_http_response(200, [], b""))
        _install_inner_backend_sequence(self, iter([first, second]))
        result = download_openai_file(_hardened_ref(), policy=_hardened_f1_policy())
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        self.assertEqual(result.code, "file_download_not_allowed")
        # the nonallowlisted redirect target's backend is never connected.
        self.assertEqual(second.connect_calls, [])

    def test_redirect_to_allowlisted_host_resolving_unsafe_is_rejected(self) -> None:
        _install_resolver(
            self,
            {
                "files.example.test": ("93.184.216.34",),
                "files-2.example.test": ("127.0.0.1",),
            },
        )
        first = _FakeInnerBackend(
            _raw_http_response(302, [(b"Location", b"https://files-2.example.test/x")], b"")
        )
        _install_inner_backend_sequence(self, iter([first]))
        policy = _hardened_f1_policy(hosts=("files.example.test", "files-2.example.test"))
        result = download_openai_file(_hardened_ref(), policy=policy)
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        self.assertEqual(result.code, "file_download_not_allowed")

    def test_https_to_http_downgrade_redirect_rejected(self) -> None:
        _install_resolver(self, {"files.example.test": ("93.184.216.34",)})
        first = _FakeInnerBackend(
            _raw_http_response(302, [(b"Location", b"http://files.example.test/x")], b"")
        )
        _install_inner_backend_sequence(self, iter([first]))
        result = download_openai_file(_hardened_ref(), policy=_hardened_f1_policy())
        self.assertIsInstance(result, FileIngressFailure)
        assert isinstance(result, FileIngressFailure)
        self.assertEqual(result.code, "file_download_not_allowed")


class TypeContractTests(unittest.TestCase):
    def test_download_openai_file_rejects_wrong_reference_type(self) -> None:
        with self.assertRaises(TypeError):
            download_openai_file("not-a-reference", policy=_f2_policy())  # type: ignore[arg-type]

    def test_download_openai_file_rejects_wrong_policy_type(self) -> None:
        with self.assertRaises(TypeError):
            download_openai_file(_ref(), policy="not-a-policy")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
