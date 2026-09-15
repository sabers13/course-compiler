from __future__ import annotations

import ast
import hashlib
import inspect
import re
import shutil
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest import mock

import course_compiler.legacy_renderer as legacy_renderer
from course_compiler import (
    DOCUMENT_CONTRACT_VERSION,
    LectureDocument,
    LectureRenderer,
    LegacyMarkdownTexRenderer,
    RejectedLecture,
    RenderedLecture,
    RendererFailure,
    RendererIdentity,
    SourceProvenance,
    StructuralMetrics,
)
from course_compiler.assembly import _PREAMBLE, LogicalTexFile
from course_compiler.compilation import (
    CompiledPdf,
    CompilationFailure,
    compile_pdf,
)


FIXTURES = Path(__file__).parent / "fixtures" / "synthetic" / "lecture-document"

_COMPILER_AVAILABLE = shutil.which("latexmk") is not None and shutil.which("xelatex") is not None


def make_document(
    source_text: object,
    *,
    contract_version: object = DOCUMENT_CONTRACT_VERSION,
    document_id: object = "invented-lecture",
    order: object = 1,
    digest: object | None = None,
) -> LectureDocument:
    if digest is None:
        digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()  # type: ignore[union-attr]
    return LectureDocument(
        contract_version=contract_version,  # type: ignore[arg-type]
        document_id=document_id,  # type: ignore[arg-type]
        order=order,  # type: ignore[arg-type]
        source_text=source_text,  # type: ignore[arg-type]
        provenance=SourceProvenance(content_sha256=digest),  # type: ignore[arg-type]
    )


def render(source_text: str, **document_fields: object):
    return LegacyMarkdownTexRenderer().render(make_document(source_text, **document_fields))


class PublicRendererTests(unittest.TestCase):
    def test_public_export_identity_and_protocol(self) -> None:
        renderer = LegacyMarkdownTexRenderer()
        self.assertIsInstance(renderer, LectureRenderer)
        self.assertEqual(
            renderer.identity,
            RendererIdentity(
                renderer_name="course-compiler-legacy-markdown-to-tex",
                renderer_version="2.0.0",
                contract_version="lecture-document/v1",
                render_profile="legacy-markdown-to-tex/v1",
            ),
        )
        self.assertIs(renderer.identity, LegacyMarkdownTexRenderer().identity)
        self.assertFalse(hasattr(renderer, "renderer_name"))
        with self.assertRaises(FrozenInstanceError):
            renderer.identity.renderer_version = "2.0.0"  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            renderer.identity = renderer.identity  # type: ignore[misc]
        with self.assertRaises(TypeError):
            LegacyMarkdownTexRenderer("option")  # type: ignore[call-arg]

    def test_render_signature_is_one_document(self) -> None:
        self.assertEqual(
            tuple(inspect.signature(LegacyMarkdownTexRenderer.render).parameters),
            ("self", "document"),
        )

    def test_non_document_and_subclass_raise_fixed_type_error(self) -> None:
        class DocumentSubclass(LectureDocument):
            pass

        source = "invented"
        digest = hashlib.sha256(source.encode()).hexdigest()
        subclass = DocumentSubclass(
            DOCUMENT_CONTRACT_VERSION,
            "invented",
            1,
            source,
            SourceProvenance(digest),
        )
        for value in (None, source, object(), subclass):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(TypeError) as caught:
                    LegacyMarkdownTexRenderer().render(value)  # type: ignore[arg-type]
                self.assertEqual(str(caught.exception), "document must be exactly LectureDocument")
                self.assertNotIn(source, str(caught.exception))


