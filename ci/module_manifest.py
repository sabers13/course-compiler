#!/usr/bin/env python3
"""Structural validator for the repository logical-module manifest.

Schema version 1 uses the YAML 1.2 JSON-compatible subset, so parsing is
deterministic with Python's standard-library ``json`` module. It intentionally
uses exact repository-relative file paths and performs no import or AST analysis.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = Path("architecture/modules.yaml")
SUPPORTED_SCHEMA_VERSION = 1
FORBIDDEN_PATH_COMPONENTS = frozenset({"local-data", "local-artifacts", "build"})
MODULE_ID_RE = re.compile(r"[a-z][a-z0-9_]*\Z")
REQUIRED_MODULE_FIELDS = frozenset(
    {
        "id",
        "purpose",
        "owned_paths",
        "allowed_dependencies",
        "public_surfaces",
        "test_paths",
        "context_docs",
        "full_gate_triggers",
    }
)
PATH_FIELDS = (
    "owned_paths",
    "public_surfaces",
    "test_paths",
    "context_docs",
    "full_gate_triggers",
)


class ModuleManifestError(Exception):
    """Raised when the logical-module manifest is malformed or inconsistent."""


@dataclass(frozen=True)
class ModuleManifestResult:
    schema_version: int
    module_ids: tuple[str, ...]
    declared_dependencies: tuple[tuple[str, tuple[str, ...]], ...]
    production_ownership: tuple[tuple[str, str], ...]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ModuleManifestError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> None:
    raise ModuleManifestError(f"non-JSON numeric constant is forbidden: {value}")


def load_manifest(manifest_path: Path) -> dict[str, Any]:
    """Load one JSON-compatible YAML manifest with duplicate-key rejection."""

    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ModuleManifestError(f"unable to read manifest: {manifest_path}") from exc
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except ModuleManifestError:
        raise
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ModuleManifestError(f"manifest is not valid JSON-compatible YAML: {exc}") from exc
    if type(value) is not dict:
        raise ModuleManifestError("manifest root must be an object")
    return value


def _string_list(module_id: str, field: str, value: Any) -> list[str]:
    if type(value) is not list:
        raise ModuleManifestError(f"module {module_id}: {field} must be a list")
    if any(type(item) is not str or not item for item in value):
        raise ModuleManifestError(
            f"module {module_id}: {field} entries must be non-empty strings"
        )
    if len(value) != len(set(value)):
        raise ModuleManifestError(f"module {module_id}: {field} contains duplicates")
    return value


def _resolve_concrete_path(raw_path: str, root: Path, *, module_id: str, field: str) -> str:
    if "\\" in raw_path:
        raise ModuleManifestError(
            f"module {module_id}: {field} path must use POSIX separators: {raw_path}"
        )
    pure = PurePosixPath(raw_path)
    if pure.is_absolute():
        raise ModuleManifestError(
            f"module {module_id}: {field} path must be repository-relative: {raw_path}"
        )
    if raw_path.startswith("./") or "//" in raw_path or any(
        part in {"", ".", ".."} for part in raw_path.split("/")
    ):
        raise ModuleManifestError(
            f"module {module_id}: {field} path is not normalized: {raw_path}"
        )
    if any(component in FORBIDDEN_PATH_COMPONENTS for component in pure.parts):
        raise ModuleManifestError(
            f"module {module_id}: {field} path enters a forbidden location: {raw_path}"
        )

    root_resolved = root.resolve()
    candidate = root / Path(*pure.parts)
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ModuleManifestError(
            f"module {module_id}: {field} path cannot be resolved: {raw_path}"
        ) from exc
    try:
        relative = resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ModuleManifestError(
            f"module {module_id}: {field} path resolves outside repository: {raw_path}"
        ) from exc
    if any(component in FORBIDDEN_PATH_COMPONENTS for component in relative.parts):
        raise ModuleManifestError(
            f"module {module_id}: {field} path resolves through a forbidden location: {raw_path}"
        )
    if not resolved.is_file():
        raise ModuleManifestError(
            f"module {module_id}: {field} path is not an existing file: {raw_path}"
        )
    return pure.as_posix()


def _resolve_manifest_path(manifest_path: Path, root: Path) -> Path:
    candidate = manifest_path if manifest_path.is_absolute() else root / manifest_path
    try:
        lexical_relative = candidate.absolute().relative_to(root)
        resolved = candidate.resolve()
        resolved_relative = resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ModuleManifestError(
            f"manifest path must resolve inside repository: {manifest_path}"
        ) from exc
    if any(
        component in FORBIDDEN_PATH_COMPONENTS
        for component in (*lexical_relative.parts, *resolved_relative.parts)
    ):
        raise ModuleManifestError(
            f"manifest path enters a forbidden location: {manifest_path}"
        )
    if not resolved.is_file():
        raise ModuleManifestError(f"manifest is not an existing file: {manifest_path}")
    return resolved


def _validate_acyclic(dependencies: dict[str, tuple[str, ...]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module_id: str, trail: tuple[str, ...]) -> None:
        if module_id in visiting:
            cycle_start = trail.index(module_id)
            cycle = trail[cycle_start:] + (module_id,)
            raise ModuleManifestError(
                "declared dependency graph contains a cycle: " + " -> ".join(cycle)
            )
        if module_id in visited:
            return
        visiting.add(module_id)
        for dependency in dependencies[module_id]:
            visit(dependency, trail + (module_id,))
        visiting.remove(module_id)
        visited.add(module_id)

    for module_id in dependencies:
        visit(module_id, ())


def validate_manifest(
    manifest_path: Path | None = None,
    *,
    root: Path = ROOT,
) -> ModuleManifestResult:
    """Validate schema, paths, graph, and complete flat-package ownership."""

    try:
        root = root.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ModuleManifestError(f"repository root cannot be resolved: {root}") from exc
    if not root.is_dir():
        raise ModuleManifestError(f"repository root is not a directory: {root}")
    path = _resolve_manifest_path(manifest_path or DEFAULT_MANIFEST, root)
    manifest = load_manifest(path)

    schema_version = manifest.get("schema_version")
    if type(schema_version) is not int or schema_version != SUPPORTED_SCHEMA_VERSION:
        raise ModuleManifestError(
            f"unsupported schema_version: {schema_version!r}; expected {SUPPORTED_SCHEMA_VERSION}"
        )
    modules = manifest.get("modules")
    if type(modules) is not list or not modules:
        raise ModuleManifestError("modules must be a non-empty list")

    module_ids: list[str] = []
    normalized_modules: list[tuple[str, dict[str, list[str]]]] = []
    for index, module in enumerate(modules):
        if type(module) is not dict:
            raise ModuleManifestError(f"modules[{index}] must be an object")
        missing = sorted(REQUIRED_MODULE_FIELDS - module.keys())
        if missing:
            raise ModuleManifestError(
                f"modules[{index}] is missing required fields: {', '.join(missing)}"
            )
        module_id = module["id"]
        if type(module_id) is not str or not MODULE_ID_RE.fullmatch(module_id):
            raise ModuleManifestError(f"modules[{index}].id is invalid: {module_id!r}")
        if module_id in module_ids:
            raise ModuleManifestError(f"duplicate module id: {module_id}")
        purpose = module["purpose"]
        if type(purpose) is not str or not purpose.strip():
            raise ModuleManifestError(f"module {module_id}: purpose must be non-empty")

        fields = {
            field: _string_list(module_id, field, module[field])
            for field in (*PATH_FIELDS, "allowed_dependencies")
        }
        module_ids.append(module_id)
        normalized_modules.append((module_id, fields))

    declared_ids = set(module_ids)
    dependencies: dict[str, tuple[str, ...]] = {}
    for module_id, fields in normalized_modules:
        dependency_list = fields["allowed_dependencies"]
        unknown = sorted(set(dependency_list) - declared_ids)
        if unknown:
            raise ModuleManifestError(
                f"module {module_id}: unknown allowed_dependencies: {', '.join(unknown)}"
            )
        if module_id in dependency_list:
            raise ModuleManifestError(f"module {module_id}: self dependency is forbidden")
        dependencies[module_id] = tuple(dependency_list)
    _validate_acyclic(dependencies)

    owners: dict[str, str] = {}
    for module_id, fields in normalized_modules:
        for field in PATH_FIELDS:
            for raw_path in fields[field]:
                relative = _resolve_concrete_path(
                    raw_path, root, module_id=module_id, field=field
                )
                if field == "owned_paths":
                    previous = owners.get(relative)
                    if previous is not None:
                        raise ModuleManifestError(
                            f"owned path {relative} is assigned to both {previous} and {module_id}"
                        )
                    owners[relative] = module_id

    package_dir = root / "course_compiler"
    if not package_dir.is_dir():
        raise ModuleManifestError("course_compiler directory does not exist")
    production_files = tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in package_dir.rglob("*.py")
            if path.is_file() and "__pycache__" not in path.parts
        )
    )
    unowned = [path for path in production_files if path not in owners]
    if unowned:
        raise ModuleManifestError(
            "unowned production Python files: " + ", ".join(unowned)
        )

    ownership = tuple((path, owners[path]) for path in production_files)
    return ModuleManifestResult(
        schema_version=schema_version,
        module_ids=tuple(module_ids),
        declared_dependencies=tuple(dependencies.items()),
        production_ownership=ownership,
    )


def format_validation_result(result: ModuleManifestResult) -> str:
    """Return stable, content-free validation output."""

    return (
        f"PASS: module manifest schema v{result.schema_version}: "
        f"{len(result.module_ids)} modules, "
        f"{len(result.production_ownership)} production Python files uniquely owned"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="manifest path relative to --root (default: architecture/modules.yaml)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="repository root (default: parent of ci/)",
    )
    args = parser.parse_args(argv)
    try:
        result = validate_manifest(args.manifest, root=args.root)
    except ModuleManifestError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(format_validation_result(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
