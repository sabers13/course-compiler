from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / ".codex-plugin" / "plugin.json"
APP_PATH = ROOT / ".app.json"
MARKETPLACE_PATH = ROOT / ".agents" / "plugins" / "marketplace.json"
AGENT_PATH = ROOT / "skills" / "course-compiler" / "agents" / "openai.yaml"
PREPARER_PATH = ROOT / "scripts" / "prepare_course_compiler_plugin.py"


def _load_preparer() -> object:
    spec = importlib.util.spec_from_file_location("t032_plugin_preparer", PREPARER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("plugin preparer could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _quoted_yaml_value(text: str, field: str, *, indent: int) -> str:
    match = re.search(
        rf"^{re.escape(' ' * indent + field)}: \"([^\"]*)\"$",
        text,
        re.MULTILINE,
    )
    if match is None:
        raise AssertionError(f"missing quoted YAML field: {field}")
    return match.group(1)


class CourseCompilerPluginMetadataTests(unittest.TestCase):
    def test_manifest_has_current_required_package_fields(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "course-compiler")
        self.assertRegex(manifest["version"], r"^\d+\.\d+\.\d+(?:[-+].+)?$")
        self.assertTrue(manifest["description"])
        self.assertTrue(manifest["author"]["name"])
        self.assertEqual(manifest["skills"], "./skills/")
        interface = manifest["interface"]
        for field in (
            "displayName",
            "shortDescription",
            "longDescription",
            "developerName",
            "category",
            "capabilities",
            "defaultPrompt",
        ):
            self.assertTrue(interface[field])
        self.assertLessEqual(len(interface["defaultPrompt"]), 3)
        self.assertTrue(all(len(item) <= 128 for item in interface["defaultPrompt"]))

    def test_skill_agent_metadata_and_t032_registered_app_binding_are_exact(self) -> None:
        text = AGENT_PATH.read_text(encoding="utf-8")
        self.assertEqual(
            _quoted_yaml_value(text, "display_name", indent=2),
            "Course Compiler",
        )
        short_description = _quoted_yaml_value(text, "short_description", indent=2)
        self.assertGreaterEqual(len(short_description), 25)
        self.assertLessEqual(len(short_description), 64)
        self.assertIn(
            "$course-compiler",
            _quoted_yaml_value(text, "default_prompt", indent=2),
        )

        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        if not APP_PATH.exists():
            self.assertNotIn("apps", manifest)
            self.assertNotIn("dependencies:", text)
            return

        self.assertEqual(manifest["apps"], "./.app.json")
        app_payload = json.loads(APP_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            app_payload,
            {
                "apps": {
                    "dev-6a803cf576248191afbf2cc7f2276197": {
                        "id": "asdk_app_6a803cf576248191afbf2cc7f2276197",
                        "category": "Other",
                    }
                }
            },
        )
        self.assertNotIn("dependencies:", text)

    def test_repo_marketplace_points_only_to_ignored_prepared_package(self) -> None:
        marketplace = json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(marketplace["name"], "course-compiler-local")
        self.assertEqual(marketplace["interface"]["displayName"], "Course Compiler Local")
        self.assertEqual(len(marketplace["plugins"]), 1)
        entry = marketplace["plugins"][0]
        self.assertEqual(entry["name"], "course-compiler")
        self.assertEqual(entry["source"]["source"], "local")
        resolved = (ROOT / entry["source"]["path"]).resolve()
        self.assertEqual(
            resolved,
            (ROOT / "local-artifacts/plugin-package/course-compiler").resolve(),
        )
        self.assertEqual(
            entry["policy"],
            {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        )
        self.assertEqual(entry["category"], "Education")


class CourseCompilerPluginPreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.preparer = _load_preparer()

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="course-compiler-plugin-package-")
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _synthetic_source(self) -> Path:
        source = self.root / "source"
        files = {
            ".codex-plugin/plugin.json": json.dumps(
                {
                    "name": "course-compiler",
                    "version": "0.1.0",
                    "description": "Synthetic package",
                    "author": {"name": "Synthetic Test"},
                    "skills": "./skills/",
                    "apps": "./.app.json",
                    "interface": {
                        "displayName": "Course Compiler",
                        "shortDescription": "Synthetic package contract fixture",
                        "longDescription": "Synthetic package contract fixture.",
                        "developerName": "Synthetic Test",
                        "category": "Education",
                        "capabilities": ["Read"],
                        "defaultPrompt": ["Use the synthetic fixture."],
                    },
                }
            ),
            ".app.json": json.dumps(
                {
                    "apps": {
                        "course-compiler": {
                            "id": "synthetic-test-connection-id",
                            "category": "Education",
                        }
                    }
                }
            ),
            "skills/course-compiler/SKILL.md": (
                "---\nname: course-compiler\n"
                "description: Synthetic Course Compiler fixture.\n---\n"
            ),
            "skills/course-compiler/agents/openai.yaml": (
                'interface:\n  display_name: "Course Compiler"\n'
                '  short_description: "Synthetic package contract fixture"\n'
                'dependencies:\n  tools:\n    - type: "mcp"\n'
                '      value: "course-compiler"\n'
                '      description: "Synthetic MCP dependency"\n'
                '      transport: "streamable_http"\n'
                '      url: "https://example.test/mcp"\n'
            ),
            "skills/course-compiler/prompts/course-authoring-v2.md": "synthetic authoring contract\n",
            "skills/course-compiler/references/coarse-workflow.md": "synthetic coarse\n",
            "skills/course-compiler/references/fine-grained-legacy.md": "synthetic legacy\n",
            "skills/course-compiler/references/lecture-authoring.md": "synthetic\n",
            "skills/course-compiler/references/workflow-recovery.md": "synthetic\n",
            "skills/course-compiler/scripts/find_source_text_offset.py": "# synthetic\n",
        }
        for relative_path, contents in files.items():
            path = source / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")
        private_marker = source / "local-data/private/source.pdf"
        private_marker.parent.mkdir(parents=True, exist_ok=True)
        private_marker.write_bytes(b"synthetic private marker")
        return source

    def test_preparer_copies_only_the_exact_public_package_allowlist(self) -> None:
        source = self._synthetic_source()
        destination = self.root / "prepared" / "course-compiler"
        prepared = self.preparer.prepare_plugin_package(source, destination)
        self.assertEqual(prepared.destination, destination)
        copied = {
            path.relative_to(destination).as_posix()
            for path in destination.rglob("*")
            if path.is_file()
        }
        self.assertEqual(
            copied,
            {path.as_posix() for path in self.preparer.PACKAGE_PATHS},
        )
        self.assertFalse((destination / "local-data").exists())
        for relative_path in self.preparer.RELAY_PACKAGE_PATHS:
            self.assertEqual(
                (destination / relative_path).read_bytes(),
                (source / relative_path).read_bytes(),
            )
        # The manifest is derived, not copied: it drops the legacy app key.
        self.assertEqual(
            json.loads((destination / self.preparer.MANIFEST_PATH).read_text(encoding="utf-8")),
            self.preparer.relay_manifest(source),
        )
        self.assertEqual(self.preparer.verify_prepared_package(source, destination), ())

    def test_preparer_packages_the_relay_without_the_legacy_app_binding(self) -> None:
        """The pre-T051 registered app is never staged, present or not.

        A live GPT E2E showed that an attached legacy app leads the model
        into the old seven-tool MCP workflow instead of one bounded relay
        turn, so the relay package must not carry it.
        """

        source = self._synthetic_source()
        destination = self.root / "prepared" / "course-compiler"
        self.preparer.prepare_plugin_package(source, destination)
        self.assertTrue((source / ".app.json").is_file())
        self.assertFalse((destination / ".app.json").exists())
        manifest = json.loads((destination / self.preparer.MANIFEST_PATH).read_text(encoding="utf-8"))
        self.assertNotIn("apps", manifest)

        # And packaging does not depend on the legacy app existing at all.
        (source / ".app.json").unlink()
        second = self.root / "prepared-without-app" / "course-compiler"
        self.preparer.prepare_plugin_package(source, second)
        self.assertFalse((second / ".app.json").exists())
        self.assertEqual(self.preparer.verify_prepared_package(source, second), ())

    def test_preparer_refuses_to_replace_an_existing_destination(self) -> None:
        source = self._synthetic_source()
        destination = self.root / "prepared"
        destination.mkdir()
        with self.assertRaisesRegex(
            self.preparer.PluginPackageError,
            "^destination_already_exists$",
        ):
            self.preparer.prepare_plugin_package(source, destination)


if __name__ == "__main__":
    unittest.main()