class ValidationFlowTests(unittest.TestCase):
    def test_every_validation_failure_is_rejected(self) -> None:
        cases = (
            ("unsupported_contract_version", {"contract_version": "lecture-document/v2"}),
            ("invalid_document_id", {"document_id": "."}),
            ("invalid_order", {"order": 0}),
            ("invalid_source_text", {"source_text": "bad\rtext", "digest": "0" * 64}),
            ("invalid_source_digest", {"digest": "BAD"}),
            ("source_digest_mismatch", {"digest": "0" * 64}),
        )
        for code, changes in cases:
            with self.subTest(code=code):
                source = changes.pop("source_text", "invented source")
                result = render(source, **changes)
                self.assertIsInstance(result, RejectedLecture)
                self.assertEqual(result.status, "validation_failed")
                self.assertEqual(tuple(item.code for item in result.diagnostics), (code,))
                self.assertTrue(all(item.severity == "error" for item in result.diagnostics))

    def test_safe_reference_is_retained_only_when_constructible(self) -> None:
        safe = render("invented source", digest="0" * 64)
        unsafe = render("invented source", document_id=".")
        self.assertIsInstance(safe, RejectedLecture)
        self.assertIsNotNone(safe.document)
        self.assertIsInstance(unsafe, RejectedLecture)
        self.assertIsNone(unsafe.document)

    def test_conversion_is_not_entered_after_validation_error(self) -> None:
        with mock.patch.object(legacy_renderer, "_convert_source") as convert:
            result = render("invented source", order=0)
        self.assertIsInstance(result, RejectedLecture)
        convert.assert_not_called()

    def test_empty_short_and_auto_closure_warnings(self) -> None:
        empty = render("")
        short = render("plain invented prose")
        code = render("```python\nvalue = 3")
        display = render("[\nx + 1 = 2")
        self.assertEqual(empty.status, "rendered_with_warnings")
        self.assertEqual(empty.tex_fragment, "")
        self.assertEqual(tuple(item.code for item in empty.diagnostics), ("empty_source",))
        self.assertEqual(tuple(item.code for item in short.diagnostics), ("short_source",))
        self.assertEqual(
            tuple(item.code for item in code.diagnostics),
            ("short_source", "unclosed_code_fence"),
        )
        self.assertIn("\\end{Verbatim}", code.tex_fragment)
        self.assertEqual(code.metrics.input_code_blocks, code.metrics.output_code_blocks)
        self.assertEqual(
            tuple(item.code for item in display.diagnostics),
            ("short_source", "unclosed_display_math"),
        )
        self.assertTrue(display.tex_fragment.endswith("\\]\n"))
        self.assertEqual(
            display.metrics.input_display_math_blocks,
            display.metrics.output_display_math_blocks,
        )
        self.assertEqual(code.diagnostics[1].line, 1)
        self.assertEqual(display.diagnostics[1].line, 1)

    def test_unclosed_quoted_display_is_a_line_aware_renderer_warning(self) -> None:
        source = "\n".join(
            (
                "> " + "invented padding " * 80,
                "> [",
                "> x + 2 = 9",
            )
        )
        self.assertGreaterEqual(len(source), 1000)
        result = render(source)
        self.assertIsInstance(result, RenderedLecture)
        self.assertEqual(result.status, "rendered_with_warnings")
        self.assertEqual(
            tuple((item.code, item.line) for item in result.diagnostics),
            (("unclosed_display_math", 2),),
        )
        self.assertIn("\\[\nx + 2 = 9\n\\]", result.tex_fragment)
        self.assertEqual(result.metrics.input_display_math_blocks, 1)
        self.assertEqual(result.metrics.output_display_math_blocks, 1)

    def test_closed_quoted_display_of_equivalent_length_is_warning_free(self) -> None:
        source = "\n".join(
            (
                "> " + "invented padding " * 80,
                "> [",
                "> x + 2 = 9",
                "> ]",
            )
        )
        self.assertGreaterEqual(len(source), 1000)
        result = render(source)
        self.assertIsInstance(result, RenderedLecture)
        self.assertEqual(result.status, "rendered")
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(result.metrics.input_display_math_blocks, 1)
        self.assertEqual(result.metrics.output_display_math_blocks, 1)


class GoldenFixtureTests(unittest.TestCase):
    def test_exact_synthetic_goldens(self) -> None:
        for stem in ("minimal", "structures", "unclosed-blocks"):
            with self.subTest(stem=stem):
                source = (FIXTURES / f"{stem}.md").read_text(encoding="utf-8")
                expected = (FIXTURES / f"{stem}.tex").read_text(encoding="utf-8")
                result = render(source, document_id=stem)
                self.assertIsInstance(result, RenderedLecture)
                self.assertEqual(result.tex_fragment, expected)

    def test_fixture_metrics_are_exact(self) -> None:
        expected = {
            "minimal": (1557, 3, 1, 0, 0, 0),
            "structures": (448, 28, 2, 1, 1, 1),
            "unclosed-blocks": (112, 6, 1, 0, 1, 0),
        }
        for stem, values in expected.items():
            source = (FIXTURES / f"{stem}.md").read_text(encoding="utf-8")
            metrics = render(source).metrics
            self.assertEqual(
                (
                    metrics.input_characters,
                    metrics.input_lines,
                    metrics.input_headings,
                    metrics.input_code_blocks,
                    metrics.input_display_math_blocks,
                    metrics.input_tables,
                ),
                values,
            )
            self.assertEqual(metrics.input_headings, metrics.output_headings)
            self.assertEqual(metrics.input_code_blocks, metrics.output_code_blocks)
            self.assertEqual(metrics.input_display_math_blocks, metrics.output_display_math_blocks)
            self.assertEqual(metrics.input_tables, metrics.output_tables)


