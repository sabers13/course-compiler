#!/usr/bin/env python3
"""Public showcase gate for Course Compiler v0.1.0-alpha.1.

Proves meaningful engineering evidence without depending on private
governance material:

- Required public files exist
- All Python sources parse
- The released module manifest validates
- No private / ignored / forbidden paths are tracked
- Public tests pass
- The synthetic XeLaTeX toolchain probe compiles if the TeX toolchain
  is available (the probe is non-fatal on hosts without TeX Live
  because the public mirror is meant to be cloneable from any platform)

This gate does NOT depend on internal task governance, state ledgers,
or implementation diaries.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REQUIRED_PUBLIC_FILES = (
    "LICENSE",
    "README.md",
    "Makefile",
    ".gitignore",
    "architecture/modules.yaml",
    "requirements-mcp.txt",
    "course_compiler/__init__.py",
    "course_compiler/app/__init__.py",
    "course_compiler/app/server.py",
    "ci/gate.py",
    "ci/module_manifest.py",
    "ci/module_graph.py",
    "ci/repo_map.py",
    "tests/fixtures/synthetic/cp0-toolchain-probe.tex",
    "docs/ARCHITECTURE.md",
    "docs/TOOLCHAIN.md",
    "scripts/prepare_course_compiler_plugin.py",
)

FORBIDDEN_PUBLIC_PATHS = (
    "tasks",
    "docs/STATE.md",
    "docs/CURRENT.md",
    "docs/ROADMAP.md",
    "docs/T056-OWNER-PRIVATE-RUN-CHECKLIST.md",
    "docs/AGENT-WORKFLOW.md",
    "docs/SESSION-HANDOFF.md",
    "docs/design",
    "docs/adr",
    "docs/PRODUCT.md",
    "AGENTS.md",
    "CLAUDE.md",
    "scripts/export_public_snapshot.py",
    "local-data",
    "local-artifacts",
    "build",
    ".env",
    ".commandcode",
    ".git",
)


def _ok(label: str) -> None:
    print(f"PASS: {label}")


def _fail(label: str, message: str) -> None:
    print(f"FAIL: {label}: {message}")
    sys.exit(1)


def check_required_files() -> None:
    missing = [path for path in REQUIRED_PUBLIC_FILES if not (ROOT / path).is_file()]
    if missing:
        _fail("required public files present", f"missing: {missing}")
    _ok("required public files present")


def check_python_sources_parse() -> None:
    bad: list[str] = []
    for py in ROOT.rglob("*.py"):
        rel = py.relative_to(ROOT).as_posix()
        if rel.startswith("local-data/"):
            continue
        if any(part in {"__pycache__", ".git"} for part in py.parts):
            continue
        try:
            ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            bad.append(f"{rel}: {exc}")
    if bad:
        _fail("python sources parse", "; ".join(bad))
    _ok("python sources parse")


def check_module_manifest_valid() -> None:
    sys.path.insert(0, str(ROOT / "ci"))
    try:
        from module_manifest import validate_manifest  # type: ignore
    except ImportError as exc:
        _fail("module manifest validator importable", str(exc))
    manifest_path = ROOT / "architecture" / "modules.yaml"
    try:
        validate_manifest(manifest_path, root=ROOT)
    except Exception as exc:  # noqa: BLE001
        _fail("released module manifest valid", str(exc))
    _ok("released module manifest valid")


def check_no_forbidden_paths_tracked() -> None:
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        _fail("git ls-files runs", str(exc))
    tracked = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    bad: list[str] = []
    for rel in tracked:
        for forbidden in FORBIDDEN_PUBLIC_PATHS:
            if rel == forbidden or rel.startswith(forbidden + "/"):
                bad.append(rel)
                break
    if bad:
        _fail("no private/governance paths tracked", f"forbidden tracked: {bad}")
    _ok("no private/governance paths tracked")


def check_ignored_roots_in_gitignore() -> None:
    gitignore = ROOT / ".gitignore"
    text = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    required = ["local-data", "local-artifacts", "build"]
    missing = [name for name in required if name not in text]
    if missing:
        _fail("ignored roots present in .gitignore", f"missing: {missing}")
    _ok("ignored roots present in .gitignore")


def run_public_tests() -> None:
    cmd = [
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        str(ROOT / "tests"),
        "-p",
        "test_*.py",
    ]
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        _fail("public test suite passes", f"unittest returned {result.returncode}")
    _ok("public test suite passes")


def run_xelatex_probe() -> None:
    if shutil.which("latexmk") is None or shutil.which("xelatex") is None:
        _ok("synthetic XeLaTeX probe compiles (skipped: toolchain unavailable)")
        return
    out_dir = ROOT / "local-artifacts" / "cp0" / "toolchain-probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "latexmk",
        "-xelatex",
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-file-line-error",
        "-g",
        f"-outdir={out_dir}",
        str(ROOT / "tests" / "fixtures" / "synthetic" / "cp0-toolchain-probe.tex"),
    ]
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        _fail("synthetic XeLaTeX probe compiles", f"latexmk returned {result.returncode}")
    _ok("synthetic XeLaTeX probe compiles")


def main() -> int:
    check_required_files()
    check_python_sources_parse()
    check_module_manifest_valid()
    check_no_forbidden_paths_tracked()
    check_ignored_roots_in_gitignore()
    run_public_tests()
    run_xelatex_probe()
    print("Public gate passed (7 checks).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
