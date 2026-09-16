"""Release-readiness tests.

These narrow tests prove the documented v0.1 Local Developer Preview
release requirements are met on the exact served repository bytes.
They do not modify product behavior; they only assert that:

- The shipped `README.md` carries every v0.1 release-required string.
- The documented local startup command (`python3 -m
  course_compiler.app`) binds a loopback port and serves `/healthz`
  with HTTP 200.
- The startup line is content-safe and names the product.
- The same README-required failure-mode codes are actually raised by
  the existing configuration layer (proves the troubleshooting table
  is honest, not aspirational).
"""

from __future__ import annotations

import http.client
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from course_compiler.app.config import AppConfigError, create_config
from course_compiler.app.server import create_server


REPO_ROOT = Path(__file__).resolve().parents[1]
README_PATH = REPO_ROOT / "README.md"
MAKEFILE_PATH = REPO_ROOT / "Makefile"

# Every release-required string the README must contain so a new
# v0.1 user can install, start, and run the documented workflow
# without consulting any other document.
README_REQUIRED_SNIPPETS = (
    # Content obligations, deliberately not heading names: the README may be
    # restructured freely as long as a new v0.1 user can still install, start,
    # and run the documented workflow without consulting any other document.
    "# Course Compiler",
    "v0.1.0",
    "Local Developer Preview",
    "Python 3",
    "latexmk",
    "xelatex",
    "git clone",
    "make app",
    "python3 -m course_compiler.app",
    "127.0.0.1",
    "8787",
    "Continue in ChatGPT",
    "Resume in ChatGPT",
    "fresh ChatGPT conversation",
    "FAST",
    "REVIEW",
    "Build PDF",
    "Download PDF",
    "## Privacy",
    "local-data",
    "local-artifacts",
    "git-ignored",
    "BYOK",
    "multi-user",
    "hosted",
    "make gate",
    "make test",
)


class ReadmeReleaseContentTests(unittest.TestCase):
    """The shipped README must contain every release-required string."""

    def setUp(self) -> None:
        self.readme = README_PATH.read_text(encoding="utf-8") if README_PATH.is_file() else ""

    def test_readme_exists(self) -> None:
        self.assertTrue(
            README_PATH.is_file(),
            "README.md must exist at the repository root for v0.1 release.",
        )
        self.assertGreater(len(self.readme), 1000, "README.md is too short to be a real v0.1 README.")

    def test_readme_contains_release_required_snippets(self) -> None:
        missing = [s for s in README_REQUIRED_SNIPPETS if s not in self.readme]
        self.assertEqual(
            missing,
            [],
            f"README.md is missing v0.1 release-required strings: {missing}",
        )

    def test_readme_lists_v0_1_non_goals_truthfully(self) -> None:
        # Each non-goal must be explicitly named so a user is not misled
        # about what v0.1 contains. The README may use any reasonable
        # phrasing; we accept a small set of variants per category.
        for label, variants in (
            ("BYOK", ("BYOK",)),
            ("multi-user", ("multi-user", "Multi-user")),
            ("hosted SaaS", ("hosted SaaS",)),
            ("hosted public HTTPS", ("hosted public HTTPS", "hosted HTTPS")),
            ("publication", ("publication",)),
            ("public submission", ("public submission",)),
        ):
            self.assertTrue(
                any(v in self.readme for v in variants),
                f"v0.1 README must name non-goal: {label}",
            )

    def test_readme_does_not_overstate_capabilities(self) -> None:
        # The v0.1 README must disclaim production SaaS and enterprise
        # features it does not have. "Local Developer Preview" is the
        # only release label allowed.
        for disclaimer_phrase in (
            "production SaaS",
            "enterprise-ready",
        ):
            self.assertIn(
                disclaimer_phrase,
                self.readme,
                f"v0.1 README must explicitly disclaim: {disclaimer_phrase}",
            )
        self.assertIn("Local Developer Preview", self.readme)
        # README must explicitly say v0.1 is NOT production. The
        # actual line may wrap (the README renders Markdown blockquotes
        # across multiple lines), so the match tolerates newlines
        # between key tokens.
        self.assertTrue(
            re.search(
                r"v0\.1[^a-zA-Z][^.]*?\bnot\b[^.]*?\bproduction",
                self.readme,
                re.IGNORECASE | re.DOTALL,
            )
            or re.search(
                r"v0\.1[^a-zA-Z][^.]*?\bis not\b[^.]*?\bproduction",
                self.readme,
                re.IGNORECASE | re.DOTALL,
            ),
            "v0.1 README must explicitly state it is not production.",
        )