class LegacyProfileTests(unittest.TestCase):
    def test_first_and_later_heading_rules(self) -> None:
        source = "# Lecture\n## Detail\n# Top\n### 2. Numeric detail\n###### Deep"
        fragment = render(source).tex_fragment
        self.assertEqual(fragment.count("\\part*{"), 1)
        self.assertIn("\\part*{\\texorpdfstring{Lecture}{Lecture}}", fragment)
        self.assertIn("\\subsection*{\\texorpdfstring{Detail}{Detail}}", fragment)
        self.assertIn("\\section*{\\texorpdfstring{Top}{Top}}", fragment)
        self.assertIn("\\section*{\\texorpdfstring{2. Numeric detail}{2. Numeric detail}}", fragment)
        self.assertIn("\\subsection*{\\texorpdfstring{Deep}{Deep}}", fragment)

    def test_explicit_math_headings_keep_readable_pdf_bookmarks(self) -> None:
        """A zero compiler exit never proves the navigation text is sound: an
        escaped delimiter silently produced the malformed
        `\\textbackslash{}\\( ... \\)` bookmark class."""

        fragment = render(
            "# Lecture\n"
            "## Parameters \\(\\theta\\), \\(\\mu\\) and \\(\\sigma\\)\n"
            "### Rates \\(\\alpha\\) with \\(x \\le y\\)"
        ).tex_fragment
        self.assertIn(
            "\\subsection*{\\texorpdfstring{Parameters \\(\\theta\\), \\(\\mu\\) and \\(\\sigma\\)}"
            "{Parameters theta, mu and sigma}}",
            fragment,
        )
        self.assertIn("{Rates alpha with x <= y}}", fragment)
        # The typeset half keeps exact math; neither half may carry an
        # escaped delimiter or a collapsed double space.
        self.assertNotIn("\\textbackslash{}", fragment)
        for bookmark in re.findall(r"\\texorpdfstring\{.*?\}\{([^{}]*)\}\}", fragment):
            self.assertNotIn("  ", bookmark)
            self.assertNotIn("\\(", bookmark)
            self.assertNotIn("\\)", bookmark)

    def test_line_oriented_prose_and_all_tex_specials(self) -> None:
        fragment = render("one line\nsecond: \\ & % # _ { } ~ ^ $").tex_fragment
        self.assertIn("one line\n\n", fragment)
        self.assertIn("second:", fragment)
        for expected in (
            r"\textbackslash{}", r"\&", r"\%", r"\#", r"\_\allowbreak{}",
            r"\{", r"\}", r"\textasciitilde{}", r"\textasciicircum{}", r"\$",
        ):
            self.assertIn(expected, fragment)

    def test_strong_emphasis_inline_code_nesting_and_unicode_math(self) -> None:
        fragment = render("**use `a_b` now** and *soft* with (x_1 = 2) β₂ Aⱼ").tex_fragment
        self.assertIn(r"\textbf{use \texttt{a\_b} now}", fragment)
        self.assertIn(r"\emph{soft}", fragment)
        self.assertIn(r"\(x_1 = 2\)", fragment)
        self.assertIn(r"\(\beta_2\)", fragment)
        self.assertIn(r"\(A_j\)", fragment)

    def test_long_inline_code_identifiers_get_targeted_breakpoints(self) -> None:
        fragment = render("See `src-ui-461eabcd54f0908b93a10adb` for details.").tex_fragment
        self.assertIn(r"src-\allowbreak{}ui-\allowbreak{}461eabcd54f0908b93a10adb", fragment)

    def test_long_code_identifiers_and_standalone_inline_math_fit_layout(self) -> None:
        fragment = render(
            "`src-ui-461eabcd54f0908b93a10adb`\n"
            r"\(\text{input}\longrightarrow\text{process}\longrightarrow\text{output}\)."
        ).tex_fragment
        self.assertIn(r"src-\allowbreak{}ui-\allowbreak{}461eabcd54f0908b93a10adb", fragment)
        self.assertIn(r"\ccfitinline{\text{input}\longrightarrow\text{process}\longrightarrow\text{output}}{.}", fragment)

    def test_standalone_inline_math_wraps_single_formula_punctuation_outside(self) -> None:
        for punct in (".", ",", ";", ":", ""):
            with self.subTest(punct=punct or "none"):
                fragment = render(r"\(x+y\)" + punct).tex_fragment
                self.assertIn(r"\ccfitinline{x+y}{" + punct + "}", fragment)
                if punct:
                    # Punctuation must remain outside the math argument, never inside.
                    self.assertNotIn(r"\ccfitinline{x+y" + punct + "}", fragment)

    def test_standalone_inline_math_does_not_collapse_multiple_formulas(self) -> None:
        fragment = render(r"\(a\) and \(b\)").tex_fragment
        self.assertNotIn(r"\ccfitinline", fragment)
        self.assertIn(r"\(a\)", fragment)
        self.assertIn(r"\(b\)", fragment)

    def test_prose_embedded_math_remains_ordinary(self) -> None:
        fragment = render(r"hello \(x\) world").tex_fragment
        self.assertNotIn(r"\ccfitinline", fragment)
        self.assertIn(r"\(x\)", fragment)

    def test_inline_code_underscore_behavior_intact(self) -> None:
        fragment = render("`a_b`").tex_fragment
        self.assertIn(r"\texttt{a\_b}", fragment)
        self.assertNotIn(r"\allowbreak", fragment)

    def test_code_paragraph_stretch_is_local_to_inline_code_paragraphs(self) -> None:
        with_code = render("See `src-ui-461eabcd54f0908b93a10adb` for details.").tex_fragment
        without_code = render("See the invented overview for details.").tex_fragment
        self.assertIn(r"\setlength{\emergencystretch}{12em}", with_code)
        self.assertNotIn(r"\setlength{\emergencystretch}{12em}", without_code)

    def test_backtick_and_tilde_fences_ignore_language(self) -> None:
        source = "```python\nvalue = 1\n```\n~~~ignored\nother = 2\n~~~~"
        result = render(source)
        self.assertEqual(result.metrics.input_code_blocks, 2)
        self.assertEqual(result.metrics.output_code_blocks, 2)
        self.assertEqual(result.tex_fragment.count("\\begin{Verbatim}[breaklines=true,breakanywhere=true]"), 2)
        self.assertNotIn("python", result.tex_fragment)
        self.assertNotIn("ignored", result.tex_fragment)

    def test_ordered_unordered_nested_and_continuation_lists(self) -> None:
        source = "Lead:\n1. First\n2. Second\n   - Nested\n     continuation prose\n* Changed kind\n- Final"
        fragment = render(source).tex_fragment
        self.assertIn("\\begin{enumerate}", fragment)
        self.assertIn("\\item[1.] First", fragment)
        self.assertIn("\\item[2.] Second", fragment)
        self.assertIn("\\begin{itemize}", fragment)
        self.assertIn("\\item Nested", fragment)
        self.assertIn("continuation prose\n\n", fragment)
        self.assertIn("\\item Changed kind", fragment)
        self.assertIn("\\item Final", fragment)

    def test_every_display_marker_and_permissive_closer(self) -> None:
        markers = (("[", "]"), (r"\[", r"\]"), ("# [", "]"), ("$$", "$$"), ("[", "$$"))
        for opener, closer in markers:
            with self.subTest(opener=opener, closer=closer):
                result = render(f"{opener}\nx + y = z\n{closer}")
                self.assertIn("x + y = z", result.tex_fragment)
                if opener == r"\[":
                    self.assertIn(r"\ccfitmath{x + y = z}", result.tex_fragment)
                self.assertEqual(result.metrics.input_display_math_blocks, 1)
                self.assertEqual(result.metrics.output_display_math_blocks, 1)

    def test_table_escaped_pipe_padding_header_and_tabularx(self) -> None:
        source = "| Name | Note | Extra |\n| --- | :---: | ---: |\n| Paper | A \\| B |\n| Felt | C | D |"
        result = render(source)
        fragment = result.tex_fragment
        self.assertIn("\\begin{tabularx}{\\linewidth}{@{}YYY@{}}", fragment)
        self.assertIn("\\textbf{Name} & \\textbf{Note} & \\textbf{Extra}", fragment)
        self.assertIn("Paper & A | B &  \\", fragment)
        self.assertEqual(result.metrics.input_tables, 1)
        self.assertEqual(result.metrics.output_tables, 1)

    def test_blockquote_with_display_and_horizontal_rule(self) -> None:
        source = "> Invented quote\n>\n> [\n> q = 4\n> ]\n\n---"
        result = render(source)
        self.assertIn("\\begin{quote}", result.tex_fragment)
        self.assertIn("\\[\nq = 4\n\\]", result.tex_fragment)
        self.assertIn("\\medskip\\hrule\\medskip", result.tex_fragment)
        self.assertEqual(result.metrics.input_display_math_blocks, 1)
        self.assertEqual(result.metrics.output_display_math_blocks, 1)

    def test_blockquoted_gpt_dollar_display_survives_real_preparation_and_render(self) -> None:
        """T051 prospective real L4 review: the reviewed application rejected
        a raw ChatGPT response using `> $$` / `> $$` for blockquoted display
        math with `math_requires_explicit_delimiters`. This drives the exact
        product route (prepare_lecture_markdown, then the legacy renderer)
        end to end, proving the blockquote survives as one quote environment
        with no leaked '>' inside the equation. Synthetic fixture only."""
        from course_compiler.math_format import prepare_lecture_markdown

        raw = (
            "> explanatory prose " + "invented padding " * 60 + "\n"
            ">\n"
            "> $$\n"
            "> x^2 + y^2\n"
            "> $$\n"
            ">\n"
            "> concluding prose " + "invented padding " * 5 + "\n"
        )
        self.assertGreaterEqual(len(raw), 1000)
        prepared = prepare_lecture_markdown(raw)
        self.assertNotIn("$$", prepared)
        result = render(prepared)
        self.assertIsInstance(result, RenderedLecture)
        self.assertEqual(result.status, "rendered")
        self.assertEqual(result.diagnostics, ())
        fragment = result.tex_fragment
        # Exactly one blockquote/quote environment, wrapping one display.
        self.assertEqual(fragment.count("\\begin{quote}"), 1)
        self.assertEqual(fragment.count("\\end{quote}"), 1)
        self.assertEqual(result.metrics.input_display_math_blocks, 1)
        self.assertEqual(result.metrics.output_display_math_blocks, 1)
        # The equation body is exactly `x^2 + y^2`, with no quote marker
        # inside it (Markdown '>' never became a mathematical operator).
        self.assertIn("x^2 + y^2", fragment)
        self.assertNotIn(">\nx^2", fragment)
        self.assertNotIn("x^2 + y^2 >", fragment)
        self.assertNotIn(">", re.search(r"\\\[\n(.*?)\n\\\]", fragment, re.S)[1])
        quote_start = fragment.index("\\begin{quote}")
        quote_end = fragment.index("\\end{quote}") + len("\\end{quote}")
        quote_block = fragment[quote_start:quote_end]
        self.assertIn("x^2 + y^2", quote_block)
        # Surrounding quote prose is preserved inside that one environment.
        self.assertIn("explanatory prose", quote_block)
        self.assertIn("concluding prose", quote_block)

    def test_unrecognized_markdown_is_escaped_prose(self) -> None:
        fragment = render("[label](target) ![image](asset) <b>tag</b>\nTitle\n===").tex_fragment
        self.assertIn("[label](target)", fragment)
        self.assertIn("![image](asset)", fragment)
        self.assertIn("<b>tag</b>", fragment)
        self.assertNotIn("\\href", fragment)
        self.assertNotIn("\\includegraphics", fragment)
        self.assertNotIn("\\section", fragment)

    def test_missing_heading_and_empty_source_are_supported(self) -> None:
        missing = render("invented plain text")
        empty = render("")
        self.assertNotIn("\\part", missing.tex_fragment)
        self.assertEqual(missing.metrics.input_headings, 0)
        self.assertEqual(empty.tex_fragment, "")
        self.assertEqual(empty.metrics.input_characters, 0)
        self.assertEqual(empty.metrics.input_lines, 0)


