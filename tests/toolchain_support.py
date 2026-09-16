"""Shared optional-dependency detection for the public test suite.

This module is intentionally dependency-free (stdlib only) so every test
module can import it unconditionally, including on minimal clean-clone
hosts that lack the optional MCP SDK or the TeX/font toolchain.

Policy (public clone-quality contract):

- A missing *documented optional* prerequisite reports a unittest SKIP
  with a precise reason naming what is missing and how to enable it.
- A present-but-broken dependency or toolchain still FAILS. Detection
  uses :func:`importlib.util.find_spec` / executable probes only; it
  never catches arbitrary runtime errors.

MCP seam: ``requirements-mcp.txt`` is the documented way to enable the
MCP tests. PDF/full-toolchain tests require the documented TeX/font
toolchain (see ``docs/TOOLCHAIN.md``).
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import unittest

MCP_REQUIREMENTS_FILE = "requirements-mcp.txt"

MCP_MISSING_REASON = (
    "optional MCP SDK not installed; install " + MCP_REQUIREMENTS_FILE
)

HTTPCORE_MISSING_REASON = (
    "optional MCP httpcore runtime not installed; install "
    + MCP_REQUIREMENTS_FILE
)

UVICORN_MISSING_REASON = (
    "optional MCP HTTP test server (uvicorn) not installed; install "
    + MCP_REQUIREMENTS_FILE
)

# Binaries that must be on PATH for a real local PDF compile.
_TEX_BINARIES = ("latexmk", "xelatex", "kpsewhich")

# TeX packages exercised by the frozen assembly preamble plus the style
# file whose absence was observed on minimal clean-clone hosts.
_TEX_PACKAGES = (
    "lmodern.sty",
    "fontspec.sty",
    "geometry.sty",
    "amsmath.sty",
    "amssymb.sty",
    "graphicx.sty",
    "unicode-math.sty",
    "hyperref.sty",
    "enumitem.sty",
    "fvextra.sty",
    "upquote.sty",
    "xcolor.sty",
    "titlesec.sty",
    "setspace.sty",
    "parskip.sty",
    "needspace.sty",
    "array.sty",
    "tabularx.sty",
)

# Pinned fonts required by the frozen assembly preamble / gate contract.
_TEX_FONTS = (
    "Latin Modern Roman",
    "Noto Sans Mono",
    "Latin Modern Math",
)

# Binaries required for real PDF page preview / visual extraction tests.
_POPPLER_BINARIES = ("pdftoppm", "pdfinfo")


def find_missing_packages(*names: str) -> tuple[str, ...]:
    """Return the subset of *names* with no importable module spec."""
    return tuple(
        name for name in names if importlib.util.find_spec(name) is None
    )


def require_optional_packages(*names: str, reason: str) -> None:
    """Raise :class:`unittest.SkipTest` if any optional package is absent.

    Call this at module import time *before* importing the optional
    third-party packages. Only the absence of the named package(s)
    causes a skip; any other import failure propagates as an error.
    """
    missing = find_missing_packages(*names)
    if missing:
        raise unittest.SkipTest(f"{reason} (missing: {', '.join(missing)})")


def _kpsewhich_available(package: str) -> bool:
    try:
        result = subprocess.run(
            ["kpsewhich", package],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def _font_available(family: str) -> bool:
    try:
        result = subprocess.run(
            ["fc-match", family, "--format=%{family}\n"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    return family.lower() in result.stdout.lower()


def tex_toolchain_status() -> tuple[bool, str]:
    """Check the full documented TeX/font toolchain for real compiles.

    Returns ``(True, "")`` when a real XeLaTeX compile of the frozen
    preamble is expected to work, else ``(False, reason)`` naming the
    first missing prerequisite. A present-but-failing compiler is NOT
    reported here; such failures must FAIL the test.
    """
    for binary in _TEX_BINARIES:
        if shutil.which(binary) is None:
            return False, (
                f"full TeX toolchain prerequisite missing: {binary} "
                "not on PATH; see docs/TOOLCHAIN.md"
            )
    for package in _TEX_PACKAGES:
        if not _kpsewhich_available(package):
            return False, (
                f"full TeX toolchain prerequisite missing: {package} "
                "not found via kpsewhich; see docs/TOOLCHAIN.md"
            )
    for family in _TEX_FONTS:
        if not _font_available(family):
            return False, (
                f"full TeX toolchain prerequisite missing: font "
                f"'{family}' not available via fontconfig; "
                "see docs/TOOLCHAIN.md"
            )
    return True, ""


TEX_TOOLCHAIN_AVAILABLE, TEX_MISSING_REASON = tex_toolchain_status()


def poppler_status() -> tuple[bool, str]:
    """Check Poppler binaries for real PDF preview/extraction tests."""
    for binary in _POPPLER_BINARIES:
        if shutil.which(binary) is None:
            return False, (
                f"Poppler prerequisite missing: {binary} not on PATH; "
                "see docs/TOOLCHAIN.md"
            )
    return True, ""


POPPLER_AVAILABLE, POPPLER_MISSING_REASON = poppler_status()


def node_available() -> bool:
    """Check the Node.js binary for tests executing shipped JS."""
    return shutil.which("node") is not None


NODE_MISSING_REASON = (
    "node not on PATH; required to execute the shipped JavaScript "
    "decision under test"
)
