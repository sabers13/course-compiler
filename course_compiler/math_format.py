"""Bounded syntax/layout repair; never infer or rewrite mathematical meaning."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .safe_math import SAFE_MATH_COMMANDS, SAFE_MATH_ENVIRONMENTS
from .snippet_contract import DIRECTIVE

# Math is data, not unrestricted TeX. Unknown commands require a corrected
# response or an explicitly reviewed allowlist extension, never execution.
_DELIM = re.compile(r'\\[()\[\]]')
_DISPLAY_DOLLAR_LINE = re.compile(r'^[ \t]*\$\$[ \t]*(?:\n)?$')
_RAW_DISPLAY_DOLLAR = re.compile(r'(?<!\\)\$\$')
_INLINE_DOLLAR_PAIR = re.compile(r'(?<!\\)\$(?!\$)[^\n$]*(?<!\\)\$(?!\$)')
_FENCE_OPEN = re.compile(r'^[ \t]*(```+|~~~+)([^\n]*)\n?$')
_FENCE_CLOSE = re.compile(r'^[ \t]*(```+|~~~+)[ \t]*\n?$')
_INLINE_CODE = re.compile(r'`([^`\n]+)`')
_MATH_ALIASES = ((r'\Longleftrightarrow', r'\iff'), (r'\longleftrightarrow', r'\leftrightarrow'))


def is_quote_line(line: str) -> bool:
    """A Markdown blockquote line, exactly as the legacy renderer sees one."""
    return line.lstrip().startswith('>')


def strip_quote(line: str) -> str:
    """Remove exactly one blockquote marker, mirroring the legacy renderer.

    Shared with ``legacy_renderer`` so normalization and rendering agree on
    what counts as "inside the blockquote" for exactly one nesting level.
    """
    content = line.lstrip()[1:]
    return content[1:] if content.startswith(' ') else content


def _fence_open(line: str) -> tuple[str, str, bool] | None:
    """Return ``(marker, info, quoted)`` if `line` opens a recognized fence.

    Recognizes a top-level fence, or one nested behind exactly one blockquote
    marker (matching :func:`is_quote_line`/:func:`strip_quote`). Never
    recurses past one quote level, matching the renderer's own quote scan.
    Every fence-tracking pass in this module opens and closes fences through
    this function and :func:`_fence_close` exclusively, so no pass can
    believe a fence is open (or closed) while another disagrees.
    """
    quoted = is_quote_line(line)
    body = strip_quote(line) if quoted else line
    match = _FENCE_OPEN.fullmatch(body)
    return (match[1], match[2], quoted) if match else None


def _fence_close(line: str) -> tuple[str, bool] | None:
    """Return ``(marker, quoted)`` if `line` closes a recognized fence.

    A quoted closer only matches a quoted opener, and vice versa: closing
    identity is ``(marker, quoted)``, exactly as opened.
    """
    quoted = is_quote_line(line)
    body = strip_quote(line) if quoted else line
    match = _FENCE_CLOSE.fullmatch(body)
    return (match[1], quoted) if match else None


@dataclass(frozen=True)
class LectureNormalizationCounts:
    dollar_display_blocks: int = 0
    math_fences: int = 0
    inline_code_math: int = 0
    math_aliases: int = 0
    escaped_source_snips: int = 0


def without_code(text: str) -> str:
    """Strip fenced and inline code so only prose reaches explicit-math validation.

    A code fence is recognized the same way as everywhere else in this
    module: top-level, or nested behind exactly one blockquote marker (see
    :func:`_fence_open`/:func:`_fence_close`). An opener without a matching
    closer of the same identity is not treated as code at all -- its bytes
    (including the literal fence markers) remain prose, exactly as an
    unclosed fence always has, so malformed/ambiguous fences keep failing
    closed instead of silently vanishing.
    """
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    index, total = 0, len(lines)
    while index < total:
        opening = _fence_open(lines[index])
        if opening is not None:
            identity = (opening[0], opening[2])
            close_index = None
            for cursor in range(index + 1, total):
                if _fence_close(lines[cursor]) == identity:
                    close_index = cursor
                    break
            if close_index is not None:
                index = close_index + 1
                continue
        output.append(_INLINE_CODE.sub('', lines[index]))
        index += 1
    return ''.join(output)


def validate_explicit_math(text: str, *, strict_prose: bool = False) -> None:
    text = without_code(text)
    active = None
    start = 0
    prose = []
    cursor = 0
    delimiters = tuple(_explicit_delimiters(text))
    index = 0
    while index < len(delimiters):
        match = delimiters[index]
        token = match[0]
        if token in (r'\(', r'\['):
            if active is not None:
                if active == r'\[' and token == r'\(':
                    island_end = _text_math_island_end(text, start, match, delimiters, index)
                    if island_end is not None:
                        index = island_end + 1
                        continue
                raise ValueError('math_delimiters_unbalanced')
            prose.append(text[cursor:match.start()])
            active, start = token, match.end()
        else:
            if active != {r'\)': r'\(', r'\]': r'\['}[token]:
                raise ValueError('math_delimiters_unbalanced')
            body = text[start:match.start()]
            _validate_math_body(body)
            active, cursor = None, match.end()
        index += 1
    if active:
        raise ValueError('math_delimiters_unbalanced')
    prose.append(text[cursor:])
    if strict_prose and (re.search(r'\\[A-Za-z]+', ''.join(prose)) or re.search(r'[α-ωΑ-Ω₀-₉⁰¹²³⁴⁵⁶⁷⁸⁹]', ''.join(prose))):
        raise ValueError('math_requires_explicit_delimiters')


def _text_math_island_end(text: str, display_start: int, opener: re.Match[str],
                          delimiters: tuple[re.Match[str], ...], index: int) -> int | None:
    """Validate one ``\\text{... \\(...\\) ...}`` island in an outer display.

    This recognizes a TeX mode transition, not general nested explicit math.  The
    inline island must be directly inside the one enclosing ``\\text`` argument,
    entirely before that argument closes, and contain no explicit delimiters.
    """
    stack: list[tuple[int, bool]] = []
    cursor = display_start
    while cursor < opener.start():
        char = text[cursor]
        if char in '{}':
            preceding = 0
            back = cursor - 1
            while back >= 0 and text[back] == '\\':
                preceding += 1
                back -= 1
            if preceding % 2 == 0:
                if char == '{':
                    stack.append((cursor, _is_exact_text_argument_open(text, cursor)))
                elif stack:
                    stack.pop()
        cursor += 1
    if not stack or not stack[-1][1]:
        return None
    argument_open = stack[-1][0]
    depth = 1
    argument_close = None
    for cursor in range(argument_open + 1, len(text)):
        char = text[cursor]
        if char not in '{}':
            continue
        preceding = 0
        back = cursor - 1
        while back >= 0 and text[back] == '\\':
            preceding += 1
            back -= 1
        if preceding % 2:
            continue
        depth += 1 if char == '{' else -1
        if depth == 0:
            argument_close = cursor
            break
    if argument_close is None:
        return None
    for inner_index in range(index + 1, len(delimiters)):
        inner = delimiters[inner_index]
        if inner.start() >= argument_close:
            return None
        if inner[0] != r'\)':
            return None
        _validate_math_body(text[opener.end():inner.start()])
        return inner_index
    return None


def _is_exact_text_argument_open(text: str, brace: int) -> bool:
    """Whether ``brace`` immediately follows one unescaped ``\\text`` command."""
    command = brace - 5
    if command < 0 or not text.startswith(r'\text{', command):
        return False
    preceding = 0
    cursor = command - 1
    while cursor >= 0 and text[cursor] == '\\':
        preceding += 1
        cursor -= 1
    return preceding % 2 == 0


def _explicit_delimiters(text: str):
    """Yield explicit math delimiters whose backslash is not TeX-escaped.

    A delimiter-looking token is genuine only when an even number of consecutive
    backslashes precede its introducing backslash.  This keeps ``\\\\[6pt]`` in a
    display body as TeX row spacing rather than treating its second backslash as
    a nested ``\\[`` opener, while still recognizing a delimiter following a
    paired row break.
    """
    for match in _DELIM.finditer(text):
        preceding = 0
        cursor = match.start() - 1
        while cursor >= 0 and text[cursor] == '\\':
            preceding += 1
            cursor -= 1
        if preceding % 2 == 0:
            yield match


def _validate_math_body(body: str) -> None:
    if not body.strip():
        raise ValueError('math_empty')
    if '^^' in body:
        raise ValueError('math_encoded_control_unsupported')
    depth = 0
    for token in re.findall(r'\\.|[{}]', body):
        if token == '{': depth += 1
        if token == '}': depth -= 1
        if depth < 0: raise ValueError('math_braces_unbalanced')
    if depth: raise ValueError('math_braces_unbalanced')
    for command in _math_commands(body):
        if command not in SAFE_MATH_COMMANDS:
            raise ValueError('math_command_unsupported')
    envs = []
    environments = re.findall(r'\\(begin|end)\s*\{([^{}]+)\}', body)
    if len(environments) != len(re.findall(r'\\(?:begin|end)\b', body)):
        raise ValueError('math_environment_unsupported')
    for action, env in environments:
        if env not in SAFE_MATH_ENVIRONMENTS:
            raise ValueError('math_environment_unsupported')
        if action == 'begin': envs.append(env)
        elif not envs or envs.pop() != env: raise ValueError('math_environment_unbalanced')
    if envs: raise ValueError('math_environment_unbalanced')
    if re.search(r'(?<!\\)%', body):
        raise ValueError('math_percent_requires_escape')


def _math_commands(body: str):
    """Yield only unescaped alphabetic TeX command names in a math body.

    The second slash in a row separator such as ``\\\\[4pt]`` is not a
    command introducer. Keeping this parity rule alongside delimiter parity
    avoids treating ordinary alignment syntax as a fictitious ``\\j`` command.
    """
    for match in re.finditer(r'\\([A-Za-z]+)', body):
        preceding = 0
        cursor = match.start() - 1
        while cursor >= 0 and body[cursor] == '\\':
            preceding += 1
            cursor -= 1
        if preceding % 2 == 0:
            yield match[1]


def normalize_dollar_display_blocks(text: str) -> tuple[str, int]:
    """Normalize standalone paired ``$$`` display fences outside code.

    Recognizes two forms of an otherwise-empty ``$$`` delimiter line: a
    top-level line, and one nested behind exactly one Markdown blockquote
    marker (``> $$``), matching the renderer's own :func:`is_quote_line`/
    :func:`strip_quote`. An opener and its closer must be the same kind
    (both top-level, or both behind exactly one blockquote marker); every
    intervening line of a blockquoted display must itself be a quoted line,
    so a display can never silently drift out of its blockquote mid-body. A
    code fence is recognized the same way (top-level or single-quoted), so a
    ``$$`` line inside one — quoted or not — remains code.

    This is deliberately not a dollar-math parser. Each accepted delimiter is
    an otherwise empty line (blockquote marker aside), so changing its ``$$``
    to ``\\[``/``\\]`` preserves every byte of the display body, the
    blockquote marker, and all surrounding text.
    """
    output: list[str] = []
    fence: tuple[str, bool] | None = None
    display_open = False
    quoted_display = False
    pairs = 0
    for line in text.splitlines(keepends=True):
        opening = _fence_open(line)
        closing = _fence_close(line)
        if fence is None and opening is not None:
            fence = (opening[0], opening[2])
            output.append(line)
            continue
        if fence is not None:
            output.append(line)
            if closing == fence:
                fence = None
            continue

        quote_body = strip_quote(line).strip() if is_quote_line(line) else None

        if not display_open:
            if _DISPLAY_DOLLAR_LINE.fullmatch(line):
                output.append(line.replace('$$', r'\[', 1))
                display_open, quoted_display = True, False
                continue
            if quote_body == '$$':
                output.append(line.replace('$$', r'\[', 1))
                display_open, quoted_display = True, True
                continue
        elif quoted_display:
            if quote_body == '$$':
                output.append(line.replace('$$', r'\]', 1))
                display_open = False
                pairs += 1
                continue
            if quote_body is None:
                # The blockquote ended (or was never truly nested) before the
                # display closed: fail closed rather than guess a boundary.
                raise ValueError('math_requires_explicit_delimiters')
        else:
            if _DISPLAY_DOLLAR_LINE.fullmatch(line):
                output.append(line.replace('$$', r'\]', 1))
                display_open = False
                pairs += 1
                continue

        if _RAW_DISPLAY_DOLLAR.search(line):
            raise ValueError('math_requires_explicit_delimiters')
        if not display_open and _INLINE_DOLLAR_PAIR.search(line):
            raise ValueError('math_requires_explicit_delimiters')
        output.append(line)
    if display_open:
        raise ValueError('math_requires_explicit_delimiters')
    return ''.join(output), pairs


def _normalize_math_aliases(body: str) -> tuple[str, int]:
    count = 0
    for source, replacement in _MATH_ALIASES:
        occurrences = body.count(source)
        if occurrences:
            body = body.replace(source, replacement)
            count += occurrences
    return body, count


def normalize_math_fences(text: str) -> tuple[str, int]:
    """Convert only complete top-level `````math`` blocks to explicit displays.

    The opener info string must be exactly ``math`` AND top-level (not behind
    a blockquote marker): a one-level quoted ``math`` fence is recognized as
    fenced code like any other quoted fence (see :func:`_fence_open`), and is
    left byte-exact rather than newly authorized as quoted display math. The
    body of a converted top-level block is copied byte for byte; a fence
    inside such a block is ambiguous and is rejected rather than being
    interpreted as Markdown or TeX.
    """
    output: list[str] = []
    fence: tuple[str, bool] | None = None
    math_fence: str | None = None
    count = 0
    for line in text.splitlines(keepends=True):
        opening = _fence_open(line)
        closing = _fence_close(line)
        if fence is not None:
            output.append(line)
            if closing == fence:
                fence = None
            continue
        if math_fence is not None:
            if closing == (math_fence, False):
                output.append("\\]\n" if line.endswith('\n') else r'\]')
                math_fence = None
                count += 1
            elif opening or closing:
                raise ValueError('math_fence_malformed')
            else:
                output.append(line)
            continue
        if opening is not None:
            marker, info, quoted = opening
            if not quoted and info == 'math':
                output.append("\\[\n" if line.endswith('\n') else r'\[')
                math_fence = marker
            else:
                fence = (marker, quoted)
                output.append(line)
        else:
            output.append(line)
    if math_fence is not None:
        raise ValueError('math_fence_malformed')
    return ''.join(output), count


def normalize_escaped_source_snips(text: str) -> tuple[str, int]:
    """Unescape the directive name only when the whole resulting line is valid."""
    output, count = [], 0
    fence: tuple[str, bool] | None = None
    for line in text.splitlines(keepends=True):
        opening = _fence_open(line)
        closing = _fence_close(line)
        if fence is not None:
            output.append(line)
            if closing == fence:
                fence = None
            continue
        if opening is not None:
            fence = (opening[0], opening[2])
            output.append(line)
            continue
        value, ending = line.rstrip('\n'), '\n' if line.endswith('\n') else ''
        if 'SOURCE\\_SNIP' in value:
            candidate = value.replace('SOURCE\\_SNIP', 'SOURCE_SNIP')
            if DIRECTIVE.fullmatch(candidate):
                value, count = candidate, count + 1
        output.append(value + ending)
    return ''.join(output), count


def _is_unambiguous_inline_math(body: str) -> bool:
    """Recognize only an allowed command, ``E[X]``, or one-letter sub/superscript."""
    if not body.strip() or body != body.strip():
        return False
    # Explicit delimiters make an inline-code span structural Markdown/TeX
    # syntax rather than the restricted bare-expression form this repair owns.
    # Preserve the code span verbatim so delimiters remain code-isolated.
    if any(_explicit_delimiters(body)):
        return False
    normalized, _ = _normalize_math_aliases(body)
    try:
        _validate_math_body(normalized)
    except ValueError:
        return False
    return (bool(re.search(r'\\[A-Za-z]+', normalized))
        or bool(re.fullmatch(r'E\[[A-Za-z0-9_{}^]+\]', normalized))
        or bool(re.fullmatch(r'[A-Za-z](?:_[A-Za-z0-9{}]+|\^[A-Za-z0-9{}]+)', normalized)))


def normalize_inline_code_math(text: str) -> tuple[str, int, int]:
    """Convert only inline-code spans proven valid by the restricted math grammar."""
    alias_count = 0
    converted = 0
    def replace(match: re.Match[str]) -> str:
        nonlocal alias_count, converted
        body = match[1]
        if not _is_unambiguous_inline_math(body):
            return match[0]
        body, aliases = _normalize_math_aliases(body)
        alias_count += aliases
        converted += 1
        return r'\(' + body + r'\)'
    output: list[str] = []
    pending: list[str] = []
    fence: tuple[str, bool] | None = None
    for line in text.splitlines(keepends=True):
        opening = _fence_open(line)
        closing = _fence_close(line)
        if fence is None and opening is not None:
            output.append(_INLINE_CODE.sub(replace, ''.join(pending)))
            pending = []
            fence = (opening[0], opening[2])
            output.append(line)
        elif fence is not None:
            output.append(line)
            if closing == fence:
                fence = None
        else:
            pending.append(line)
    output.append(_INLINE_CODE.sub(replace, ''.join(pending)))
    return ''.join(output), converted, alias_count


def normalize_explicit_math_aliases(text: str) -> tuple[str, int]:
    count = 0
    def paired(match: re.Match[str]) -> str:
        nonlocal count
        opener, body, closer = ((match[1], match[2], match[3]) if match[1] is not None
            else (match[4], match[5], match[6]))
        body, aliases = _normalize_math_aliases(body)
        count += aliases
        return opener + body + closer
    pattern = r'(\\\()(.*?)(\\\))|(\\\[)(.*?)(\\\])'
    output: list[str] = []
    pending: list[str] = []
    fence: tuple[str, bool] | None = None
    for line in text.splitlines(keepends=True):
        opening = _fence_open(line)
        closing = _fence_close(line)
        if fence is None and opening is not None:
            output.append(re.sub(pattern, paired, ''.join(pending), flags=re.S))
            pending = []
            fence = (opening[0], opening[2])
            output.append(line)
        elif fence is not None:
            output.append(line)
            if closing == fence:
                fence = None
        else:
            pending.append(line)
    output.append(re.sub(pattern, paired, ''.join(pending), flags=re.S))
    return ''.join(output), count


def prepare_lecture_markdown(text: str) -> str:
    return prepare_lecture_markdown_with_counts(text)[0]


def prepare_lecture_markdown_with_counts(text: str) -> tuple[str, LectureNormalizationCounts]:
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text, dollars = normalize_dollar_display_blocks(text)
    text, fences = normalize_math_fences(text)
    text, snippets = normalize_escaped_source_snips(text)
    text, inline, inline_aliases = normalize_inline_code_math(text)
    text, explicit_aliases = normalize_explicit_math_aliases(text)
    validate_explicit_math(text, strict_prose=True)
    # Normalize only outside code. Inline/fenced examples remain byte-exact.
    def normalize(chunk):
        def reflow(match: re.Match[str]) -> str:
            if match[1] is None:
                return match[0]
            # A pair whose opener and closer lines are both already inside a
            # blockquote (as `normalize_dollar_display_blocks` produces, one
            # delimiter per otherwise-empty quoted line) is already exactly
            # the canonical form: reflowing it here would rebuild the display
            # on fresh unquoted lines and silently drop the blockquote.
            opener_start = chunk.rfind('\n', 0, match.start()) + 1
            opener_end = chunk.find('\n', match.start())
            opener_line = chunk[opener_start:opener_end if opener_end != -1 else len(chunk)]
            closer_start = chunk.rfind('\n', 0, match.end() - 1) + 1
            closer_end = chunk.find('\n', match.end() - 1)
            closer_line = chunk[closer_start:closer_end if closer_end != -1 else len(chunk)]
            if is_quote_line(opener_line) and is_quote_line(closer_line):
                return match[0]
            return "\n\\[\n" + match[1].strip() + "\n\\]\n"
        return re.sub(r"`[^`\n]*`|\\\[(.*?)\\\]", reflow, chunk, flags=re.S)
    output, pending, fence = [], [], None
    for line in text.splitlines(keepends=True):
        opening = _fence_open(line)
        closing = _fence_close(line)
        if fence is None and opening is not None:
            output.append(normalize("".join(pending)))
            pending = []
            fence = (opening[0], opening[2])
            output.append(line)
        elif fence is not None:
            output.append(line)
            if closing == fence:
                fence = None
        else:
            pending.append(line)
    output.append(normalize("".join(pending)))
    return "".join(output), LectureNormalizationCounts(dollars, fences, inline, inline_aliases + explicit_aliases, snippets)


def wrap_display(body: str) -> str:
    """Wrap only top-level arrow steps in a whole boxed workflow."""
    if body.startswith(r'\boxed{') and body.endswith('}') and len(body) > 120:
        inner = body[7:-1]
        depth, start, steps = 0, 0, []
        for m in re.finditer(r'\\[A-Za-z]+|\\.|[{}]', inner):
            token = m[0]
            if token == '{': depth += 1
            elif token == '}': depth -= 1
            elif token == r'\rightarrow' and depth == 0:
                steps.append(inner[start:m.start()].strip() + r' \rightarrow')
                start = m.end()
        if steps:
            steps.append(inner[start:].strip())
            body = r'\boxed{\begin{gathered}' + r' \\ '.join(steps) + r'\end{gathered}}'
    return body