@unittest.skipUnless(_COMPILER_AVAILABLE, "frozen XeLaTeX profile unavailable")
class StandaloneLayoutCompileTests(unittest.TestCase):
    """Behavioral compiler regressions for the two release defects.

    Each test drives the actual renderer, the actual assembly preamble,
    and the actual local compiler on invented content only.
    """

    def compile_fragment(self, source_text: str):
        result = render(source_text)
        self.assertIsInstance(result, RenderedLecture)
        tex = _PREAMBLE + result.tex_fragment + "\\end{document}\n"
        return compile_pdf(LogicalTexFile("L99.tex", tex))

    def test_code_heavy_paragraph_compiles_without_overflow(self) -> None:
        ids = " ".join(
            "`src-ui-%024x`" % (index * 2654435761 % 16**24) for index in range(10)
        )
        prose = (
            " invented padding prose referencing pages seven through thirteen with"
            " channel notes and review markers for the layout regression study." * 6
        ).strip()
        source = "The invented review bundle references " + ids + " " + prose
        fragment = render(source).tex_fragment
        # The local flexibility must be engaged for this paragraph.
        self.assertIn(r"\setlength{\emergencystretch}{12em}", fragment)
        compiled = self.compile_fragment(source)
        self.assertIsInstance(compiled, CompiledPdf)

    def test_modestly_oversized_standalone_math_takes_bounded_fit(self) -> None:
        body = "+".join("x_{%d}" % index for index in range(1, 20)) + " = y"
        compiled = self.compile_fragment("\\(" + body + "\\).")
        self.assertIsInstance(compiled, CompiledPdf)

    def test_breakable_oversized_standalone_math_is_not_shrunk(self) -> None:
        body = "=".join("w_{%d}" % index for index in range(1, 30))
        fragment = render("\\(" + body + "\\).").tex_fragment
        self.assertIn(r"\ccfitinline{", fragment)
        compiled = self.compile_fragment("\\(" + body + "\\).")
        self.assertIsInstance(compiled, CompiledPdf)

    def test_indivisible_oversized_math_is_not_silently_shrunk(self) -> None:
        compiled = self.compile_fragment("\\(" + "7" * 120 + "\\).")
        self.assertIsInstance(compiled, CompilationFailure)
        self.assertIn(
            "compiler_overflow",
            tuple(item.code for item in compiled.diagnostics),
        )


