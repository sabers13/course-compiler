"""T051 GPT compatibility stability checkpoint (pre-L7).

Durable synthetic regression coverage for the complete bounded family of GPT
serialization/math forms accepted through L6. Every case here is invented; no
private course content. This module asserts the already-reviewed compatibility
contract holds together as one coherent boundary -- it does not broaden the
TeX allowlist, change parser behavior, or claim arbitrary-LaTeX support.

SUPPORTED form -> accepted/preserved/canonicalized exactly as intended.
UNSUPPORTED/AMBIGUOUS form -> deterministic fail-closed rejection.
"""
from __future__ import annotations

import hashlib
import unittest

from course_compiler.math_format import (
    prepare_lecture_markdown,
    prepare_lecture_markdown_with_counts,
    validate_explicit_math,
)
from course_compiler.contracts import LectureDocument, SourceProvenance
from course_compiler.legacy_renderer import LegacyMarkdownTexRenderer
from course_compiler.rendering import document_reference
from course_compiler.assembly import assemble_course_tex
from course_compiler.compilation import compile_pdf
from course_compiler.snippet_contract import parse_snippets
from tests.toolchain_support import TEX_MISSING_REASON, TEX_TOOLCHAIN_AVAILABLE

_XELATEX_AVAILABLE = TEX_TOOLCHAIN_AVAILABLE
_XELATEX_MISSING_REASON = TEX_MISSING_REASON


def _render_and_assemble(markdown_source: str):
    """prepare -> validate -> render -> assemble one single-lecture document."""
    prepared = prepare_lecture_markdown(markdown_source)
    document = LectureDocument(
        "lecture-document/v1", "l1", 1, prepared,
        SourceProvenance(hashlib.sha256(prepared.encode()).hexdigest()),
    )
    result = LegacyMarkdownTexRenderer().render(document)
    reference = document_reference(document)
    assembled = assemble_course_tex((reference,), (result,))
    return prepared, result, assembled


