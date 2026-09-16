"""Static safe-math catalog and frozen-profile compatibility proof."""
from __future__ import annotations

import unittest

from course_compiler.assembly import LogicalTexFile, _PREAMBLE
from course_compiler.compilation import CompiledPdf, compile_pdf
from course_compiler.math_format import prepare_lecture_markdown, validate_explicit_math
from course_compiler.safe_math import (
    FORBIDDEN_TEX_CAPABILITY_CLASSES,
    SAFE_MATH_ACCENTS,
    SAFE_MATH_ARROWS,
    SAFE_MATH_BINARY_OPERATORS,
    SAFE_MATH_CATALOG,
    SAFE_MATH_COMMANDS,
    SAFE_MATH_DELIMITERS,
    SAFE_MATH_ENVIRONMENTS,
    SAFE_MATH_FONTS,
    SAFE_MATH_FUNCTIONS,
    SAFE_MATH_LANGUAGE_VERSION,
    SAFE_MATH_LARGE_OPERATORS,
    SAFE_MATH_RELATIONS,
    SAFE_MATH_SPACING,
    SAFE_MATH_STRUCTURAL_COMMANDS,
    SAFE_MATH_SYMBOLS,
)
from tests.toolchain_support import TEX_MISSING_REASON, TEX_TOOLCHAIN_AVAILABLE


_COMPILER_AVAILABLE = TEX_TOOLCHAIN_AVAILABLE
_COMPILER_MISSING_REASON = TEX_MISSING_REASON


def _command_expressions() -> tuple[str, ...]:
    """One valid frozen-profile invocation for every catalog command."""
    expressions: list[str] = []
    expressions += [rf"\{item.name}" for item in SAFE_MATH_SYMBOLS]
    expressions += [rf"a \{item.name} b" for item in SAFE_MATH_BINARY_OPERATORS]
    expressions += [rf"a \{item.name} b" for item in SAFE_MATH_RELATIONS]
    expressions += [rf"A \{item.name}{{n}} B" for item in SAFE_MATH_ARROWS]
    expressions += [rf"\{item.name} x" for item in SAFE_MATH_FUNCTIONS]
    expressions += [rf"\{item.name}_{{i=1}}^n i" for item in SAFE_MATH_LARGE_OPERATORS]
    for item in SAFE_MATH_DELIMITERS:
        if item.name == "left":
            expressions.append(r"\left( x \right)")
        elif item.name == "right":
            continue
        elif item.name == "middle":
            expressions.append(r"\left( x \middle| y \right)")
        elif item.name in {"big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr", "biggl", "biggr", "Biggl", "Biggr"}:
            expressions.append(rf"\{item.name}(")
        else:
            expressions.append(rf"\{item.name}")
    for item in SAFE_MATH_ACCENTS:
        if item.name in {"overset", "underset", "stackrel"}:
            expressions.append(rf"\{item.name}{{a}}{{b}}")
        else:
            expressions.append(rf"\{item.name}{{x}}")
    for item in SAFE_MATH_FONTS:
        expressions.append(rf"\{item.name} x" if item.name.endswith("style") else rf"\{item.name}{{x}}")
    for item in SAFE_MATH_SPACING:
        expressions.append(rf"\sum\{item.name}_{{i=1}}^n" if item.name in {"limits", "nolimits"} else rf"a \{item.name} b")
    for item in SAFE_MATH_STRUCTURAL_COMMANDS:
        templates = {
            "frac": r"\frac{a}{b}", "dfrac": r"\dfrac{a}{b}", "tfrac": r"\tfrac{a}{b}",
            "sqrt": r"\sqrt[3]{x}", "substack": r"\substack{i\\j}",
            "operatorname": r"\operatorname{argmin}", "text": r"\text{if}",
        }
        if item.name in templates:
            expressions.append(templates[item.name])
    for environment in sorted(SAFE_MATH_ENVIRONMENTS):
        body = (r"a &= b \\ c &= d" if environment in {"aligned", "alignedat"}
                else r"a = b \\ c = d" if environment == "gathered"
                else r"a & b \\ c & d")
        argument = "{2}" if environment == "alignedat" else ""
        expressions.append(rf"\begin{{{environment}}}{argument}{body}\end{{{environment}}}")
    return tuple(expressions)


class SafeMathCatalogTests(unittest.TestCase):
    def test_catalog_is_static_versioned_and_nonoverlapping(self) -> None:
        self.assertEqual(SAFE_MATH_LANGUAGE_VERSION, "safe-math/v1")
        names = [entry.name for entry in SAFE_MATH_CATALOG]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(frozenset(names), SAFE_MATH_COMMANDS)
        self.assertTrue(all(entry.expected_mode == "math" and entry.evidence for entry in SAFE_MATH_CATALOG))

    def test_all_catalog_commands_validate_in_explicit_math(self) -> None:
        for expression in _command_expressions():
            validate_explicit_math(r"\[" + expression + r"\]")

    def test_every_forbidden_capability_and_unknown_command_fail_closed(self) -> None:
        for commands in FORBIDDEN_TEX_CAPABILITY_CLASSES.values():
            for command in commands:
                with self.assertRaises(ValueError, msg=command):
                    prepare_lecture_markdown(rf"\[\{command}{{x}}\]")
        with self.assertRaises(ValueError):
            prepare_lecture_markdown(r"\[\unreviewedmathprimitive{x}\]")
        with self.assertRaises(ValueError):
            prepare_lecture_markdown(r"\[\begin{equation}x\end{equation}\]")

    @unittest.skipUnless(_COMPILER_AVAILABLE, f"frozen XeLaTeX profile unavailable ({_COMPILER_MISSING_REASON})")
    def test_every_catalog_entry_compiles_under_the_frozen_profile(self) -> None:
        displays = "\n".join(r"\[" + expression + r"\]" for expression in _command_expressions())
        tex = _PREAMBLE + "\n" + displays + "\n\\end{document}\n"
        result = compile_pdf(LogicalTexFile("L99.tex", tex))
        self.assertIsInstance(result, CompiledPdf)
