"""Synthetic regression tests for logical-module manifest validation."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from ci.module_manifest import (
    ModuleManifestError,
    format_validation_result,
    validate_manifest,
)


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


class TestModuleManifest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.root = self.base / "repo"
        for directory in (
            "architecture",
            "course_compiler",
            "docs",
            "tests",
        ):
            (self.root / directory).mkdir(parents=True, exist_ok=True)

        self._write("course_compiler/domain.py")
        self._write("docs/persistence-owned.txt")
        self._write("docs/domain.md")
        self._write("docs/persistence.md")
        self._write("tests/test_domain.py")
        self._write("tests/test_persistence.py")
        self.manifest = {
            "schema_version": 1,
            "modules": [
                {
                    "id": "domain",
                    "purpose": "Synthetic domain.",
                    "owned_paths": ["course_compiler/domain.py"],
                    "allowed_dependencies": [],
                    "public_surfaces": ["course_compiler/domain.py"],
                    "test_paths": ["tests/test_domain.py"],
                    "context_docs": ["docs/domain.md"],
                    "full_gate_triggers": ["course_compiler/domain.py"],
                },
                {
                    "id": "persistence",
                    "purpose": "Synthetic persistence.",
                    "owned_paths": ["docs/persistence-owned.txt"],
                    "allowed_dependencies": ["domain"],
                    "public_surfaces": ["docs/persistence-owned.txt"],
                    "test_paths": ["tests/test_persistence.py"],
                    "context_docs": ["docs/persistence.md"],
                    "full_gate_triggers": ["docs/persistence-owned.txt"],
                },
            ],
        }
        self.manifest_path = self.root / "architecture" / "modules.yaml"
        self._save()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write(self, relative: str, content: str = "synthetic\n") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def _save(self) -> None:
        self.manifest_path.write_text(
            json.dumps(self.manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _validate(self):
        self._save()
        return validate_manifest(self.manifest_path, root=self.root)

    def test_valid_synthetic_manifest(self) -> None:
        result = self._validate()
        self.assertEqual(result.module_ids, ("domain", "persistence"))
        self.assertEqual(
            result.production_ownership,
            (("course_compiler/domain.py", "domain"),),
        )

    def test_duplicate_module_ids_rejected(self) -> None:
        self.manifest["modules"].append(copy.deepcopy(self.manifest["modules"][0]))
        with self.assertRaisesRegex(ModuleManifestError, "duplicate module id"):
            self._validate()

    def test_unknown_dependency_ids_rejected(self) -> None:
        self.manifest["modules"][1]["allowed_dependencies"] = ["unknown"]
        with self.assertRaisesRegex(ModuleManifestError, "unknown allowed_dependencies"):
            self._validate()

    def test_self_dependency_rejected(self) -> None:
        self.manifest["modules"][0]["allowed_dependencies"] = ["domain"]
        with self.assertRaisesRegex(ModuleManifestError, "self dependency"):
            self._validate()

    def test_dependency_cycle_rejected(self) -> None:
        self.manifest["modules"][0]["allowed_dependencies"] = ["persistence"]
        with self.assertRaisesRegex(ModuleManifestError, "contains a cycle"):
            self._validate()

    def test_unowned_production_file_rejected(self) -> None:
        self._write("course_compiler/unowned.py")
        with self.assertRaisesRegex(ModuleManifestError, "unowned production Python files"):
            self._validate()

    def test_multiply_owned_production_file_rejected(self) -> None:
        self.manifest["modules"][1]["owned_paths"].append(
            "course_compiler/domain.py"
        )
        with self.assertRaisesRegex(ModuleManifestError, "assigned to both"):
            self._validate()

    def test_owned_symlink_alias_does_not_own_production_path(self) -> None:
        alias = self.root / "docs" / "domain-alias.py"
        alias.symlink_to(self.root / "course_compiler" / "domain.py")
        self.manifest["modules"][0]["owned_paths"] = ["docs/domain-alias.py"]
        with self.assertRaisesRegex(ModuleManifestError, "unowned production Python files"):
            self._validate()

    def test_unowned_production_symlink_is_not_collapsed_into_target(self) -> None:
        alias = self.root / "course_compiler" / "unowned.py"
        alias.symlink_to(self.root / "course_compiler" / "domain.py")
        with self.assertRaisesRegex(ModuleManifestError, "unowned production Python files"):
            self._validate()

    def test_nonexistent_concrete_path_rejected(self) -> None:
        self.manifest["modules"][0]["context_docs"] = ["docs/missing.md"]
        with self.assertRaisesRegex(ModuleManifestError, "not an existing file"):
            self._validate()

    def test_traversal_outside_repository_rejected(self) -> None:
        (self.base / "outside.md").write_text("outside\n", encoding="utf-8")
        self.manifest["modules"][0]["context_docs"] = ["../outside.md"]
        with self.assertRaisesRegex(ModuleManifestError, "not normalized"):
            self._validate()

    def test_absolute_external_path_rejected(self) -> None:
        outside = self.base / "outside.md"
        outside.write_text("outside\n", encoding="utf-8")
        self.manifest["modules"][0]["context_docs"] = [str(outside)]
        with self.assertRaisesRegex(ModuleManifestError, "repository-relative"):
            self._validate()

    def test_external_manifest_path_rejected(self) -> None:
        outside = self.base / "outside-modules.yaml"
        outside.write_text(json.dumps(self.manifest), encoding="utf-8")
        with self.assertRaisesRegex(ModuleManifestError, "inside repository"):
            validate_manifest(outside, root=self.root)

    def test_manifest_symlink_to_external_or_forbidden_path_rejected(self) -> None:
        outside = self.base / "outside-modules.yaml"
        outside.write_text(json.dumps(self.manifest), encoding="utf-8")
        private = self._write("local-data/modules.yaml", json.dumps(self.manifest))
        artifacts = self._write("local-artifacts/modules.yaml", json.dumps(self.manifest))
        root_build = self._write("build/modules.yaml", json.dumps(self.manifest))
        nested_build = self._write("nested/build/modules.yaml", json.dumps(self.manifest))
        cases = (
            ("architecture/external.yaml", outside, "inside repository"),
            ("architecture/private.yaml", private, "forbidden location"),
            ("architecture/artifacts.yaml", artifacts, "forbidden location"),
            ("architecture/root-build.yaml", root_build, "forbidden location"),
            ("architecture/nested-build.yaml", nested_build, "forbidden location"),
        )
        for relative, target, message in cases:
            with self.subTest(relative=relative):
                link = self.root / relative
                link.symlink_to(target)
                with self.assertRaisesRegex(ModuleManifestError, message):
                    validate_manifest(link, root=self.root)

    def test_malformed_encoding_has_deterministic_error(self) -> None:
        self.manifest_path.write_bytes(b"\xff\xfe")
        with self.assertRaisesRegex(ModuleManifestError, "unable to read manifest"):
            validate_manifest(self.manifest_path, root=self.root)

    def test_unresolvable_path_has_deterministic_error(self) -> None:
        loop = self.root / "docs" / "loop.md"
        loop.symlink_to(loop)
        self.manifest["modules"][0]["context_docs"] = ["docs/loop.md"]
        with self.assertRaisesRegex(ModuleManifestError, "cannot be resolved"):
            self._validate()

    def test_private_and_build_components_rejected(self) -> None:
        baseline = copy.deepcopy(self.manifest)
        rejected = (
            "local-data/private.md",
            "local-artifacts/generated.md",
            "build/output.md",
            "nested/build/output.md",
        )
        for relative in rejected:
            with self.subTest(relative=relative):
                self.manifest = copy.deepcopy(baseline)
                self._write(relative)
                self.manifest["modules"][0]["context_docs"] = [relative]
                with self.assertRaisesRegex(ModuleManifestError, "forbidden location"):
                    self._validate()

    def test_build_substring_without_build_component_is_valid(self) -> None:
        self._write("docs/building-boundaries.md")
        self.manifest["modules"][0]["context_docs"] = [
            "docs/building-boundaries.md"
        ]
        self._validate()

    def test_repeated_validation_and_output_are_deterministic(self) -> None:
        first = self._validate()
        second = validate_manifest(self.manifest_path, root=self.root)
        self.assertEqual(first, second)
        self.assertEqual(
            format_validation_result(first),
            format_validation_result(second),
        )

    def test_nested_package_rglob_discovery(self) -> None:
        # Verify that nested course_compiler/app/*.py files are discovered via rglob and require ownership
        self._write("course_compiler/app/__init__.py")
        self._write("course_compiler/app/config.py")
        with self.assertRaisesRegex(ModuleManifestError, "unowned production Python files"):
            self._validate()
        # Now own them
        self.manifest["modules"].append(
            {
                "id": "app",
                "purpose": "nested app",
                "owned_paths": ["course_compiler/app/__init__.py", "course_compiler/app/config.py"],
                "allowed_dependencies": ["domain"],
                "public_surfaces": ["course_compiler/app/config.py"],
                "test_paths": ["tests/test_domain.py"],
                "context_docs": ["docs/domain.md"],
                "full_gate_triggers": ["course_compiler/app/config.py"],
            }
        )
        result = self._validate()
        self.assertIn(("course_compiler/app/config.py", "app"), result.production_ownership)

    def test_pycache_is_excluded(self) -> None:
        pycache = self.root / "course_compiler/__pycache__/cached.cpython-312.pyc"
        pycache.parent.mkdir(parents=True, exist_ok=True)
        pycache.write_bytes(b"\x00")
        # Should not be considered unowned
        result = self._validate()
        self.assertNotIn("course_compiler/__pycache__/cached.cpython-312.pyc", [p for p, _ in result.production_ownership])


if __name__ == "__main__":
    unittest.main()