class SupportedMathClassMatrix(unittest.TestCase):
    """Section 6: every currently-accepted compatibility class, invented forms."""

    def test_canonical_explicit_inline_and_display(self):
        self.assertEqual(prepare_lecture_markdown(r'\(x\)'), r'\(x\)')
        self.assertIn('x+y', prepare_lecture_markdown('\\[\nx+y\n\\]\n'))

    def test_top_level_gpt_dollar_display(self):
        prepared = prepare_lecture_markdown('$$\nx+y\n$$\n')
        self.assertIn(r'\[', prepared)
        self.assertIn('x+y', prepared)
        self.assertNotIn('$$', prepared)

    def test_single_level_blockquoted_dollar_display(self):
        raw = '> Because of independence,\n>\n> $$\n> x+y\n> $$\n>\n> as claimed.\n'
        prepared = prepare_lecture_markdown(raw)
        self.assertIn('> \\[\n> x+y\n> \\]', prepared)
        self.assertNotIn('$$', prepared)

    def test_math_fence_canonicalization(self):
        prepared = prepare_lecture_markdown('```math\nx+y\n```\n')
        self.assertIn(r'\[', prepared)
        self.assertIn('x+y', prepared)

    def test_restricted_inline_code_math_canonicalization(self):
        prepared = prepare_lecture_markdown('Use `\\theta` and `E[X]` and `m_2`.\n')
        self.assertIn(r'\(\theta\)', prepared)
        self.assertIn(r'\(E[X]\)', prepared)
        self.assertIn(r'\(m_2\)', prepared)

    def test_inline_code_with_explicit_backslash_delimiters_remains_code(self):
        # "Explicit delimiters" in this contract's own vocabulary are the
        # backslash bracket forms (`\(`/`\)`/`\[`/`\]`); an inline-code span
        # carrying them is left untouched by `normalize_inline_code_math`
        # (see `_is_unambiguous_inline_math`'s explicit-delimiter guard).
        for span in ('`\\(x\\)`', '`\\[x\\]`'):
            text = span + '\n'
            self.assertEqual(prepare_lecture_markdown(text), text, msg=span)

    def test_bare_dollar_inside_inline_code_is_not_code_isolated(self):
        # Documented boundary, not a regression: `normalize_dollar_display_
        # blocks` scans lines for bare `$`/`$$` before any inline-code-aware
        # pass runs, and it has no concept of a single-backtick span (only
        # fenced code and blockquote lines). A bare dollar sign was never a
        # supported top-level form either (Section 12), so this is an
        # intentionally-unsupported form failing closed, not a new defect.
        for span in ('`$x$`', '`$$x$$`'):
            with self.assertRaises(ValueError, msg=span):
                prepare_lecture_markdown(span + '\n')

    def test_reviewed_arrow_aliases(self):
        prepared = prepare_lecture_markdown(r'\[A \Longleftrightarrow B\]')
        self.assertIn(r'\iff', prepared)
        self.assertNotIn(r'\Longleftrightarrow', prepared)
        prepared = prepare_lecture_markdown(r'\[A \longleftrightarrow B\]')
        self.assertIn(r'\leftrightarrow', prepared)
        self.assertNotIn(r'\longleftrightarrow', prepared)

    def test_escaped_source_snip_spelling(self):
        raw = '[[SOURCE\\_SNIP source="src-synthetic" page=1 purpose="Diagram"]]\n'
        prepared = prepare_lecture_markdown(raw)
        self.assertIn('[[SOURCE_SNIP source="src-synthetic" page=1 purpose="Diagram"]]', prepared)

    def test_row_breaks_and_optional_spacing(self):
        for spacing in ('', '[4pt]', '[6pt]', '[0pt]', '[-2pt]'):
            body = '\\[\na \\\\' + spacing + '\nb\n\\]'
            validate_explicit_math(body)
            self.assertIn('a \\\\' + spacing, prepare_lecture_markdown(body))

    def test_safe_reviewed_operators_and_neighbors(self):
        for command in ('limsup', 'liminf', 'longmapsto', 'lim', 'sup', 'inf', 'mapsto', 'longrightarrow', 'xrightarrow'):
            source = rf'\[\{command} x\]'
            self.assertIn(rf'\{command}', prepare_lecture_markdown(source), msg=command)

    def test_xrightarrow_with_upper_label_is_preserved(self):
        source = r'\[A \xrightarrow{n} B\]'
        prepared = prepare_lecture_markdown(source)
        validate_explicit_math(prepared)
        self.assertIn(r'\xrightarrow{n}', prepared)

    def test_text_mode_transition_islands(self):
        for source in (
            r'\[\text{before \(x\) after}\]',
            r'\[\boxed{\text{\(x\) versus \(y\)}}\]',
        ):
            prepared = prepare_lecture_markdown(source)
            self.assertIn(r'\text', prepared)
            self.assertEqual(prepared.count(r'\('), source.count(r'\('))

    def test_every_supported_environment_minimal_example(self):
        environments = (
            'aligned', 'alignedat', 'gathered', 'cases', 'matrix', 'pmatrix',
            'bmatrix', 'vmatrix', 'Vmatrix', 'smallmatrix',
        )
        bodies = {
            'aligned': r'a &= b \\ c &= d',
            'alignedat': r'a &= b \\ c &= d',
            'gathered': r'a = b \\ c = d',
            'cases': r'a & x > 0 \\ b & x \le 0',
            'matrix': r'a & b \\ c & d',
            'pmatrix': r'a & b \\ c & d',
            'bmatrix': r'a & b \\ c & d',
            'vmatrix': r'a & b \\ c & d',
            'Vmatrix': r'a & b \\ c & d',
            'smallmatrix': r'a & b \\ c & d',
        }
        for env in environments:
            args = '{2}' if env == 'alignedat' else ''
            source = rf'\[\begin{{{env}}}{args}{bodies[env]}\end{{{env}}}\]'
            prepared = prepare_lecture_markdown(source)
            self.assertIn(rf'\begin{{{env}}}', prepared, msg=env)
            self.assertIn(rf'\end{{{env}}}', prepared, msg=env)

    def test_common_allowed_mathematical_structures_combined(self):
        source = (
            r'\[\frac{a}{b} + \sqrt{c} + \sum_{i=1}^{n} x_i + \prod_{j=1}^{m} y_j '
            r'+ \int_0^1 f(x)\,dx + \lim_{n\to\infty} x_n + x_i^2 + A \subseteq B '
            r'+ \Pr(X \le t) + A \to B + \overline{x} + \|x\| + |x| + \{x\}\]'
        )
        prepared = prepare_lecture_markdown(source)
        for fragment in (r'\frac', r'\sqrt', r'\sum', r'\prod', r'\int', r'\lim',
                          r'\subseteq', r'\Pr', r'\overline', r'\|x\|'):
            self.assertIn(fragment, prepared, msg=fragment)