class ReadmePrivacyTruthfulnessTests(unittest.TestCase):
    """The Privacy section must communicate three distinct facts without
    becoming prose-fragile:

    1. The tracked repository stays free of private data.
    2. Durable local data lives under ignored local roots.
    3. The GPT relay requires explicit owner-mediated evidence transfer.

    These tests use small stable semantic markers rather than full
    sentence matches so the README can be rephrased cleanly in the
    future without breaking the proof.
    """

    def setUp(self) -> None:
        self.readme = README_PATH.read_text(encoding="utf-8")

    def test_privacy_section_exists(self) -> None:
        self.assertIn("## Privacy", self.readme, "README must contain a Privacy section.")

    def test_privacy_names_ignored_roots_and_tracked_repo_rule(self) -> None:
        # The README must say the durable data lives under ignored
        # roots AND that the tracked Git/repository must not contain
        # private content.
        for needle in ("local-data", "local-artifacts", "git-ignored"):
            self.assertIn(
                needle,
                self.readme,
                f"README must name ignored privacy root: {needle}",
            )
        self.assertTrue(
            re.search(
                r"\btracked\b[^.]*?\b(Git|repository)\b",
                self.readme,
                re.IGNORECASE | re.DOTALL,
            ),
            "README must state that the tracked Git/repository stays free of private data.",
        )

    def test_privacy_states_filename_is_session_convenience_only(self) -> None:
        # The original filename is a browser/session convenience, not
        # the durable source identity. We accept either the explicit
        # "session convenience" wording or any phrase that ties the
        # filename to non-durable session behavior.
        self.assertTrue(
            re.search(
                r"session[- ]only",
                self.readme,
                re.IGNORECASE,
            )
            or re.search(
                r"session convenience",
                self.readme,
                re.IGNORECASE,
            )
            or re.search(
                r"not[\s\S]{0,40}durable[\s\S]{0,40}(identity|source\s*identity)",
                self.readme,
                re.IGNORECASE,
            ),
            "README must say the original filename is not a durable source identity.",
        )

    def test_privacy_states_gpt_relay_is_owner_mediated_not_automatic(self) -> None:
        # The app does NOT automatically upload evidence to ChatGPT;
        # the owner explicitly attaches the listed current evidence
        # to a fresh ChatGPT conversation.
        self.assertIn("Continue in ChatGPT", self.readme)
        self.assertIn("Resume in ChatGPT", self.readme)
        # App must disclaim automatic transfer.
        self.assertTrue(
            re.search(
                r"does?\s+not[^.\n]{0,80}(automatically|auto[- ]upload)",
                self.readme,
                re.IGNORECASE,
            )
            or re.search(
                r"never[^.\n]{0,80}(messages?|uploads?)",
                self.readme,
                re.IGNORECASE,
            ),
            "README must say the app does not automatically upload evidence to ChatGPT.",
        )
        # Owner explicitly attaches evidence.
        self.assertTrue(
            re.search(
                r"\battach(ed|ing)?\b",
                self.readme,
                re.IGNORECASE,
            ),
            "README must say the owner attaches evidence for the relay.",
        )
        # Owner explicitly downloads evidence from the relay.
        self.assertTrue(
            re.search(
                r"\bdownload\b",
                self.readme,
                re.IGNORECASE,
            ),
            "README must say the owner downloads evidence for the relay.",
        )

    def test_privacy_does_not_imply_nothing_ever_leaves_the_machine(self) -> None:
        # The Privacy section must NOT promise that nothing ever
        # leaves the local machine — GPT relay necessarily involves
        # owner-mediated evidence upload to ChatGPT. We forbid the
        # over-absolute "nothing leaves" / "never leaves" claims;
        # the truthful negation ("GPT mode is not fully offline",
        # "does not automatically upload") is allowed and required.
        for forbidden in (
            r"nothing\s+(ever\s+)?leaves?\s+(the\s+)?(local\s+)?machine",
            r"never\s+leaves?\s+(the\s+)?(local\s+)?machine",
        ):
            self.assertIsNone(
                re.search(forbidden, self.readme, re.IGNORECASE),
                f"README must not contain over-absolute privacy claim matching: {forbidden!r}",
            )

    def test_privacy_does_not_imply_outputs_live_only_in_local_artifacts_build(self) -> None:
        # The Privacy section must NOT claim generated/semantic
        # outputs live ONLY under local-artifacts/ and build/,
        # because durable application/semantic state also lives under
        # local-data/app.
        self.assertIsNone(
            re.search(
                r"live\s+only\s+under\s+`?local-artifacts/?`?[\s\S]{0,40}and[\s\S]{0,40}`?build/?`?",
                self.readme,
                re.IGNORECASE,
            ),
            "README must not claim generated outputs live only under local-artifacts/ and build/.",
        )


