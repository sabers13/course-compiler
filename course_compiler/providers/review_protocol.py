"""Strict, lifecycle-safe parser for the T053 semantic review verdict."""

from __future__ import annotations

import re


def parse_review_verdict(text: str, lecture_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Return canonical correction IDs; reject advisory prose as authority."""
    if type(text) is not str or type(lecture_ids) is not tuple:
        raise ValueError("review_verdict_required")
    if re.fullmatch(r"# Review verdict\n\nNo corrections required\.(?:\n\n## Findings\n\n[\s\S]+)?", text):
        return ()
    match = re.fullmatch(
        r"# Review verdict\n\n## Corrections required\n\n((?:- l[1-9][0-9]{0,3}\n)+)(?:\n## Findings\n\n[\s\S]+)?",
        text,
    )
    if match is None:
        raise ValueError("review_verdict_required")
    ids = tuple(line[2:] for line in match.group(1).splitlines())
    if len(ids) != len(set(ids)) or any(item not in lecture_ids for item in ids):
        raise ValueError("review_verdict_required")
    return tuple(item for item in lecture_ids if item in ids)