class MarkdownAdjacencyTests(unittest.TestCase):
    """Section 7: math boundaries stay correct next to ordinary Markdown."""

    def test_math_survives_headings_lists_tables_blockquotes(self):
        source = (
            "# Heading\n\n"
            "Paragraph with \\(x\\) inline.\n\n"
            "- bullet with \\(y\\)\n"
            "1. numbered with \\(z\\)\n\n"
            "> quoted prose with \\(w\\)\n\n"
            "| a | b |\n| --- | --- |\n| \\(x\\) | \\(y\\) |\n\n"
            "```python\nplain code, no math\n```\n\n"
            "`inline code, no math`\n\n"
            '[[SOURCE_SNIP source="src-synthetic" page=1 purpose="Diagram"]]\n'
        )
        prepared = prepare_lecture_markdown(source)
        for token in (r'\(x\)', r'\(y\)', r'\(z\)', r'\(w\)', 'SOURCE_SNIP'):
            self.assertIn(token, prepared)


class CodeIsolationMatrix(unittest.TestCase):
    """Section 8: adversarial code containing TeX-like bytes stays inert."""

    _ADVERSARIAL_PAYLOADS = (
        '$$', '$x$', r'\[', r'\]', r'\(', r'\)', r'\\[4pt]',
        r'\text{\(x\)}', r'\limsup', r'\longmapsto',
        '[[SOURCE_SNIP source="src-synthetic" page=1 purpose="x"]]',
    )

    def test_top_level_fenced_code_is_inert(self):
        for payload in self._ADVERSARIAL_PAYLOADS:
            fenced = '```text\n' + payload + '\n```\n'
            self.assertEqual(prepare_lecture_markdown(fenced), fenced, msg=payload)

    # T051: a fenced code block nested behind exactly one blockquote marker
    # is now isolated end-to-end, the same as a top-level fence -- every
    # preparation stage (dollar/math-fence/inline-code/alias normalization,
    # SOURCE_SNIP unescaping, the final explicit-math validator, and the
    # final display reflow) recognizes the fence via the shared
    # `_fence_open`/`_fence_close` one-level quote contract before treating
    # any byte inside it as prose, TeX, or a directive.
    _BLOCKQUOTED_FENCE_SAFE_PAYLOADS = ('$$', r'$x$', r'\\[4pt]',
        '[[SOURCE_SNIP source="src-synthetic" page=1 purpose="x"]]',
        r'\[', r'\]', r'\(', r'\)', r'\text{\(x\)}', r'\limsup', r'\longmapsto')

    def test_blockquoted_fenced_code_is_inert(self):
        for payload in self._BLOCKQUOTED_FENCE_SAFE_PAYLOADS:
            fenced = '> ```text\n> ' + payload + '\n> ```\n'
            self.assertEqual(prepare_lecture_markdown(fenced), fenced, msg=payload)

    def test_inline_code_spans_are_inert_for_backslash_delimited_payloads(self):
        # A bare allowlisted command like `\limsup` inside a single-backtick
        # span is deliberately NOT in this list: `_is_unambiguous_inline_math`
        # canonicalizes it to `\(\limsup\)` (Section 6.E), which is the
        # already-covered restricted-inline-code-math class, not code
        # isolation. Only payloads carrying an explicit delimiter, or falling
        # outside the restricted grammar, stay untouched as code.
        for payload in (r'\[', r'\]', r'\(', r'\)', r'\\[4pt]', r'\text{\(x\)}',
                        '[[SOURCE_SNIP source="src-synthetic" page=1 purpose="x"]]'):
            span = '`' + payload + '`\n'
            self.assertEqual(prepare_lecture_markdown(span), span, msg=payload)


