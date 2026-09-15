#!/usr/bin/env python3
"""Stage only the public Course Compiler relay plugin package into an ignored root.

T051 packaging boundary
-----------------------

The production Skill is a *bounded semantic worker* for exactly one browser-
mediated relay turn. A live GPT Product E2E proved that shipping the pre-T051
registered-app binding alongside it is not merely redundant: with the legacy
app attached, the model reached for the old seven-tool MCP workflow
(``record_lecture_map`` over a local connector) instead of returning one
strict ``SemanticWorkResult``.

So the prepared relay package carries:

* every public Skill file the production ``SKILL.md`` instructs the model to
  read (reference closure), byte-identical to the authoritative repository;
* a relay manifest derived from the tracked manifest with the legacy ``apps``
  binding removed.

It never carries ``.app.json``. The legacy MCP surface is preserved in the
repository itself (tracked ``.app.json`` plus the tracked manifest's ``apps``
key) and remains available as the explicitly separate optional local
development ingress . Packaging separates the two; it
does not erase the legacy path.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


# The tracked manifest. It is transformed (never copied verbatim) so the
# prepared relay package cannot reference a legacy app binding it omits.
MANIFEST_PATH = Path(".codex-plugin/plugin.json")

# Byte-identical public Skill files. Reference closure is mandatory: every
# reference the production SKILL.md tells the model to read must be present,
# or a fresh conversation follows an instruction it cannot satisfy.
RELAY_PACKAGE_PATHS = (
    Path("skills/course-compiler/SKILL.md"),
    Path("skills/course-compiler/prompts/course-authoring-v2.md"),
    Path("skills/course-compiler/agents/openai.yaml"),
    Path("skills/course-compiler/references/coarse-workflow.md"),
    Path("skills/course-compiler/references/lecture-authoring.md"),
    Path("skills/course-compiler/scripts/find_source_text_offset.py"),
)

# Every file the prepared package contains, manifest included.
PACKAGE_PATHS = (MANIFEST_PATH,) + RELAY_PACKAGE_PATHS

# Never staged into a relay package. Attaching the T032-era registered app
# exposes the pre-T051 seven-tool MCP tools to a bounded relay turn.
EXCLUDED_PACKAGE_PATHS = (Path(".app.json"),)

# Manifest keys stripped when deriving the relay manifest.
EXCLUDED_MANIFEST_KEYS = ("apps",)


class PluginPackageError(Exception):
    """A fixed, content-free staging failure."""


@dataclass(frozen=True, slots=True)
class PreparedPluginPackage:
    destination: Path
    copied_paths: tuple[Path, ...]


def relay_manifest(source_root: Path) -> dict:
    """Derive the relay manifest from the tracked manifest.

    Identical to the tracked manifest except that every legacy app-binding
    key is removed, so an enabled relay package cannot attach the old MCP
    tool surface.
    """

    try:
        manifest = json.loads((source_root / MANIFEST_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PluginPackageError("required_package_file_unavailable") from None
    if type(manifest) is not dict:
        raise PluginPackageError("required_package_file_unavailable")
    return {key: value for key, value in manifest.items() if key not in EXCLUDED_MANIFEST_KEYS}


def prepare_plugin_package(
    source_root: Path,
    destination: Path,
) -> PreparedPluginPackage:
    """Atomically stage the exact public relay allowlist, never the repository tree."""

    if not isinstance(source_root, Path) or not isinstance(destination, Path):
        raise TypeError("source_root and destination must be Paths")
    if destination.exists():
        raise PluginPackageError("destination_already_exists")

    for relative_path in PACKAGE_PATHS:
        source = source_root / relative_path
        if not source.is_file() or source.is_symlink():
            raise PluginPackageError("required_package_file_unavailable")

    manifest = relay_manifest(source_root)

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging_root = Path(
            tempfile.mkdtemp(
                prefix=".course-compiler-package-",
                dir=destination.parent,
            )
        )
    except OSError:
        raise PluginPackageError("package_destination_unavailable") from None

    try:
        for relative_path in RELAY_PACKAGE_PATHS:
            target = staging_root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_root / relative_path, target)
        manifest_target = staging_root / MANIFEST_PATH
        manifest_target.parent.mkdir(parents=True, exist_ok=True)
        manifest_target.write_text(
            json.dumps(manifest, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        os.replace(staging_root, destination)
    except OSError:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise PluginPackageError("package_copy_failed") from None

    return PreparedPluginPackage(destination, PACKAGE_PATHS)


def verify_prepared_package(source_root: Path, destination: Path) -> tuple[str, ...]:
    """Return fixed, content-free failure codes for a staged package.

    Proves the properties a live relay activation depends on: exact-byte
    Skill/reference fidelity, full reference closure, no legacy app binding,
    and no stray files.
    """

    failures: list[str] = []
    if not destination.is_dir():
        return ("package_missing",)

    for relative_path in RELAY_PACKAGE_PATHS:
        staged = destination / relative_path
        if not staged.is_file() or staged.is_symlink():
            failures.append(f"missing_package_file:{relative_path.as_posix()}")
            continue
        if staged.read_bytes() != (source_root / relative_path).read_bytes():
            failures.append(f"package_file_not_byte_identical:{relative_path.as_posix()}")

    staged_manifest_path = destination / MANIFEST_PATH
    if not staged_manifest_path.is_file():
        failures.append("missing_package_file:" + MANIFEST_PATH.as_posix())
    else:
        try:
            staged_manifest = json.loads(staged_manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            staged_manifest = None
        if type(staged_manifest) is not dict:
            failures.append("package_manifest_invalid")
        else:
            for key in EXCLUDED_MANIFEST_KEYS:
                if key in staged_manifest:
                    failures.append(f"legacy_manifest_key_present:{key}")
            if staged_manifest != relay_manifest(source_root):
                failures.append("package_manifest_not_derived_from_source")

    for relative_path in EXCLUDED_PACKAGE_PATHS:
        if (destination / relative_path).exists():
            failures.append(f"excluded_path_present:{relative_path.as_posix()}")

    allowed = {path.as_posix() for path in PACKAGE_PATHS}
    for staged in sorted(destination.rglob("*")):
        if staged.is_dir():
            continue
        relative = staged.relative_to(destination).as_posix()
        if relative not in allowed:
            failures.append(f"unexpected_package_file:{relative}")

    return tuple(failures)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare the exact public Course Compiler relay plugin package.",
    )
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing prepared package at the destination.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify an already-prepared package without staging a new one.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    if not args.verify_only:
        if args.force and args.destination.exists():
            shutil.rmtree(args.destination, ignore_errors=True)
        try:
            prepare_plugin_package(args.source_root, args.destination)
        except PluginPackageError as error:
            print(json.dumps({"status": "package_failed", "code": str(error)}))
            return 2
    failures = verify_prepared_package(args.source_root, args.destination)
    if failures:
        print(json.dumps({"status": "package_invalid", "failures": list(failures)}, sort_keys=True))
        return 2
    print(
        json.dumps(
            {
                "status": "verified" if args.verify_only else "prepared",
                "destination": str(args.destination),
                "files": [path.as_posix() for path in PACKAGE_PATHS],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
