"""Pure renderer for the frozen legacy Markdown-to-TeX profile."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Mapping

from .math_format import is_quote_line, strip_quote, validate_explicit_math, wrap_display

from .contracts import (
    DOCUMENT_CONTRACT_VERSION,
    LectureDocument,
    RenderDiagnostic,
    has_validation_errors,
    validate_document,
)
from .rendering import (
    LEGACY_RENDER_PROFILE,
    LectureRenderResult,
    RejectedLecture,
    RenderedLecture,
    RendererFailure,
    RendererIdentity,
    StructuralMetrics,
    document_reference,
    structural_postconditions_match,
)


__all__ = ["LegacyMarkdownTexRenderer"]


_IDENTITY = RendererIdentity(
    renderer_name="course-compiler-legacy-markdown-to-tex",
    renderer_version="2.0.0",
    contract_version=DOCUMENT_CONTRACT_VERSION,
    render_profile=LEGACY_RENDER_PROFILE,
)

_LATEX_SPECIALS = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_DISPLAY_STARTS = frozenset(("[", r"\[", "# [", "$$"))
_DISPLAY_ENDS = frozenset(("]", r"\]", "$$"))
_CODE_FENCE = re.compile(r"^(```+|~~~+)(.*)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_ORDERED_ITEM = re.compile(r"^(\s*)(\d+)\.(?:\s+(.*))?$")
_UNORDERED_ITEM = re.compile(r"^(\s*)[*-]\s+(.*)$")
_INLINE_CODE = re.compile(r"`([^`]+)`")
_STRONG = re.compile(r"\*\*(.+?)\*\*")
_EMPHASIS = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_UNICODE_MATH = re.compile(r"β[₀₁₂₃₄₅₆₇₈₉ⱼ]*|[A-Za-z][₀₁₂₃₄₅₆₇₈₉ⱼ]+")
_UNICODE_SUBSCRIPTS = str.maketrans("₀₁₂₃₄₅₆₇₈₉ⱼ", "0123456789j")
_CONTINUATION = re.compile(
    r"^(?:and|or|if|is|are|also|thus|therefore|then|hence|equivalently|provided|"
    r"where|which|we\s+have|by\s+symmetry|"
    r"the\s+(?:total|resulting|corresponding|final)|"
    r"correctly\s+classified|"
    r"this\s+is\s+the\s+(?:smallest|largest|minimum|maximum)\b[^:;,.]*[.:,]?|"
    r"it\s+does\s+not\s+(?:necessarily\s+)?follow)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _Conversion:
    tex_fragment: str
    metrics: StructuralMetrics
    safe_offsets: frozenset[int] = frozenset()


@dataclass(slots=True)
class _Counts:
    input_headings: int = 0
    output_headings: int = 0
    input_code_blocks: int = 0
    output_code_blocks: int = 0
    input_display_math_blocks: int = 0
    output_display_math_blocks: int = 0
    input_tables: int = 0
    output_tables: int = 0

    def metrics(self, source_text: str) -> StructuralMetrics:
        return StructuralMetrics(
            input_characters=len(source_text),
            input_lines=len(source_text.splitlines()),
            input_headings=self.input_headings,
            output_headings=self.output_headings,
            input_code_blocks=self.input_code_blocks,
            output_code_blocks=self.output_code_blocks,
            input_display_math_blocks=self.input_display_math_blocks,
            output_display_math_blocks=self.output_display_math_blocks,
            input_tables=self.input_tables,
            output_tables=self.output_tables,
        )


class LegacyMarkdownTexRenderer:
    """Render one validated lecture with the fixed legacy profile."""

    __slots__ = ()
    identity = _IDENTITY

    def render(self, document: LectureDocument) -> LectureRenderResult:
        if type(document) is not LectureDocument:
            raise TypeError("document must be exactly LectureDocument")

        diagnostics = validate_document(document)
        if has_validation_errors(diagnostics):
            errors = tuple(item for item in diagnostics if item.severity == "error")
            return RejectedLecture(
                status="validation_failed",
                document=document_reference(document),
                diagnostics=errors,
            )

        try:
            validate_explicit_math(document.source_text)
        except ValueError:
            return RendererFailure(status="render_failed", document=document_reference(document),
                renderer=self.identity, diagnostics=(_render_error("invalid_explicit_math"),))

        quoted_display_warnings = _unclosed_quoted_display_warnings(
            document.source_text
        )

        reference = document_reference(document)
        if reference is None:
            raise RuntimeError("validated document reference is unavailable")

        try:
            converted = _convert_source(document.source_text)
        except Exception:
            return RendererFailure(
                status="render_failed",
                document=reference,
                renderer=self.identity,
                diagnostics=(_render_error("renderer_exception"),),
            )

        if not structural_postconditions_match(converted.metrics):
            return RendererFailure(
                status="render_failed",
                document=reference,
                renderer=self.identity,
                diagnostics=(_render_error("structural_postcondition_mismatch"),),
            )

        warnings = tuple(
            sorted(
                (
                    *(item for item in diagnostics if item.severity == "warning"),
                    *quoted_display_warnings,
                ),
                key=lambda item: (item.code, item.line or 0),
            )
        )
        return RenderedLecture(
            status="rendered_with_warnings" if warnings else "rendered",
            document=reference,
            renderer=self.identity,
            tex_fragment=converted.tex_fragment,
            metrics=converted.metrics,
            diagnostics=warnings,
        )


def _render_error(code: str) -> RenderDiagnostic:
    return RenderDiagnostic(code=code, severity="error", stage="render", line=None)


def _escape_text(text: str) -> str:
    return "".join(r"\$" if char == "$" else _LATEX_SPECIALS.get(char, char) for char in text)


def _is_mathish(content: str) -> bool:
    value = content.strip()
    if not value:
        return False
    if any(token in value for token in ("\\", "->", "<-", ":=", "<=", ">=", "^", "_", "{", "}")):
        return True
    if re.search(r"[=<>|]|do\s*\(", value):
        return True
    if re.fullmatch(r"[A-Za-z](?:\s*,\s*[A-Za-z])+", value):
        return True
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_']*", value):
        return len(value) <= 2 or bool(re.search(r"[0-9_']", value))
    return bool(
        re.fullmatch(r"[A-Za-z0-9_\\^{}+*/<>=.,:;!|()\[\]%'\-]+", value)
        and re.search(r"[0-9_\\^{}+*/<>=|%()\[\]\-]", value)
    )


def _replace_parenthesized(text: str, replacement: Callable[[str], str]) -> str:
    output: list[str] = []
    index = 0
    while index < len(text):
        if text[index] != "(":
            output.append(text[index])
            index += 1
            continue
        depth = 1
        end = index + 1
        while end < len(text) and depth:
            if text[end] == "(":
                depth += 1
            elif text[end] == ")":
                depth -= 1
            end += 1
        if depth:
            output.append(text[index])
            index += 1
            continue
        output.append(replacement(text[index + 1 : end - 1]))
        index = end
    return "".join(output)


def _convert_inline(text: str) -> str:
    # Protect math/code before emphasis while retaining Markdown nesting.
    saved = {}
    prefix = "CCPROTECTED"
    while prefix in text:
        prefix += "X"
    def protect(match):
        token = prefix + str(len(saved)) + "END"
        saved[token] = (match[0] if match[1] is not None else
            _inline_code_tex(match[0][1:-1]))
        return token
    protected = re.sub(r"`[^`]+`|\\\((.*?)\\\)", protect, text)
    output = _convert_inline_legacy(protected)
    for token, value in saved.items():
        output = output.replace(token, value)
    return output


def _convert_inline_legacy(text: str) -> str:
    output: list[str] = []
    cursor = 0
    for match in _STRONG.finditer(text):
        output.append(_convert_inline_code(text[cursor : match.start()]))
        output.append(r"\textbf{" + _convert_inline_code(match.group(1)) + "}")
        cursor = match.end()
    output.append(_convert_inline_code(text[cursor:]))
    return "".join(output)


def _convert_inline_code(text: str) -> str:
    output: list[str] = []
    cursor = 0
    for match in _INLINE_CODE.finditer(text):
        output.append(_convert_inline_prose(text[cursor : match.start()]))
        output.append(_inline_code_tex(match.group(1)))
        cursor = match.end()
    output.append(_convert_inline_prose(text[cursor:]))
    return "".join(output)


def _inline_code_tex(value: str) -> str:
    """Permit line breaks at literal code-identifier delimiters only."""
    escaped = _escape_text(value)
    for delimiter in ("-", "/", "."):
        escaped = escaped.replace(delimiter, delimiter + r"\allowbreak{}")
    return r"\texttt{" + escaped + "}"


def _code_paragraph_tex(converted: str) -> str:
    """Scope extra emergency flexibility to one inline-code paragraph.

    Applies only to a top-level ordinary paragraph whose source line
    contains Markdown inline-code spans. The global preamble stays at
    4em; the paragraph is line-broken with local 12em inside this group.
    """
    return (
        "\\begingroup\n"
        "\\setlength{\\emergencystretch}{12em}\n"
        + converted
        + "\n\\par\n\\endgroup\n\n"
    )


def _convert_inline_prose(text: str) -> str:
    placeholders: dict[str, str] = {}

    def replace_parentheses(content: str) -> str:
        if not _is_mathish(content):
            return "(" + _replace_parenthesized(content, replace_parentheses) + ")"
        token = f"XXMATH{len(placeholders)}XX"
        placeholders[token] = r"\(" + _normalize_math(content).replace("#", r"\#") + r"\)"
        return token

    value = _replace_parenthesized(text, replace_parentheses)

    def replace_unicode(match: re.Match[str]) -> str:
        token = f"XXMATH{len(placeholders)}XX"
        raw = match.group(0)
        base = r"\beta" if raw.startswith("β") else raw[0]
        subscript = raw[1:].translate(_UNICODE_SUBSCRIPTS)
        placeholders[token] = rf"\({base}{'_' + subscript if subscript else ''}\)"
        return token

    value = _UNICODE_MATH.sub(replace_unicode, value)
    value = _escape_text(value).replace(r"\_", r"\_\allowbreak{}")
    value = _EMPHASIS.sub(lambda match: r"\emph{" + match.group(1) + "}", value)
    for token, replacement in placeholders.items():
        value = value.replace(token, replacement)
    return value


def _find_group_end(text: str, opening_brace: int) -> int | None:
    depth = 1
    index = opening_brace + 1
    while index < len(text):
        if text[index] in "{}" and (index == 0 or text[index - 1] != "\\"):
            depth += 1 if text[index] == "{" else -1
            if depth == 0:
                return index
        index += 1
    return None


def _replace_visible_group(
    text: str,
    pattern: re.Pattern[str],
    prefix: str | Callable[[re.Match[str]], str],
) -> str:
    output: list[str] = []
    cursor = 0
    while match := pattern.search(text, cursor):
        opening = match.end() - 1
        closing = _find_group_end(text, opening)
        if closing is None:
            break
        output.append(text[cursor : match.start()])
        replacement = prefix(match) if callable(prefix) else prefix
        output.append(replacement + r"\{" + text[opening + 1 : closing] + r"\}")
        cursor = closing + 1
    output.append(text[cursor:])
    return "".join(output)


def _escape_texttt_specials(text: str) -> str:
    pattern = re.compile(r"\\texttt\s*\{")
    output: list[str] = []
    cursor = 0
    while match := pattern.search(text, cursor):
        opening = match.end() - 1
        closing = _find_group_end(text, opening)
        if closing is None:
            break
        body = re.sub(r"(?<!\\)([_#&$])", r"\\\1", text[opening + 1 : closing])
        output.extend((text[cursor : opening + 1], body, "}"))
        cursor = closing + 1
    output.append(text[cursor:])
    return "".join(output)


def _repair_text_math_symbol(text: str, command: str) -> str:
    pattern = re.compile(r"\\text\{([^{}]*?)\(" + re.escape(command) + r"\)([^{}]*?)\}")

    def replacement(match: re.Match[str]) -> str:
        before, after = match.groups()
        value = (r"\text{" + before + "}") if before else ""
        value += f"({command})"
        return value + ((r"\text{" + after + "}") if after else "")

    return pattern.sub(replacement, text)


def _normalize_math(content: str) -> str:
    value = content.replace(")*+", ")_+")
    value = value.replace(r"\hat\alpha*\lambda", r"\hat\alpha_\lambda")
    value = value.replace(r"\boldsymbol", r"\symbf")
    value = re.sub(
        r"(\\operatorname\{err\})\*\{(\\mathrm\{(?:test|train)\})\}",
        r"\1_{\2}",
        value,
    )
    value = re.sub(r"(?<=[A-Za-z0-9}])\s*,\s*(?=\\(?:mathbf|mathbb)\s*1)", " ", value)
    value = re.sub(r"(?<=\d),(?=b\^\{\[\d+\]\})", r"\\,", value)
    value = re.sub(r"(?<!\\),d(?=x\b)", r"\\,d", value)
    value = value.replace(r",\middle|,", r"\middle|")
    value = value.replace(r"\delta,\operatorname{sign}", r"\delta\operatorname{sign}")
    value = re.sub(r"(?<!\\);\\(times|cdot);", r"\\;\\\1\\;", value)
    value = re.sub(r"(?<!\\)%", r"\\%", value)
    value = _escape_texttt_specials(value)
    value = _repair_text_math_symbol(value, r"\ell")
    value = _replace_visible_group(
        value,
        re.compile(r"\\(?P<style>mathbf|mathbb)\s*1\s*\{"),
        lambda match: rf"\{match.group('style')}{{1}}",
    )
    value = _replace_visible_group(
        value,
        re.compile(r"\\(?P<relation>in|notin|subseteq)\s*\{"),
        lambda match: rf"\{match.group('relation')}",
    )
    value = _replace_visible_group(value, re.compile(r"=\s*\{(?=[^}])"), "=")
    value = re.sub(
        r"(?<![\\A-Za-z])\{([+-]?\d+(?:\.\d+)?(?:\s*,\s*[+-]?\d+(?:\.\d+)?)+)\}",
        r"\\{\1\\}",
        value,
    )
    for pattern, prefix in (
        (re.compile(r"\\min\s*\{"), r"\min"),
        (re.compile(r"\\operatorname\{median\}\s*\{"), r"\operatorname{median}"),
        (re.compile(r"\\text\{uniform distribution on \}\s*\{"), r"\text{uniform distribution on }"),
    ):
        value = _replace_visible_group(value, pattern, prefix)
    return value


def _normalize_display_line(line: str) -> str:
    value = line.strip()
    if re.fullmatch(r"-{2,}", value) or value == "*":
        value = "-"
    value = value.replace(r"\left{", r"\left\{").replace(r"\right}", r"\right\}")
    value = re.sub(r"(?<!\\)\\(?=x_)", r"\\\\", value)
    value = re.sub(r"(?<!\\)\\\\(?=(?:theta|vdots)(?:_|\b))", r"\\\\\\", value)
    value = re.sub(r"(?<=[0-9}])\\(?=[+-]?\d)", r"\\\\", value)
    value = re.sub(
        r"(?<!\\)(?P<punct>[,.;]?)\s*\[(?P<space>\d+(?:\.\d+)?(?:pt|mm|cm|em|ex))\]\s*$",
        lambda match: match.group("punct") + r"\\" + f"[{match.group('space')}]",
        value,
    )
    value = re.sub(r"(?<!\\)\\\s*$", r"\\\\", value)
    return _normalize_math(value).replace("#", r"\#")


def _decode_math_lines(lines: list[str]) -> list[str]:
    decoded: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if match := re.fullmatch(r"##\s+(.+)", line):
            decoded.extend((match.group(1).strip(), "-"))
        elif match := re.fullmatch(r"#\s+(.+)", line):
            decoded.extend((match.group(1).strip(), "="))
        elif re.fullmatch(r"={2,}", line):
            decoded.append("=")
        elif re.fullmatch(r"-{2,}", line) or line == "*":
            decoded.append("-")
        else:
            decoded.append(line)
    return decoded


def _shrink_long_text_math(lines: list[str]) -> list[str]:
    length = sum(
        len(match.group(1))
        for line in lines
        for match in re.finditer(r"\\text\{([^{}]*)\}", line)
    )
    threshold = 75 if any(r"\boxed{" in line for line in lines) else 85
    if length < threshold:
        return lines
    return [line.replace(r"\text{", r"\text{\small ", 1) if r"\text{" in line else line for line in lines]


def _brace_delta(line: str) -> int:
    value = re.sub(r"\\[{}]", "", line)
    return value.count("{") - value.count("}")


def _collect_fraction(lines: list[str], start: int) -> tuple[str, int]:
    blob = "\n".join(lines[start:])
    numerator_open = blob.find("{")
    if numerator_open < 0:
        return lines[start], start + 1
    numerator_close = _find_group_end(blob, numerator_open)
    if numerator_close is None:
        return lines[start], start + 1
    denominator_open = numerator_close + 1
    while denominator_open < len(blob) and blob[denominator_open].isspace():
        denominator_open += 1
    if denominator_open >= len(blob) or blob[denominator_open] != "{":
        return lines[start], start + 1
    denominator_close = _find_group_end(blob, denominator_open)
    if denominator_close is None:
        return lines[start], start + 1
    line_end = blob.find("\n", denominator_close)
    line_end = len(blob) if line_end < 0 else line_end
    expression = re.sub(r"\s+", " ", blob[:line_end]).strip()
    consumed = blob[:line_end].count("\n") + 1
    return expression, start + consumed


def _compact_math(lines: list[str]) -> list[str]:
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line == r"\frac{":
            fraction, index = _collect_fraction(lines, index)
            output.append(fraction)
            continue
        if r"\frac{" in line and index + 1 < len(lines) and lines[index + 1].startswith("{"):
            output.append(line + " " + lines[index + 1])
            index += 2
            continue
        depth = _brace_delta(line)
        if depth > 0:
            pieces = [line]
            lookahead = index + 1
            while lookahead < len(lines) and depth > 0:
                pieces.append(lines[lookahead])
                depth += _brace_delta(lines[lookahead])
                lookahead += 1
            if depth == 0:
                output.append(" ".join(pieces))
                index = lookahead
                continue
        if r"\left" in line and r"\right" not in line:
            pieces = [line]
            depth = line.count(r"\left") - line.count(r"\right")
            index += 1
            while index < len(lines):
                pieces.append(lines[index])
                depth += lines[index].count(r"\left") - lines[index].count(r"\right")
                index += 1
                if depth <= 0:
                    break
            output.append(" ".join(pieces))
            continue
        output.append(line)
        index += 1
    return output


def _display_tex(lines: list[str], *, explicit: bool = False) -> str:
    if explicit:
        body = wrap_display("\n".join(lines).strip())
        return "\\[\n" + r"\ccfitmath{" + body + "}\n\\]\n"

    decoded = _shrink_long_text_math(_decode_math_lines(lines))
    normalized = [_normalize_display_line(line) for line in decoded]
    compacted = _compact_math(normalized)
    body = ("\n".join(compacted) if any(r"\begin{" in line for line in compacted) else " ".join(compacted)).strip("\n")
    return "\\[\n" + body + "\n\\]\n"


def _pdf_heading_text(text: str) -> str:
    value = text.replace("**", "").replace("`", "")
    # Explicit math delimiters are structure, not prose. Leaving them here
    # escapes them into the malformed `\textbackslash{}\( ... \)` bookmark
    # class, which no compiler diagnostic reports.
    value = re.sub(r"\\[()\[\]]", "", value)
    for old, new in {
        r"\mid": "|", r"\rightarrow": "->", r"\leftarrow": "<-", r"\perp": "independent",
        r"\sim": "~", r"\le": "<=", r"\ge": ">=", r"\alpha": "alpha", r"\beta": "beta",
        r"\lambda": "lambda", r"\rho": "rho", r"\gamma": "gamma", r"\ldots": "...", r"\times": "x",
        r"\Gamma": "Gamma", r"\Delta": "Delta", r"\Theta": "Theta", r"\Lambda": "Lambda",
        r"\Sigma": "Sigma", r"\Phi": "Phi", r"\Psi": "Psi", r"\Omega": "Omega", r"\Pi": "Pi",
        r"\delta": "delta", r"\epsilon": "epsilon", r"\varepsilon": "epsilon", r"\zeta": "zeta",
        r"\eta": "eta", r"\theta": "theta", r"\kappa": "kappa", r"\mu": "mu", r"\nu": "nu",
        r"\xi": "xi", r"\pi": "pi", r"\sigma": "sigma", r"\tau": "tau", r"\phi": "phi",
        r"\varphi": "phi", r"\chi": "chi", r"\psi": "psi", r"\omega": "omega",
    }.items():
        value = value.replace(old, new)
    value = re.sub(r"\\text\{([^{}]*)\}", r"\1", value)
    value = re.sub(r"\\mathrm\{([^{}]*)\}", r"\1", value)
    value = re.sub(r"\\operatorname\{([^{}]*)\}", r"\1", value)
    return re.sub(r"[ \t]{2,}", " ", re.sub(r"\\[a-zA-Z]+", "", value))


def _heading_title(text: str) -> str:
    visible = _convert_inline(text).strip()
    bookmark = _escape_text(_pdf_heading_text(text)).strip()
    return rf"\texorpdfstring{{{visible}}}{{{bookmark}}}"


def _heading_command(level: int, title: str) -> str:
    return "section" if level <= 1 or re.match(r"\d+\.", title) else "subsection"


def _write_heading(command: str, title: str, needed: int) -> str:
    return (
        f"\\Needspace{{{needed}\\baselineskip}}\n"
        f"\\phantomsection\n\\{command}*{{{title}}}\n"
        f"\\addcontentsline{{toc}}{{{command}}}{{{title}}}\n\n"
    )


def _write_part(title: str) -> str:
    return f"\\phantomsection\n\\part*{{{title}}}\n\\addcontentsline{{toc}}{{part}}{{{title}}}\n\n"


def _next_nonempty_index(lines: list[str], start: int) -> int | None:
    return next((index for index in range(start, len(lines)) if lines[index].strip()), None)


def _previous_nonempty_index(lines: list[str], start: int) -> int | None:
    return next((index for index in range(start, -1, -1) if lines[index].strip()), None)


def _next_nonempty(lines: list[str], start: int) -> str:
    index = _next_nonempty_index(lines, start)
    return lines[index] if index is not None else ""


def _display_end(lines: list[str], start: int) -> int | None:
    return next((index for index in range(start + 1, len(lines)) if lines[index].strip() in _DISPLAY_ENDS), None)


def _is_continuation(line: str) -> bool:
    value = line.strip()
    return bool(_CONTINUATION.match(value) or re.match(r"^[a-z]", value))


def _is_heading_lead_in(line: str) -> bool:
    value = line.strip()
    return value.endswith(":") or bool(re.search(
        r"\b(?:types?|forms?|cases?|categories|components?|parts?|steps?|approaches|options|possibilities|outputs?)\b[^.!?]*[.]$",
        value,
        re.IGNORECASE,
    ))


def _linked_display_count(lines: list[str], start: int, limit: int = 8) -> int:
    count = 0
    display_index = start
    while count < limit and lines[display_index].strip() in _DISPLAY_STARTS:
        end = _display_end(lines, display_index)
        if end is None:
            break
        count += 1
        connector = _next_nonempty_index(lines, end + 1)
        if connector is None:
            break
        if lines[connector].strip() in _DISPLAY_STARTS:
            display_index = connector
            continue
        if not _is_continuation(lines[connector]):
            break
        following = _next_nonempty_index(lines, connector + 1)
        if following is None or lines[following].strip() not in _DISPLAY_STARTS:
            break
        display_index = following
    return max(1, count)


def _heading_display_count(lines: list[str], start: int) -> int:
    prose = 0
    for index in range(start, len(lines)):
        value = lines[index].strip()
        if not value:
            continue
        if value in _DISPLAY_STARTS:
            return _linked_display_count(lines, index)
        if _HEADING.match(value):
            continue
        if _ORDERED_ITEM.match(lines[index]) or _UNORDERED_ITEM.match(lines[index]):
            break
        if value == "---" or _CODE_FENCE.match(value) or _is_table_start(lines, index):
            break
        prose += 1
        if prose > 2:
            break
    return 0


def _code_line_count(lines: list[str], start: int, fence: str) -> int:
    count = 0
    for line in lines[start + 1 :]:
        if line.strip().startswith(fence):
            break
        count += 1
    return count


def _heading_needed_lines(lines: list[str], start: int, parent_level: int | None = None) -> int:
    needed = 8 if parent_level is not None and parent_level >= 3 else 10
    prose = 0
    included_child = False
    for index in range(start, len(lines)):
        value = lines[index].strip()
        if not value:
            continue
        if value in _DISPLAY_STARTS:
            return min(16, needed + 2)
        if fence := _CODE_FENCE.match(value):
            return min(44, max(needed, _code_line_count(lines, index, fence.group(1)) + 14))
        if _is_table_start(lines, index):
            return min(20, needed + _table_block_line_count(lines, index))
        if child := _HEADING.match(value):
            is_child = parent_level is not None and len(child.group(1)) > parent_level
            if not is_child or included_child:
                break
            included_child = True
            needed += 3
            continue
        if value == "---":
            break
        if _ORDERED_ITEM.match(lines[index]) or _UNORDERED_ITEM.match(lines[index]):
            return min(20, needed + 6)
        prose += 1
        needed += 1
        following = _next_nonempty_index(lines, index + 1)
        if value.endswith(":") and following is not None and _HEADING.match(lines[following].strip()):
            return 20
        if prose >= 3:
            break
    return min(14, needed)


def _split_table_row(line: str) -> list[str]:
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|"):
        value = value[:-1]
    return [cell.replace(r"\|", "|").strip() for cell in re.split(r"(?<!\\)\|", value)]


def _is_table_separator(line: str) -> bool:
    cells = _split_table_row(line)
    return len(cells) >= 2 and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)


def _is_table_start(lines: list[str], index: int) -> bool:
    return (
        index + 1 < len(lines)
        and "|" in lines[index]
        and len(_split_table_row(lines[index])) >= 2
        and _is_table_separator(lines[index + 1])
    )


def _table_block_line_count(lines: list[str], start: int) -> int:
    rows = 1
    index = start + 2
    while index < len(lines):
        line = lines[index]
        if not line.strip() or "|" not in line or _is_table_separator(line):
            break
        rows += 1
        index += 1
    return rows + 4


def _table_tex(lines: list[str], start: int) -> tuple[str, int]:
    header = _split_table_row(lines[start])
    rows: list[list[str]] = []
    index = start + 2
    while index < len(lines):
        line = lines[index]
        if not line.strip() or "|" not in line or _is_table_separator(line):
            break
        rows.append(_split_table_row(line))
        index += 1
    columns = max((len(header), *(len(row) for row in rows)))

    def normalize(cells: list[str]) -> list[str]:
        return (cells + [""] * columns)[:columns]

    def row_tex(cells: list[str], header_row: bool = False) -> str:
        converted = [_convert_inline(cell) for cell in cells]
        if header_row:
            converted = [rf"\textbf{{{cell}}}" for cell in converted]
        return " & ".join(converted) + r" \\" + "\n"

    output = [
        "\\begin{center}\n",
        "\\renewcommand{\\arraystretch}{1.15}\n",
        "\\setlength{\\tabcolsep}{4pt}\n",
        f"\\begin{{tabularx}}{{\\linewidth}}{{@{{}}{'Y' * columns}@{{}}}}\n",
        "\\hline\n",
        row_tex(normalize(header), True),
        "\\hline\n",
    ]
    output.extend(row_tex(normalize(row)) for row in rows)
    output.extend(("\\hline\n", "\\end{tabularx}\n", "\\end{center}\n\n"))
    return "".join(output), index


def _unclosed_quoted_display_warnings(source_text: str) -> tuple[RenderDiagnostic, ...]:
    """Find display blocks that the quote renderer closes at quote end.

    T002 validation deliberately recognizes only its established top-level
    markers. This renderer-local scan covers the equivalent supported markers
    after consecutive blockquote prefixes without changing that contract.
    """

    warnings: list[RenderDiagnostic] = []
    lines = source_text.splitlines()
    index = 0
    while index < len(lines):
        if not is_quote_line(lines[index]):
            index += 1
            continue
        opener_line: int | None = None
        while index < len(lines) and is_quote_line(lines[index]):
            value = strip_quote(lines[index]).strip()
            if opener_line is None:
                if value in _DISPLAY_STARTS:
                    opener_line = index + 1
            elif value in _DISPLAY_ENDS:
                opener_line = None
            index += 1
        if opener_line is not None:
            warnings.append(
                RenderDiagnostic(
                    code="unclosed_display_math",
                    severity="warning",
                    stage="validation",
                    line=opener_line,
                )
            )
    return tuple(warnings)


def _quote_tex(lines: list[str]) -> tuple[str, int]:
    output = ["\\begin{quote}\n"]
    displays = 0
    index = 0
    while index < len(lines):
        value = lines[index].strip()
        if value in _DISPLAY_STARTS:
            math_lines: list[str] = []
            index += 1
            while index < len(lines) and lines[index].strip() not in _DISPLAY_ENDS:
                math_lines.append(lines[index])
                index += 1
            output.append(_display_tex(math_lines, explicit=value == r"\["))
            displays += 1
            if index < len(lines):
                index += 1
        else:
            output.append((_convert_inline(value) if value else "") + "\n")
            index += 1
    output.append("\\end{quote}\n\n")
    return "".join(output), displays


def _close_lists(output: list[str], stack: list[str]) -> None:
    while stack:
        output.append(f"\\end{{{stack.pop()}}}\n")


def _update_list_stack(output: list[str], stack: list[str], indent: int, environment: str) -> None:
    desired_depth = indent // 2 + 1
    while len(stack) > desired_depth:
        output.append(f"\\end{{{stack.pop()}}}\n")
    while len(stack) < desired_depth:
        stack.append(environment)
        output.append(f"\\begin{{{environment}}}\n")
    if stack and stack[-1] != environment:
        output.append(f"\\end{{{stack.pop()}}}\n")
        stack.append(environment)
        output.append(f"\\begin{{{environment}}}\n")


def _list_siblings(lines: list[str], index: int, environment: str, indent: int) -> list[int]:
    pattern = _ORDERED_ITEM if environment == "enumerate" else _UNORDERED_ITEM

    def belongs(line: str) -> bool:
        if not line.strip():
            return True
        ordered = _ORDERED_ITEM.match(line)
        unordered = _UNORDERED_ITEM.match(line)
        item = ordered or unordered
        if item:
            item_indent = len(item.group(1).replace("\t", "    "))
            item_environment = "enumerate" if ordered else "itemize"
            return item_indent > indent or (item_indent == indent and item_environment == environment)
        expanded = line.expandtabs(4)
        return len(expanded) - len(expanded.lstrip()) > indent

    start = index
    while start > 0 and belongs(lines[start - 1]):
        start -= 1
    end = index + 1
    while end < len(lines) and belongs(lines[end]):
        end += 1
    return [
        item_index
        for item_index in range(start, end)
        if (match := pattern.match(lines[item_index]))
        and len(match.group(1).replace("\t", "    ")) == indent
    ]


def _next_logical_sibling(lines: list[str], index: int, environment: str, indent: int) -> int | None:
    cursor = index + 1
    while cursor < len(lines):
        line = lines[cursor]
        value = line.strip()
        if not value:
            cursor += 1
            continue
        if value in _DISPLAY_STARTS:
            end = _display_end(lines, cursor)
            if end is None:
                return None
            cursor = end + 1
            continue
        ordered = _ORDERED_ITEM.match(line)
        unordered = _UNORDERED_ITEM.match(line)
        item = ordered or unordered
        if item:
            item_indent = len(item.group(1).replace("\t", "    "))
            item_environment = "enumerate" if ordered else "itemize"
            if item_indent == indent and item_environment == environment:
                return cursor
            if item_indent > indent or (environment == "enumerate" and item_environment == "itemize"):
                cursor += 1
                continue
            return None
        expanded = line.expandtabs(4)
        if len(expanded) - len(expanded.lstrip()) > indent:
            cursor += 1
            continue
        return None
    return None


def _previous_logical_sibling(lines: list[str], index: int, environment: str, indent: int) -> int | None:
    cursor = index - 1
    while cursor >= 0:
        line = lines[cursor]
        value = line.strip()
        if not value:
            cursor -= 1
            continue
        if value in _DISPLAY_ENDS:
            cursor -= 1
            while cursor >= 0 and lines[cursor].strip() not in _DISPLAY_STARTS:
                cursor -= 1
            cursor -= 1
            continue
        ordered = _ORDERED_ITEM.match(line)
        unordered = _UNORDERED_ITEM.match(line)
        item = ordered or unordered
        if item:
            item_indent = len(item.group(1).replace("\t", "    "))
            item_environment = "enumerate" if ordered else "itemize"
            if item_indent == indent and item_environment == environment:
                return cursor
            if item_indent > indent or (environment == "enumerate" and item_environment == "itemize"):
                cursor -= 1
                continue
            return None
        expanded = line.expandtabs(4)
        if len(expanded) - len(expanded.lstrip()) > indent:
            cursor -= 1
            continue
        return None
    return None


def _penultimate_needed(lines: list[str], index: int, environment: str, indent: int) -> int:
    siblings = _list_siblings(lines, index, environment, indent)
    if len(siblings) >= 4 and index == siblings[-2]:
        return 8
    following = _next_logical_sibling(lines, index, environment, indent)
    if following is None or _next_logical_sibling(lines, following, environment, indent) is not None:
        return 0
    count = 2
    previous = _previous_logical_sibling(lines, index, environment, indent)
    while previous is not None:
        count += 1
        previous = _previous_logical_sibling(lines, previous, environment, indent)
    return 12 if count >= 4 else 0


def _list_block_needed(lines: list[str], start: int) -> int:
    first = _ORDERED_ITEM.match(lines[start]) or _UNORDERED_ITEM.match(lines[start])
    if first is None:
        return 6
    environment = "enumerate" if _ORDERED_ITEM.match(lines[start]) else "itemize"
    pattern = _ORDERED_ITEM if environment == "enumerate" else _UNORDERED_ITEM
    indent = len(first.group(1).replace("\t", "    "))
    siblings = _list_siblings(lines, start, environment, indent)
    if not siblings or len(siblings) > 6:
        return 6
    needed = 3
    for item_index in siblings:
        match = pattern.match(lines[item_index])
        assert match is not None
        text = (match.group(3) or "") if environment == "enumerate" else match.group(2)
        needed += max(1, (len(text) + 79) // 80)
    for line in lines[siblings[0] + 1 : siblings[-1]]:
        if line.strip() and pattern.match(line) is None and line[:1].isspace():
            needed += max(1, (len(line.strip()) + 79) // 80)
    cursor = siblings[-1] + 1
    while cursor < len(lines) and (not lines[cursor].strip() or lines[cursor][:1].isspace()):
        if lines[cursor].strip():
            needed += max(1, (len(lines[cursor].strip()) + 79) // 80)
        cursor += 1
    following = _next_nonempty_index(lines, cursor)
    if following is not None and lines[following].strip() in _DISPLAY_STARTS:
        needed += 4
    return min(16, max(6, needed))


def _list_item_math_needed(lines: list[str], index: int, environment: str, indent: int) -> int:
    pattern = _ORDERED_ITEM if environment == "enumerate" else _UNORDERED_ITEM
    match = pattern.match(lines[index])
    if match is None:
        return 0
    text = (match.group(3) or "") if environment == "enumerate" else match.group(2)
    needed = max(1, (len(text) + 79) // 80)
    has_display = False
    cursor = index + 1
    while cursor < len(lines):
        line = lines[cursor]
        value = line.strip()
        if not value:
            cursor += 1
            continue
        sibling = _ORDERED_ITEM.match(line) or _UNORDERED_ITEM.match(line)
        if sibling and len(sibling.group(1).replace("\t", "    ")) <= indent:
            break
        expanded = line.expandtabs(4)
        if len(expanded) - len(expanded.lstrip()) <= indent:
            break
        if value in _DISPLAY_STARTS:
            has_display = True
            needed += 4
            cursor += 1
            while cursor < len(lines) and lines[cursor].strip() not in _DISPLAY_ENDS:
                cursor += 1
            cursor += cursor < len(lines)
            continue
        needed += max(1, (len(value) + 79) // 80)
        cursor += 1
    return min(20, needed + 2) if has_display else 0


def _line_start_offsets(source_text: str) -> tuple[int, ...]:
    """Code-point offsets for the start of each `str.splitlines()` line, plus EOF.

    Built from `splitlines(keepends=True)` segment lengths so the boundaries
    line up exactly with the same Unicode line-boundary rules `_convert_source`
    already uses to split `source_text` into `lines`. `offsets[0]` is always
    `0`; `offsets[len(lines)]` is always `len(source_text)`.
    """

    offsets = [0]
    cursor = 0
    for segment in source_text.splitlines(keepends=True):
        cursor += len(segment)
        offsets.append(cursor)
    return tuple(offsets)


def _convert_source(
    source_text: str, insertions: Mapping[int, str] | None = None
) -> _Conversion:
    """Convert one source text, optionally splicing in already-formed TeX.

    `insertions` is a purely generic code-point-offset-to-TeX mapping with no
    knowledge of assets, placements, the compiler, or workflow state. Passing
    `None` (or an empty mapping) reproduces the exact ordinary conversion.
    Insertion only ever happens at a `safe_offsets` member: the top of the
    main loop when no multi-line construct or keep-together/samepage span is
    active, or after all ordinary end-of-source closure has been emitted.
    """

    lines = source_text.splitlines()
    line_offsets = _line_start_offsets(source_text)
    output: list[str] = []
    counts = _Counts()
    list_stack: list[str] = []
    in_code = False
    code_fence = ""
    in_display = False
    display_explicit = False
    display_lines: list[str] = []
    saw_title = False
    kept_displays = 0
    keep_trailing_prose = False
    keep_revision = False
    keep_code = False
    keep_table = False
    keep_quote = False
    protected_until_offset: int | None = None
    safe_offsets: set[int] = set()

    index = 0
    while index < len(lines):
        line = lines[index]
        value = line.strip()

        if in_code:
            if value.startswith(code_fence):
                output.append("\\end{Verbatim}\n")
                output.append("\\end{keeptogether}\n" if keep_code else "\\end{samepage}\n")
                keep_code = False
                in_code = False
                counts.output_code_blocks += 1
            else:
                output.append(line + "\n")
            index += 1
            continue

        if in_display:
            if value in _DISPLAY_ENDS:
                output.append(_display_tex(display_lines, explicit=display_explicit))
                if kept_displays:
                    kept_displays -= 1
                    if kept_displays == 0:
                        trailing_index = _next_nonempty_index(lines, index + 1)
                        trailing = lines[trailing_index].strip() if trailing_index is not None else ""
                        after_index = _next_nonempty_index(lines, trailing_index + 1) if trailing_index is not None else None
                        after = lines[after_index].strip() if after_index is not None else ""
                        if _is_continuation(trailing) and after not in _DISPLAY_STARTS:
                            keep_trailing_prose = True
                        else:
                            output.append("\\end{keeptogether}\n")
                display_lines = []
                in_display = False
                counts.output_display_math_blocks += 1
            else:
                display_lines.append(line)
            index += 1
            continue

        boundary_offset = line_offsets[index]
        # `protected_until_offset` closes the whole short source-boundary
        # interval that a preceding prose lead-in's own Needspace-only
        # pagination policy already bound to a dependent heading or list
        # below: every boundary strictly after the lead-in and through the
        # dependent construct's own start boundary, including any blank
        # lines T004's next-nonempty-line lookup skipped to find it. It
        # expires as soon as that final boundary has been evaluated, so
        # every later independent boundary keeps its ordinary safety
        # semantics.
        boundary_is_protected = (
            protected_until_offset is not None
            and boundary_offset <= protected_until_offset
        )
        if protected_until_offset is not None and boundary_offset >= protected_until_offset:
            protected_until_offset = None
        if not boundary_is_protected and not (
            list_stack
            or kept_displays
            or keep_trailing_prose
            or keep_revision
            or keep_table
            or keep_quote
            or keep_code
        ):
            safe_offsets.add(boundary_offset)
            if insertions and boundary_offset in insertions:
                output.append(insertions[boundary_offset])

        fence_match = _CODE_FENCE.match(value)
        if fence_match:
            _close_lists(output, list_stack)
            in_code = True
            code_fence = fence_match.group(1)
            counts.input_code_blocks += 1
            if not keep_code:
                needed = min(32, max(8, _code_line_count(lines, index, code_fence) + 3))
                output.extend((f"\\Needspace{{{needed}\\baselineskip}}\n", "\\begin{samepage}\n"))
            output.append("\\begin{Verbatim}[breaklines=true,breakanywhere=true]\n")
            index += 1
            continue

        if value in _DISPLAY_STARTS:
            if not (list_stack and line[:1].isspace()):
                _close_lists(output, list_stack)
            in_display = True
            display_explicit = value == r"\["
            counts.input_display_math_blocks += 1
            index += 1
            continue

        if value.startswith("[[SOURCE_SNIP"):
            # The composer supplies the image at this exact source boundary.
            # Render a plain provenance caption; no model TeX/path is executed.
            from .snippet_contract import DIRECTIVE
            snippet = DIRECTIVE.fullmatch(value)
            if snippet is None:
                raise ValueError("invalid_source_snippet_directive")
            _close_lists(output, list_stack)
            output.append(r"\par\noindent\textit{" + _escape_text(
                f"Source {snippet[1]}, page {snippet[2]}: {snippet[3]}") + "}\n\n")
            index += 1
            continue

        if _is_table_start(lines, index):
            _close_lists(output, list_stack)
            table, index = _table_tex(lines, index)
            output.append(table)
            if keep_table:
                output.append("\\end{keeptogether}\n")
                keep_table = False
            counts.input_tables += 1
            counts.output_tables += 1
            continue

        heading = _HEADING.match(value)
        if heading:
            _close_lists(output, list_stack)
            level = len(heading.group(1))
            raw_title = heading.group(2)
            title = _heading_title(raw_title)
            if not saw_title:
                output.append(_write_part(title))
                saw_title = True
            else:
                command = _heading_command(level, raw_title)
                display_count = _heading_display_count(lines, index + 1)
                split_calculation = bool(re.match(r"^Split\s+\(", raw_title, re.IGNORECASE))
                if split_calculation:
                    output.append("\\Needspace{22\\baselineskip}\n")
                if display_count and kept_displays == 0:
                    output.append("\\begin{keeptogether}\n")
                    kept_displays = display_count
                if "revision paragraph" in raw_title.lower() or "one-paragraph revision" in raw_title.lower():
                    output.append("\\begin{keeptogether}\n")
                    keep_revision = True
                needed = _heading_needed_lines(lines, index + 1, level)
                if split_calculation:
                    needed = max(22, needed)
                output.append(_write_heading(command, title, needed))
            counts.input_headings += 1
            counts.output_headings += 1
            index += 1
            continue

        if value == "---":
            _close_lists(output, list_stack)
            output.append("\\medskip\\hrule\\medskip\n\n")
            index += 1
            continue

        if is_quote_line(line):
            _close_lists(output, list_stack)
            quote_lines: list[str] = []
            while index < len(lines) and is_quote_line(lines[index]):
                quote_lines.append(strip_quote(lines[index]))
                index += 1
            quote, quote_displays = _quote_tex(quote_lines)
            output.append(quote)
            counts.input_display_math_blocks += quote_displays
            counts.output_display_math_blocks += quote_displays
            if keep_quote:
                output.append("\\end{keeptogether}\n")
                keep_quote = False
            continue

        ordered = _ORDERED_ITEM.match(line)
        unordered = _UNORDERED_ITEM.match(line)
        if ordered or unordered:
            item = ordered or unordered
            assert item is not None
            indent = len(item.group(1).replace("\t", "    "))
            environment = "enumerate" if ordered else "itemize"
            text = (ordered.group(3) or "") if ordered else unordered.group(2)
            _update_list_stack(output, list_stack, indent, environment)
            item_needed = _list_item_math_needed(lines, index, environment, indent)
            if item_needed:
                output.append(f"\\Needspace{{{item_needed}\\baselineskip}}\n")
            pair_needed = _penultimate_needed(lines, index, environment, indent)
            if pair_needed:
                output.append(f"\\Needspace{{{pair_needed}\\baselineskip}}\n")
            if _next_nonempty(lines, index + 1).strip() in _DISPLAY_STARTS:
                output.append("\\Needspace{6\\baselineskip}\n")
            label = f"[{ordered.group(2)}.]" if ordered else ""
            output.append(f"\\item{label} " + _convert_inline(text) + "\n")
            index += 1
            continue

        if not value:
            output.append("\n")
            index += 1
            continue

        if list_stack and line[:1].isspace():
            if _next_nonempty(lines, index + 1).strip() in _DISPLAY_STARTS:
                output.append("\\Needspace{6\\baselineskip}\n")
            output.append(_convert_inline(value) + "\n\n")
            if keep_trailing_prose:
                output.append("\\end{keeptogether}\n")
                keep_trailing_prose = False
            index += 1
            continue

        _close_lists(output, list_stack)
        following_index = _next_nonempty_index(lines, index + 1)
        following = lines[following_index].strip() if following_index is not None else ""
        if _is_continuation(line) and not keep_trailing_prose:
            output.append("\\nopagebreak[4]\n")
        if following in _DISPLAY_STARTS and kept_displays == 0:
            output.append("\\begin{keeptogether}\n")
            assert following_index is not None
            kept_displays = _linked_display_count(lines, following_index)
        elif following_index is not None and _is_table_start(lines, following_index):
            output.append("\\begin{keeptogether}\n")
            keep_table = True
        elif is_quote_line(following):
            output.append("\\begin{keeptogether}\n")
            keep_quote = True
        elif following_index is not None and _HEADING.match(following) and _is_heading_lead_in(line):
            previous_index = _previous_nonempty_index(lines, index - 1)
            if previous_index is None or not _HEADING.match(lines[previous_index].strip()):
                following_heading = _HEADING.match(following)
                assert following_heading is not None
                needed = _heading_needed_lines(lines, following_index + 1, len(following_heading.group(1))) + 2
                output.append(f"\\Needspace{{{min(18, needed)}\\baselineskip}}\n")
                # This lead-in's own pagination policy reserves space for the
                # dependent heading `following_index` located below, across
                # any blank lines in between; no boundary in that interval
                # is an independent insertion point.
                protected_until_offset = line_offsets[following_index]
        elif following_index is not None and (_ORDERED_ITEM.match(lines[following_index]) or _UNORDERED_ITEM.match(lines[following_index])):
            output.append(f"\\Needspace{{{_list_block_needed(lines, following_index)}\\baselineskip}}\n")
            # Same reservation relationship as above, for a following list.
            protected_until_offset = line_offsets[following_index]
        elif following_index is not None and _CODE_FENCE.match(following):
            output.append("\\begin{keeptogether}\n")
            keep_code = True
        standalone_inline_math = re.fullmatch(r"\s*\\\((.+?)\\\)\s*([.,;:]?)\s*", line)
        body = standalone_inline_math.group(1) if standalone_inline_math is not None else None
        if (
            standalone_inline_math is not None
            and body is not None
            and r"\(" not in body
            and r"\)" not in body
        ):
            output.append(
                r"\ccfitinline{" + standalone_inline_math.group(1) + "}{"
                + standalone_inline_math.group(2)
                + "}\n\n"
            )
        else:
            converted = _convert_inline(line)
            if _INLINE_CODE.search(line) is not None:
                output.append(_code_paragraph_tex(converted))
            else:
                output.append(converted + "\n\n")
        if keep_trailing_prose:
            output.append("\\end{keeptogether}\n")
            keep_trailing_prose = False
        if keep_revision:
            output.append("\\end{keeptogether}\n")
            keep_revision = False
        index += 1

    if in_code:
        output.append("\\end{Verbatim}\n")
        output.append("\\end{keeptogether}\n" if keep_code else "\\end{samepage}\n")
        counts.output_code_blocks += 1
    if in_display:
        output.append(_display_tex(display_lines, explicit=display_explicit))
        if kept_displays:
            output.append("\\end{keeptogether}\n")
        counts.output_display_math_blocks += 1

    _close_lists(output, list_stack)

    # End-of-source is always a safe insertion point, even when a construct
    # reaching EOF triggered the renderer's own ordinary closure above: the
    # insertion still lands strictly after that closure, never inside it.
    eof_offset = line_offsets[len(lines)]
    safe_offsets.add(eof_offset)
    if insertions and eof_offset in insertions:
        output.append(insertions[eof_offset])

    return _Conversion(
        "".join(output), counts.metrics(source_text), frozenset(safe_offsets)
    )


def _renderer_safe_offsets(source_text: str) -> frozenset[int]:
    """The exact code-point offsets where generic TeX insertion is safe."""

    return _convert_source(source_text).safe_offsets


def _render_with_insertions(source_text: str, insertions: Mapping[int, str]) -> str:
    """Render one source text with already-formed TeX spliced at safe offsets.

    `insertions` is a purely generic offset-to-TeX mapping; this function has
    no asset, placement, compiler, or workflow domain knowledge. An empty
    mapping reproduces the exact ordinary conversion.
    """

    return _convert_source(source_text, insertions).tex_fragment