class DelimiterParityMatrix(unittest.TestCase):
    """Section 9: backslash-parity semantics for delimiter-looking tokens."""

    def test_row_spacing_sequences_are_body_syntax(self):
        # Only an even count of backslashes immediately before a bracket
        # introduces a genuine nested opener; a TeX row break (`\\`, optionally
        # followed by `[<spacing>]`) carries an odd count and is body syntax.
        validate_explicit_math('\\[\na \\\\[4pt]\nb\n\\]')
        validate_explicit_math('\\[\na \\\\\nb\n\\]')

    def test_missing_stray_and_mismatched_closers_fail_closed(self):
        for bad in (r'\[a', r'a\]', r'\(a\]', r'\[a\)'):
            with self.assertRaises(ValueError, msg=bad):
                validate_explicit_math(bad)

    def test_genuine_nested_display_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_explicit_math(r'\[a\[b\]\]')

    def test_direct_nested_inline_inside_display_requires_text_owner(self):
        # Bare nested inline (no \text owner) is rejected...
        with self.assertRaises(ValueError):
            validate_explicit_math(r'\[\(x\)\]')
        # ...while the reviewed \text-owned island is accepted.
        validate_explicit_math(r'\[\text{\(x\)}\]')

    def test_malformed_text_mode_islands_fail_closed(self):
        for bad in (
            r'\[\\text{\(x\)}\]',        # escaped owner
            r'\[\mytext{\(x\)}\]',       # fake/suffix owner
            r'\[\boxed{\(x\)}\]',        # another command context
            r'\[\text{{\(x\)}}\]',       # nested local text brace
            r'\[\text{\[x\]}\]',         # inner display
        ):
            with self.assertRaises(ValueError, msg=bad):
                prepare_lecture_markdown(bad)


class TeXSecurityMatrix(unittest.TestCase):
    """Section 11: unsafe commands and malformed TeX remain rejected."""

    def test_unsafe_commands_are_rejected(self):
        for command in ('unknownoperator', 'newcommand', 'def', 'input',
                         'include', 'usepackage', 'write', 'tag'):
            with self.assertRaises(ValueError, msg=command):
                prepare_lecture_markdown(rf'\[\{command}{{x}}\]')

    def test_unsupported_environment_is_rejected(self):
        with self.assertRaises(ValueError):
            prepare_lecture_markdown(r'\[\begin{unsupported}x\end{unsupported}\]')

    def test_unbalanced_braces_and_raw_percent_are_rejected(self):
        with self.assertRaises(ValueError):
            prepare_lecture_markdown(r'\(\frac{x}{y\)')
        with self.assertRaises(ValueError):
            prepare_lecture_markdown(r'\(x%hide\)')

    def test_encoded_control_form_is_rejected(self):
        with self.assertRaises(ValueError):
            prepare_lecture_markdown(r'\(^^5cinput{secret}\)')

    def test_allowlist_is_unchanged_by_this_checkpoint(self):
        # This checkpoint does not add commands: an invented unsupported
        # command stays an expected-rejection case, never a new allowlist
        # decision.
        with self.assertRaises(ValueError):
            prepare_lecture_markdown(r'\[\notarealcommand{x}\]')

    def test_unknown_arrow_remains_rejected(self):
        with self.assertRaisesRegex(ValueError, 'math_command_unsupported'):
            prepare_lecture_markdown(r'\[A \xRightarrow{n} B\]')


class DollarMathSecurityMatrix(unittest.TestCase):
    """Section 12: dollar-syntax contract stays narrow."""

    def test_paired_display_dollar_in_reviewed_context_is_supported(self):
        prepare_lecture_markdown('$$\nx+y\n$$\n')  # does not raise

    def test_inline_dollar_math_is_rejected(self):
        with self.assertRaises(ValueError):
            prepare_lecture_markdown('Inline $x$ math.\n')

    def test_dollar_embedded_in_prose_is_rejected(self):
        with self.assertRaises(ValueError):
            prepare_lecture_markdown('prose $$ prose\n')

    def test_malformed_unpaired_dollar_is_rejected(self):
        with self.assertRaises(ValueError):
            prepare_lecture_markdown('$$\nx\n')

    def test_quote_depth_drift_is_rejected(self):
        with self.assertRaises(ValueError):
            prepare_lecture_markdown('> $$\n> x\n>> $$\n')


