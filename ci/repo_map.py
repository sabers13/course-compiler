#!/usr/bin/env python3
"""Deterministic AST repository and symbol map for course_compiler.

Uses only the Python standard library and the validated T042 manifest for
ownership. No production code is imported or executed.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from ci.module_manifest import ModuleManifestError, validate_manifest
except ImportError:
    from module_manifest import ModuleManifestError, validate_manifest  # type: ignore

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = Path("architecture/modules.yaml")
SCHEMA_VERSION = 1


class RepoMapError(Exception):
    """Raised when repository mapping fails closed."""


@dataclass(frozen=True)
class SymbolInfo:
    name: str
    kind: str
    line: int


@dataclass(frozen=True)
class FileEntry:
    path: str
    python_module: str
    logical_module: str
    internal_import_targets: tuple[str, ...]
    top_level_symbols: tuple[SymbolInfo, ...]


@dataclass(frozen=True)
class RepositoryMap:
    schema_version: int
    production_files: int
    files: tuple[FileEntry, ...]
    symbol_count: int
    internal_edge_count: int


def _python_module_for_path(path: str) -> str:
    if path == "course_compiler/__init__.py":
        return "course_compiler"
    if path.endswith("/__init__.py"):
        # e.g. course_compiler/app/__init__.py -> course_compiler.app
        without_init = path[: -len("/__init__.py")]
        return without_init.replace("/", ".")
    # e.g. course_compiler/app/config.py -> course_compiler.app.config
    without_suffix = path[: -len(".py")] if path.endswith(".py") else path
    return without_suffix.replace("/", ".")


def _package_for_path(path: str) -> str:
    """Return the Python package for a file path."""

    module = _python_module_for_path(path)
    if path.endswith("/__init__.py"):
        return module
    if "." in module:
        return module.rpartition(".")[0]
    return module


def _extract_top_level_symbols(tree: ast.AST) -> tuple[SymbolInfo, ...]:
    symbols: list[SymbolInfo] = []
    # Only direct children of Module body are top-level.
    body = getattr(tree, "body", [])
    for node in body:
        if isinstance(node, ast.ClassDef):
            symbols.append(SymbolInfo(name=node.name, kind="class", line=node.lineno))
        elif isinstance(node, ast.FunctionDef):
            symbols.append(SymbolInfo(name=node.name, kind="function", line=node.lineno))
        elif isinstance(node, ast.AsyncFunctionDef):
            symbols.append(SymbolInfo(name=node.name, kind="async_function", line=node.lineno))
        elif isinstance(node, ast.Assign):
            # Conservative simple assigned names: Name targets or Tuple/List of Names.
            for target in node.targets:
                if isinstance(target, ast.Name):
                    symbols.append(SymbolInfo(name=target.id, kind="variable", line=target.lineno))
                elif isinstance(target, (ast.Tuple, ast.List)):
                    for elt in target.elts:
                        if isinstance(elt, ast.Name):
                            symbols.append(SymbolInfo(name=elt.id, kind="variable", line=elt.lineno))
        elif isinstance(node, ast.AnnAssign):
            tgt = node.target
            if isinstance(tgt, ast.Name):
                symbols.append(SymbolInfo(name=tgt.id, kind="variable", line=tgt.lineno))
            elif isinstance(tgt, (ast.Tuple, ast.List)):
                for elt in tgt.elts:
                    if isinstance(elt, ast.Name):
                        symbols.append(SymbolInfo(name=elt.id, kind="variable", line=elt.lineno))
    # Deterministic ordering by (line, name, kind) but preserve file order primarily by line.
    # Already in line order; sort stable to guarantee determinism if duplicate lines (should not happen).
    symbols.sort(key=lambda s: (s.line, s.name, s.kind))
    return tuple(symbols)


def _resolve_internal_imports(
    tree: ast.AST,
    current_path: str,
    module_to_path: dict[str, str],
    known_files_set: set[str],
) -> tuple[str, ...]:
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name == "course_compiler" or name.startswith("course_compiler."):
                    if name == "course_compiler":
                        resolved = "course_compiler/__init__.py"
                        if resolved not in known_files_set:
                            raise RepoMapError(
                                f"unresolved course_compiler import in {current_path}: import {name} — "
                                f"expected {resolved} not in repository map"
                            )
                        targets.add(resolved)
                    else:
                        resolved = module_to_path.get(name)
                        if resolved is None:
                            raise RepoMapError(
                                f"unresolved course_compiler import in {current_path}: import {name} "
                                f"does not resolve to a known repository Python module"
                            )
                        targets.add(resolved)
                else:
                    continue
        elif isinstance(node, ast.ImportFrom):
            level = node.level
            mod = node.module
            if level == 0:
                if mod is None:
                    continue
                if mod == "course_compiler" or mod.startswith("course_compiler."):
                    if mod == "course_compiler":
                        resolved = "course_compiler/__init__.py"
                        if resolved not in known_files_set:
                            raise RepoMapError(
                                f"unresolved course_compiler import in {current_path}: from {mod} import ... — "
                                f"expected {resolved} not in repository map"
                            )
                        targets.add(resolved)
                    else:
                        resolved = module_to_path.get(mod)
                        if resolved is None:
                            raise RepoMapError(
                                f"unresolved course_compiler import in {current_path}: from {mod} import ... "
                                f"does not resolve to a known repository Python module"
                            )
                        targets.add(resolved)
                else:
                    continue
            else:
                if level != 1:
                    raise RepoMapError(
                        f"unsupported relative import level {level} in {current_path}: {ast.dump(node)}"
                    )
                package = _package_for_path(current_path)
                if mod is not None:
                    absolute = f"{package}.{mod}"
                    resolved = module_to_path.get(absolute)
                    if resolved is None:
                        raise RepoMapError(
                            f"unresolved relative import in {current_path}: from .{mod} import ... "
                            f"does not resolve to known module {absolute}"
                        )
                    targets.add(resolved)
                else:
                    for alias in node.names:
                        if alias.name == "*":
                            continue
                        absolute = f"{package}.{alias.name}"
                        resolved = module_to_path.get(absolute)
                        if resolved is None:
                            raise RepoMapError(
                                f"unresolved relative import in {current_path}: from . import {alias.name} "
                                f"does not resolve to known module {absolute}"
                            )
                        targets.add(resolved)
    return tuple(sorted(targets))


def build_repo_map(
    root: Path = ROOT,
    manifest_path: Path | None = None,
) -> RepositoryMap:
    """Build deterministic repository map for the given root.

    Raises RepoMapError or ModuleManifestError on failure.
    """
    try:
        root = root.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise RepoMapError(f"repository root cannot be resolved: {root}") from exc
    if not root.is_dir():
        raise RepoMapError(f"repository root is not a directory: {root}")

    try:
        manifest_result = validate_manifest(manifest_path or DEFAULT_MANIFEST, root=root)
    except ModuleManifestError as exc:
        raise RepoMapError(f"module manifest validation failed: {exc}") from exc

    ownership: dict[str, str] = dict(manifest_result.production_ownership)
    # production_ownership already sorted by path
    known_files = tuple(sorted(ownership.keys()))
    known_files_set = set(known_files)

    module_to_path: dict[str, str] = {}
    path_to_module: dict[str, str] = {}
    for path in known_files:
        mod = _python_module_for_path(path)
        module_to_path[mod] = path
        path_to_module[path] = mod

    file_entries: list[FileEntry] = []
    total_symbols = 0
    total_edges = 0
    for path in known_files:
        logical_module = ownership[path]
        python_module = path_to_module[path]
        file_path = root / Path(path)
        try:
            source = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise RepoMapError(f"unable to read {path}: {exc}") from exc
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError as exc:
            raise RepoMapError(f"syntax error in {path}: {exc}") from exc
        except ValueError as exc:
            raise RepoMapError(f"unable to parse {path}: {exc}") from exc

        symbols = _extract_top_level_symbols(tree)
        targets = _resolve_internal_imports(tree, path, module_to_path, known_files_set)
        total_symbols += len(symbols)
        total_edges += len(targets)
        entry = FileEntry(
            path=path,
            python_module=python_module,
            logical_module=logical_module,
            internal_import_targets=targets,
            top_level_symbols=symbols,
        )
        file_entries.append(entry)

    # Already sorted by path because known_files sorted
    return RepositoryMap(
        schema_version=SCHEMA_VERSION,
        production_files=len(known_files),
        files=tuple(file_entries),
        symbol_count=total_symbols,
        internal_edge_count=total_edges,
    )


def repo_map_to_dict(repo_map: RepositoryMap) -> dict[str, Any]:
    files_list: list[dict[str, Any]] = []
    for fe in sorted(repo_map.files, key=lambda x: x.path):
        files_list.append(
            {
                "path": fe.path,
                "python_module": fe.python_module,
                "logical_module": fe.logical_module,
                "internal_import_targets": list(fe.internal_import_targets),
                "top_level_symbols": [
                    {"name": s.name, "kind": s.kind, "line": s.line}
                    for s in sorted(fe.top_level_symbols, key=lambda x: (x.line, x.name, x.kind))
                ],
            }
        )
    return {
        "schema_version": repo_map.schema_version,
        "production_files": repo_map.production_files,
        "symbol_count": repo_map.symbol_count,
        "internal_edge_count": repo_map.internal_edge_count,
        "files": files_list,
    }


def format_human(repo_map: RepositoryMap) -> str:
    lines = [
        f"Repository map: {repo_map.production_files} production files, "
        f"{repo_map.symbol_count} top-level symbols, {repo_map.internal_edge_count} internal edges",
        f"Schema v{repo_map.schema_version} — deterministic AST index (no execution)",
        "",
    ]
    for fe in sorted(repo_map.files, key=lambda x: x.path):
        lines.append(
            f"{fe.path} [{fe.logical_module}] ({fe.python_module}) -> "
            f"{len(fe.internal_import_targets)} imports, {len(fe.top_level_symbols)} symbols"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit deterministic JSON to stdout")
    parser.add_argument("--module", dest="module_filter", type=str, default=None, help="filter to logical module id")
    parser.add_argument("--path", dest="path_filter", type=str, default=None, help="filter to exact repository-relative path")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="manifest path relative to --root")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    args = parser.parse_args(argv)

    try:
        repo_map = build_repo_map(root=args.root, manifest_path=args.manifest)
    except (RepoMapError, ModuleManifestError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    # Apply optional filters (bounded, simple)
    filtered_files = list(repo_map.files)
    if args.module_filter is not None:
        filtered_files = [f for f in filtered_files if f.logical_module == args.module_filter]
        if not filtered_files:
            print(f"FAIL: no files match module filter: {args.module_filter}", file=sys.stderr)
            return 1
    if args.path_filter is not None:
        filtered_files = [f for f in filtered_files if f.path == args.path_filter]
        if not filtered_files:
            print(f"FAIL: no file matches path filter: {args.path_filter}", file=sys.stderr)
            return 1

    if filtered_files is not repo_map.files:
        # Recompute counts for filtered view but keep schema_version
        filtered_map = RepositoryMap(
            schema_version=repo_map.schema_version,
            production_files=len(filtered_files),
            files=tuple(sorted(filtered_files, key=lambda x: x.path)),
            symbol_count=sum(len(f.top_level_symbols) for f in filtered_files),
            internal_edge_count=sum(len(f.internal_import_targets) for f in filtered_files),
        )
        emit_map = filtered_map
    else:
        emit_map = repo_map

    if args.json:
        data = repo_map_to_dict(emit_map)
        # Deterministic byte-for-byte: sorted keys, indent 2, ensure_ascii False
        json_output = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        sys.stdout.write(json_output)
    else:
        sys.stdout.write(format_human(emit_map) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
