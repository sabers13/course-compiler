"""Invented cross-domain GPT math grammar and canonical prompt regressions."""
from __future__ import annotations

import hashlib
import unittest

from course_compiler.assembly import assemble_course_tex
from course_compiler.compilation import CompiledPdf, compile_pdf
from course_compiler.contracts import LectureDocument, SourceProvenance
from course_compiler.legacy_renderer import LegacyMarkdownTexRenderer
from course_compiler.math_format import prepare_lecture_markdown
from course_compiler.providers.chatgpt_relay import CONTRACT_PATH
from course_compiler.rendering import document_reference
from tests.toolchain_support import TEX_MISSING_REASON, TEX_TOOLCHAIN_AVAILABLE


_COMPILER_AVAILABLE = TEX_TOOLCHAIN_AVAILABLE
_COMPILER_MISSING_REASON = TEX_MISSING_REASON

_DOMAIN_LECTURES = (
    r"""# Probability

For \(A,B\), independence means \(\Pr(A \cap B)=\Pr(A)\Pr(B)\).

\[
\mathbb{E}[X] = \sum_{x \in \mathcal{X}} x\,\Pr(X=x), \qquad
\operatorname{Var}(X) = \mathbb{E}[(X-\mathbb{E}X)^2].
\]
""",
    r"""# Statistics

The estimator \(\widehat{\theta}_n\) has score \(\partial \ell(\theta)/\partial\theta\).

\[
\sqrt{n}(\widehat{\theta}_n-\theta_0) \xrightarrow{D} N(0,I(\theta_0)^{-1}).
\]
""",
    r"""# Linear algebra

For \(A\in\mathbb{R}^{m\times n}\), compare \(\lVert Ax\rVert\) with \(\lVert x\rVert\).

\[
\begin{pmatrix}a&b\\c&d\end{pmatrix}
\begin{pmatrix}x\\y\end{pmatrix} = \lambda
\begin{pmatrix}x\\y\end{pmatrix}.
\]
""",
    r"""# Calculus

The gradient is \(\nabla f(x)\), and a Hessian uses second partial derivatives.

\[
\limsup_{n\to\infty} a_n \le \liminf_{n\to\infty} b_n,
\qquad \int_0^1 x^2\,dx=\tfrac13.
\]
""",
    r"""# Optimization

The feasible set is \(\{x: g(x)\le0\}\).

\[
\begin{aligned}
x^* &\in \operatorname{argmin}_{x\in\mathbb{R}^d} f(x),\\
\nabla f(x^*) + \sum_i \lambda_i\nabla g_i(x^*) &= 0.
\end{aligned}
\]
""",
    r"""# Machine learning

- A loss can be \(\ell(y,\widehat y)\).
- A table entry may contain \(O(n\log n)\).

> The update is \(w_{t+1}=w_t-\eta\nabla L(w_t)\).
""",
    r"""# Logic and sets

\[
\forall x\in A,\quad x\notin B \implies x\in A\setminus B,
\qquad A\subseteq B \iff A\cap B=A.
\]

| relation | notation |
| --- | --- |
| mapping | \(f:A\longmapsto B\) |
""",
)


class CanonicalPromptTests(unittest.TestCase):
    def test_v2_is_active_compact_and_states_canonical_math(self) -> None:
        text = CONTRACT_PATH.read_text(encoding="utf-8")
        self.assertEqual(CONTRACT_PATH.name, "course-authoring-v2.md")
        self.assertIn("semantic authoring contract v2", text)
        for required in ("`\\( ... \\)`", "`\\[ ... \\]`", "`aligned`", "`gathered`",
                         "`cases`", "`$$...$$`", "custom macros", "silently normalize"):
            self.assertIn(required, text)
        self.assertLess(len(text.encode("utf-8")), 8 * 1024)


class CrossDomainMathGrammarTests(unittest.TestCase):
    def test_invented_cross_domain_lectures_prepare_to_canonical_explicit_math(self) -> None:
        self.assertEqual(len(_DOMAIN_LECTURES), 7)
        prepared_lectures = []
        for source in _DOMAIN_LECTURES:
            prepared = prepare_lecture_markdown(source)
            self.assertIn(r"\(", prepared)
            prepared_lectures.append(prepared)
        self.assertTrue(any(r"\[" in prepared for prepared in prepared_lectures))

    @unittest.skipUnless(_COMPILER_AVAILABLE, f"frozen XeLaTeX profile unavailable ({_COMPILER_MISSING_REASON})")
    def test_invented_cross_domain_lectures_render_assemble_and_compile(self) -> None:
        renderer = LegacyMarkdownTexRenderer()
        references, rendered = [], []
        for order, source in enumerate(_DOMAIN_LECTURES, 1):
            prepared = prepare_lecture_markdown(source)
            document = LectureDocument(
                "lecture-document/v1", f"l{order}", order, prepared,
                SourceProvenance(hashlib.sha256(prepared.encode("utf-8")).hexdigest()),
            )
            references.append(document_reference(document))
            rendered.append(renderer.render(document))
        assembled = assemble_course_tex(tuple(references), tuple(rendered))
        result = compile_pdf(assembled.combined)
        self.assertIsInstance(result, CompiledPdf)
