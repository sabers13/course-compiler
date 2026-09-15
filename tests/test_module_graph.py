"""Synthetic regression tests for AST module dependency graph."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ci.module_manifest import ModuleManifestError
from ci.module_graph import ModuleGraphError, build_module_graph, graph_to_dict, validate_module_graph
from ci.repo_map import build_repo_map

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _make_synthetic_repo(base: Path, files: dict[str, str], manifest: dict) -> Path:
    root = base / "repo"
    (root / "architecture").mkdir(parents=True, exist_ok=True)
    (root / "course_compiler").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    _write(root / "docs" / "placeholder.md", "placeholder\n")
    _write(root / "tests" / "placeholder.py", "placeholder\n")
    for rel, content in files.items():
        _write(root / rel, content)
    manifest_path = root / "architecture" / "modules.yaml"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return root


class TestRealRepositoryGraph(unittest.TestCase):
    def test_real_repository_graph_passes(self) -> None:
        graph = build_module_graph(root=REPOSITORY_ROOT)
        self.assertTrue(graph.is_valid)
        self.assertEqual(graph.violations, ())
        # Also validate entrypoint should not raise
        validated = validate_module_graph(root=REPOSITORY_ROOT)
        self.assertTrue(validated.is_valid)

    def test_logical_graph_deterministic(self) -> None:
        g1 = build_module_graph(root=REPOSITORY_ROOT)
        g2 = build_module_graph(root=REPOSITORY_ROOT)
        self.assertEqual(g1.logical_dependencies, g2.logical_dependencies)

    def test_reverse_graph_deterministic(self) -> None:
        g1 = build_module_graph(root=REPOSITORY_ROOT)
        g2 = build_module_graph(root=REPOSITORY_ROOT)
        self.assertEqual(g1.file_reverse_dependencies, g2.file_reverse_dependencies)
        self.assertEqual(g1.logical_reverse_dependencies, g2.logical_reverse_dependencies)

    def test_deterministic_json(self) -> None:
        g1 = build_module_graph(root=REPOSITORY_ROOT)
        g2 = build_module_graph(root=REPOSITORY_ROOT)
        j1 = json.dumps(graph_to_dict(g1), indent=2, sort_keys=True)
        j2 = json.dumps(graph_to_dict(g2), indent=2, sort_keys=True)
        self.assertEqual(j1, j2)

    def test_cli_json_deterministic(self) -> None:
        p1 = subprocess.run([sys.executable, "ci/module_graph.py", "--json"], cwd=REPOSITORY_ROOT, capture_output=True, text=True)
        p2 = subprocess.run([sys.executable, "ci/module_graph.py", "--json"], cwd=REPOSITORY_ROOT, capture_output=True, text=True)
        self.assertEqual(p1.returncode, 0)
        self.assertEqual(p2.returncode, 0)
        self.assertEqual(p1.stdout, p2.stdout)

    def test_package_api_imports_represented(self) -> None:
        graph = build_module_graph(root=REPOSITORY_ROOT)
        deps = dict(graph.logical_dependencies)
        # package_api should depend on domain,persistence,build,application per T042
        self.assertEqual(set(deps["package_api"]), {"domain", "persistence", "build", "application"})
        # Verify file dependencies for __init__.py include many targets
        fdeps = dict(graph.file_dependencies)
        init_deps = fdeps["course_compiler/__init__.py"]
        self.assertGreater(len(init_deps), 10)
        # Should include at least one file from each allowed module
        # Check that init imports workflow policy via workflow etc.

    def test_integrations_edges_represented(self) -> None:
        graph = build_module_graph(root=REPOSITORY_ROOT)
        deps = dict(graph.logical_dependencies)
        self.assertEqual(set(deps["integrations"]), {"domain", "persistence", "build", "application"})
        # File-level check: mcp_adapter should have edges to policy_persistence, etc.
        fdeps = dict(graph.file_dependencies)
        self.assertIn("course_compiler/policy_persistence.py", fdeps["course_compiler/mcp_runtime.py"])


class TestSyntheticGraphValidation(unittest.TestCase):
    def test_same_module_imports_do_not_create_cross_edge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            files = {
                "course_compiler/a.py": "from .b import X\n",
                "course_compiler/b.py": "X=1\n",
                "course_compiler/c.py": "import course_compiler.a\n",
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
                    },
                    {
                        "id": "application",
                        "purpose": "a",
                        "owned_paths": ["course_compiler/c.py"],
                        "allowed_dependencies": ["domain"],
                        "public_surfaces": ["course_compiler/c.py"],
                        "test_paths": ["tests/placeholder.py"],
                        "context_docs": ["docs/placeholder.md"],
                        "full_gate_triggers": ["course_compiler/c.py"],
                    },
                ],
            }
            root = _make_synthetic_repo(base, files, manifest)
            graph = build_module_graph(root=root)
            logical = dict(graph.logical_dependencies)
            # a->b is same module domain, so no cross edge domain->domain
            self.assertEqual(logical["domain"], ())
            # c->a is application -> domain, should be present
            self.assertEqual(logical["application"], ("domain",))
            self.assertTrue(graph.is_valid)

    def test_allowed_cross_module_import_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            files = {
                "course_compiler/persist.py": "from .domain import X\n",
                "course_compiler/domain.py": "X=1\n",
            }
            manifest = {
                "schema_version": 1,
                "modules": [
                    {
                        "id": "domain",
                        "purpose": "d",
                        "owned_paths": ["course_compiler/domain.py"],
                        "allowed_dependencies": [],
                        "public_surfaces": ["course_compiler/domain.py"],
                        "test_paths": ["tests/placeholder.py"],
                        "context_docs": ["docs/placeholder.md"],
                        "full_gate_triggers": ["course_compiler/domain.py"],
                    },
                    {
                        "id": "persistence",
                        "purpose": "p",
                        "owned_paths": ["course_compiler/persist.py"],
                        "allowed_dependencies": ["domain"],
                        "public_surfaces": ["course_compiler/persist.py"],
                        "test_paths": ["tests/placeholder.py"],
                        "context_docs": ["docs/placeholder.md"],
                        "full_gate_triggers": ["course_compiler/persist.py"],
                    },
                ],
            }
            root = _make_synthetic_repo(base, files, manifest)
            graph = build_module_graph(root=root)
            self.assertTrue(graph.is_valid)
            # also validate_module_graph should not raise
            validated = validate_module_graph(root=root)
            self.assertTrue(validated.is_valid)

    def test_undeclared_cross_module_import_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            files = {
                "course_compiler/persist.py": "from .domain import X\n",
                "course_compiler/domain.py": "X=1\n",
            }
            manifest = {
                "schema_version": 1,
                "modules": [
                    {
                        "id": "domain",
                        "purpose": "d",
                        "owned_paths": ["course_compiler/domain.py"],
                        "allowed_dependencies": [],
                        "public_surfaces": ["course_compiler/domain.py"],
                        "test_paths": ["tests/placeholder.py"],
                        "context_docs": ["docs/placeholder.md"],
                        "full_gate_triggers": ["course_compiler/domain.py"],
                    },
                    {
                        "id": "persistence",
                        "purpose": "p",
                        "owned_paths": ["course_compiler/persist.py"],
                        "allowed_dependencies": [],  # missing domain!
                        "public_surfaces": ["course_compiler/persist.py"],
                        "test_paths": ["tests/placeholder.py"],
                        "context_docs": ["docs/placeholder.md"],
                        "full_gate_triggers": ["course_compiler/persist.py"],
                    },
                ],
            }
            root = _make_synthetic_repo(base, files, manifest)
            graph = build_module_graph(root=root)
            self.assertFalse(graph.is_valid)
            self.assertEqual(len(graph.violations), 1)
            v = graph.violations[0]
            self.assertEqual(v.source_module, "persistence")
            self.assertEqual(v.target_module, "domain")
            self.assertEqual(v.importing_file, "course_compiler/persist.py")
            self.assertEqual(v.imported_file, "course_compiler/domain.py")
            with self.assertRaisesRegex(ModuleGraphError, "missing allowed dependency"):
                validate_module_graph(root=root)
            # Also test diagnostic contains file/module ids via build graph
            self.assertIn("persistence", str(graph.violations))

    def test_diagnostic_identifies_source_target_files_and_module_ids(self) -> None:
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
                        "owned_paths": ["course_compiler/b.py"],
                        "allowed_dependencies": [],
                        "public_surfaces": ["course_compiler/b.py"],
                        "test_paths": ["tests/placeholder.py"],
                        "context_docs": ["docs/placeholder.md"],
                        "full_gate_triggers": ["course_compiler/b.py"],
                    },
                    {
                        "id": "application",
                        "purpose": "a",
                        "owned_paths": ["course_compiler/a.py"],
                        "allowed_dependencies": [],  # no domain allowed
                        "public_surfaces": ["course_compiler/a.py"],
                        "test_paths": ["tests/placeholder.py"],
                        "context_docs": ["docs/placeholder.md"],
                        "full_gate_triggers": ["course_compiler/a.py"],
                    },
                ],
            }
            root = _make_synthetic_repo(base, files, manifest)
            try:
                validate_module_graph(root=root)
                self.fail("expected ModuleGraphError")
            except ModuleGraphError as exc:
                msg = str(exc)
                self.assertIn("course_compiler/a.py", msg)
                self.assertIn("application", msg)
                self.assertIn("course_compiler/b.py", msg)
                self.assertIn("domain", msg)

    def test_malformed_manifest_continues_to_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            files = {
                "course_compiler/a.py": "x=1\n",
            }
            # Missing required field allowed_dependencies -> structural failure
            manifest = {
                "schema_version": 1,
                "modules": [
                    {
                        "id": "domain",
                        "purpose": "d",
                        "owned_paths": ["course_compiler/a.py"],
                        # intentionally missing allowed_dependencies
                        "public_surfaces": ["course_compiler/a.py"],
                        "test_paths": ["tests/placeholder.py"],
                        "context_docs": ["docs/placeholder.md"],
                        "full_gate_triggers": ["course_compiler/a.py"],
                    }
                ],
            }
            root = _make_synthetic_repo(base, files, manifest)
            with self.assertRaises(ModuleGraphError):
                build_module_graph(root=root)

    def test_file_reverse_dependencies(self) -> None:
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
            graph = build_module_graph(root=root)
            fwd = dict(graph.file_dependencies)
            rev = dict(graph.file_reverse_dependencies)
            self.assertEqual(fwd["course_compiler/a.py"], ("course_compiler/b.py",))
            self.assertEqual(fwd["course_compiler/b.py"], ())
            self.assertEqual(rev["course_compiler/b.py"], ("course_compiler/a.py",))
            self.assertEqual(rev["course_compiler/a.py"], ())


if __name__ == "__main__":
    unittest.main()
