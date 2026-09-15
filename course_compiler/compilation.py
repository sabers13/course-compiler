"""Local compilation of one complete logical TeX file to in-memory PDF bytes.

The compiler also accepts one canonical immutable bundle of that same exact
root TeX file plus generic companion files carrying exact bytes at safe
logical POSIX-style relative filenames. The compiler-input layer is generic:
it knows nothing about what a companion's bytes mean, where they came from,
or where they are intended to appear. Translating any higher-level domain
value into a compiler filename and byte payload belongs to a future
application layer, not here.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeAlias

from .assembly import LogicalTexFile


PDF_COMPILATION_PROFILE = "latexmk-xelatex-pdf/v1"
COMPILER_TIMEOUT_SECONDS = 120

__all__ = [
    "COMPILATION_IDENTITY",
    "COMPILER_TIMEOUT_SECONDS",
    "PDF_COMPILATION_PROFILE",
    "CompilationDiagnostic",
    "CompilationFailure",
    "CompilationProfileIdentity",
    "CompiledPdf",
    "CompilerCompanionFile",
    "CompilerInputBundle",
    "LocalPdfCompiler",
    "PdfCompilationResult",
    "compile_pdf",
    "compile_pdf_bundle",
]


_DIAGNOSTICS = {
    "invalid_compilation_input": (
        "input",
        "The logical TeX input is invalid.",
    ),
    "invalid_build_workspace": (
        "workspace",
        "The disposable compilation workspace is unavailable.",
    ),
    "compiler_unavailable": (
        "compiler",
        "The fixed local TeX compiler is unavailable.",
    ),
    "compiler_timeout": (
        "compiler",
        "The fixed local TeX compiler timed out.",
    ),
    "compiler_missing_glyph": ("compiler", "A required glyph is missing from the selected font."),
    "compiler_fatal_diagnostic": ("compiler", "TeX reported a fatal formatting or font error."),
    "compiler_overflow": ("compiler", "Document content exceeds its layout bounds; split long formulas or blocks."),
    "compiler_nonzero_exit": (
        "compiler",
        "The fixed local TeX compiler reported a failure.",
    ),
    "expected_pdf_missing": (
        "output",
        "The expected compiled PDF was not produced.",
    ),
    "expected_pdf_unreadable": (
        "output",
        "The expected compiled PDF could not be read.",
    ),
    "compilation_exception": (
        "adapter",
        "The local compilation adapter failed.",
    ),
}

_FORBIDDEN_FILENAME_CHARACTERS = ("\\", ":", "\x00")

_COMPILER_ARGV_PREFIX = (
    "latexmk",
    "-norc",
    "-xelatex",
    "-interaction=nonstopmode",
    "-halt-on-error",
    "-file-line-error",
    "-g",
)

# The fixed profile is byte-reproducible: XeTeX otherwise stamps the wall-clock
# time into `/CreationDate`, `/ModDate`, and the trailer `/ID`, so two runs of
# the same exact logical input would differ. Pinning the epoch makes identical
# input compile to identical bytes, which is what lets a deleted memoized cache
# be re-derived into the same PDF. The argv and `PDF_COMPILATION_PROFILE` are
# unchanged: this pins the environment the accepted profile runs in.
COMPILER_SOURCE_DATE_EPOCH = "1700000000"
_COMPILER_DETERMINISTIC_ENVIRONMENT = {
    "SOURCE_DATE_EPOCH": COMPILER_SOURCE_DATE_EPOCH,
    "FORCE_SOURCE_DATE": "1",
}


def _compiler_environment() -> dict[str, str]:
    """The inherited environment with the fixed reproducible-build pins applied."""

    environment = dict(os.environ)
    environment.update(_COMPILER_DETERMINISTIC_ENVIRONMENT)
    return environment


@dataclass(frozen=True, slots=True)
class CompilationProfileIdentity:
    """The one fixed local TeX-to-PDF compilation profile."""

    profile: Literal["latexmk-xelatex-pdf/v1"]

    def __post_init__(self) -> None:
        if self.profile != PDF_COMPILATION_PROFILE:
            raise ValueError("compilation profile is unsupported")


COMPILATION_IDENTITY = CompilationProfileIdentity(PDF_COMPILATION_PROFILE)


@dataclass(frozen=True, slots=True)
class CompilationDiagnostic:
    """One registered, fixed, content-safe compilation diagnostic."""

    code: str
    classification: Literal["input", "workspace", "compiler", "output", "adapter"]
    message: str

    def __post_init__(self) -> None:
        if self.code not in _DIAGNOSTICS:
            raise ValueError("compilation diagnostic code is not registered")
        classification, message = _DIAGNOSTICS[self.code]
        if self.classification != classification or self.message != message:
            raise ValueError("compilation diagnostic fields are not registered")


@dataclass(frozen=True, slots=True)
class CompiledPdf:
    """An ephemeral in-memory PDF produced by the fixed local compiler."""

    status: Literal["compiled"]
    compilation_profile: CompilationProfileIdentity
    logical_filename: str
    pdf_content: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if self.status != "compiled":
            raise ValueError("compiled PDF status is invalid")
        if not _is_valid_compilation_identity(self.compilation_profile):
            raise ValueError("compiled PDF profile is invalid")
        if not _is_valid_pdf_filename(self.logical_filename):
            raise ValueError("logical PDF filename is invalid")
        if type(self.pdf_content) is not bytes or not self.pdf_content:
            raise ValueError("compiled PDF content must be non-empty bytes")


@dataclass(frozen=True, slots=True)
class CompilationFailure:
    """A content-safe failure with no source, output, logs, or physical paths."""

    status: Literal["compilation_failed"]
    diagnostics: tuple[CompilationDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "compilation_failed":
            raise ValueError("compilation failure status is invalid")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) != 1
            or not _is_valid_compilation_diagnostic(self.diagnostics[0])
        ):
            raise ValueError("compilation failure diagnostics are invalid")


PdfCompilationResult: TypeAlias = CompiledPdf | CompilationFailure


@dataclass(frozen=True, slots=True)
class CompilerCompanionFile:
    """One generic compiler input file: a safe logical name and exact bytes."""

    logical_filename: str
    content_bytes: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not _is_valid_companion_filename(self.logical_filename):
            raise ValueError("compiler companion filename is invalid")
        if type(self.content_bytes) is not bytes:
            raise ValueError("compiler companion content must be exact bytes")


@dataclass(frozen=True, slots=True)
class CompilerInputBundle:
    """One exact root TeX file plus its canonical generic companion files."""

    root_tex: LogicalTexFile
    companion_files: tuple[CompilerCompanionFile, ...]

    def __post_init__(self) -> None:
        if not _is_valid_logical_tex_file(self.root_tex):
            raise ValueError("compiler input root TeX file is invalid")
        if type(self.companion_files) is not tuple or not all(
            _is_valid_companion_file(item) for item in self.companion_files
        ):
            raise ValueError("compiler input companion files are invalid")
        if _has_logical_path_collision(
            _complete_logical_path_set(self.root_tex, self.companion_files)
        ):
            raise ValueError("compiler input logical file set has a path collision")
        object.__setattr__(
            self,
            "companion_files",
            tuple(
                sorted(self.companion_files, key=lambda item: item.logical_filename)
            ),
        )


class LocalPdfCompiler:
    """Compile exactly one logical TeX file with no caller configuration."""

    __slots__ = ()
    identity = COMPILATION_IDENTITY

    def compile(self, logical_tex: LogicalTexFile) -> PdfCompilationResult:
        return compile_pdf(logical_tex)

    def compile_bundle(
        self, compiler_input: CompilerInputBundle
    ) -> PdfCompilationResult:
        return compile_pdf_bundle(compiler_input)


def compile_pdf(logical_tex: LogicalTexFile) -> PdfCompilationResult:
    """Compile one exact, revalidated T005 logical TeX file in isolation."""

    if type(logical_tex) is not LogicalTexFile:
        raise TypeError("logical_tex must be exactly LogicalTexFile")

    try:
        valid_input = _is_valid_logical_tex_file(logical_tex)
    except Exception:
        return _failure("compilation_exception")
    if not valid_input:
        return _failure("invalid_compilation_input")

    return _run_compilation(logical_tex, ())


def compile_pdf_bundle(compiler_input: CompilerInputBundle) -> PdfCompilationResult:
    """Compile one exact, revalidated root TeX file with its companion files."""

    if type(compiler_input) is not CompilerInputBundle:
        raise TypeError("compiler_input must be exactly CompilerInputBundle")

    try:
        valid_input = _is_valid_compiler_input_bundle(compiler_input)
    except Exception:
        return _failure("compilation_exception")
    if not valid_input:
        return _failure("invalid_compilation_input")

    return _run_compilation(compiler_input.root_tex, compiler_input.companion_files)


def _run_compilation(
    root_tex: LogicalTexFile,
    companion_files: tuple[CompilerCompanionFile, ...],
) -> PdfCompilationResult:
    """Stage and compile an already fully validated logical file set."""

    try:
        workspace_manager = _temporary_workspace()
    except Exception:
        return _failure("invalid_build_workspace")

    try:
        with workspace_manager as workspace_name:
            try:
                return _compile_in_workspace(
                    root_tex, companion_files, Path(workspace_name)
                )
            except Exception:
                return _failure("compilation_exception")
    except Exception:
        return _failure("invalid_build_workspace")


def _is_valid_logical_tex_file(value: object) -> bool:
    if type(value) is not LogicalTexFile:
        return False
    if not all(hasattr(value, name) for name in ("logical_filename", "tex_source")):
        return False
    if type(value.logical_filename) is not str or type(value.tex_source) is not str:
        return False
    try:
        LogicalTexFile(
            logical_filename=value.logical_filename,
            tex_source=value.tex_source,
        )
    except ValueError:
        return False
    return True


def _is_valid_companion_filename(value: object) -> bool:
    """Validate one logical POSIX-style relative compiler filename.

    The grammar is purely logical and host-independent: no `Path.resolve()`,
    no Unicode normalization, no case folding, no separator rewriting, and no
    segment collapsing. Exact string equality remains the filename identity.
    """

    if type(value) is not str or not value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    if any(character in value for character in _FORBIDDEN_FILENAME_CHARACTERS):
        return False
    if value.startswith("/") or value.endswith("/") or "//" in value:
        return False
    return not any(segment in ("", ".", "..") for segment in value.split("/"))


def _is_valid_companion_file(value: object) -> bool:
    if type(value) is not CompilerCompanionFile:
        return False
    if not all(hasattr(value, name) for name in ("logical_filename", "content_bytes")):
        return False
    try:
        CompilerCompanionFile(
            logical_filename=value.logical_filename,
            content_bytes=value.content_bytes,
        )
    except ValueError:
        return False
    return True


def _complete_logical_path_set(
    root_tex: LogicalTexFile,
    companion_files: tuple[CompilerCompanionFile, ...],
) -> tuple[str, ...]:
    """Every logical path the compiler workspace will own, including output."""

    return (
        root_tex.logical_filename,
        _logical_pdf_filename(root_tex.logical_filename),
        *(item.logical_filename for item in companion_files),
    )


def _has_logical_path_collision(paths: tuple[str, ...]) -> bool:
    """Reject duplicates and path-segment prefix conflicts in one file set.

    A conflict exists when one complete logical path is a path-segment prefix
    of another, because one name would then have to be both a file and a
    directory. `"image"` is deliberately not a prefix of `"images/plot.png"`.
    """

    if len(set(paths)) != len(paths):
        return True
    for index, outer in enumerate(paths):
        prefix = outer + "/"
        for other_index, inner in enumerate(paths):
            if index != other_index and inner.startswith(prefix):
                return True
    return False


def _is_valid_compiler_input_bundle(value: object) -> bool:
    if type(value) is not CompilerInputBundle:
        return False
    if not all(hasattr(value, name) for name in ("root_tex", "companion_files")):
        return False
    if type(value.companion_files) is not tuple:
        return False
    try:
        revalidated = CompilerInputBundle(
            root_tex=value.root_tex,
            companion_files=value.companion_files,
        )
    except ValueError:
        return False
    return revalidated.companion_files == value.companion_files


def _is_valid_compilation_identity(value: object) -> bool:
    if (
        type(value) is not CompilationProfileIdentity
        or not hasattr(value, "profile")
        or type(value.profile) is not str
    ):
        return False
    try:
        CompilationProfileIdentity(profile=value.profile)
    except ValueError:
        return False
    return True


def _is_valid_compilation_diagnostic(value: object) -> bool:
    if type(value) is not CompilationDiagnostic or not all(
        hasattr(value, name) for name in ("code", "classification", "message")
    ):
        return False
    if not all(
        type(item) is str
        for item in (value.code, value.classification, value.message)
    ):
        return False
    try:
        CompilationDiagnostic(
            code=value.code,
            classification=value.classification,
            message=value.message,
        )
    except ValueError:
        return False
    return True


def _temporary_workspace() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(prefix="course-compiler-")


def _compile_in_workspace(
    root_tex: LogicalTexFile,
    companion_files: tuple[CompilerCompanionFile, ...],
    workspace: Path,
) -> PdfCompilationResult:
    try:
        _stage_compiler_input(root_tex, companion_files, workspace)
    except OSError:
        return _failure("invalid_build_workspace")

    argv = (*_COMPILER_ARGV_PREFIX, root_tex.logical_filename)
    try:
        completed = subprocess.run(
            argv,
            cwd=workspace,
            check=False,
            capture_output=True,
            timeout=COMPILER_TIMEOUT_SECONDS,
            shell=False,
            env=_compiler_environment(),
        )
    except FileNotFoundError:
        return _failure("compiler_unavailable")
    except subprocess.TimeoutExpired:
        return _failure("compiler_timeout")
    except Exception:
        return _failure("compilation_exception")

    log_path = workspace / (root_tex.logical_filename[:-4] + ".log")
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    log += "\n" + (getattr(completed, "stdout", b"") or b"").decode("utf-8", errors="replace")
    log += "\n" + (getattr(completed, "stderr", b"") or b"").decode("utf-8", errors="replace")
    diagnostic = inspect_compiler_log(log)
    if diagnostic:
        return _failure(diagnostic)
    if completed.returncode != 0:
        return _failure("compiler_nonzero_exit")
    logical_pdf_filename = _logical_pdf_filename(root_tex.logical_filename)
    pdf_path = workspace / logical_pdf_filename
    if not pdf_path.is_file():
        return _failure("expected_pdf_missing")
    try:
        pdf_content = pdf_path.read_bytes()
    except OSError:
        return _failure("expected_pdf_unreadable")
    if not pdf_content:
        return _failure("expected_pdf_unreadable")
    return CompiledPdf(
        status="compiled",
        compilation_profile=COMPILATION_IDENTITY,
        logical_filename=logical_pdf_filename,
        pdf_content=pdf_content,
    )


def _stage_compiler_input(
    root_tex: LogicalTexFile,
    companion_files: tuple[CompilerCompanionFile, ...],
    workspace: Path,
) -> None:
    """Write the already validated logical file set into a fresh workspace.

    The root keeps its accepted T005/T006 UTF-8 TeX byte semantics. Each
    companion's exact bytes are written unchanged, with no decoding,
    re-encoding, newline normalization, hashing, or conversion.
    """

    root_path = workspace / root_tex.logical_filename
    root_path.write_bytes(root_tex.tex_source.encode("utf-8"))
    for companion in companion_files:
        companion_path = workspace / companion.logical_filename
        companion_path.parent.mkdir(parents=True, exist_ok=True)
        companion_path.write_bytes(companion.content_bytes)


def _logical_pdf_filename(logical_tex_filename: str) -> str:
    return logical_tex_filename[:-4] + ".pdf"


def _is_valid_pdf_filename(value: object) -> bool:
    if type(value) is not str or not value.endswith(".pdf"):
        return False
    tex_filename = value[:-4] + ".tex"
    try:
        LogicalTexFile(logical_filename=tex_filename, tex_source="")
    except ValueError:
        return False
    return True


def _failure(code: str) -> CompilationFailure:
    classification, message = _DIAGNOSTICS[code]
    return CompilationFailure(
        status="compilation_failed",
        diagnostics=(
            CompilationDiagnostic(
                code=code,
                classification=classification,
                message=message,
            ),
        ),
    )


def inspect_compiler_log(log: str) -> str | None:
    """Fixed diagnostics only: never expose raw logs or lecture text."""
    if "Missing character:" in log:
        return "compiler_missing_glyph"
    if re.search(r"(?m)^!|LaTeX Error:|Package .+ Error:|Font .+ not found|Emergency stop|Fatal error", log):
        return "compiler_fatal_diagnostic"
    if any(float(size) > 1.0 for size in re.findall(r"Overfull \\[hv]box \(([0-9.]+)pt too", log)):
        return "compiler_overflow"
    return None
