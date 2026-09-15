"""Static, versioned safe mathematical language for untrusted Markdown.

This is deliberately a reviewed catalog, not a view of installed TeX packages.
The frozen Course Compiler profile is the evidence for every entry; package files
are never inspected at runtime and unknown commands are not executable authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final


SAFE_MATH_LANGUAGE_VERSION: Final = "safe-math/v1"
FROZEN_PROFILE_EVIDENCE: Final = (
    "course-tex-assembly/v1: TeX Live 2023; base LaTeX + amsmath 2.17o, "
    "amssymb 3.01, unicode-math"
)


@dataclass(frozen=True)
class SafeMathCommand:
    """One public math-mode command and its bounded invocation class."""

    name: str
    category: str
    argument_shape: str = "leaf"
    expected_mode: str = "math"
    evidence: str = FROZEN_PROFILE_EVIDENCE


def _catalog(category: str, names: str, argument_shape: str = "leaf") -> tuple[SafeMathCommand, ...]:
    return tuple(SafeMathCommand(name, category, argument_shape) for name in names.split())


# Public ordinary symbols. This grouping makes a reviewable language boundary;
# it intentionally excludes package/document and TeX-programming commands.
SAFE_MATH_SYMBOLS = _catalog("symbol", """
alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa
varkappa lambda mu nu xi omicron pi varpi rho varrho sigma varsigma tau upsilon
phi varphi chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega
infty partial nabla ell hbar imath jmath wp Re Im aleph beth gimel daleth emptyset
varnothing top bot angle measuredangle sphericalangle triangle Box Diamond square
blacksquare lozenge blacklozenge clubsuit diamondsuit heartsuit spadesuit
ldots cdots vdots ddots dots
forall exists neg therefore because prime backprime complement mho eth
checkmark
land lor implies iff
""")

SAFE_MATH_BINARY_OPERATORS = _catalog("binary_operator", """
pm mp times div cdot cdotp ast star circ bullet diamond oplus ominus otimes oslash
odot bigcirc dagger ddagger amalg wr cap cup uplus sqcap sqcup vee wedge setminus
smallsetminus triangleleft triangleright lhd rhd unlhd unrhd ltimes rtimes
leftthreetimes rightthreetimes barwedge veebar
curlywedge curlyvee boxminus boxplus boxtimes intercal circledast
circledcirc circleddash
""")

SAFE_MATH_RELATIONS = _catalog("relation", """
neq ne equiv approx sim simeq cong propto le leq leqslant ge geq geqslant ll gg
prec succ preceq succeq preccurlyeq succcurlyeq precsim succsim
subset supset subseteq supseteq subsetneq supsetneq
sqsubset sqsupset sqsubseteq sqsupseteq in notin ni owns mid nmid parallel nparallel
perp models vdash dashv Vdash vDash Vvdash nvdash nvDash nVdash nVDash bowtie
smile frown asymp doteq doteqdot circeq triangleq bumpeq Bumpeq fallingdotseq
risingdotseq lesssim gtrsim lessapprox gtrapprox lessgtr gtrless lesseqgtr gtreqless
lesseqqgtr gtreqqless trianglelefteq trianglerighteq between backsim
backsimeq eqsim eqslantgtr geqq gtrdot
""")

SAFE_MATH_ARROWS = _catalog("arrow", """
leftarrow rightarrow leftrightarrow Leftarrow Rightarrow Leftrightarrow
longleftarrow longrightarrow longleftrightarrow Longleftarrow Longrightarrow
Longleftrightarrow to mapsto longmapsto hookleftarrow hookrightarrow leftharpoonup
leftharpoondown rightharpoonup rightharpoondown rightleftharpoons leftrightharpoons
rightleftarrows leftrightarrows twoheadleftarrow twoheadrightarrow leftleftarrows
rightrightarrows uparrow downarrow updownarrow Uparrow Downarrow Updownarrow nearrow
searrow swarrow nwarrow rightsquigarrow leadsto curvearrowleft curvearrowright
circlearrowleft circlearrowright dashleftarrow dashrightarrow multimap xrightarrow
xleftarrow
""", "optional_label")

SAFE_MATH_FUNCTIONS = _catalog("function", """
sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh coth exp log ln lg lim
limsup liminf sup inf max min det dim ker deg gcd hom arg Pr mod bmod pmod
""")

SAFE_MATH_LARGE_OPERATORS = _catalog("large_operator", """
sum prod coprod int iint iiint iiiint oint bigcap bigcup bigsqcup bigvee bigwedge
bigodot bigotimes bigoplus biguplus
""")

SAFE_MATH_DELIMITERS = _catalog("delimiter", """
left right middle big Big bigg Bigg bigl bigr Bigl Bigr biggl biggr Biggl Biggr
lbrace rbrace langle rangle lvert rvert vert lVert rVert Vert lfloor rfloor lceil
rceil lgroup rgroup backslash
""", "delimiter_or_sizing")

SAFE_MATH_ACCENTS = _catalog("accent", """
hat widehat check breve acute grave tilde widetilde bar overline underline vec dot
ddot dddot ddddot overbrace underbrace overrightarrow overleftarrow overleftrightarrow
underrightarrow underleftarrow underleftrightarrow overset underset stackrel boxed
phantom hphantom vphantom
""", "bounded_brace_arguments")

SAFE_MATH_FONTS = _catalog("font_or_style", """
mathrm mathbf mathit mathsf mathtt mathcal mathbb mathfrak mathscr boldsymbol
displaystyle textstyle scriptstyle scriptscriptstyle
""", "bounded_brace_arguments")

SAFE_MATH_SPACING = _catalog("spacing", """
quad qquad thinspace medspace thickspace enspace limits nolimits
""", "spacing_or_limit_placement")

SAFE_MATH_STRUCTURAL_COMMANDS = _catalog("structural", """
frac dfrac tfrac sqrt substack operatorname text begin end
""", "bounded_structure")

SAFE_MATH_ENVIRONMENTS: Final = frozenset((
    "aligned", "alignedat", "gathered", "cases", "matrix", "pmatrix",
    "bmatrix", "vmatrix", "Vmatrix", "smallmatrix",
))

# Kept as capability classes, rather than a misleadingly incomplete command
# blacklist. The catalog above remains authoritative; these names anchor the
# adversarial regression corpus and public security documentation.
FORBIDDEN_TEX_CAPABILITY_CLASSES: Final = {
    "file_input": ("input", "include", "includeonly", "openin", "read"),
    "file_output": ("openout", "write", "closein", "closeout", "immediate"),
    "macro_definition": ("def", "gdef", "edef", "xdef", "let", "newcommand", "renewcommand"),
    "package_or_document": ("usepackage", "RequirePackage", "documentclass", "includegraphics"),
    "dynamic_control": ("csname", "endcsname", "expandafter", "scantokens"),
    "state_or_category": ("catcode", "mathcode", "lccode", "uccode", "global"),
    "execution": ("write18", "directlua", "special", "shipout"),
    "references": ("label", "ref", "pageref", "eqref", "tag", "href", "url"),
}

SAFE_MATH_CATALOG: Final = (
    SAFE_MATH_SYMBOLS + SAFE_MATH_BINARY_OPERATORS + SAFE_MATH_RELATIONS +
    SAFE_MATH_ARROWS + SAFE_MATH_FUNCTIONS + SAFE_MATH_LARGE_OPERATORS +
    SAFE_MATH_DELIMITERS + SAFE_MATH_ACCENTS + SAFE_MATH_FONTS +
    SAFE_MATH_SPACING + SAFE_MATH_STRUCTURAL_COMMANDS
)
SAFE_MATH_COMMANDS: Final = frozenset(entry.name for entry in SAFE_MATH_CATALOG)