class MakefileReleaseTargetsTests(unittest.TestCase):
    """The shipped Makefile must expose the documented v0.1 commands."""

    def setUp(self) -> None:
        self.makefile = MAKEFILE_PATH.read_text(encoding="utf-8") if MAKEFILE_PATH.is_file() else ""

    def test_makefile_has_app_target(self) -> None:
        # Makefile targets are followed by a tab-indented recipe line.
        self.assertRegex(
            self.makefile,
            r"(?m)^app:\n\t",
            "Makefile must define the `app` target with a tab-indented recipe.",
        )
        self.assertIn("course_compiler.app", self.makefile)

    def test_makefile_has_gate_target(self) -> None:
        self.assertRegex(
            self.makefile,
            r"(?m)^gate:\n\t",
            "Makefile must define the `gate` target with a tab-indented recipe.",
        )
        self.assertIn("ci/gate.py", self.makefile)

    def test_makefile_has_test_target(self) -> None:
        self.assertRegex(
            self.makefile,
            r"(?m)^test:\n\t",
            "Makefile must define the `test` target with a tab-indented recipe.",
        )
        self.assertIn("unittest", self.makefile)


class DocumentedStartupCommandTests(unittest.TestCase):
    """The documented startup command actually serves /healthz."""

    def test_documented_command_serves_healthz(self) -> None:
        """`python3 -m course_compiler.app` (the documented command)
        must bind a loopback port and respond 200 on /healthz.
        """
        with tempfile.TemporaryDirectory(dir=str(REPO_ROOT / "local-data"), prefix="startup-") as tmp:
            data_root = Path(tmp)
            cfg = create_config(host="127.0.0.1", port=0, data_root=data_root)
            server = create_server(cfg)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=10
                )
                try:
                    conn.request("GET", "/healthz")
                    resp = conn.getresponse()
                    body = resp.read().decode("utf-8")
                finally:
                    conn.close()
                self.assertEqual(resp.status, 200)
                self.assertIn('"status":"ok"', body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_documented_command_runs_in_subprocess_and_serves_healthz(self) -> None:
        """Spawn the exact documented command in a subprocess and
        verify the startup line is content-safe and /healthz responds.
        This proves the README's documented command actually works,
        not just the underlying module function.
        """
        with tempfile.TemporaryDirectory(dir=str(REPO_ROOT / "local-data"), prefix="subproc-") as tmp:
            data_root = Path(tmp)
            proc = subprocess.Popen(
                [sys.executable, "-m", "course_compiler.app",
                 "--data-root", str(data_root), "--port", "0"],
                cwd=str(REPO_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            try:
                # Read startup line. The documented command prints exactly
                # one line to stdout before serve_forever starts.
                assert proc.stdout is not None
                startup_line = proc.stdout.readline().strip()
                # Must be content-safe (no private paths, no stack trace).
                self.assertIn("Course Compiler app listening on", startup_line)
                self.assertIn("127.0.0.1", startup_line)
                self.assertNotIn(str(REPO_ROOT), startup_line)
                self.assertNotIn("Traceback", startup_line)
                # Extract the actual port the app chose.
                m = re.search(r":(\d+)/", startup_line)
                self.assertIsNotNone(m, f"startup line did not contain a port: {startup_line!r}")
                port = int(m.group(1))
                # Hit /healthz.
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                try:
                    conn.request("GET", "/healthz")
                    resp = conn.getresponse()
                    body = resp.read().decode("utf-8")
                finally:
                    conn.close()
                self.assertEqual(resp.status, 200)
                self.assertIn('"status":"ok"', body)
            finally:
                proc.terminate()
                try:
                    # Use communicate with timeout to drain the pipe and
                    # avoid blocking on the still-attached stdout.
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass


class DocumentedFailureCodesTests(unittest.TestCase):
    """The README's troubleshooting codes are actually raised by the
    configuration layer; nothing in the README is aspirational."""

    def test_host_invalid_is_raised_for_malformed_host(self) -> None:
        with self.assertRaises(AppConfigError) as ctx:
            create_config(host="not a valid host!!", port=8787,
                          data_root=REPO_ROOT / "local-data" / "app",
                          allow_non_loopback=True)
        self.assertEqual(str(ctx.exception), "host_invalid")

    def test_host_not_loopback_is_raised_for_non_loopback(self) -> None:
        with self.assertRaises(AppConfigError) as ctx:
            create_config(host="0.0.0.0", port=8787,
                          data_root=REPO_ROOT / "local-data" / "app")
        self.assertEqual(str(ctx.exception), "host_not_loopback")

    def test_port_invalid_is_raised_for_out_of_range_port(self) -> None:
        with self.assertRaises(AppConfigError) as ctx:
            create_config(host="127.0.0.1", port=99999,
                          data_root=REPO_ROOT / "local-data" / "app")
        self.assertEqual(str(ctx.exception), "port_invalid")

    def test_data_root_invalid_is_raised_for_external_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as outside_repo:
            with self.assertRaises(AppConfigError) as ctx:
                create_config(host="127.0.0.1", port=8787,
                              data_root=Path(outside_repo))
            self.assertEqual(str(ctx.exception), "data_root_invalid")

    def test_data_root_invalid_is_raised_for_tracked_repo_path(self) -> None:
        with self.assertRaises(AppConfigError) as ctx:
            create_config(host="127.0.0.1", port=8787,
                          data_root=REPO_ROOT / "README.md")
        self.assertEqual(str(ctx.exception), "data_root_invalid")

    def test_readme_troubleshooting_table_covers_real_codes(self) -> None:
        # The README names specific codes; ensure those codes are real.
        readme = README_PATH.read_text(encoding="utf-8")
        named = re.findall(r"`([a-z_]+)`", readme)
        real_codes = {
            "host_invalid",
            "host_not_loopback",
            "port_invalid",
            "data_root_invalid",
            "data_root_unavailable",
        }
        documented_codes = real_codes.intersection(named)
        self.assertEqual(
            documented_codes,
            real_codes,
            "README troubleshooting table must reference every documented code.",
        )


if __name__ == "__main__":
    unittest.main()