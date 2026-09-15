"""Synthetic regression tests for AST repository / symbol map."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ci.module_manifest import validate_manifest
from ci.repo_map import RepoMapError, build_repo_map, repo_map_to_dict

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _make_synthetic_repo(base: Path, files: dict[str, str], extra_manifest: dict | None = None) -> Path:
    # base is temp directory; create repo under base/repo
    root = base / "repo"
    (root / "architecture").mkdir(parents=True, exist_ok=True)
    (root / "course_compiler").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    # write minimal docs/tests placeholders for manifest validation if needed
    _write(root / "docs" / "placeholder.md", "placeholder\n")
    _write(root / "tests" / "placeholder.py", "placeholder\n")

    for rel, content in files.items():
        _write(root / rel, content)

    # Build manifest; if extra_manifest provided, use it, else infer from files
    if extra_manifest is not None:
        manifest = extra_manifest
    else:
        # Infer ownership: all course_compiler/*.py owned by domain, unless init owned by package_api pattern
        owned = sorted([p for p in files.keys() if p.startswith("course_compiler/")])
        manifest = {
            "schema_version": 1,
            "modules": [
                {
                    "id": "domain",
                    "purpose": "synthetic domain",
                    "owned_paths": owned,
                    "allowed_dependencies": [],
                    "public_surfaces": owned,
                    "test_paths": ["tests/placeholder.py"],
                    "context_docs": ["docs/placeholder.md"],
                    "full_gate_triggers": owned,
                }
            ],
        }
    manifest_path = root / "architecture" / "modules.yaml"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return root


class TestRealRepositoryRepoMap(unittest.TestCase):
    def test_real_repository_maps_successfully(self) -> None:
        repo_map = build_repo_map(root=REPOSITORY_ROOT)
        self.assertEqual(repo_map.production_files, 50)
        self.assertGreater(repo_map.symbol_count, 0)
        self.assertGreaterEqual(repo_map.internal_edge_count, 0)

    def test_every_production_file_represented_once(self) -> None:
        repo_map = build_repo_map(root=REPOSITORY_ROOT)
        paths = [fe.path for fe in repo_map.files]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(len(paths), 50)
        self.assertEqual(sorted(paths), paths)
        # Every course_compiler/**/*.py must be present (excluding __pycache__)
        expected = sorted(
            p.relative_to(REPOSITORY_ROOT).as_posix()
            for p in (REPOSITORY_ROOT / "course_compiler").rglob("*.py")
            if "__pycache__" not in p.parts
        )
        self.assertEqual(paths, expected)

    def test_logical_ownership_agrees_with_manifest(self) -> None:
        repo_map = build_repo_map(root=REPOSITORY_ROOT)
        manifest = validate_manifest(root=REPOSITORY_ROOT)
        ownership = dict(manifest.production_ownership)
        for fe in repo_map.files:
            self.assertEqual(fe.logical_module, ownership[fe.path])

    def test_top_level_class_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            files = {
                "course_compiler/foo.py": "class MyClass:\n    pass\n",
                "course_compiler/bar.py": "x = 1\n",
            }
            root = _make_synthetic_repo(base, files)
            repo_map = build_repo_map(root=root)
            foo = next(fe for fe in repo_map.files if fe.path == "course_compiler/foo.py")
            self.assertTrue(any(s.name == "MyClass" and s.kind == "class" and s.line == 1 for s in foo.top_level_symbols))

    def test_top_level_function_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            files = {
                "course_compiler/foo.py": "def my_func():\n    pass\n",
            }
            root = _make_synthetic_repo(base, files)
            repo_map = build_repo_map(root=root)
            foo = next(fe for fe in repo_map.files if fe.path == "course_compiler/foo.py")
            self.assertTrue(any(s.name == "my_func" and s.kind == "function" for s in foo.top_level_symbols))

    def test_top_level_async_function_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            files = {
                "course_compiler/foo.py": "async def my_async():\n    pass\n",
            }
            root = _make_synthetic_repo(base, files)
            repo_map = build_repo_map(root=root)
            foo = next(fe for fe in repo_map.files if fe.path == "course_compiler/foo.py")
            self.assertTrue(any(s.name == "my_async" and s.kind == "async_function" for s in foo.top_level_symbols))

    def test_private_symbol_indexing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            files = {
                "course_compiler/foo.py": "def _private_helper():\n    pass\nclass _PrivateClass:\n    pass\n",
            }
            root = _make_synthetic_repo(base, files)
            repo_map = build_repo_map(root=root)
            foo = next(fe for fe in repo_map.files if fe.path == "course_compiler/foo.py")
            names = {s.name for s in foo.top_level_symbols}
            self.assertIn("_private_helper", names)
            self.assertIn("_PrivateClass", names)


class TestImportResolution(unittest.TestCase):
    def test_absolute_internal_import_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            files = {
                "course_compiler/a.py": "import course_compiler.b\n",
                "course_compiler/b.py": "x=1\n",
            }
            manifest = {
                "schema_version": 1,
                "modules": [
                    {
                        "id": "domain",
                        "purpose": "d",
                        "owned_paths": ["course_compiler/a.py", "course_compiler/b.py"],
                        "allowed_dependencies": [],
                        "public_surfaces": ["course_compiler/a.py"],
                        "test_paths": ["tests/placeholder.py"],
                        "context_docs": ["docs/placeholder.md"],
                        "full_gate_triggers": ["course_compiler/a.py"],
                    }
                ],
            }
            root = _make_synthetic_repo(base, files, manifest)
            repo_map = build_repo_map(root=root)
            a = next(fe for fe in repo_map.files if fe.path == "course_compiler/a.py")
            self.assertEqual(a.internal_import_targets, ("course_compiler/b.py",))

    def test_relative_internal_import_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            files = {
                "course_compiler/a.py": "from .b import X\n",
                "course_compiler/b.py": "X=1\n",
            }
            root = _make_synthetic_repo(base, files)
            repo_map = build_repo_map(root=root)
            a = next(fe for fe in repo_map.files if fe.path == "course_compiler/a.py")
            self.assertEqual(a.internal_import_targets, ("course_compiler/b.py",))

    def test_relative_import_dot_import_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            files = {
                "course_compiler/a.py": "from . import b\n",
                "course_compiler/b.py": "x=1\n",
            }
            root = _make_synthetic_repo(base, files)
            repo_map = build_repo_map(root=root)
            a = next(fe for fe in repo_map.files if fe.path == "course_compiler/a.py")
            self.assertEqual(a.internal_import_targets, ("course_compiler/b.py",))

    def test_package_level_import_handling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            files = {
                "course_compiler/__init__.py": "x=1\n",
                "course_compiler/a.py": "from course_compiler import X\n",
            }
            # Need to own both files; they belong to different logical modules or same
            manifest = {
                "schema_version": 1,
                "modules": [
                    {
                        "id": "domain",
                        "purpose": "d",
                        "owned_paths": ["course_compiler/a.py"],
                        "allowed_dependencies": ["package_api"],
                        "public_surfaces": ["course_compiler/a.py"],
                        "test_paths": ["tests/placeholder.py"],
                        "context_docs": ["docs/placeholder.md"],
                        "full_gate_triggers": ["course_compiler/a.py"],
                    },
                    {
                        "id": "package_api",
                        "purpose": "p",
                        "owned_paths": ["course_compiler/__init__.py"],
                        "allowed_dependencies": [],
                        "public_surfaces": ["course_compiler/__init__.py"],
                        "test_paths": ["tests/placeholder.py"],
                        "context_docs": ["docs/placeholder.md"],
                        "full_gate_triggers": ["course_compiler/__init__.py"],
                    },
                ],
            }
            root = _make_synthetic_repo(base, files, manifest)
            repo_map = build_repo_map(root=root)
            a = next(fe for fe in repo_map.files if fe.path == "course_compiler/a.py")
            self.assertEqual(a.internal_import_targets, ("course_compiler/__init__.py",))

    def test_external_imports_do_not_become_edges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            files = {
                "course_compiler/a.py": "import os\nimport pathlib\nfrom typing import Literal\nfrom dataclasses import dataclass\n",
            }
            root = _make_synthetic_repo(base, files)
            repo_map = build_repo_map(root=root)
            a = next(fe for fe in repo_map.files if fe.path == "course_compiler/a.py")
            self.assertEqual(a.internal_import_targets, ())

    def test_import_alias_handling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            files = {
                "course_compiler/a.py": "import course_compiler.b as bb\n",
                "course_compiler/b.py": "x=1\n",
            }
            root = _make_synthetic_repo(base, files)
            repo_map = build_repo_map(root=root)
            a = next(fe for fe in repo_map.files if fe.path == "course_compiler/a.py")
            self.assertEqual(a.internal_import_targets, ("course_compiler/b.py",))


class TestFailureModes(unittest.TestCase):
    def test_syntax_invalid_source_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            files = {
                "course_compiler/a.py": "def bad(:\n",
            }
            root = _make_synthetic_repo(base, files)
            with self.assertRaisesRegex(RepoMapError, "syntax error"):
                build_repo_map(root=root)

    def test_unresolved_course_compiler_import_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            files = {
                "course_compiler/a.py": "import course_compiler.nonexistent\n",
                "course_compiler/b.py": "x=1\n",
            }
            manifest = {
                "schema_version": 1,
                "modules": [
                    {
                        "id": "domain",
                        "purpose": "d",
                        "owned_paths": ["course_compiler/a.py", "course_compiler/b.py"],
                        "allowed_dependencies": [],
                        "public_surfaces": ["course_compiler/a.py"],
                        "test_paths": ["tests/placeholder.py"],
                        "context_docs": ["docs/placeholder.md"],
                        "full_gate_triggers": ["course_compiler/a.py"],
                    }
                ],
            }
            root = _make_synthetic_repo(base, files, manifest)
            with self.assertRaisesRegex(RepoMapError, "unresolved"):
                build_repo_map(root=root)

    def test_unresolved_relative_import_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            files = {
                "course_compiler/a.py": "from .nonexistent import X\n",
            }
            root = _make_synthetic_repo(base, files)
            with self.assertRaisesRegex(RepoMapError, "unresolved"):
                build_repo_map(root=root)

    def test_source_is_parsed_but_never_executed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            files = {
                "course_compiler/a.py": 'raise RuntimeError("should not be executed")\n\ndef foo():\n    pass\n',
            }
            root = _make_synthetic_repo(base, files)
            # Should succeed without raising RuntimeError; parsing only.
            repo_map = build_repo_map(root=root)
            a = next(fe for fe in repo_map.files if fe.path == "course_compiler/a.py")
            self.assertTrue(any(s.name == "foo" for s in a.top_level_symbols))

    def test_deterministic_json_output(self) -> None:
        first = build_repo_map(root=REPOSITORY_ROOT)
        second = build_repo_map(root=REPOSITORY_ROOT)
        j1 = json.dumps(repo_map_to_dict(first), indent=2, sort_keys=True)
        j2 = json.dumps(repo_map_to_dict(second), indent=2, sort_keys=True)
        self.assertEqual(j1, j2)

    def test_repeated_cli_json_deterministic_via_subprocess(self) -> None:
        # Use subprocess to verify byte-for-byte CLI output
        p1 = subprocess.run([sys.executable, "ci/repo_map.py", "--json"], cwd=REPOSITORY_ROOT, capture_output=True, text=True)
        p2 = subprocess.run([sys.executable, "ci/repo_map.py", "--json"], cwd=REPOSITORY_ROOT, capture_output=True, text=True)
        self.assertEqual(p1.returncode, 0)
        self.assertEqual(p2.returncode, 0)
        self.assertEqual(p1.stdout, p2.stdout)


class TestNestedPackageHandling(unittest.TestCase):
    def test_nested_python_module_mapping(self) -> None:
        from ci.repo_map import _package_for_path, _python_module_for_path

        self.assertEqual(_python_module_for_path("course_compiler/__init__.py"), "course_compiler")
        self.assertEqual(_python_module_for_path("course_compiler/app/__init__.py"), "course_compiler.app")
        self.assertEqual(_python_module_for_path("course_compiler/app/config.py"), "course_compiler.app.config")
        self.assertEqual(_python_module_for_path("course_compiler/mcp_adapter.py"), "course_compiler.mcp_adapter")
        self.assertEqual(_package_for_path("course_compiler/app/config.py"), "course_compiler.app")
        self.assertEqual(_package_for_path("course_compiler/app/__init__.py"), "course_compiler.app")
        self.assertEqual(_package_for_path("course_compiler/mcp_adapter.py"), "course_compiler")

    def test_nested_relative_import_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            files = {
                "course_compiler/app/__init__.py": "x=1\n",
                "course_compiler/app/a.py": "from .b import X\n",
                "course_compiler/app/b.py": "X=1\n",
                "course_compiler/mid.py": "Y=1\n",
            }
            manifest = {
                "schema_version": 1,
                "modules": [
                    {
                        "id": "app",
                        "purpose": "a",
                        "owned_paths": ["course_compiler/app/__init__.py", "course_compiler/app/a.py", "course_compiler/app/b.py"],
                        "allowed_dependencies": [],
                        "public_surfaces": ["course_compiler/app/a.py"],
                        "test_paths": ["tests/placeholder.py"],
                        "context_docs": ["docs/placeholder.md"],
                        "full_gate_triggers": ["course_compiler/app/a.py"],
                    },
                    {
                        "id": "midmod",
                        "purpose": "m",
                        "owned_paths": ["course_compiler/mid.py"],
                        "allowed_dependencies": [],
                        "public_surfaces": ["course_compiler/mid.py"],
                        "test_paths": ["tests/placeholder.py"],
                        "context_docs": ["docs/placeholder.md"],
                        "full_gate_triggers": ["course_compiler/mid.py"],
                    },
                ],
            }
            root = _make_synthetic_repo(base, files, manifest)
            repo_map = build_repo_map(root=root)
            a = next(fe for fe in repo_map.files if fe.path == "course_compiler/app/a.py")
            self.assertEqual(a.internal_import_targets, ("course_compiler/app/b.py",))

    def test_nested_relative_import_missing_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            files = {
                "course_compiler/app/__init__.py": "x=1\n",
                "course_compiler/app/a.py": "from .mid import X\n",
                "course_compiler/mid.py": "X=1\n",
            }
            manifest = {
                "schema_version": 1,
                "modules": [
                    {
                        "id": "app",
                        "purpose": "a",
                        "owned_paths": ["course_compiler/app/__init__.py", "course_compiler/app/a.py"],
                        "allowed_dependencies": [],
                        "public_surfaces": ["course_compiler/app/a.py"],
                        "test_paths": ["tests/placeholder.py"],
                        "context_docs": ["docs/placeholder.md"],
                        "full_gate_triggers": ["course_compiler/app/a.py"],
                    },
                    {
                        "id": "midmod",
                        "purpose": "m",
                        "owned_paths": ["course_compiler/mid.py"],
                        "allowed_dependencies": [],
                        "public_surfaces": ["course_compiler/mid.py"],
                        "test_paths": ["tests/placeholder.py"],
                        "context_docs": ["docs/placeholder.md"],
                        "full_gate_triggers": ["course_compiler/mid.py"],
                    },
                ],
            }
            root = _make_synthetic_repo(base, files, manifest)
            with self.assertRaisesRegex(RepoMapError, "unresolved relative import"):
                build_repo_map(root=root)

    def test_nested_dot_import_missing_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            files = {
                "course_compiler/app/__init__.py": "x=1\n",
                "course_compiler/app/a.py": "from . import mid\n",
                "course_compiler/mid.py": "X=1\n",
            }
            manifest = {
                "schema_version": 1,
                "modules": [
                    {
                        "id": "app",
                        "purpose": "a",
                        "owned_paths": ["course_compiler/app/__init__.py", "course_compiler/app/a.py"],
                        "allowed_dependencies": [],
                        "public_surfaces": ["course_compiler/app/a.py"],
                        "test_paths": ["tests/placeholder.py"],
                        "context_docs": ["docs/placeholder.md"],
                        "full_gate_triggers": ["course_compiler/app/a.py"],
                    },
                    {
                        "id": "midmod",
                        "purpose": "m",
                        "owned_paths": ["course_compiler/mid.py"],
                        "allowed_dependencies": [],
                        "public_surfaces": ["course_compiler/mid.py"],
                        "test_paths": ["tests/placeholder.py"],
                        "context_docs": ["docs/placeholder.md"],
                        "full_gate_triggers": ["course_compiler/mid.py"],
                    },
                ],
            }
            root = _make_synthetic_repo(base, files, manifest)
            with self.assertRaisesRegex(RepoMapError, "unresolved relative import"):
                build_repo_map(root=root)

    def test_top_level_relative_still_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            files = {
                "course_compiler/a.py": "from .b import X\n",
                "course_compiler/b.py": "X=1\n",
            }
            root = _make_synthetic_repo(base, files)
            repo_map = build_repo_map(root=root)
            a = next(fe for fe in repo_map.files if fe.path == "course_compiler/a.py")
            self.assertEqual(a.internal_import_targets, ("course_compiler/b.py",))


if __name__ == "__main__":
    unittest.main()