class MathRepairTests(unittest.TestCase):
    def display(self, *lines: str) -> str:
        return render("[\n" + "\n".join(lines) + "\n]").tex_fragment

    def test_subscript_boldsymbol_and_error_subscript_repairs(self) -> None:
        fragment = self.display(
            r"a)*+b + \hat\alpha*\lambda + \boldsymbol{x}",
            r"\operatorname{err}*{\mathrm{test}}",
        )
        self.assertIn(r"a)_+b", fragment)
        self.assertIn(r"\hat\alpha_\lambda", fragment)
        self.assertIn(r"\symbf{x}", fragment)
        self.assertIn(r"\operatorname{err}_{\mathrm{test}}", fragment)

    def test_spacing_differential_percentage_and_texttt_repairs(self) -> None:
        fragment = self.display(
            r"x, \mathbf 1{A} + 1,b^{[2]} + f,dx",
            r"a,\middle|,b + \delta,\operatorname{sign}(x)",
            r"p;\times;q + 25% + \texttt{initial_state#1}",
        )
        self.assertIn(r"x \mathbf{1}\{A\}", fragment)
        self.assertIn(r"1\,b^{[2]}", fragment)
        self.assertIn(r"f\,dx", fragment)
        self.assertIn(r"a\middle|b", fragment)
        self.assertIn(r"\delta\operatorname{sign}", fragment)
        self.assertIn(r"p\;\times\;q", fragment)
        self.assertIn(r"25\%", fragment)
        self.assertIn(r"\texttt{initial\_state\\#1}", fragment)

    def test_visible_set_brace_families(self) -> None:
        fragment = self.display(
            r"x \in{a,b}, y \notin{c}, A \subseteq{B}",
            r"S={1,2}, {-1,+1}, \min{3,4}",
            r"\operatorname{median}{5,6}, \text{uniform distribution on }{7,8}",
        )
        for expected in (
            r"\in\{a,b\}", r"\notin\{c\}", r"\subseteq\{B\}",
            r"S=\{1,2\}", r"\{-1,+1\}", r"\min\{3,4\}",
            r"\operatorname{median}\{5,6\}",
            r"\text{uniform distribution on }\{7,8\}",
        ):
            self.assertIn(expected, fragment)

    def test_multiline_fraction_and_left_right_compaction(self) -> None:
        fraction = self.display(r"\frac{", "a + b", "}{", "c + d", "}")
        delimiters = self.display(r"\left(", "x + y", r"\right)")
        self.assertIn(r"\frac{ a + b }{ c + d }", fraction)
        self.assertIn(r"\left( x + y \right)", delimiters)

    def test_matrix_row_break_repairs(self) -> None:
        fragment = self.display(r"1\x_1\\vdots\x_p", r"\theta_0\\theta_1", r"1\0", r"a,[2mm]")
        self.assertIn(r"1\\x_1\\\vdots\\x_p", fragment)
        self.assertIn(r"\theta_0\\\theta_1", fragment)
        self.assertIn(r"1\\0", fragment)
        self.assertIn(r"a,\\[2mm]", fragment)

    def test_left_right_braces_and_long_text_math_shrinking(self) -> None:
        long_text = "invented words " * 8
        fragment = self.display(r"\left{x \right}", rf"\text{{{long_text}}}")
        self.assertIn(r"\left\{x \right\}", fragment)
        self.assertIn(r"\text{\small invented words", fragment)