class SourceSnipBoundary(unittest.TestCase):
    """Section 13: SOURCE_SNIP directive grammar and code isolation."""

    def test_canonical_directive_parses(self):
        directive = '[[SOURCE_SNIP source="src-synthetic" page=1 purpose="Diagram"]]'
        snippet = parse_snippets(directive)[0]
        self.assertEqual((snippet.source_id, snippet.page), ('src-synthetic', 1))

    def test_escaped_spelling_normalizes_before_validation(self):
        raw = '[[SOURCE\\_SNIP source="src-synthetic" page=1 purpose="Diagram"]]\n'
        prepared = prepare_lecture_markdown(raw)
        self.assertEqual(len(parse_snippets(prepared)), 1)

    def test_malformed_and_unknown_source_and_invalid_page_are_rejected(self):
        base = '[[SOURCE_SNIP source="src-synthetic" page=1 purpose="Diagram"]]'
        for invalid in ('../secret', '/tmp/file', 'https://site', '..'):
            with self.assertRaises(ValueError, msg=invalid):
                parse_snippets(base.replace('source="src-synthetic"', f'source="{invalid}"'))
        for invalid in ('page=0', 'page=-1', 'page="1"'):
            with self.assertRaises(ValueError, msg=invalid):
                parse_snippets(base.replace('page=1', invalid))

    def test_directive_inside_code_does_not_bind(self):
        self.assertEqual(parse_snippets('```\n[[SOURCE_SNIP invalid]]\n```'), ())

    def test_directive_adjacent_to_math_binds_only_the_directive(self):
        text = '\\(x\\)\n[[SOURCE_SNIP source="src-synthetic" page=1 purpose="Diagram"]]\n'
        self.assertEqual(len(parse_snippets(text)), 1)


