#!/usr/bin/env python3
"""Find an exact Unicode code-point offset for a semantic text anchor.

Input is one JSON object on stdin:

    {"source_text": "...", "anchor": "..."}

or, to select one match explicitly:

    {"source_text": "...", "anchor": "...", "occurrence": 2}

`occurrence` is one-based. If the JSON field is present it must be a positive
integer; explicit `null` is invalid. The script performs exact Python string
matching and never normalizes, case-folds, trims, or otherwise transforms either string.
"""

from __future__ import annotations

import json
import sys
from typing import Any


class OffsetError(ValueError):
    """A fixed, content-free validation error."""


def find_source_text_offset(
    source_text: str,
    anchor: str,
    *,
    occurrence: int | None = None,
) -> tuple[int, int]:
    """Return `(offset, match_count)` using Python Unicode code-point indexes."""

    if type(source_text) is not str:
        raise OffsetError("source_text_must_be_string")
    if type(anchor) is not str:
        raise OffsetError("anchor_must_be_string")
    if not anchor:
        raise OffsetError("anchor_must_be_nonempty")
    if occurrence is not None and (type(occurrence) is not int or occurrence < 1):
        raise OffsetError("occurrence_must_be_positive_integer")

    offsets: list[int] = []
    start = 0
    while True:
        index = source_text.find(anchor, start)
        if index < 0:
            break
        offsets.append(index)
        start = index + 1

    if not offsets:
        raise OffsetError("anchor_not_found")
    if occurrence is None:
        if len(offsets) != 1:
            raise OffsetError("anchor_ambiguous")
        return offsets[0], 1
    if occurrence > len(offsets):
        raise OffsetError("occurrence_out_of_range")
    return offsets[occurrence - 1], len(offsets)


def _decode_request(raw: Any) -> tuple[str, str, int | None]:
    if type(raw) is not dict:
        raise OffsetError("request_must_be_object")
    if set(raw) - {"source_text", "anchor", "occurrence"}:
        raise OffsetError("request_has_unknown_fields")
    if "source_text" not in raw or "anchor" not in raw:
        raise OffsetError("request_missing_required_fields")
    if "occurrence" in raw:
        occurrence = raw["occurrence"]
        if type(occurrence) is not int or occurrence < 1:
            raise OffsetError("occurrence_must_be_positive_integer")
    else:
        occurrence = None
    return raw["source_text"], raw["anchor"], occurrence


def main() -> int:
    try:
        request = json.load(sys.stdin)
        source_text, anchor, occurrence = _decode_request(request)
        offset, match_count = find_source_text_offset(
            source_text,
            anchor,
            occurrence=occurrence,
        )
    except (json.JSONDecodeError, OffsetError) as exc:
        code = "invalid_json" if isinstance(exc, json.JSONDecodeError) else str(exc)
        json.dump({"status": "error", "code": code}, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 2

    json.dump(
        {"status": "ok", "offset": offset, "match_count": match_count},
        sys.stdout,
        ensure_ascii=False,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
