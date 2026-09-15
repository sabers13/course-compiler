#!/usr/bin/env python3
"""Deterministic AST logical dependency graph on top of the repository map.

Consumes validated architecture/modules.yaml and ci/repo_map.py structural
results. Derives file and logical forward/reverse dependencies and validates
that actual cross-module edges are a subset of allowed_dependencies.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from ci.module_manifest import ModuleManifestError, validate_manifest
except ImportError:
    from module_manifest import ModuleManifestError, validate_manifest  # type: ignore

try:
    from ci.repo_map import RepoMapError, RepositoryMap, build_repo_map, repo_map_to_dict
except ImportError:
    from repo_map import RepoMapError, RepositoryMap, build_repo_map, repo_map_to_dict  # type: ignore

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = Path("architecture/modules.yaml")
SCHEMA_VERSION = 1


class ModuleGraphError(Exception):
    """Raised when module graph validation fails closed."""


@dataclass(frozen=True)
class Violation:
    importing_file: str
    source_module: str
    imported_file: str
    target_module: str
    missing_dependency: str


@dataclass(frozen=True)
class ModuleGraph:
    schema_version: int
    files: tuple[str, ...]
    file_dependencies: tuple[tuple[str, tuple[str, ...]], ...]
    file_reverse_dependencies: tuple[tuple[str, tuple[str, ...]], ...]
    logical_dependencies: tuple[tuple[str, tuple[str, ...]], ...]
    logical_reverse_dependencies: tuple[tuple[str, tuple[str, ...]], ...]
    violations: tuple[Violation, ...]
    is_valid: bool


def _build_file_reverse(
    file_deps: dict[str, tuple[str, ...]], files: tuple[str, ...]
) -> dict[str, tuple[str, ...]]:
    reverse: dict[str, set[str]] = {f: set() for f in files}
    for src, targets in file_deps.items():
        for tgt in targets:
            if tgt in reverse:
                reverse[tgt].add(src)
            else:
                # Should not happen: target is always a known file, but handle
                reverse[tgt] = {src}
    return {k: tuple(sorted(v)) for k, v in sorted(reverse.items())}


def build_module_graph(
    root: Path = ROOT,
    manifest_path: Path | None = None,
) -> ModuleGraph:
    """Build file/logical graphs and validate against allowed dependencies.

    Raises ModuleGraphError or RepoMapError or ModuleManifestError on failure.
    """
    try:
        root = root.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ModuleGraphError(f"repository root cannot be resolved: {root}") from exc
    if not root.is_dir():
        raise ModuleGraphError(f"repository root is not a directory: {root}")

    try:
        manifest_result = validate_manifest(manifest_path or DEFAULT_MANIFEST, root=root)
    except ModuleManifestError as exc:
        raise ModuleGraphError(f"module manifest validation failed: {exc}") from exc

    try:
        repo_map = build_repo_map(root=root, manifest_path=manifest_path)
    except (RepoMapError, ModuleManifestError) as exc:
        raise ModuleGraphError(f"repository map failed: {exc}") from exc

    # Maps
    file_to_module: dict[str, str] = {fe.path: fe.logical_module for fe in repo_map.files}
    files_sorted = tuple(sorted(file_to_module.keys()))

    # file_dependencies: source -> sorted targets (already sorted in repo_map)
    file_deps: dict[str, tuple[str, ...]] = {}
    for fe in repo_map.files:
        file_deps[fe.path] = fe.internal_import_targets
    # Ensure every file appears even if zero deps
    for f in files_sorted:
        file_deps.setdefault(f, tuple())

    # Sorted representation
    file_deps_sorted = tuple((k, file_deps[k]) for k in sorted(file_deps.keys()))

    # file reverse
    file_rev_dict = _build_file_reverse(file_deps, files_sorted)
    file_rev_sorted = tuple((k, file_rev_dict[k]) for k in sorted(file_rev_dict.keys()))

    # logical dependencies
    # allowed_dependencies map
    allowed: dict[str, tuple[str, ...]] = dict(manifest_result.declared_dependencies)
    # Build actual logical edges
    logical_deps_sets: dict[str, set[str]] = {mid: set() for mid in allowed.keys()}
    # Track which file pair caused each edge for violation diagnostics
    edge_to_file_pairs: dict[tuple[str, str], list[tuple[str, str]]] = {}

    for src_file, targets in file_deps.items():
        src_mod = file_to_module.get(src_file)
        if src_mod is None:
            continue
        for tgt_file in targets:
            tgt_mod = file_to_module.get(tgt_file)
            if tgt_mod is None:
                continue
            if tgt_mod == src_mod:
                continue
            # cross-module edge
            edge = (src_mod, tgt_mod)
            if edge not in edge_to_file_pairs:
                edge_to_file_pairs[edge] = []
            edge_to_file_pairs[edge].append((src_file, tgt_file))
            logical_deps_sets[src_mod].add(tgt_mod)

    logical_deps: dict[str, tuple[str, ...]] = {
        k: tuple(sorted(v)) for k, v in sorted(logical_deps_sets.items())
    }
    logical_deps_sorted = tuple((k, logical_deps[k]) for k in sorted(logical_deps.keys()))

    # logical reverse
    logical_rev_sets: dict[str, set[str]] = {mid: set() for mid in allowed.keys()}
    for src_mod, targets in logical_deps.items():
        for tgt_mod in targets:
            logical_rev_sets[tgt_mod].add(src_mod)
    logical_rev: dict[str, tuple[str, ...]] = {
        k: tuple(sorted(v)) for k, v in sorted(logical_rev_sets.items())
    }
    logical_rev_sorted = tuple((k, logical_rev[k]) for k in sorted(logical_rev.keys()))

    # Validate actual ⊆ allowed
    violations: list[Violation] = []
    for (src_mod, tgt_mod), file_pairs in sorted(edge_to_file_pairs.items()):
        if tgt_mod not in allowed.get(src_mod, ()):
            # For diagnostic, use first file pair; but could collect all - use first
            importing_file, imported_file = sorted(file_pairs)[0]
            violations.append(
                Violation(
                    importing_file=importing_file,
                    source_module=src_mod,
                    imported_file=imported_file,
                    target_module=tgt_mod,
                    missing_dependency=tgt_mod,
                )
            )

    violations_sorted = tuple(sorted(violations, key=lambda v: (v.source_module, v.target_module, v.importing_file, v.imported_file)))
    is_valid = len(violations_sorted) == 0

    return ModuleGraph(
        schema_version=SCHEMA_VERSION,
        files=files_sorted,
        file_dependencies=file_deps_sorted,
        file_reverse_dependencies=file_rev_sorted,
        logical_dependencies=logical_deps_sorted,
        logical_reverse_dependencies=logical_rev_sorted,
        violations=violations_sorted,
        is_valid=is_valid,
    )


def validate_module_graph(
    root: Path = ROOT,
    manifest_path: Path | None = None,
) -> ModuleGraph:
    """Build graph and fail closed on undeclared cross-module edges.

    Returns graph if valid, raises ModuleGraphError with diagnostics otherwise.
    """
    graph = build_module_graph(root=root, manifest_path=manifest_path)
    if not graph.is_valid:
        details = "; ".join(
            f"{v.importing_file} [{v.source_module}] -> {v.imported_file} [{v.target_module}] "
            f"missing allowed dependency {v.source_module} -> {v.missing_dependency}"
            for v in graph.violations
        )
        raise ModuleGraphError(
            f"undeclared cross-module dependency edges: {details}"
        )
    return graph


def graph_to_dict(graph: ModuleGraph) -> dict[str, Any]:
    return {
        "schema_version": graph.schema_version,
        "files": list(graph.files),
        "file_dependencies": {k: list(v) for k, v in graph.file_dependencies},
        "file_reverse_dependencies": {k: list(v) for k, v in graph.file_reverse_dependencies},
        "logical_dependencies": {k: list(v) for k, v in graph.logical_dependencies},
        "logical_reverse_dependencies": {k: list(v) for k, v in graph.logical_reverse_dependencies},
        "violations": [
            {
                "importing_file": v.importing_file,
                "source_module": v.source_module,
                "imported_file": v.imported_file,
                "target_module": v.target_module,
                "missing_dependency": v.missing_dependency,
            }
            for v in graph.violations
        ],
        "is_valid": graph.is_valid,
    }


def format_human(graph: ModuleGraph) -> str:
    lines = [
        f"Module graph: {len(graph.files)} files, "
        f"{sum(len(v) for _, v in graph.file_dependencies)} file edges, "
        f"{sum(len(v) for _, v in graph.logical_dependencies)} logical edges",
        f"Schema v{graph.schema_version} — validation {'PASS' if graph.is_valid else 'FAIL'}: "
        f"actual cross-module ⊆ allowed",
        "",
        "Logical forward dependencies:",
    ]
    for mod, deps in graph.logical_dependencies:
        if deps:
            lines.append(f"  {mod} -> {', '.join(deps)}")
        else:
            lines.append(f"  {mod} -> (none)")
    lines.append("")
    lines.append("Logical reverse dependencies:")
    for mod, rev in graph.logical_reverse_dependencies:
        if rev:
            lines.append(f"  {mod} <- {', '.join(rev)}")
        else:
            lines.append(f"  {mod} <- (none)")
    if graph.violations:
        lines.append("")
        lines.append("Violations (undeclared edges):")
        for v in graph.violations:
            lines.append(
                f"  {v.importing_file} [{v.source_module}] -> {v.imported_file} [{v.target_module}] "
                f"missing {v.source_module} -> {v.missing_dependency}"
            )
    else:
        lines.append("")
        lines.append("No undeclared cross-module edges — all actual edges are permitted.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit deterministic JSON")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="manifest path relative to --root")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    parser.add_argument("--check", action="store_true", help="alias for validation (default behavior)")
    args = parser.parse_args(argv)

    try:
        graph = build_module_graph(root=args.root, manifest_path=args.manifest)
    except (ModuleGraphError, RepoMapError, ModuleManifestError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    if args.json:
        data = graph_to_dict(graph)
        # Deterministic JSON
        output = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        sys.stdout.write(output)
        # Exit code indicates validation status
        return 0 if graph.is_valid else 1
    else:
        sys.stdout.write(format_human(graph) + "\n")
        return 0 if graph.is_valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