class FailureAndPurityTests(unittest.TestCase):
    def test_injected_structural_mismatch_fails_without_fragment(self) -> None:
        source = "private-partial-sentinel"
        metrics = StructuralMetrics(
            input_characters=len(source),
            input_lines=1,
            input_headings=1,
            output_headings=0,
            input_code_blocks=0,
            output_code_blocks=0,
            input_display_math_blocks=0,
            output_display_math_blocks=0,
            input_tables=0,
            output_tables=0,
        )
        injected = legacy_renderer._Conversion(source, metrics)
        with mock.patch.object(legacy_renderer, "_convert_source", return_value=injected):
            result = render(source)
        self.assertIsInstance(result, RendererFailure)
        self.assertEqual(tuple(item.code for item in result.diagnostics), ("structural_postcondition_mismatch",))
        self.assertFalse(hasattr(result, "tex_fragment"))
        self.assertNotIn(source, repr(result))

    def test_injected_exception_is_contained_and_redacted(self) -> None:
        secret = "private-exception-sentinel"
        with mock.patch.object(legacy_renderer, "_convert_source", side_effect=ValueError(secret)):
            result = render("invented valid source")
        self.assertIsInstance(result, RendererFailure)
        self.assertEqual(tuple(item.code for item in result.diagnostics), ("renderer_exception",))
        self.assertFalse(hasattr(result, "tex_fragment"))
        self.assertNotIn(secret, repr(result))

    def test_base_exception_is_not_contained(self) -> None:
        class StopRendering(BaseException):
            pass

        with mock.patch.object(legacy_renderer, "_convert_source", side_effect=StopRendering):
            with self.assertRaises(StopRendering):
                render("invented valid source")

    def test_repeated_render_and_document_identity_independence(self) -> None:
        source = "# Invented\n\nA value (x_1 = 2)."
        first = render(source, document_id="first", order=1)
        repeat = render(source, document_id="first", order=1)
        other = render(source, document_id="second", order=9999)
        self.assertEqual(first, repeat)
        self.assertEqual(first.tex_fragment, other.tex_fragment)
        self.assertEqual(first.metrics, other.metrics)
        self.assertNotEqual(first.document, other.document)

    def test_repr_and_programming_exception_redact_content(self) -> None:
        secret = "private-render-source-sentinel"
        result = render(secret)
        self.assertNotIn(secret, repr(result))
        with self.assertRaises(TypeError) as caught:
            LegacyMarkdownTexRenderer().render(secret)  # type: ignore[arg-type]
        self.assertNotIn(secret, str(caught.exception))

    def test_production_module_has_only_pure_standard_library_imports(self) -> None:
        path = Path("course_compiler/legacy_renderer.py")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        forbidden_imports = {
            "legacy", "os", "pathlib", "shutil", "subprocess", "tempfile", "socket",
            "urllib", "http", "json", "pickle", "marshal", "requests",
        }
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module.split(".")[0])
        self.assertFalse(forbidden_imports & imports)
        self.assertTrue(imports <= {"__future__", "re", "dataclasses", "typing"})
        forbidden_calls = {"open", "exec", "eval", "compile", "__import__"}
        called_names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertFalse(forbidden_calls & called_names)


if __name__ == "__main__":
    unittest.main()