class SyntheticCrossFeatureLectureProfiles(unittest.TestCase):
    """Section 14/15: invented lecture-shaped fixtures through the real pipeline."""

    PROFILE_A_MATH_HEAVY = (
        "# Estimation review\n\n"
        "Overall priority: HIGH.\n\n"
        "## Setup\n"
        "The parameter is \\(\\theta\\) and the scale is \\(\\sigma>0\\).\n\n"
        "$$\n"
        "z_i = \\frac{x_i-\\mu}{\\sigma}\n"
        "$$\n\n"
        "## Cases and alignment\n"
        "\\[\n"
        "\\begin{cases} 1 & x > 0 \\\\ 0 & x \\le 0 \\end{cases}\n"
        "\\]\n\n"
        "\\[\n"
        "\\begin{aligned}\n"
        "a &= b \\\\[4pt]\n"
        "c &= d\n"
        "\\end{aligned}\n"
        "\\]\n\n"
        "## Limits and mappings\n"
        "\\[\\limsup_{n\\to\\infty} X_n \\longmapsto L\\]\n\n"
        "## Worked note\n"
        "\\[\\text{decision rule: reject when \\(z_i > 1.96\\)}\\]\n\n"
        "- HIGH: standardization\n"
        "- MEDIUM: assumptions\n\n"
        '[[SOURCE_SNIP source="src-synthetic" page=1 purpose="Worked example"]]\n\n'
        "| Quantity | Symbol |\n| --- | --- |\n| mean | \\(\\mu\\) |\n"
    )

    PROFILE_B_CODE_HEAVY = (
        "# Data pipeline notes\n\n"
        "Overall priority: MEDIUM.\n\n"
        "## Script excerpt\n"
        "```python\n"
        "# literal TeX-like bytes must stay inert\n"
        "text = r'$$ \\\\[4pt] \\\\text{\\\\(x\\\\)} \\\\limsup'\n"
        "print(text)\n"
        "```\n\n"
        "Inline code like `df['x'].sum()` and `\\theta` (a real math token) "
        "and `not_math_at_all` sit side by side.\n\n"
        "## Small amount of math\n"
        "The count is \\(n\\) and the mean is \\(\\bar{x}\\).\n\n"
        "| Column | Type |\n| --- | --- |\n| id | int |\n| value | float |\n\n"
        "- load data\n"
        "- clean data\n\n"
        '[[SOURCE_SNIP source="src-synthetic" page=2 purpose="Script listing"]]\n'
    )

    PROFILE_C_PROSE_HEAVY = (
        "# Course overview\n\n"
        "Overall priority: LOW PRIORITY / SKIM.\n\n"
        "## Background\n"
        "This section is entirely prose, describing the shape of the course "
        "without leaning on notation.\n\n"
        "1. First topic\n2. Second topic\n3. Third topic\n\n"
        "> A short quoted remark carried over from the source discussion.\n\n"
        "| Week | Topic |\n| --- | --- |\n| 1 | Intro |\n| 2 | Review |\n\n"
        '[[SOURCE_SNIP source="src-synthetic" page=3 purpose="Overview slide"]]\n\n'
        "## One equation\n"
        "\\(\\alpha + \\beta = \\gamma\\)\n"
    )

    def _check_profile(self, markdown_source: str):
        prepared, result, assembled = _render_and_assemble(markdown_source)
        self.assertEqual(result.status, 'rendered' if not result.diagnostics
                          else 'rendered_with_warnings')
        self.assertEqual(assembled.status, 'assembled')
        # Determinism: identical input prepares/renders/assembles to
        # byte-identical output across repeated runs.
        prepared_again, result_again, assembled_again = _render_and_assemble(markdown_source)
        self.assertEqual(prepared, prepared_again)
        self.assertEqual(result.tex_fragment, result_again.tex_fragment)
        self.assertEqual(assembled.combined.tex_source, assembled_again.combined.tex_source)
        return assembled

    def test_profile_a_math_heavy_prepares_and_renders(self):
        assembled = self._check_profile(self.PROFILE_A_MATH_HEAVY)
        tex = assembled.combined.tex_source
        for fragment in (r'\begin{cases}', r'\begin{aligned}', r'\limsup', r'\longmapsto', r'\text{decision'):
            self.assertIn(fragment, tex)

    def test_profile_b_code_heavy_keeps_code_isolated(self):
        assembled = self._check_profile(self.PROFILE_B_CODE_HEAVY)
        tex = assembled.combined.tex_source
        # The literal adversarial payload inside the fenced code must survive
        # verbatim (escaped for TeX specials by the renderer, never
        # interpreted as math) and the real math token outside code must be
        # canonicalized.
        self.assertIn(r'\(\theta\)', tex)
        self.assertIn(r'\bar{x}', tex)

    def test_profile_c_prose_heavy_prepares_and_renders(self):
        assembled = self._check_profile(self.PROFILE_C_PROSE_HEAVY)
        self.assertIn(r'\alpha', assembled.combined.tex_source)

    @unittest.skipUnless(_XELATEX_AVAILABLE, f'frozen XeLaTeX profile unavailable ({_XELATEX_MISSING_REASON})')
    def test_representative_profiles_compile_under_frozen_xelatex(self):
        for markdown_source in (self.PROFILE_A_MATH_HEAVY, self.PROFILE_B_CODE_HEAVY, self.PROFILE_C_PROSE_HEAVY):
            _, _, assembled = _render_and_assemble(markdown_source)
            compiled = compile_pdf(assembled.combined)
            self.assertEqual(getattr(compiled, 'status', None), 'compiled', msg=getattr(compiled, 'diagnostics', compiled))
            self.assertTrue(compiled.pdf_content.startswith(b'%PDF'))

    @unittest.skipUnless(_XELATEX_AVAILABLE, f'frozen XeLaTeX profile unavailable ({_XELATEX_MISSING_REASON})')
    def test_compilation_is_byte_identical_across_repeated_runs(self):
        _, _, assembled = _render_and_assemble(self.PROFILE_A_MATH_HEAVY)
        first = compile_pdf(assembled.combined)
        second = compile_pdf(assembled.combined)
        self.assertEqual(first.status, 'compiled')
        self.assertEqual(second.status, 'compiled')
        self.assertEqual(first.pdf_content, second.pdf_content)


