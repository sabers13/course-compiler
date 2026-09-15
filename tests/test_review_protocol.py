import unittest

from course_compiler.providers.review_protocol import parse_review_verdict


class ReviewProtocolTests(unittest.TestCase):
    def test_no_corrections(self):
        self.assertEqual(parse_review_verdict("# Review verdict\n\nNo corrections required.", ("l1", "l2")), ())
        self.assertEqual(parse_review_verdict("# Review verdict\n\nNo corrections required.\n\n## Findings\n\nAdvisory prose.", ("l1", "l2")), ())

    def test_corrections_are_canonical_and_findings_are_advisory(self):
        self.assertEqual(parse_review_verdict("# Review verdict\n\n## Corrections required\n\n- l2\n\n## Findings\n\nAdvisory prose.", ("l1", "l2")), ("l2",))

    def test_rejects_unknown_duplicate_and_free_form_verdicts(self):
        for text in ("NO_CORRECTIONS", "# Review verdict\n\n## Corrections required\n\n- l3\n", "# Review verdict\n\n## Corrections required\n\n- l1\n- l1\n"):
            with self.assertRaises(ValueError):
                parse_review_verdict(text, ("l1", "l2"))
