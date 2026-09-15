"""Bounded relay activation / plugin packaging regressions.

A bounded relay turn returns exactly one ``SemanticWorkResult``; it never
orchestrates the legacy multi-tool surface. The prepared relay package must
close over every reference the production ``SKILL.md`` tells the model to
read, must be byte-identical to the authoritative repository, and must never
carry the legacy registered-app binding or any private/ignored repository
state.

These tests stage the real relay package from the exact current repository
bytes and pin the properties a live relay activation depends on. Invented
synthetic bytes only; no real course content, no ChatGPT, no network egress.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL_ROOT = ROOT / "skills" / "course-compiler"
SKILL_PATH = SKILL_ROOT / "SKILL.md"
MANIFEST_PATH = ROOT / ".codex-plugin" / "plugin.json"
APP_PATH = ROOT / ".app.json"
MARKETPLACE_PATH = ROOT / ".agents" / "plugins" / "marketplace.json"
PREPARER_PATH = ROOT / "scripts" / "prepare_course_compiler_plugin.py"

# The pre-relay multi-tool MCP surface. A bounded relay turn must never be
# directed to any of these.
LEGACY_MCP_TOOLS = (
    "ingest_course_source",
    "ingest_workflow_artifact",
    "apply_course_workflow_request",
    "get_course_workflow",
    "preview_source_pdf_page",
    "extract_source_pdf_region",
    "build_course_pdf",
)


def _load_preparer() -> object:
    spec = importlib.util.spec_from_file_location("relay_plugin_preparer", PREPARER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("plugin preparer could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class T051PluginRelayPackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.preparer = _load_preparer()
        cls.skill_text = SKILL_PATH.read_text(encoding="utf-8")

    def _stage(self) -> tuple[tempfile.TemporaryDirectory, Path]:
        """Stage the real package from the exact reviewed repository bytes."""

        temporary = tempfile.TemporaryDirectory(prefix="relay-package-")
        destination = Path(temporary.name) / "course-compiler"
        self.preparer.prepare_plugin_package(ROOT, destination)
        return temporary, destination

    # -- P1 ---------------------------------------------------------------
    def test_p1_package_closes_over_every_reference_the_skill_tells_the_model_to_read(self) -> None:
        """Every `references/<name>.md` named by the production SKILL.md is
        packaged. A live failure once followed a Skill instruction whose file
        the package omitted."""

        referenced = set(re.findall(r"references/([A-Za-z0-9._-]+\.md)", self.skill_text))
        self.assertIn("coarse-workflow.md", referenced, "SKILL.md must still teach the relay contract")

        packaged = {
            path.name
            for path in self.preparer.RELAY_PACKAGE_PATHS
            if path.parent.as_posix().endswith("references")
        }
        missing = referenced - packaged
        self.assertEqual(missing, set(), f"SKILL.md references files the package omits: {sorted(missing)}")

        # And every packaged reference genuinely exists in the repository.
        for relative_path in self.preparer.RELAY_PACKAGE_PATHS:
            self.assertTrue((ROOT / relative_path).is_file(), relative_path.as_posix())

    # -- P2 ---------------------------------------------------------------
    def test_p2_staged_skill_and_references_are_byte_identical_to_the_repository(self) -> None:
        temporary, destination = self._stage()
        try:
            staged_skill = destination / "skills/course-compiler/SKILL.md"
            self.assertEqual(staged_skill.read_bytes(), SKILL_PATH.read_bytes())
            for relative_path in self.preparer.RELAY_PACKAGE_PATHS:
                self.assertEqual(
                    (destination / relative_path).read_bytes(),
                    (ROOT / relative_path).read_bytes(),
                    relative_path.as_posix(),
                )
            self.assertEqual(self.preparer.verify_prepared_package(ROOT, destination), ())
        finally:
            temporary.cleanup()

    def test_p2_verification_detects_a_drifted_package(self) -> None:
        temporary, destination = self._stage()
        try:
            staged_skill = destination / "skills/course-compiler/SKILL.md"
            staged_skill.write_text("drifted\n", encoding="utf-8")
            failures = self.preparer.verify_prepared_package(ROOT, destination)
            self.assertIn(
                "package_file_not_byte_identical:skills/course-compiler/SKILL.md",
                failures,
            )
        finally:
            temporary.cleanup()

    # -- P3 ---------------------------------------------------------------
    def test_p3_no_private_or_repository_state_enters_the_package(self) -> None:
        temporary, destination = self._stage()
        try:
            staged = sorted(
                path.relative_to(destination).as_posix()
                for path in destination.rglob("*")
                if path.is_file()
            )
            self.assertEqual(
                staged,
                sorted(path.as_posix() for path in self.preparer.PACKAGE_PATHS),
            )
            for forbidden in ("local-data", "local-artifacts", "build", ".git", "course_compiler", "tests", "docs"):
                self.assertFalse((destination / forbidden).exists(), forbidden)
            for path in destination.rglob("*"):
                if path.is_file():
                    self.assertNotIn(path.suffix, (".pdf", ".sqlite3", ".log"), path.name)
                self.assertFalse(path.is_symlink(), path.as_posix())
        finally:
            temporary.cleanup()

    # -- P4 ---------------------------------------------------------------
    def test_p4_relay_activation_carries_no_legacy_tool_dependency(self) -> None:
        """The prepared relay package cannot attach the old app/tool surface:
        it stages no `.app.json` and its manifest declares no `apps` key, so
        the legacy MCP tools cannot be selected for a relay handoff."""

        temporary, destination = self._stage()
        try:
            self.assertFalse((destination / ".app.json").exists())
            manifest = json.loads((destination / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
            self.assertNotIn("apps", manifest)
            self.assertEqual(manifest["skills"], "./skills/")

            # Packaged agent metadata declares no MCP tool dependency.
            agent_text = (destination / "skills/course-compiler/agents/openai.yaml").read_text(encoding="utf-8")
            self.assertNotIn("dependencies:", agent_text)
            self.assertNotIn("mcp", agent_text.lower())

            # The Skill the model actually reads never directs a relay turn
            # at a legacy tool.
            for tool in LEGACY_MCP_TOOLS:
                self.assertNotIn(tool, self.skill_text, f"production Skill body must not direct {tool}")
            self.assertNotIn("record_lecture_map", self.skill_text)
        finally:
            temporary.cleanup()

    def test_p4_repository_retains_the_separate_legacy_development_ingress(self) -> None:
        """Separation, not erasure: the repository keeps the legacy binding
        so the optional local development ingress still exists."""

        self.assertTrue(APP_PATH.is_file(), "legacy registered-app binding must be preserved in-repo")
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(manifest["apps"], "./.app.json")
        self.assertNotIn("apps", self.preparer.relay_manifest(ROOT))

    # -- P6 ---------------------------------------------------------------
    def test_p6_marketplace_resolves_to_the_prepared_package_path(self) -> None:
        marketplace = json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8"))
        entry = marketplace["plugins"][0]
        resolved = (ROOT / entry["source"]["path"]).resolve()

        # The configured path is task-neutral and inside the ignored root.
        self.assertEqual(resolved, (ROOT / "local-artifacts/plugin-package/course-compiler").resolve())
        self.assertIn("local-artifacts", entry["source"]["path"])

        # Preparing to exactly that path yields a package that verifies.
        with tempfile.TemporaryDirectory(prefix="relay-marketplace-smoke-") as tmp:
            staged_root = Path(tmp) / "local-artifacts/plugin-package/course-compiler"
            self.preparer.prepare_plugin_package(ROOT, staged_root)
            self.assertEqual(self.preparer.verify_prepared_package(ROOT, staged_root), ())
            self.assertTrue((staged_root / "skills/course-compiler/SKILL.md").is_file())
            self.assertEqual(
                staged_root.relative_to(Path(tmp)).as_posix(),
                entry["source"]["path"].removeprefix("./"),
            )

    # -- P7 ---------------------------------------------------------------
    def test_p7_production_relay_skill_expects_no_tunnel_or_local_connector(self) -> None:
        """The production relay Skill must never require a tunnel or local
        connector, and must forbid localhost access from ChatGPT cloud."""

        body = self.skill_text
        lowered = body.lower()
        for forbidden in ("tunnel", "connector", "ngrok", "cloudflared"):
            self.assertNotIn(forbidden, lowered, forbidden)

        # Localhost is named only to prohibit it.
        self.assertIn("must not attempt localhost", lowered)
        for match in re.finditer(r"127\.0\.0\.1", body):
            window = body[max(0, match.start() - 200) : match.end() + 80].lower()
            self.assertIn("must not", window, "127.0.0.1 must appear only in a prohibition")

        # The same property holds for the packaged bytes the model reads.
        temporary, destination = self._stage()
        try:
            staged = (destination / "skills/course-compiler/SKILL.md").read_text(encoding="utf-8")
            self.assertNotIn("tunnel", staged.lower())
            relay_reference = (
                destination / "skills/course-compiler/references/coarse-workflow.md"
            ).read_text(encoding="utf-8")
            self.assertNotIn("tunnel", relay_reference.lower())
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