class BlockquotedFencedCodeIsolationMatrix(unittest.TestCase):
    """T051: exactly one blockquote level of fenced code is byte-inert
    end-to-end, matching the existing top-level fence contract without
    broadening it to deeper or ambiguous Markdown forms.
    """

    _PAYLOADS = (
        '$$\nx\n$$', r'\(x\)', '$$\nx\n$$', '$x$', '[4pt]',
        r'\Longleftrightarrow', r'\longleftrightarrow', r'\limsup', r'\liminf',
        r'\longmapsto', r'\text{\(x\)}', r'`\theta`',
        '[[SOURCE_SNIP source="src-synthetic" page=1 purpose="literal"]]',
        'SOURCE\\_SNIP',
    )

    def test_quoted_fenced_payload_matrix_is_byte_identical(self):
        for payload in self._PAYLOADS:
            fenced = '> ```text\n' + '\n'.join('> ' + line for line in payload.split('\n')) + '\n> ```\n'
            self.assertEqual(prepare_lecture_markdown(fenced), fenced, msg=payload)

    def test_quoted_math_fence_is_not_newly_converted(self):
        # A quoted ```math``` fence stays fenced code, not a new quoted
        # display-math authorization (Section 17).
        fenced = '> ```math\n> x+y\n> ```\n'
        self.assertEqual(prepare_lecture_markdown(fenced), fenced)

    def test_top_level_math_fence_still_converts(self):
        fenced = '```math\nx+y\n```\n'
        self.assertIn(r'\[', prepare_lecture_markdown(fenced))

    def test_inline_code_math_regression_inside_vs_outside_quoted_code(self):
        outside = '`\\theta`\n'
        self.assertEqual(prepare_lecture_markdown(outside), r'\(\theta\)' + '\n')
        inside = '> ```text\n> `\\theta`\n> ```\n'
        self.assertEqual(prepare_lecture_markdown(inside), inside)

    def test_alias_regression_inside_vs_outside_quoted_code(self):
        outside = '\\[\nA \\Longleftrightarrow B\n\\]\n'
        self.assertIn(r'\iff', prepare_lecture_markdown(outside))
        inside = '> ```text\n> \\[\n> A \\Longleftrightarrow B\n> \\]\n> ```\n'
        self.assertEqual(prepare_lecture_markdown(inside), inside)

    def test_final_reflow_does_not_run_inside_quoted_code(self):
        # A literal, non-canonical `\[ ... \]` spread across several lines
        # inside quoted code must not be reflowed onto fresh unquoted lines --
        # the visible change the final display-reflow stage would otherwise
        # make outside code.
        inside = '> ```text\n> \\[   x   \\]\n> ```\n'
        self.assertEqual(prepare_lecture_markdown(inside), inside)
        outside = 'prose \\[   x   \\] more\n'
        reflowed = prepare_lecture_markdown(outside)
        self.assertNotEqual(reflowed, outside)
        self.assertIn('\\[\nx\n\\]', reflowed)

    def test_known_minimal_reproducer_is_now_inert(self):
        fenced = '> ```text\n> \\[\n> ```\n'
        self.assertEqual(prepare_lecture_markdown(fenced), fenced)

    def test_unsafe_tex_inside_quoted_code_is_not_inspected(self):
        for payload in (r'\unknownoperator', r'\input', r'\usepackage'):
            fenced = '> ```text\n> ' + payload + '\n> ```\n'
            self.assertEqual(prepare_lecture_markdown(fenced), fenced, msg=payload)

    def test_same_unsafe_tex_outside_code_still_rejects(self):
        for payload in (r'\unknownoperator', r'\input', r'\usepackage'):
            with self.assertRaises(ValueError, msg=payload):
                validate_explicit_math('\\[\n' + payload + '\n\\]')

    def test_unclosed_top_level_fence_fails_closed(self):
        with self.assertRaises(ValueError):
            prepare_lecture_markdown('```text\n\\[\n')

    def test_unclosed_quoted_fence_fails_closed(self):
        with self.assertRaises(ValueError):
            prepare_lecture_markdown('> ```text\n> \\[\n')

    def test_quoted_opener_with_unquoted_closer_does_not_close(self):
        # The closer is a different fence identity (unquoted), so the quoted
        # opener never finds its close -- fails closed, matching the existing
        # exact-identity fence contract (no new leniency added).
        with self.assertRaises(ValueError):
            prepare_lecture_markdown('> ```text\n> \\[\n```\n')

    def test_unquoted_opener_with_quoted_closer_does_not_close(self):
        with self.assertRaises(ValueError):
            prepare_lecture_markdown('```text\n\\[\n> ```\n')

    def test_mismatched_marker_does_not_close(self):
        # A tilde closer never closes a backtick opener (or vice versa); the
        # existing contract still fails closed rather than newly tolerating it.
        with self.assertRaises(ValueError):
            prepare_lecture_markdown('> ```text\n> \\[\n> ~~~\n')

    def test_deeper_quote_nesting_remains_out_of_scope(self):
        # Two blockquote markers are deliberately NOT a recognized fence
        # (Section 8/19): this is documented existing behavior, not a new
        # capability, and it is expected to fail closed here because the
        # unrecognized ">> ```" line is scanned as ordinary prose containing
        # a bare, unbalanced display opener.
        with self.assertRaises(ValueError):
            prepare_lecture_markdown('>> ```text\n>> \\[\n>> ```\n')

    def test_apparent_closer_with_trailing_text_does_not_close_any_pass(self):
        # A line that merely starts with the fence marker but carries extra,
        # non-whitespace text is not a real closer for any fence-tracking
        # pass in this module (`_fence_close` requires the marker to be the
        # entire line, aside from trailing whitespace). Every pass -- the
        # dollar-display pass included -- must agree the fence is still open
        # (regression: the dollar pass used to recognize such a line as a
        # closer via the now-removed, prefix-matching `_fence_marker`, while
        # every other pass still considered the fence open -- letting a `$$`
        # display past the fake closer be silently converted to `\[`/`\]`
        # even though later passes still treated those bytes as fenced-code
        # payload). The whole tail, `$$` included, must stay byte-inert.
        text = '```text\npayload\n``` not a real closer\n$$\nx\n$$\n'
        prepared, counts = prepare_lecture_markdown_with_counts(text)
        self.assertEqual(prepared, text)
        self.assertEqual(counts.dollar_display_blocks, 0)

    _CROSS_FEATURE_QUOTED_CODE = (
        '> ```text\n'
        '> \\[\n'
        '> A \\Longleftrightarrow B\n'
        '> \\]\n'
        '> `\\theta`\n'
        '> a \\\\[4pt] b\n'
        '> [[SOURCE_SNIP source="src-synthetic" page=1 purpose="literal"]]\n'
        '> \\unknownoperator\n'
        '> ```\n'
    )

    def test_cross_feature_quoted_code_fixture_is_byte_identical(self):
        self.assertEqual(
            prepare_lecture_markdown(self._CROSS_FEATURE_QUOTED_CODE),
            self._CROSS_FEATURE_QUOTED_CODE,
        )

    PROFILE_D_QUOTED_CODE = (
        "# Ordinary prose\n\n"
        "Overall priority: MEDIUM.\n\n"
        "Some setup text before the excerpt.\n\n"
        + _CROSS_FEATURE_QUOTED_CODE +
        "\n"
        "The real display outside the excerpt still renders as math.\n\n"
        "\\[\n"
        "\\alpha + \\beta = \\gamma\n"
        "\\]\n"
    )

    def test_cross_feature_quoted_code_renders_without_leaking_into_math(self):
        prepared, result, assembled = _render_and_assemble(self.PROFILE_D_QUOTED_CODE)
        # The quoted fenced block survives preparation byte-identical.
        self.assertIn(self._CROSS_FEATURE_QUOTED_CODE, prepared)
        self.assertEqual(result.status, 'rendered' if not result.diagnostics else 'rendered_with_warnings')
        self.assertEqual(assembled.status, 'assembled')
        tex = assembled.combined.tex_source
        # Outside math still renders as math.
        self.assertIn(r'\alpha + \beta = \gamma', tex)
        # The literal, adversarial-looking alias/backslash payload from
        # inside the quoted code never canonicalizes into the alias form.
        self.assertNotIn(r'A \iff B', tex)


if __name__ == '__main__':
    unittest.main()
