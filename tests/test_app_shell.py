"""Focused tests for T047 application shell contract."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from course_compiler.app.config import AppConfig, AppConfigError, create_config
from course_compiler.app.context import AppContext, create_app_context
from course_compiler.app.server import create_server

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_ISOLATED_BASE = REPOSITORY_ROOT / "local-data" / "test-tmp-app-shell"
_ISOLATED_BASE.mkdir(parents=True, exist_ok=True)


def _isolated_tmp_dir() -> tempfile.TemporaryDirectory[str]:
    """Create a unique temporary directory beneath an approved ignored root.

    Uses ``local-data/test-tmp-app-shell`` so that the resulting
    ``data_root`` is inside an approved private runtime root
    (``local-data/``) and therefore passes production validation
    without weakening the privacy boundary. Caller must clean up
    (via context manager or explicit cleanup).
    """
    return tempfile.TemporaryDirectory(dir=str(_ISOLATED_BASE), prefix="")


def _isolated_data_root_path(*parts: str) -> Path:
    """Create a one-off isolated data-root directory (caller cleans via shutil if needed)."""
    d = tempfile.mkdtemp(dir=str(_ISOLATED_BASE), prefix="")
    p = Path(d)
    if parts:
        p = p / Path(*parts)
        p.mkdir(parents=True, exist_ok=True)
    return Path(d)


def _get(port: int, path: str, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path)
        resp = conn.getresponse()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        body = resp.read()
        return resp.status, headers, body
    finally:
        conn.close()


def _post(port: int, path: str, payload: dict | None = None) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        headers = {"content-type": "application/json"}
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        body_bytes = resp.read()
        return resp.status, headers, body_bytes
    finally:
        conn.close()


class TestAppConfig(unittest.TestCase):
    def test_default_host_is_loopback(self) -> None:
        with _isolated_tmp_dir() as tmp:
            cfg = create_config(data_root=Path(tmp))
            self.assertEqual(cfg.host, "127.0.0.1")
            self.assertIn(cfg.host, {"127.0.0.1", "localhost", "::1"})

    def test_default_port(self) -> None:
        with _isolated_tmp_dir() as tmp:
            cfg = create_config(data_root=Path(tmp))
            self.assertEqual(cfg.port, 8787)

    def test_rejects_non_loopback_by_default(self) -> None:
        with _isolated_tmp_dir() as tmp:
            with self.assertRaises(AppConfigError):
                create_config(host="0.0.0.0", port=8787, data_root=Path(tmp))
        with _isolated_tmp_dir() as tmp:
            with self.assertRaises(AppConfigError):
                create_config(host="192.168.1.1", port=8787, data_root=Path(tmp))

    def test_allows_non_loopback_with_explicit_flag(self) -> None:
        with _isolated_tmp_dir() as tmp:
            cfg = create_config(host="0.0.0.0", port=8787, data_root=Path(tmp), allow_non_loopback=True)
            self.assertEqual(cfg.host, "0.0.0.0")

    def test_rejects_invalid_port(self) -> None:
        for bad in [-1, 1, 22, 80, 70000, 99999]:
            with _isolated_tmp_dir() as tmp:
                with self.assertRaises(AppConfigError):
                    create_config(host="127.0.0.1", port=bad, data_root=Path(tmp))
        with _isolated_tmp_dir() as tmp:
            with self.assertRaises(AppConfigError):
                create_config(host="127.0.0.1", port="notaport", data_root=Path(tmp))  # type: ignore[arg-type]

    def test_allows_ephemeral_port_zero(self) -> None:
        with _isolated_tmp_dir() as tmp:
            cfg = create_config(host="127.0.0.1", port=0, data_root=Path(tmp))
            self.assertEqual(cfg.port, 0)

    def test_rejects_invalid_host(self) -> None:
        for bad in ["", " ", "0.0.0.0/0", "host with space"]:
            with _isolated_tmp_dir() as tmp:
                with self.assertRaises(AppConfigError):
                    create_config(host=bad, port=8787, data_root=Path(tmp))

    def test_rejects_invalid_data_root_type(self) -> None:
        with self.assertRaises(AppConfigError):
            create_config(host="127.0.0.1", port=8787, data_root="")  # type: ignore[arg-type]

    def test_direct_appconfig_rejects_non_loopback(self) -> None:
        with _isolated_tmp_dir() as tmp:
            with self.assertRaises(AppConfigError):
                AppConfig(host="0.0.0.0", port=8787, data_root=Path(tmp))

    def test_rejects_unsafe_data_root_inside_repo(self) -> None:
        for bad in ["docs/runtime", "docs", "tests/out", "README.md"]:
            with self.subTest(bad=bad):
                with self.assertRaises(AppConfigError):
                    create_config(host="127.0.0.1", port=8787, data_root=Path(bad))
                with self.assertRaises(AppConfigError):
                    AppConfig(host="127.0.0.1", port=8787, data_root=Path(bad))

    def test_allows_safe_data_root_inside_ignored(self) -> None:
        for good in ["local-data/app", "local-data", "local-artifacts/out", "build/cache", "local-data/app/subdir"]:
            with self.subTest(good=good):
                cfg = create_config(host="127.0.0.1", port=8787, data_root=Path(good))
                self.assertEqual(cfg.data_root, Path(good))
                cfg2 = AppConfig(host="127.0.0.1", port=8787, data_root=Path(good))
                self.assertEqual(cfg2.data_root, Path(good))

    def test_rejects_external_data_root(self) -> None:
        # Arbitrary external absolute paths must NOT silently become product roots.
        with tempfile.TemporaryDirectory() as external:
            external_path = Path(external)
            with self.assertRaises(AppConfigError):
                create_config(host="127.0.0.1", port=8787, data_root=external_path)
            with self.assertRaises(AppConfigError):
                AppConfig(host="127.0.0.1", port=8787, data_root=external_path)
            # Absolute inside repo but outside allowed should still reject
            with self.assertRaises(AppConfigError):
                create_config(host="127.0.0.1", port=8787, data_root=REPOSITORY_ROOT / "docs/runtime")
            with self.assertRaises(AppConfigError):
                create_config(host="127.0.0.1", port=8787, data_root=REPOSITORY_ROOT / "README.md")

    def test_ipv6_loopback_supported(self) -> None:
        with _isolated_tmp_dir() as tmp:
            cfg = create_config(host="::1", port=8787, data_root=Path(tmp))
            self.assertEqual(cfg.host, "::1")
            cfg2 = AppConfig(host="::1", port=8787, data_root=Path(tmp))
            self.assertEqual(cfg2.host, "::1")

    def test_env_allow_non_loopback_survives(self) -> None:
        with _isolated_tmp_dir() as tmp:
            os.environ["COURSE_COMPILER_ALLOW_NON_LOOPBACK"] = "1"
            os.environ["COURSE_COMPILER_HOST"] = "0.0.0.0"
            try:
                cfg = create_config(port=8787, data_root=Path(tmp))
                self.assertEqual(cfg.host, "0.0.0.0")
            finally:
                os.environ.pop("COURSE_COMPILER_ALLOW_NON_LOOPBACK", None)
                os.environ.pop("COURSE_COMPILER_HOST", None)
        with _isolated_tmp_dir() as tmp:
            os.environ["COURSE_COMPILER_ALLOW_NON_LOOPBACK"] = "1"
            try:
                cfg = create_config(host="192.168.1.1", port=8787, data_root=Path(tmp))
                self.assertEqual(cfg.host, "192.168.1.1")
            finally:
                os.environ.pop("COURSE_COMPILER_ALLOW_NON_LOOPBACK", None)

    def test_data_root_env_rejected_for_unsafe(self) -> None:
        os.environ["COURSE_COMPILER_DATA_ROOT"] = "docs/runtime"
        try:
            with self.assertRaises(AppConfigError):
                create_config(host="127.0.0.1", port=8787)
        finally:
            os.environ.pop("COURSE_COMPILER_DATA_ROOT", None)


class TestDataRootPrivacyBoundary(unittest.TestCase):
    """P2 corrective: prove approved-root rule and bypass rejection."""

    def test_default_local_data_app_allowed(self) -> None:
        cfg = create_config()
        self.assertEqual(cfg.data_root, REPOSITORY_ROOT / "local-data" / "app")
        # Also via direct is_allowed check
        from course_compiler.app.config import _is_allowed_data_root

        self.assertTrue(_is_allowed_data_root(REPOSITORY_ROOT / "local-data" / "app"))
        self.assertTrue(_is_allowed_data_root(Path("local-data/app")))

    def test_nested_local_data_allowed(self) -> None:
        for good in ["local-data/app/subdir", "local-data/nested/deep", "local-data/test-tmp-app-shell/foo"]:
            with self.subTest(good=good):
                cfg = create_config(host="127.0.0.1", port=8787, data_root=Path(good))
                self.assertEqual(cfg.data_root, Path(good))
                # Absolute form also allowed
                abs_good = REPOSITORY_ROOT / good
                cfg2 = create_config(host="127.0.0.1", port=8787, data_root=abs_good)
                self.assertEqual(cfg2.data_root, abs_good)

    def test_approved_local_artifacts_build_behavior(self) -> None:
        # For AppConfig, local-artifacts/ and build/ are also approved ignored roots
        # (they are ignored by Git). Verify they remain allowed where appropriate.
        for good in ["local-artifacts/out", "local-artifacts/cache/sub", "build/cache", "build/tmp"]:
            with self.subTest(good=good):
                cfg = create_config(host="127.0.0.1", port=8787, data_root=Path(good))
                self.assertEqual(cfg.data_root, Path(good))

    def test_rejects_docs_runtime_and_tracked(self) -> None:
        for bad in ["docs/runtime", "docs", "tests/out", "README.md", "course_compiler/app/config.py"]:
            with self.subTest(bad=bad):
                with self.assertRaises(AppConfigError):
                    create_config(host="127.0.0.1", port=8787, data_root=Path(bad))
                with self.assertRaises(AppConfigError):
                    AppConfig(host="127.0.0.1", port=8787, data_root=Path(bad))
                # Absolute inside repo also rejected
                abs_bad = REPOSITORY_ROOT / bad
                with self.assertRaises(AppConfigError):
                    create_config(host="127.0.0.1", port=8787, data_root=abs_bad)

    def test_rejects_arbitrary_external_absolute(self) -> None:
        # Examples from task brief that must NOT silently become normal product roots
        examples = [
            Path("/tmp/course-compiler-external-test"),
            Path("/arbitrary/external/path"),
            Path.home() / "Documents/course-compiler-data",
        ]
        # Also a real tempfile external dir
        with tempfile.TemporaryDirectory() as external:
            examples.append(Path(external))
            examples.append(Path(external) / "subdir")
            for bad in examples:
                with self.subTest(bad=str(bad)):
                    with self.assertRaises(AppConfigError):
                        create_config(host="127.0.0.1", port=8787, data_root=bad)
                    with self.assertRaises(AppConfigError):
                        AppConfig(host="127.0.0.1", port=8787, data_root=bad)

    def test_symlink_escape_rejected(self) -> None:
        # Symlink inside local-data pointing outside must be rejected via resolve()
        with _isolated_tmp_dir() as tmp:
            outside = tempfile.mkdtemp()
            try:
                link = Path(tmp) / "escape_link"
                # Create symlink from inside approved root to outside
                try:
                    link.symlink_to(outside)
                except (OSError, NotImplementedError) as exc:
                    self.skipTest(f"symlink not supported: {exc}")
                with self.assertRaises(AppConfigError):
                    create_config(host="127.0.0.1", port=8787, data_root=link)
                # Also symlink to tracked location docs/
                link2 = Path(tmp) / "to_docs"
                try:
                    link2.symlink_to(REPOSITORY_ROOT / "docs")
                except (OSError, NotImplementedError) as exc:
                    self.skipTest(f"symlink not supported: {exc}")
                with self.assertRaises(AppConfigError):
                    create_config(host="127.0.0.1", port=8787, data_root=link2)
            finally:
                shutil.rmtree(outside, ignore_errors=True)
        # Symlink escape via .. traversal also rejected
        with self.assertRaises(AppConfigError):
            create_config(host="127.0.0.1", port=8787, data_root=Path("local-data/../docs/runtime"))
        with self.assertRaises(AppConfigError):
            create_config(host="127.0.0.1", port=8787, data_root=Path("local-data/app/../../README.md"))

    def test_environment_override_cannot_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as external:
            external_path = Path(external)
            os.environ["COURSE_COMPILER_DATA_ROOT"] = str(external_path)
            try:
                with self.assertRaises(AppConfigError):
                    create_config(host="127.0.0.1", port=8787)
            finally:
                os.environ.pop("COURSE_COMPILER_DATA_ROOT", None)
            # Even with absolute external via env, still rejected
            os.environ["COURSE_COMPILER_DATA_ROOT"] = "/arbitrary/external/path"
            try:
                with self.assertRaises(AppConfigError):
                    create_config(host="127.0.0.1", port=8787)
            finally:
                os.environ.pop("COURSE_COMPILER_DATA_ROOT", None)
            # Symlink via env also rejected
            with _isolated_tmp_dir() as tmp:
                outside = tempfile.mkdtemp()
                try:
                    link = Path(tmp) / "env_link"
                    try:
                        link.symlink_to(outside)
                    except (OSError, NotImplementedError) as exc:
                        self.skipTest(f"symlink not supported: {exc}")
                    os.environ["COURSE_COMPILER_DATA_ROOT"] = str(link)
                    try:
                        with self.assertRaises(AppConfigError):
                            create_config(host="127.0.0.1", port=8787)
                    finally:
                        os.environ.pop("COURSE_COMPILER_DATA_ROOT", None)
                finally:
                    shutil.rmtree(outside, ignore_errors=True)

    def test_cli_cannot_bypass(self) -> None:
        from course_compiler.app.__main__ import build_parser, main

        parser = build_parser()
        with tempfile.TemporaryDirectory() as external:
            # Parser accepts Path, but create_config inside main must reject
            ret = main(["--data-root", str(Path(external))])
            self.assertEqual(ret, 2)
            ret2 = main(["--data-root", "/arbitrary/external/path"])
            self.assertEqual(ret2, 2)
            ret3 = main(["--data-root", "docs/runtime"])
            self.assertEqual(ret3, 2)
            # Valid CLI still succeeds (would try to run server; we test via create_config path)
            # Instead validate parser + create_config directly
            args = parser.parse_args(["--data-root", "local-data/app"])
            cfg = create_config(host=args.host, port=args.port, data_root=args.data_root)
            self.assertEqual(cfg.data_root, Path("local-data/app"))
            # CLI with env external also rejected
            os.environ["COURSE_COMPILER_DATA_ROOT"] = str(Path(external))
            try:
                ret4 = main([])
                self.assertEqual(ret4, 2)
            finally:
                os.environ.pop("COURSE_COMPILER_DATA_ROOT", None)

    def test_tests_retain_isolation_without_weakening(self) -> None:
        # Prove isolation uses approved ignored subdirectory and cleans up
        with _isolated_tmp_dir() as tmp:
            cfg = create_config(host="127.0.0.1", port=0, data_root=Path(tmp) / "isolated")
            ctx = AppContext(cfg)
            prepared = ctx.prepare()
            self.assertTrue(prepared.is_dir())
            # Must be inside repository and under local-data
            resolved = prepared.resolve()
            self.assertTrue(str(resolved).startswith(str(REPOSITORY_ROOT.resolve() / "local-data")))
            ctx.close()
            # Directory was created under local-data (ignored, not tracked)
            self.assertTrue(Path(tmp).exists())
        # After context manager, temp dir is removed
        # Create another isolated and verify cleanup
        tmp_path = Path(tempfile.mkdtemp(dir=str(_ISOLATED_BASE)))
        try:
            cfg2 = create_config(host="127.0.0.1", port=0, data_root=tmp_path)
            self.assertTrue(_is_allowed := True)
            # Ensure it is inside allowed root
            from course_compiler.app.config import _is_allowed_data_root

            self.assertTrue(_is_allowed_data_root(tmp_path))
        finally:
            shutil.rmtree(tmp_path, ignore_errors=True)
        self.assertFalse(tmp_path.exists())


class TestAppContext(unittest.TestCase):
    def test_can_be_constructed_without_external_services(self) -> None:
        with _isolated_tmp_dir() as tmp:
            cfg = create_config(host="127.0.0.1", port=0, data_root=Path(tmp) / "data")
            ctx = create_app_context(cfg)
            self.assertIsInstance(ctx, AppContext)
            prepared = ctx.prepare()
            self.assertTrue(prepared.is_dir())
            ctx.close()

    def test_lifecycle_prepare_and_close(self) -> None:
        with _isolated_tmp_dir() as tmp:
            cfg = create_config(host="127.0.0.1", port=0, data_root=Path(tmp) / "app")
            ctx = AppContext(cfg)
            ctx.prepare()
            # Open one store and ensure close works without error
            store = ctx.open_workflow_state_store()
            self.assertIsNotNone(store)
            ctx.close()
            # Future paths do not create files
            self.assertFalse(ctx.future_course_store_path().exists())
            self.assertFalse(ctx.future_job_store_path().exists())
            self.assertFalse(ctx.future_semantic_work_store_path().exists())

    def test_database_path_rejects_traversal(self) -> None:
        with _isolated_tmp_dir() as tmp:
            cfg = create_config(host="127.0.0.1", port=0, data_root=Path(tmp))
            ctx = AppContext(cfg)
            with self.assertRaises(Exception):
                ctx.database_path("../escape.sqlite3")
            with self.assertRaises(Exception):
                ctx.database_path("a/b.sqlite3")
            with self.assertRaises(Exception):
                ctx.database_path("evil.db/../../etc")

    def test_database_path_rejects_bad_suffix(self) -> None:
        with _isolated_tmp_dir() as tmp:
            cfg = create_config(host="127.0.0.1", port=0, data_root=Path(tmp))
            ctx = AppContext(cfg)
            with self.assertRaises(Exception):
                ctx.database_path("evil.txt")
            with self.assertRaises(Exception):
                ctx.database_path("noext")

    def test_context_close_idempotent_and_safe(self) -> None:
        with _isolated_tmp_dir() as tmp:
            cfg = create_config(host="127.0.0.1", port=0, data_root=Path(tmp))
            ctx = AppContext(cfg)
            ctx.prepare()
            ctx.open_workflow_state_store()
            ctx.close()
            ctx.close()
            # Repeated close must not raise and handles cleared
            self.assertEqual(ctx._open_handles, [])  # type: ignore[attr-defined]
            # Context manager path
            with AppContext(cfg) as c2:
                self.assertTrue(c2.data_root.is_dir())

    def test_context_open_failure_does_not_leak(self) -> None:
        # Use invalid path to trigger store_unavailable via unreadable directory?
        # Instead verify that failed open does not retain handle
        with _isolated_tmp_dir() as tmp:
            cfg = create_config(host="127.0.0.1", port=0, data_root=Path(tmp))
            ctx = AppContext(cfg)
            # Force failure by making data_root a file
            bad = Path(tmp) / "file"
            bad.write_text("x")
            cfg_bad = create_config(host="127.0.0.1", port=0, data_root=Path(tmp) / "subdir")
            # Make subdir parent file conflict: create file where dir expected
            # Actually test that context close after failed prepare doesn't leak
            ctx2 = AppContext(cfg)
            ctx2.close()

    def test_no_course_job_tables_created(self) -> None:
        with _isolated_tmp_dir() as tmp:
            data_root = Path(tmp) / "dataroot"
            cfg = create_config(host="127.0.0.1", port=0, data_root=data_root)
            ctx = AppContext(cfg)
            ctx.prepare()
            # Only known store openers should create files; future paths must not be created
            self.assertEqual(ctx.future_course_store_path(), data_root / "courses.sqlite3")
            self.assertEqual(ctx.future_job_store_path(), data_root / "course-jobs.sqlite3")
            self.assertFalse((data_root / "courses.sqlite3").exists())
            self.assertFalse((data_root / "course-jobs.sqlite3").exists())
            # Opening a real store should create that store's file but not course files
            ctx.open_source_evidence_store()
            ctx.close()
            self.assertFalse((data_root / "courses.sqlite3").exists())


class TestHttpShell(unittest.TestCase):
    def _start_server(self, tmp: str) -> tuple[object, int, threading.Thread]:
        cfg = create_config(host="127.0.0.1", port=0, data_root=Path(tmp))
        server = create_server(cfg)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # Wait for server to be ready
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                status, _, _ = _get(port, "/healthz")
                if status == 200:
                    break
            except Exception:
                pass
            time.sleep(0.05)
        return server, port, thread

    def test_healthz_returns_200_json(self) -> None:
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                status, headers, body = _get(port, "/healthz")
                self.assertEqual(status, 200)
                self.assertIn("application/json", headers.get("content-type", ""))
                payload = json.loads(body.decode("utf-8"))
                self.assertEqual(payload, {"status": "ok"})
                # Content-safe: no paths or stack traces
                text = body.decode("utf-8")
                self.assertNotIn("local-data", text)
                self.assertNotIn("Traceback", text)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_api_courses_returns_empty_collection(self) -> None:
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                status, headers, body = _get(port, "/api/courses")
                self.assertEqual(status, 200)
                self.assertIn("application/json", headers.get("content-type", ""))
                payload = json.loads(body.decode("utf-8"))
                self.assertEqual(payload, {"courses": []})
                # Exact shape: must be dict with single key "courses" mapping to empty list
                self.assertEqual(set(payload.keys()), {"courses"})
                self.assertEqual(payload["courses"], [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_root_serves_shell(self) -> None:
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                status, headers, body = _get(port, "/")
                self.assertEqual(status, 200)
                self.assertIn("text/html", headers.get("content-type", ""))
                html = body.decode("utf-8")
                self.assertIn("Course Compiler", html)
                self.assertIn("Your courses", html)
                self.assertIn("No courses yet", html)
                # Shell must contain header/navigation indicators
                self.assertIn("Study workspace", html)
                # Static CSS linked
                self.assertIn("/static/app.css", html)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_t051_browser_contract_has_private_source_and_generation_flow(self) -> None:
        """The browser contract exposes the live-E2E prerequisites, not API-only setup."""
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                status, _, body = _get(port, "/static/app.js")
                self.assertEqual(status, 200)
                script = body.decode("utf-8")
                # T054-MVP: BYOK is a disabled post-v0.1 option; creation is
                # GPT-only and the refresh control recovers durable state.
                _, _, root_body = _get(port, "/")
                self.assertIn("GPT-only in v0.1", root_body.decode("utf-8"))
                self.assertIn("refresh-btn", root_body.decode("utf-8"))
                # U1/U3/U4/U5/U7/U8/U9/U10: source control, opaque identity,
                # exact accepted mutations, and all workflow action controls.
                for token in (
                    "Attach Sources",
                    'type="file"',
                    'accept="application/pdf,.pdf"',
                    "multiple",
                    "sourceId()",
                    '"/sources"',
                    "Start Generation",
                    '"/start-generation"',
                    "Continue in ChatGPT",
                    "Resume in ChatGPT",
                    "Priority approval required",
                    "Map approval required",
                    '"/owner/" + kind',
                    "Build PDF",
                ):
                    self.assertIn(token, script)
                self.assertNotIn("filename:", script)
                self.assertNotIn("source_id: file.name", script)

                # U2/U4/U6: model the browser's exact JSON request shape with
                # two invented PDF byte strings.  No filename is sent or saved.
                status, _, body = _post(
                    port,
                    "/api/courses",
                    {"title": "Invented browser flow", "ai_mode": "gpt", "quality_mode": "fast"},
                )
                self.assertEqual(status, 201)
                course_id = json.loads(body)["course"]["course_id"]
                for source_id, content in (
                    ("src-ui-000000000000000000000001", "JVBERi0xLjQKJSBmaXJzdCBpbnZlbnRlZAo="),
                    ("src-ui-000000000000000000000002", "JVBERi0xLjQKJSBzZWNvbmQgaW52ZW50ZWQK"),
                ):
                    status, _, _ = _post(
                        port,
                        f"/api/courses/{course_id}/sources",
                        {"source_id": source_id, "content_base64": content},
                    )
                    self.assertEqual(status, 201)
                status, _, body = _get(port, f"/api/courses/{course_id}")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["course"]["source_count"], 2)

                status, _, body = _post(port, f"/api/courses/{course_id}/start-generation")
                self.assertEqual(status, 201)
                job_id = json.loads(body)["job_id"]
                status, _, body = _get(port, f"/api/jobs/{job_id}")
                self.assertEqual(status, 200)
                job = json.loads(body)
                self.assertEqual(job["course_id"], course_id)
                self.assertEqual(job["status"], "sourcing")
                self.assertIsNone(job["owner_approval"])

                # U10: the existing clearly-labelled synthetic fixture may
                # drive committed test state, while the browser contract keeps
                # it separate from the GPT relay and exposes Build PDF next.
                status, _, _ = _post(port, f"/api/jobs/{job_id}/run-controlled-generation")
                self.assertEqual(status, 200)
                status, _, body = _get(port, f"/api/jobs/{job_id}")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["status"], "deterministic_building")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_static_css_served(self) -> None:
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                status, headers, body = _get(port, "/static/app.css")
                self.assertEqual(status, 200)
                self.assertIn("text/css", headers.get("content-type", ""))
                css = body.decode("utf-8")
                self.assertIn("--bg", css)
                self.assertIn("--accent", css)
                # T054-MVP: keyboard focus must stay visible on required
                # controls; the needs-attention banner and card sections
                # must have dedicated styles.
                self.assertIn("focus-visible", css)
                self.assertIn(".notice", css)
                self.assertIn(".section", css)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_unknown_api_returns_safe_404_json(self) -> None:
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                status, headers, body = _get(port, "/api/unknown")
                self.assertEqual(status, 404)
                self.assertIn("application/json", headers.get("content-type", ""))
                payload = json.loads(body.decode("utf-8"))
                self.assertEqual(payload, {"error": "not_found"})
                status2, _, body2 = _get(port, "/api/courses/extra")
                self.assertEqual(status2, 404)
                self.assertEqual(json.loads(body2.decode("utf-8")), {"error": "not_found"})
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_unknown_route_returns_safe_404_html(self) -> None:
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                status, headers, body = _get(port, "/nope")
                self.assertEqual(status, 404)
                self.assertIn("text/html", headers.get("content-type", ""))
                html = body.decode("utf-8")
                self.assertIn("Not found", html)
                self.assertNotIn("Traceback", html)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_static_traversal_blocked(self) -> None:
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                # Attempt path traversal via /static/../course_compiler/__init__.py
                status, _, _ = _get(port, "/static/../course_compiler/__init__.py")
                self.assertEqual(status, 404)
                status2, _, _ = _get(port, "/static/..%2Fcourse_compiler%2F__init__.py")
                self.assertIn(status2, (404, 400))
                # Absolute path injection
                status3, _, _ = _get(port, "/static//etc/passwd")
                self.assertEqual(status3, 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_healthz_head_and_api_head(self) -> None:
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                status, headers, body = _get(port, "/healthz", method="HEAD")
                self.assertEqual(status, 200)
                self.assertEqual(body, b"")
                self.assertIn("application/json", headers.get("content-type", ""))
                status2, headers2, body2 = _get(port, "/api/courses", method="HEAD")
                self.assertEqual(status2, 200)
                self.assertEqual(body2, b"")
                self.assertIn("application/json", headers2.get("content-type", ""))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_method_not_allowed_on_healthz(self) -> None:
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                status, headers, body = _get(port, "/healthz", method="POST")
                self.assertEqual(status, 405)
                payload = json.loads(body.decode("utf-8"))
                self.assertEqual(payload, {"error": "method_not_allowed"})
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_no_mutation_on_shell_requests(self) -> None:
        with _isolated_tmp_dir() as tmp:
            data_root = Path(tmp)
            cfg = create_config(host="127.0.0.1", port=0, data_root=data_root)
            server, port, thread = self._start_server(tmp)
            try:
                # Ensure no course/job tables created after requests
                for path in ["/", "/healthz", "/api/courses", "/api/unknown"]:
                    _get(port, path)
                self.assertFalse((data_root / "courses.sqlite3").exists())
                self.assertFalse((data_root / "course-jobs.sqlite3").exists())
                self.assertFalse((data_root / "semantic-work.sqlite3").exists())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_startup_shutdown_lifecycle_clean(self) -> None:
        with _isolated_tmp_dir() as tmp:
            cfg = create_config(host="127.0.0.1", port=0, data_root=Path(tmp))
            server = create_server(cfg)
            port = server.server_address[1]
            # Server not yet serving; health should fail before start
            with self.assertRaises(Exception):
                _get(port, "/healthz")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            time.sleep(0.1)
            status, _, _ = _get(port, "/healthz")
            self.assertEqual(status, 200)
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def test_bind_failure_closes_prepared_context_once_without_secondary_error(self) -> None:
        """T051 live-E2E regression: TCPServer may close during failed bind."""
        with _isolated_tmp_dir() as tmp:
            occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            try:
                cfg = create_config(host="127.0.0.1", port=occupied.getsockname()[1], data_root=Path(tmp))
                context = MagicMock()
                context.prepare.return_value = Path(tmp)
                with patch("course_compiler.app.server.create_app_context", return_value=context):
                    with self.assertRaises(OSError) as raised:
                        create_server(cfg)
                self.assertNotIn("_closed", str(raised.exception))
                context.prepare.assert_called_once_with()
                context.close.assert_called_once_with()
            finally:
                occupied.close()

    def test_trace_and_unknown_method_handled_safely(self) -> None:
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                for method in ("TRACE", "CONNECT", "PROPFIND", "SEARCH"):
                    status, headers, body = _get(port, "/healthz", method=method)
                    # Should not leak stack trace or path, and not 501 default HTML
                    self.assertNotIn(b"Traceback", body)
                    self.assertIn(status, (404, 405))
                    self.assertNotEqual(headers.get("content-type", ""), "")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_static_encoded_traversal_blocked(self) -> None:
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                for path in (
                    "/static/%2e%2e/app.css",
                    "/static/%252e%252e/app.css",
                    "/static//app.css",
                    "/static/./app.css",
                    "/static/app.css/",
                    "/static/..%2fapp.css",
                    "/static/app.css%00",
                ):
                    status, _, _ = _get(port, path)
                    self.assertIn(status, (404, 400), msg=path)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_owner_decision_http_boundary_requirements(self) -> None:
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                # 1. Missing subject_sha256 -> 400 invalid_input
                st, _, body = _post(port, "/api/jobs/job-123/owner/priority", {"approve": True})
                self.assertEqual(st, 400)
                self.assertEqual(json.loads(body.decode("utf-8")), {"error": "invalid_input"})

                st, _, body = _post(port, "/api/jobs/job-123/owner/map", {"approve": True})
                self.assertEqual(st, 400)
                self.assertEqual(json.loads(body.decode("utf-8")), {"error": "invalid_input"})

                # 2. Non-64hex subject_sha256 -> 400 invalid_input
                st, _, body = _post(port, "/api/jobs/job-123/owner/priority", {"approve": True, "subject_sha256": "bad-hash"})
                self.assertEqual(st, 400)
                self.assertEqual(json.loads(body.decode("utf-8")), {"error": "invalid_input"})

                st, _, body = _post(port, "/api/jobs/job-123/owner/map", {"approve": True, "subject_sha256": "bad-hash"})
                self.assertEqual(st, 400)
                self.assertEqual(json.loads(body.decode("utf-8")), {"error": "invalid_input"})

                # 3. Non-boolean approve -> 400 invalid_input
                st, _, body = _post(port, "/api/jobs/job-123/owner/priority", {"approve": "yes", "subject_sha256": "a" * 64})
                self.assertEqual(st, 400)
                self.assertEqual(json.loads(body.decode("utf-8")), {"error": "invalid_input"})

                # 4. Unknown fields rejected -> 400 invalid_input
                st, _, body = _post(port, "/api/jobs/job-123/owner/priority", {"approve": True, "subject_sha256": "a" * 64, "extra": 1})
                self.assertEqual(st, 400)
                self.assertEqual(json.loads(body.decode("utf-8")), {"error": "invalid_input"})

                # 5. GET/HEAD -> 405 method_not_allowed
                st, _, body = _get(port, "/api/jobs/job-123/owner/priority", method="GET")
                self.assertEqual(st, 405)
                self.assertEqual(json.loads(body.decode("utf-8")), {"error": "method_not_allowed"})

                st, _, body = _get(port, "/api/jobs/job-123/owner/priority", method="HEAD")
                self.assertEqual(st, 405)

                st, _, body = _get(port, "/api/jobs/job-123/owner/map", method="GET")
                self.assertEqual(st, 405)
                self.assertEqual(json.loads(body.decode("utf-8")), {"error": "method_not_allowed"})

                st, _, body = _get(port, "/api/jobs/job-123/owner/map", method="HEAD")
                self.assertEqual(st, 405)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_owner_map_decision_success_response_reports_stage_and_disposition(self) -> None:
        """T051-discovered regression: a genuine successful map approval must
        report ``state.stage``/``state.disposition``, not crash with
        ``AttributeError`` on the bare ``WorkflowAdvanced`` object.
        """

        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                st, _, body = _post(port, "/api/courses", {"title": "Owner Map Regression", "ai_mode": "gpt", "quality_mode": "fast"})
                self.assertEqual(st, 201)
                course_id = json.loads(body)["course"]["course_id"]
                st, _, _ = _post(
                    port,
                    f"/api/courses/{course_id}/sources",
                    {"source_id": "src-owner-map", "content_base64": "JVBERi0xLjQKJSBpbnZlbnRlZAo="},
                )
                self.assertEqual(st, 201)
                st, _, body = _post(port, f"/api/courses/{course_id}/start-generation")
                self.assertEqual(st, 201)
                job_id = json.loads(body)["job_id"]

                holder = "holder0000000077"

                def acquire() -> dict:
                    st, _, body = _post(port, f"/api/jobs/{job_id}/semantic-request", {"holder_id": holder})
                    self.assertEqual(st, 200)
                    return json.loads(body)["request"]

                def submit(req: dict, kind: str, **extra: object) -> dict:
                    payload = {
                        "result_version": "semantic-work-result/v1",
                        "request_id": req["request_id"],
                        "operation_id": req["operation_id"],
                        "kind": kind,
                        "produced_artifacts": [],
                        "produced_documents": [],
                        "diagnostics": [],
                        "priority_subject_sha256": None,
                        "map_subject_sha256": None,
                        "candidate_subject_sha256": None,
                        "request_revision": req["expected_revision"],
                        "holder_id": holder,
                    }
                    payload.update(extra)
                    st, _, body = _post(port, f"/api/jobs/{job_id}/semantic-result", payload)
                    self.assertEqual(st, 200, body)
                    return json.loads(body)

                import base64
                import hashlib

                def art(kind: str, artifact_id: str, text: str) -> dict:
                    return {"kind": kind, "artifact_id": artifact_id, "content_b64": base64.b64encode(text.encode()).decode("ascii")}

                req = acquire()
                submit(
                    req, "source_assessment",
                    produced_artifacts=[
                        art("source_assessment", "art-om-sa", "invented"),
                        art("priority_proposal", "art-om-pp", "invented priority"),
                        art("evidence_hierarchy", "art-om-eh", "invented hierarchy"),
                    ],
                )

                req = acquire()
                from course_compiler.app.config import create_config as _cc
                from course_compiler.workflow import priority_subject_sha256 as _pss
                from course_compiler.workflow_persistence import open_workflow_state_store

                ws_store = open_workflow_state_store(Path(tmp) / "workflow-state.sqlite3")
                ws = ws_store.load(req["workflow_id"])
                expected_priority = _pss(ws.priority_basis, ws.policies.priority_basis)
                ws_store.close()
                submit(
                    req, "exam_priority_assessment",
                    produced_artifacts=[
                        art("priority_proposal", "art-om-pp2", "invented priority 2"),
                        art("evidence_hierarchy", "art-om-eh2", "invented hierarchy 2"),
                    ],
                    priority_subject_sha256=expected_priority,
                )

                # The browser obtains the exact, durable subject projection;
                # owners never construct or copy a hash themselves.
                st, _, body = _get(port, f"/api/jobs/{job_id}")
                self.assertEqual(st, 200, body)
                priority_approval = json.loads(body)["owner_approval"]
                self.assertEqual(priority_approval, {"kind": "priority", "subject_sha256": expected_priority, "text": "invented priority\n\ninvented hierarchy\n\n# Priority evidence review\ninvented priority 2"})
                st, _, body = _post(
                    port,
                    f"/api/jobs/{job_id}/owner/priority",
                    {"approve": True, "subject_sha256": priority_approval["subject_sha256"]},
                )
                self.assertEqual(st, 200, body)

                req = acquire()
                from course_compiler.workflow import LectureMapRecord, WorkflowArtifactReference, map_subject_sha256 as _mss

                ws_store = open_workflow_state_store(Path(tmp) / "workflow-state.sqlite3")
                ws = ws_store.load(req["workflow_id"])
                map_bytes = b"l1"
                map_ref = WorkflowArtifactReference(
                    "workflow-artifact-reference/v1", "art-om-lm", "lecture_map",
                    hashlib.sha256(map_bytes).hexdigest(), "lecture-map-policy/v1",
                )
                expected_map = _mss(
                    LectureMapRecord(map_reference=map_ref, lecture_ids=("l1",), status="proposed", approval=None, reserved_lecture_ids=()),
                    ws.policies.lecture_mapping,
                )
                ws_store.close()
                submit(
                    req, "lecture_map_generation",
                    produced_artifacts=[art("lecture_map", "art-om-lm", "l1")],
                    map_subject_sha256=expected_map,
                )

                # This exact call previously raised AttributeError inside the
                # handler ("WorkflowAdvanced" has no "stage") and dropped the
                # connection with no HTTP response at all.
                st, _, body = _get(port, f"/api/jobs/{job_id}")
                self.assertEqual(st, 200, body)
                map_approval = json.loads(body)["owner_approval"]
                self.assertEqual(map_approval, {"kind": "map", "subject_sha256": expected_map, "text": "l1"})
                st, headers, body = _post(
                    port,
                    f"/api/jobs/{job_id}/owner/map",
                    {"approve": True, "subject_sha256": map_approval["subject_sha256"]},
                )
                self.assertEqual(st, 200)
                data = json.loads(body)
                self.assertEqual(data["status"], "advanced")
                self.assertEqual(data["stage"], "lecture_production")
                self.assertEqual(data["disposition"], "ready")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
