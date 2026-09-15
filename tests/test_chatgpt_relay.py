"""Semantic contract, format repair, plan completeness and local identity tests."""
import hashlib
import re
from pathlib import Path
import unittest
from types import SimpleNamespace
from course_compiler.providers.chatgpt_relay import CONTRACT_PATH, build_semantic_prompt, bind_semantic_text
from course_compiler.lecture_plan import parse_plan, lecture_ids_from_artifact
from course_compiler.math_format import (normalize_dollar_display_blocks, prepare_lecture_markdown,
    prepare_lecture_markdown_with_counts, validate_explicit_math, wrap_display)
from course_compiler.compilation import inspect_compiler_log
from course_compiler.contracts import LectureDocument, SourceProvenance
from course_compiler.legacy_renderer import LegacyMarkdownTexRenderer, _convert_inline, _display_tex
from course_compiler.snippet_contract import parse_snippets
from test_t051_product_parity import PLAN, LECTURE, GUIDANCE


class SemanticContractTests(unittest.TestCase):
    def test_canonical_contract_and_package_references(self):
        text = CONTRACT_PATH.read_text()
        for clause in ('45–90 minutes','LOW PRIORITY / SKIM','Exam-driven is not exam-only',
            'model-derived solution','Overall priority + reason','Section-level priority',
            'Exam-recognition rules','Reusable workflow/recipe','Worked examples',
            'What to remember for the exam','One-paragraph revision version',
            'page/slide','coverage obligation','Common mistakes','source exercises',
            'Non-negotiable approved-plan execution','an obligation, not a suggestion',
            'does not satisfy that obligation','A worked example shows reasoning',
            'Bullets support explanation; they do not replace it',
            'silently check that every active Scope obligation is taught',r'\( ... \)',r'\[ ... \]'):
            self.assertIn(clause,text)
        root = CONTRACT_PATH.parents[1]
        for file in ('SKILL.md','references/coarse-workflow.md','references/lecture-authoring.md'):
            self.assertIn('course-authoring-v2.md',(root/file).read_text())
        prompt = build_semantic_prompt(SimpleNamespace(kind='lecture_generation',input_refs=('l2',)),
            SimpleNamespace(lecture_map=SimpleNamespace(content=PLAN)), (), GUIDANCE)
        self.assertTrue(prompt.startswith(text))
        self.assertIn(GUIDANCE,prompt)
        self.assertIn(PLAN,prompt)
        self.assertIn(parse_plan(PLAN)[1].markdown,prompt)

    def test_contract_makes_named_exercise_execution_and_self_check_explicit(self):
        text = CONTRACT_PATH.read_text()
        self.assertIn('If Coverage says to solve, work, derive or demonstrate a named exercise', text)
        self.assertRegex(text, r'include the\s+actual worked solution or derivation')
        self.assertIn('Do not output that self-check, a checklist, JSON, or other', text)

    def test_map_prompt_makes_optional_priority_review_actionable_without_protocol(self):
        evidence = (
            SimpleNamespace(evidence_id='art-assessment', label='source_assessment', evidence_kind='artifact_text'),
            SimpleNamespace(evidence_id='art-priority', label='priority_proposal', evidence_kind='artifact_text'),
            SimpleNamespace(evidence_id='art-hierarchy', label='evidence_hierarchy', evidence_kind='artifact_text'),
            SimpleNamespace(evidence_id='art-review', label='priority_evidence_review', evidence_kind='artifact_text'),
            SimpleNamespace(evidence_id='src-invented', label='source-1', evidence_kind='source_pdf'),
        )
        original = tuple((item.evidence_id, item.label, item.evidence_kind) for item in evidence)
        prompt = build_semantic_prompt(
            SimpleNamespace(kind='lecture_map_generation', input_refs=()), SimpleNamespace(), evidence
        )
        self.assertIn('owner-approved priority basis', prompt)
        self.assertIn('accepted semantic review of the approved priority basis', prompt)
        self.assertIn('Incorporate its supported findings', prompt)
        self.assertIn('does not grant owner approval', prompt)
        self.assertIn('Synthesize the approved priority basis, accepted priority review, and official source evidence', prompt)
        self.assertIn('Return the plan only.', prompt)
        for forbidden in ('request_id', 'operation_id', 'holder_id', 'subject_sha256', 'Base64', 'Return JSON'):
            self.assertNotIn(forbidden, prompt)
        self.assertEqual(original, tuple((item.evidence_id, item.label, item.evidence_kind) for item in evidence))

    def test_map_prompt_omits_review_instruction_when_review_is_not_available(self):
        evidence = (
            SimpleNamespace(evidence_id='art-assessment', label='source_assessment', evidence_kind='artifact_text'),
            SimpleNamespace(evidence_id='art-priority', label='priority_proposal', evidence_kind='artifact_text'),
            SimpleNamespace(evidence_id='art-hierarchy', label='evidence_hierarchy', evidence_kind='artifact_text'),
            SimpleNamespace(evidence_id='src-invented', label='source-1', evidence_kind='source_pdf'),
        )
        prompt = build_semantic_prompt(
            SimpleNamespace(kind='lecture_map_generation', input_refs=()), SimpleNamespace(), evidence
        )
        self.assertIn('# Lecture-map evidence roles', prompt)
        self.assertIn('owner-approved priority basis', prompt)
        self.assertNotIn('accepted semantic review', prompt)
        self.assertNotIn('priority_evidence_review attachment', prompt)
        self.assertIn('Return the plan only.', prompt)

    def test_rich_plan_requires_actual_obligations_and_orders_locally(self):
        self.assertEqual(lecture_ids_from_artifact(PLAN.encode()),('l1','l2'))
        for field in ('Scope','Priority','Priority reason','Sources','Topics','Prerequisites','Coverage'):
            import re
            bad = re.sub('^'+field+r':.*\n','',PLAN,count=1,flags=re.M)
            with self.assertRaises(ValueError): parse_plan(bad)
        with self.assertRaises(ValueError): parse_plan('l1|l2')
        with self.assertRaises(ValueError): parse_plan(PLAN.replace('## l2','## l1'))
        self.assertEqual(lecture_ids_from_artifact(b'l1|l2'),('l1','l2'))

    def test_indented_field_labels_are_normalized_at_the_correct_structural_location(self):
        # Regression (T051 private golden Product E2E, Phase 1): a real
        # ChatGPT lecture-map response rendered 'Prerequisites:' and
        # 'Coverage:' with two leading spaces in every block (inherited
        # from the preceding bulleted Topics list's continuation depth).
        # The strict flush-left parser treated those lines as continuation
        # text instead of field labels and raised incomplete_lecture_plan
        # for an otherwise fully specified plan. Synthetic fixture only;
        # no real course content.
        indented = re.sub(r'(?m)^(Prerequisites|Coverage):', r'  \1:', PLAN)
        self.assertNotEqual(indented, PLAN)
        flush_left = parse_plan(PLAN)
        normalized = parse_plan(indented)
        self.assertEqual(len(flush_left), len(normalized))
        for expected, actual in zip(flush_left, normalized):
            self.assertEqual(expected.lecture_id, actual.lecture_id)
            self.assertEqual(expected.title, actual.title)
            self.assertEqual(expected.fields, actual.fields)

        # A genuinely incomplete plan (a required field truly absent) must
        # still fail, indentation aside.
        missing_coverage = re.sub(r'(?m)^Coverage:.*\n?', '', PLAN, count=1)
        with self.assertRaises(ValueError): parse_plan(missing_coverage)

        # Deep/ambiguous indentation is never reinterpreted as a field: it
        # remains ordinary continuation text of whatever field is open.
        deeply_indented = re.sub(r'(?m)^(Prerequisites|Coverage):', r'      \1:', PLAN)
        with self.assertRaises(ValueError): parse_plan(deeply_indented)

        # An indented label out of the field's structural position (here,
        # 'Coverage:' appearing before 'Prerequisites:' has been collected)
        # is never promoted to a field either; the real Coverage line is
        # then a duplicate-looking continuation and the block stays
        # incomplete rather than silently accepting the wrong content.
        reordered = PLAN.replace(
            'Prerequisites: Arithmetic and squaring.\nCoverage:',
            '  Coverage: out of place\nPrerequisites: Arithmetic and squaring.\n',
        )
        with self.assertRaises(ValueError): parse_plan(reordered)

    def test_explicit_math_survives_markdown_and_prose_escaping(self):
        math = r'\(\theta_i^2+\frac{\mu}{\sigma}+\alpha\)'
        tex = _convert_inline('**Before '+math+' after** & 10%')
        self.assertIn(math,tex)
        self.assertIn(r'\textbf{Before',tex)
        self.assertIn(r'\& 10\%',tex)
        self.assertNotIn(r'\textbackslash{}\(',tex)
        self.assertIn(r'\textbf{use \texttt{a\_b} now}',_convert_inline('**use `a_b` now**'))
        display = r'\frac{\theta_i^2-\mu}{\sigma^2}+\alpha'
        self.assertIn(display,_display_tex([display],explicit=True))

    def test_format_repair_is_bounded_and_semantics_preserving(self):
        self.assertEqual(prepare_lecture_markdown(r'Before \[x_i^2\] after.'),'Before \n\\[\nx_i^2\n\\]\n after.')
        self.assertEqual(prepare_lecture_markdown('```tex\n'+r'\[x\]'+'\n```'), '```tex\n'+r'\[x\]'+'\n```')
        for bad in (r'\(x',r'x\)',r'\(\frac{x}{y\)',r'\(\input{secret}\)',r'\(x%hide\)',r'\(\end {document}\)',r'\(\begin aligned x\)',r'\(^^5cinput{secret}\)',r'\[\begin{aligned}x\]',r'raw \theta', 'raw θ'):
            with self.assertRaises(ValueError,msg=bad): prepare_lecture_markdown(bad)
        body = r'\boxed{'+r' \rightarrow '.join([r'\text{A lengthy but unaltered workflow step}']*4)+'}'
        wrapped = wrap_display(body)
        self.assertEqual(wrapped.count(r'\rightarrow'),3)
        self.assertEqual(wrapped.count('A lengthy but unaltered workflow step'),4)
        self.assertIn(r'\begin{gathered}',wrapped)

    def test_explicit_math_delimiters_respect_tex_backslash_parity(self):
        # A TeX row break optionally followed by vertical spacing contains a
        # delimiter-looking second backslash. It is body syntax, not a nested
        # display opener. The amount of spacing is deliberately varied.
        for body in (r'a \\[4pt]' + '\n' + 'b', r'a \\[6pt]' + '\n' + 'b', r'a \\' + '\n' + 'b'):
            validate_explicit_math('\\[\n' + body + '\n\\]')
        # Canonical inline/display math remains valid, while genuine nesting,
        # stray/mismatched closers and missing closers remain fail-closed.
        validate_explicit_math(r'\[a\]')
        validate_explicit_math(r'\(a\)')
        for bad in (r'a\]', r'\[a', r'\[a\[b\]\]', r'\(a\]'):
            with self.assertRaises(ValueError, msg=bad):
                validate_explicit_math(bad)
        # Dollar normalization feeds the same validator; code isolation means
        # literal delimiter-looking row spacing in a fence is never scanned.
        self.assertIn(r'\\[4pt]', prepare_lecture_markdown('$$\na ' + r'\\[4pt]' + '\nb\n$$\n'))
        self.assertEqual(prepare_lecture_markdown('```tex\n\\\\[4pt]\n```\n'), '```tex\n\\\\[4pt]\n```\n')
        with self.assertRaises(ValueError):
            prepare_lecture_markdown(r'prose \[ malformed')

    def test_text_mode_math_islands_are_narrowly_validated_inside_displays(self):
        accepted = (
            r'\[\text{value \(x^2\)}\]',
            r'\[\boxed{\text{value \(x^2\)}}\]',
            r'\[\text{\(x\) versus \(y\)}\]',
            r'\[\text{before \(x+y\) after}\]',
            r'\[\text{value \(\frac{x}{y}\)}\]',
        )
        for source in accepted:
            prepared = prepare_lecture_markdown(source)
            self.assertIn(r'\text', prepared)
            self.assertEqual(prepared.count(r'\('), source.count(r'\('))
            self.assertEqual(prepared.count(r'\)'), source.count(r'\)'))
        dollar = '$$\n\\text{value \\(x+y\\)}\n$$\n'
        prepared = prepare_lecture_markdown(dollar)
        self.assertIn(r'\text{value \(x+y\)}', prepared)
        for source in (
            r'\[\(x\)\]', r'\[\[x\]\]', r'\[\text{value \(x}\]',
            r'\[\text{value x\)}\]', r'\[\text{value \(x} y\)\]',
            r'\[\text{value \[x\]}\]', r'\[\text{value \(x \(y\)\)}\]',
            r'\[\boxed{\(x\)}\]', r'\[\text{{ \(x\) }}\]',
            # An escaped ``\\text`` is a TeX row-break followed by letters,
            # not the exact text-mode command that owns an island.
            r'\[\\text{\(x\)}\]',
            r'\[\text{value \(\unknownoperator{x}\)}\]',
            r'\[\text{value \(\begin{unknown}x\end{unknown}\)}\]',
            r'\[\text{value \(\frac{x}{y\)}\]', r'\[\text{value \($x$\)}\]',
        ):
            with self.assertRaises(ValueError, msg=source):
                prepare_lecture_markdown(source)
        fenced = '```tex\n\\[\\text{\\(x\\)}\\]\n```\n'
        inline = '`\\[\\text{\\(x\\)}\\]`\n'
        self.assertEqual(prepare_lecture_markdown(fenced), fenced)
        self.assertEqual(prepare_lecture_markdown(inline), inline)
        rendered_source = prepare_lecture_markdown(r'\[\boxed{\text{value \(x^2\)}}\]')
        document = LectureDocument(
            'lecture-document/v1', 'l1', 1, rendered_source,
            SourceProvenance(hashlib.sha256(rendered_source.encode()).hexdigest()),
        )
        rendered = LegacyMarkdownTexRenderer().render(document)
        self.assertEqual(rendered.status, 'rendered_with_warnings')
        self.assertEqual(tuple(item.code for item in rendered.diagnostics), ('short_source',))
        self.assertIn(r'\boxed{\text{value \(x^2\)}}', rendered.tex_fragment)

    def test_safe_limit_operators_and_longmapsto_are_allowed_and_nearby_commands_remain_rejected(self):
        for command in ('limsup', 'liminf'):
            source = rf'\[\{command}_{{n\to\infty}} X_n\]'
            self.assertIn(rf'\{command}_{{n\to\infty}} X_n', prepare_lecture_markdown(source))
        for command in ('longmapsto', 'mapsto'):
            source = rf'\[x \{command} y\]'
            prepared = prepare_lecture_markdown(source)
            self.assertEqual(prepared.count(rf'\{command}'), 1)
        for command in ('tag', 'unknownoperator', 'newcommand', 'input', 'usepackage'):
            with self.assertRaises(ValueError, msg=command):
                prepare_lecture_markdown(rf'\[\{command}{{x}}\]')

    def test_unambiguous_dollar_display_blocks_are_normalized_without_body_rewrite(self):
        body_one = r'\frac{\theta_i^2}{n} \leq \alpha^2'
        body_two = r'\begin{aligned} a_{i} &\geq \frac{b^2}{c} \\ d &= \{x\}\end{aligned}'
        body_three = r'\begin{gathered}\sum_{i=1}^{n} x_i \\ \prod_{j=1}^{m} y_j\end{gathered}'
        raw = 'Before.\n$$\n' + body_one + '\n$$\nMiddle.\n$$\n' + body_two + '\n$$\n$$\n' + body_three + '\n$$\nAfter.\n'
        normalized, pairs = normalize_dollar_display_blocks(raw)
        expected = 'Before.\n\\[\n' + body_one + '\n\\]\nMiddle.\n\\[\n' + body_two + '\n\\]\n\\[\n' + body_three + '\n\\]\nAfter.\n'
        self.assertEqual((normalized, pairs), (expected, 3))
        self.assertEqual(normalized.split('\\[\n', 1)[1].split('\n\\]', 1)[0], body_one)
        self.assertIn('Prose price $5 stays.\n', normalize_dollar_display_blocks('Prose price $5 stays.\n')[0])
        self.assertEqual(normalize_dollar_display_blocks('```tex\n$$\n\\theta\n$$\n```\n'), ('```tex\n$$\n\\theta\n$$\n```\n', 0))
        for bad in ('Inline $x_i$ math.\n', '$$ x $$\n', '$$\n\\theta\n', '$$\n\\theta\n$$\nthen $x$\n'):
            with self.assertRaises(ValueError, msg=bad):
                normalize_dollar_display_blocks(bad)
        self.assertIn(body_one, prepare_lecture_markdown('$$\n' + body_one + '\n$$\n'))

    def test_blockquoted_dollar_display_blocks_are_normalized_without_body_rewrite(self):
        # Regression (T051 prospective real L4 review): the reviewed
        # application rejected a real raw ChatGPT lecture response with
        # `math_requires_explicit_delimiters` because it wrapped several
        # display-math blocks in a Markdown blockquote (`> $$` ... `> $$`).
        # Synthetic fixture only; no real lecture content.
        body = r'L(\theta;x)=\prod_{i=1}^n f_\theta(x_i).'
        raw = '> Because the observations are independent, the likelihood is\n>\n> $$\n> ' + body + '\n> $$\n>\n> concluding prose\n'
        normalized, pairs = normalize_dollar_display_blocks(raw)
        expected = '> Because the observations are independent, the likelihood is\n>\n> \\[\n> ' + body + '\n> \\]\n>\n> concluding prose\n'
        self.assertEqual((normalized, pairs), (expected, 1))
        # 1. A top-level standalone pair still works (covered above) and a
        #    blockquoted pair converts identically otherwise.
        # 3. The quoted body remains quoted: every body/opener/closer line
        #    keeps its exact '> ' marker; only the '$$' token changed.
        self.assertIn('> \\[\n> ' + body + '\n> \\]', normalized)
        self.assertNotIn('$$', normalized)
        # 12. Byte preservation outside the two delimiter tokens.
        self.assertEqual(normalized.replace('\\[', '$$').replace('\\]', '$$'), raw)

        # 2. Simple blockquote pair via the full preparer too.
        self.assertIn('> \\[\n> ' + body + '\n> \\]', prepare_lecture_markdown(raw))

        # 4. A nested-quote closer (">> $$") does not match a depth-1
        #    opener: the renderer only strips one quote marker, so treating
        #    a deeper nesting as safely paired would leak a literal '>'
        #    into the rendered math body. Fails closed instead.
        with self.assertRaises(ValueError):
            normalize_dollar_display_blocks('> $$\n> ' + body + '\n>> $$\n')

        # 5. Opener/closer kind mismatch (blockquoted opener, top-level
        #    closer) fails closed rather than guessing a boundary.
        with self.assertRaises(ValueError):
            normalize_dollar_display_blocks('> $$\n> ' + body + '\n$$\n')
        with self.assertRaises(ValueError):
            normalize_dollar_display_blocks('$$\n' + body + '\n> $$\n')

        # 6. An unclosed quoted display (the blockquote ends before the
        #    closer) fails closed.
        with self.assertRaises(ValueError):
            normalize_dollar_display_blocks('> $$\n> ' + body + '\nunquoted prose resumes.\n')
        with self.assertRaises(ValueError):
            normalize_dollar_display_blocks('> $$\n> ' + body + '\n')

        # 7. Raw '$$' inside ordinary blockquoted prose (not its own line)
        #    still fails closed.
        with self.assertRaises(ValueError):
            normalize_dollar_display_blocks('> prose $$ prose\n')

        # 8. Inline '$...$' inside a blockquote still fails closed.
        with self.assertRaises(ValueError):
            normalize_dollar_display_blocks('> Inline $x_i$ math.\n')

        # 9/10. '$$' inside a code fence remains code, whether the fence is
        #    top-level or itself blockquoted.
        self.assertEqual(
            normalize_dollar_display_blocks('```tex\n$$\n' + body + '\n$$\n```\n'),
            ('```tex\n$$\n' + body + '\n$$\n```\n', 0),
        )
        quoted_fence = '> ```tex\n> $$\n> ' + body + '\n> $$\n> ```\n'
        self.assertEqual(normalize_dollar_display_blocks(quoted_fence), (quoted_fence, 0))

        # 11. Canonical quoted \(...\)/\[...\] input is untouched (no dollar
        #     tokens to normalize).
        canonical_quoted = '> Canonical \\(\\theta\\) text.\n'
        self.assertEqual(normalize_dollar_display_blocks(canonical_quoted), (canonical_quoted, 0))

    def test_gpt_safe_format_canonicalization_is_bounded(self):
        raw = ('Before `\\theta`, `E[X]`, `m_2`, and `\\operatorname{Var}(X)`.\n'
            '```math\nA \\Longleftrightarrow B\n```\n'
            '[[SOURCE\\_SNIP source="src-synthetic" page=1 purpose="Diagram"]]\n')
        expected = ('Before \\(\\theta\\), \\(E[X]\\), \\(m_2\\), and \\(\\operatorname{Var}(X)\\).\n\n'
            '\\[\nA \\iff B\n\\]\n\n'
            '[[SOURCE_SNIP source="src-synthetic" page=1 purpose="Diagram"]]\n')
        actual, counts = prepare_lecture_markdown_with_counts(raw)
        self.assertEqual(actual, expected)
        self.assertEqual(counts.math_fences, 1)
        self.assertEqual(counts.inline_code_math, 4)
        self.assertEqual(counts.math_aliases, 1)
        self.assertEqual(counts.escaped_source_snips, 1)
        self.assertEqual(prepare_lecture_markdown('Canonical \\(\\theta\\) text.\n'), 'Canonical \\(\\theta\\) text.\n')
        self.assertEqual(prepare_lecture_markdown('```python\n`\\theta`\n```\n'), '```python\n`\\theta`\n```\n')
        self.assertEqual(prepare_lecture_markdown('Use `src-lecture-notes` and `run.sh`.\n'),
            'Use `src-lecture-notes` and `run.sh`.\n')
        self.assertEqual(prepare_lecture_markdown('`ordinary words`\n'), '`ordinary words`\n')
        self.assertEqual(prepare_lecture_markdown('[[SOURCE\\_SNIP invalid]]\n'), '[[SOURCE\\_SNIP invalid]]\n')
        self.assertEqual(prepare_lecture_markdown('Prose \\_ remains.\n'), 'Prose \\_ remains.\n')
        for bad in ('```math\n\\theta\n', '```math\n```python\n```\n',
                    r'\(\tag{x}\)', r'\(\input{x}\)'):
            with self.assertRaises(ValueError, msg=bad):
                prepare_lecture_markdown(bad)

    def test_compiler_success_is_insufficient(self):
        for text,code in [('Missing character: There is no X','compiler_missing_glyph'),
            ('! Bad math environment delimiter.','compiler_fatal_diagnostic'),
            (r'Overfull \hbox (8.5pt too wide)','compiler_overflow'),
            (r'Overfull \vbox (2.0pt too high)','compiler_overflow')]:
            self.assertEqual(inspect_compiler_log(text),code)
        self.assertIsNone(inspect_compiler_log(r'Overfull \hbox (0.5pt too wide)'))
        self.assertIsNone(inspect_compiler_log('Underfull box: harmless spacing'))

    def test_snippet_grammar_has_no_paths_or_silent_drops(self):
        snippet = parse_snippets(LECTURE)[0]
        self.assertEqual((snippet.source_id,snippet.page),('src-synthetic',1))
        for invalid in ('../secret','/tmp/file','https://site','..'):
            with self.assertRaises(ValueError): parse_snippets(LECTURE.replace('source="src-synthetic"',f'source="{invalid}"'))
        for invalid in ('page=0','page=-1','page="1"'):
            with self.assertRaises(ValueError): parse_snippets(LECTURE.replace('page=1',invalid))
        self.assertEqual(parse_snippets('```\n[[SOURCE_SNIP invalid]]\n```'),())
