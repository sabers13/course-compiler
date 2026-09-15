"""Pure in-memory assembly for the fixed course TeX profile."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Literal, TypeAlias

from .contracts import RenderDiagnostic
from .rendering import (
    DocumentReference,
    LectureRenderResult,
    RejectedLecture,
    RenderedLecture,
    RendererFailure,
    RendererIdentity,
    StructuralMetrics,
)


COURSE_TEX_ASSEMBLY_PROFILE = "course-tex-assembly/v1"

__all__ = [
    "ASSEMBLY_IDENTITY",
    "COURSE_TEX_ASSEMBLY_PROFILE",
    "AssemblyDiagnostic",
    "AssemblyFailure",
    "AssemblyProfileIdentity",
    "CourseTexAssembler",
    "CourseTexAssemblyResult",
    "CourseTexAssemblySuccess",
    "LogicalTexFile",
    "assemble_course_tex",
]


_PREAMBLE = r"""\documentclass[11pt,a4paper]{article}
\usepackage{fontspec}
\usepackage[a4paper,margin=1in]{geometry}
\usepackage{amsmath,amssymb}
\usepackage{graphicx}
\newsavebox{\ccmathbox}
\newcommand{\ccfitmath}[1]{%
  \sbox{\ccmathbox}{$\displaystyle #1$}%
  \ifdim\wd\ccmathbox>1.25\linewidth
    \PackageError{coursecompiler}{Formula requires explicit line breaks}{}%
  \else
    \ifdim\wd\ccmathbox>\linewidth
      \resizebox{\linewidth}{!}{\usebox{\ccmathbox}}%
    \else\usebox{\ccmathbox}\fi
  \fi}
% Standalone inline math stays inline text math: render unchanged when it
% fits, bounded-resize (1.25 policy) when modestly oversized, otherwise
% break locally at relations/binops. Punctuation travels as #2, outside
% the math. Genuinely indivisible content still fails via overflow checks.
\newcommand{\ccfitinline}[2]{%
  \par\noindent
  \sbox{\ccmathbox}{\(#1\)#2}%
  \ifdim\wd\ccmathbox>\linewidth
    \ifdim\wd\ccmathbox>1.25\linewidth
      \begingroup\relpenalty=0\binoppenalty=0\(#1\)#2\endgroup
    \else
      \resizebox{\linewidth}{!}{\usebox{\ccmathbox}}%
    \fi
  \else\usebox{\ccmathbox}\fi}

\usepackage{unicode-math}
\usepackage{hyperref}
\usepackage{enumitem}
\usepackage{fvextra}
\usepackage{upquote}
\usepackage{xcolor}
\usepackage{titlesec}
\usepackage{setspace}
\usepackage{parskip}
\usepackage{needspace}
\usepackage{array,tabularx}
\setmainfont{Latin Modern Roman}
\setmonofont{Noto Sans Mono}
\setmathfont{Latin Modern Math}
% Fallback flexibility for paragraphs containing several inline-code
% identifiers. Kept at the long-standing 4em value; the l9/l11 failure
% class is addressed by targeted breakpoints inside inline-code spans
% rather than by loosening every paragraph globally. Applies only after
% normal line-breaking has failed.
\setlength{\emergencystretch}{4em}
\widowpenalty=10000
\clubpenalty=10000
\displaywidowpenalty=10000
\hypersetup{colorlinks=true,linkcolor=blue,urlcolor=blue}
\setlist[itemize]{leftmargin=2em}
\setlist[enumerate]{leftmargin=2.5em}
\newcolumntype{Y}{>{\raggedright\arraybackslash}X}
% ``samepage`` still permits a break immediately before display math.  A
% minipage is a real indivisible vertical box, so short explanatory text,
% headings, and the equations they introduce cannot be separated.
\newenvironment{keeptogether}
  {\par\noindent\begin{minipage}{\linewidth}%
   \setlength{\parskip}{0.5\baselineskip plus 2pt}}
  {\end{minipage}\par}
\makeatletter
\@addtoreset{section}{part}
\makeatother
\renewcommand{\thesection}{\arabic{part}.\arabic{section}}
\renewcommand{\thesubsection}{\thesection.\arabic{subsection}}
\renewcommand{\thesubsubsection}{\thesubsection.\arabic{subsubsection}}
\titleformat{\section}{\normalfont\Large\bfseries}{\thesection}{0.75em}{}
\titleformat{\subsection}{\normalfont\large\bfseries}{\thesubsection}{0.75em}{}
\titleformat{\subsubsection}{\normalfont\normalsize\bfseries}{\thesubsubsection}{0.75em}{}
\begin{document}
"""

_CLASSIFICATIONS = {
    "invalid_reference_collection": "input",
    "invalid_result_collection": "input",
    "empty_reference_collection": "input",
    "invalid_document_reference": "input",
    "invalid_render_result": "input",
    "duplicate_document_id": "collection",
    "duplicate_document_order": "collection",
    "non_contiguous_document_orders": "collection",
    "rejected_lecture_result": "render",
    "renderer_failure_result": "render",
    "mixed_renderer_identity": "render",
    "extra_render_result": "binding",
    "duplicate_render_result_binding": "binding",
    "reference_result_mismatch": "binding",
    "missing_render_result": "binding",
    "assembly_exception": "assembly",
}
_LOGICAL_FILENAME_RE = re.compile(r"L[0-9]{2,}\.tex\Z")


@dataclass(frozen=True, slots=True)
class AssemblyProfileIdentity:
    """The one fixed complete-document assembly profile."""

    profile: Literal["course-tex-assembly/v1"]

    def __post_init__(self) -> None:
        if self.profile != COURSE_TEX_ASSEMBLY_PROFILE:
            raise ValueError("assembly profile is unsupported")


ASSEMBLY_IDENTITY = AssemblyProfileIdentity(COURSE_TEX_ASSEMBLY_PROFILE)


@dataclass(frozen=True, slots=True)
class AssemblyDiagnostic:
    """A fixed, content-free assembly diagnostic."""

    code: str
    classification: Literal["input", "collection", "render", "binding", "assembly"]
    position: int | None

    def __post_init__(self) -> None:
        if self.code not in _CLASSIFICATIONS:
            raise ValueError("assembly diagnostic code is not registered")
        if self.classification != _CLASSIFICATIONS[self.code]:
            raise ValueError("assembly diagnostic classification is not registered")
        if self.position is not None and (
            isinstance(self.position, bool)
            or not isinstance(self.position, int)
            or self.position < 1
        ):
            raise ValueError("assembly diagnostic position must be a one-based integer or None")


@dataclass(frozen=True, slots=True)
class LogicalTexFile:
    """One in-memory TeX source with a deterministic logical filename."""

    logical_filename: str
    tex_source: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.logical_filename, str) or (
            self.logical_filename != "Course_Study_Lectures.tex"
            and _LOGICAL_FILENAME_RE.fullmatch(self.logical_filename) is None
        ):
            raise ValueError("logical TeX filename is invalid")
        if not isinstance(self.tex_source, str):
            raise ValueError("logical TeX source must be a string")
        try:
            self.tex_source.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("logical TeX source must be UTF-8 encodable") from None


@dataclass(frozen=True, slots=True)
class CourseTexAssemblySuccess:
    status: Literal["assembled"]
    assembly_profile: AssemblyProfileIdentity
    renderer: RendererIdentity
    combined: LogicalTexFile
    lectures: tuple[LogicalTexFile, ...]
    warnings: tuple[RenderDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "assembled":
            raise ValueError("assembly success status is invalid")
        if type(self.assembly_profile) is not AssemblyProfileIdentity:
            raise ValueError("assembly success profile is invalid")
        if type(self.renderer) is not RendererIdentity:
            raise ValueError("assembly success renderer is invalid")
        if type(self.combined) is not LogicalTexFile:
            raise ValueError("assembly success combined file is invalid")
        if (
            type(self.lectures) is not tuple
            or not self.lectures
            or any(type(item) is not LogicalTexFile for item in self.lectures)
        ):
            raise ValueError("assembly success lectures are invalid")
        if type(self.warnings) is not tuple or any(
            type(item) is not RenderDiagnostic or item.severity != "warning"
            for item in self.warnings
        ):
            raise ValueError("assembly success warnings are invalid")


@dataclass(frozen=True, slots=True)
class AssemblyFailure:
    status: Literal["assembly_failed"]
    diagnostics: tuple[AssemblyDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "assembly_failed":
            raise ValueError("assembly failure status is invalid")
        if type(self.diagnostics) is not tuple or not self.diagnostics or any(
            type(item) is not AssemblyDiagnostic for item in self.diagnostics
        ):
            raise ValueError("assembly failure diagnostics are invalid")


CourseTexAssemblyResult: TypeAlias = CourseTexAssemblySuccess | AssemblyFailure


class CourseTexAssembler:
    """Assemble validated rendered lectures into complete logical TeX files."""

    __slots__ = ()
    identity = ASSEMBLY_IDENTITY

    def assemble(
        self,
        references: tuple[DocumentReference, ...],
        results: tuple[LectureRenderResult, ...],
    ) -> CourseTexAssemblyResult:
        return assemble_course_tex(references, results)


def assemble_course_tex(
    references: tuple[DocumentReference, ...],
    results: tuple[LectureRenderResult, ...],
) -> CourseTexAssemblyResult:
    """Fail-closed assembly from approved references and renderer results."""

    try:
        failure = _validate_collection(references, results)
        if failure is not None:
            return failure
        return _assemble_validated(references, results)
    except Exception:
        return _failure("assembly_exception")


def _validate_collection(
    references: object, results: object
) -> AssemblyFailure | None:
    if type(references) is not tuple:
        return _failure("invalid_reference_collection")
    if type(results) is not tuple:
        return _failure("invalid_result_collection")
    if not references:
        return _failure("empty_reference_collection")
    for position, reference in enumerate(references, start=1):
        if not _is_exact_document_reference(reference):
            return _failure("invalid_document_reference", position)
    duplicate_id = _first_duplicate_position(reference.document_id for reference in references)
    if duplicate_id is not None:
        return _failure("duplicate_document_id", duplicate_id)
    duplicate_order = _first_duplicate_position(reference.order for reference in references)
    if duplicate_order is not None:
        return _failure("duplicate_document_order", duplicate_order)
    if tuple(sorted(reference.order for reference in references)) != tuple(range(1, len(references) + 1)):
        return _failure("non_contiguous_document_orders")
    for position, result in enumerate(results, start=1):
        if type(result) is RejectedLecture:
            return _failure("rejected_lecture_result", position)
        if type(result) is RendererFailure:
            return _failure("renderer_failure_result", position)
        if not _is_exact_rendered_lecture(result):
            return _failure("invalid_render_result", position)
    if results:
        renderer = results[0].renderer
        for position, result in enumerate(results[1:], start=2):
            if result.renderer != renderer:
                return _failure("mixed_renderer_identity", position)
    references_by_id = {reference.document_id: reference for reference in references}
    bound_ids: set[str] = set()
    for position, result in enumerate(results, start=1):
        expected = references_by_id.get(result.document.document_id)
        if expected is None:
            return _failure("extra_render_result", position)
        if result.document != expected:
            return _failure("reference_result_mismatch", position)
        if expected.document_id in bound_ids:
            return _failure("duplicate_render_result_binding", position)
        bound_ids.add(expected.document_id)
    if len(bound_ids) != len(references):
        return _failure("missing_render_result")
    return None


def _is_exact_document_reference(value: object) -> bool:
    if type(value) is not DocumentReference or not _has_required_fields(
        value,
        ("contract_version", "document_id", "order", "content_sha256"),
    ):
        return False
    if not (
        type(value.contract_version) is str
        and type(value.document_id) is str
        and type(value.order) is int
        and type(value.content_sha256) is str
    ):
        return False
    try:
        DocumentReference(
            contract_version=value.contract_version,
            document_id=value.document_id,
            order=value.order,
            content_sha256=value.content_sha256,
        )
    except ValueError:
        return False
    return True


def _is_exact_renderer_identity(value: object) -> bool:
    if type(value) is not RendererIdentity or not _has_required_fields(
        value,
        (
            "renderer_name",
            "renderer_version",
            "contract_version",
            "render_profile",
        ),
    ):
        return False
    if not (
        type(value.renderer_name) is str
        and type(value.renderer_version) is str
        and type(value.contract_version) is str
        and type(value.render_profile) is str
    ):
        return False
    try:
        RendererIdentity(
            renderer_name=value.renderer_name,
            renderer_version=value.renderer_version,
            contract_version=value.contract_version,
            render_profile=value.render_profile,
        )
    except ValueError:
        return False
    return True


def _is_exact_metrics(value: object) -> bool:
    field_names = (
        "input_characters",
        "input_lines",
        "input_headings",
        "output_headings",
        "input_code_blocks",
        "output_code_blocks",
        "input_display_math_blocks",
        "output_display_math_blocks",
        "input_tables",
        "output_tables",
    )
    if type(value) is not StructuralMetrics or not _has_required_fields(
        value, field_names
    ):
        return False
    if not all(
        type(getattr(value, field_name)) is int
        for field_name in field_names
    ):
        return False
    try:
        StructuralMetrics(
            input_characters=value.input_characters,
            input_lines=value.input_lines,
            input_headings=value.input_headings,
            output_headings=value.output_headings,
            input_code_blocks=value.input_code_blocks,
            output_code_blocks=value.output_code_blocks,
            input_display_math_blocks=value.input_display_math_blocks,
            output_display_math_blocks=value.output_display_math_blocks,
            input_tables=value.input_tables,
            output_tables=value.output_tables,
        )
    except ValueError:
        return False
    return True


def _is_exact_diagnostic(value: object) -> bool:
    if type(value) is not RenderDiagnostic or not _has_required_fields(
        value, ("code", "severity", "stage", "line")
    ):
        return False
    if not (
        type(value.code) is str
        and type(value.severity) is str
        and type(value.stage) is str
        and (value.line is None or type(value.line) is int)
    ):
        return False
    try:
        RenderDiagnostic(
            code=value.code,
            severity=value.severity,
            stage=value.stage,
            line=value.line,
        )
    except ValueError:
        return False
    return True


def _is_exact_rendered_lecture(value: object) -> bool:
    if type(value) is not RenderedLecture or not _has_required_fields(
        value,
        ("status", "document", "renderer", "tex_fragment", "metrics", "diagnostics"),
    ):
        return False
    if (
        type(value.status) is not str
        or value.status not in ("rendered", "rendered_with_warnings")
        or not _is_exact_document_reference(value.document)
        or not _is_exact_renderer_identity(value.renderer)
        or type(value.tex_fragment) is not str
        or not _is_exact_metrics(value.metrics)
        or type(value.diagnostics) is not tuple
        or any(not _is_exact_diagnostic(diagnostic) for diagnostic in value.diagnostics)
    ):
        return False
    try:
        RenderedLecture(
            status=value.status,
            document=value.document,
            renderer=value.renderer,
            tex_fragment=value.tex_fragment,
            metrics=value.metrics,
            diagnostics=value.diagnostics,
        )
    except ValueError:
        return False
    return True


def _has_required_fields(value: object, field_names: tuple[str, ...]) -> bool:
    return all(hasattr(value, field_name) for field_name in field_names)


def _assemble_validated(
    references: tuple[DocumentReference, ...], results: tuple[LectureRenderResult, ...]
) -> CourseTexAssemblySuccess:
    ordered_references = tuple(sorted(references, key=lambda reference: reference.order))
    results_by_id = {result.document.document_id: result for result in results}
    ordered_results = tuple(results_by_id[reference.document_id] for reference in ordered_references)
    renderer = ordered_results[0].renderer
    lectures = tuple(
        LogicalTexFile(
            logical_filename=_lecture_filename(reference.order),
            tex_source=_PREAMBLE + result.tex_fragment + "\\end{document}\n",
        )
        for reference, result in zip(ordered_references, ordered_results)
    )
    combined = LogicalTexFile(
        logical_filename="Course_Study_Lectures.tex",
        tex_source=(
            _PREAMBLE
            + "\\tableofcontents\n\\newpage\n"
            + "".join(
                "\\clearpage\n" + result.tex_fragment
                for result in ordered_results
            )
            + "\\end{document}\n"
        ),
    )
    warnings = tuple(
        diagnostic
        for result in ordered_results
        for diagnostic in result.diagnostics
    )
    return CourseTexAssemblySuccess(
        status="assembled",
        assembly_profile=ASSEMBLY_IDENTITY,
        renderer=renderer,
        combined=combined,
        lectures=lectures,
        warnings=warnings,
    )


def _first_duplicate_position(values: object) -> int | None:
    seen: set[object] = set()
    for position, value in enumerate(values, start=1):
        if value in seen:
            return position
        seen.add(value)
    return None


def _lecture_filename(order: int) -> str:
    return f"L{order:0{max(2, len(str(order)))}d}.tex"


def _failure(code: str, position: int | None = None) -> AssemblyFailure:
    return AssemblyFailure(
        status="assembly_failed",
        diagnostics=(
            AssemblyDiagnostic(
                code=code,
                classification=_CLASSIFICATIONS[code],
                position=position,
            ),
        ),
    )
