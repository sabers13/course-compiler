"""Immutable versioned logical course identity."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


COURSE_REFERENCE_VERSION = "course-reference/v1"

_COURSE_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")


def _valid_course_id(value: object) -> bool:
    return (
        type(value) is str
        and value not in (".", "..")
        and _COURSE_ID_RE.fullmatch(value) is not None
    )


@dataclass(frozen=True, slots=True)
class CourseReference:
    """Content-free logical identity for one course scope."""

    reference_version: Literal["course-reference/v1"]
    course_id: str

    def __post_init__(self) -> None:
        if (
            type(self.reference_version) is not str
            or self.reference_version != COURSE_REFERENCE_VERSION
        ):
            raise ValueError("course reference version is unsupported")
        if not _valid_course_id(self.course_id):
            raise ValueError("course reference ID is invalid")


__all__ = ["COURSE_REFERENCE_VERSION", "CourseReference"]
