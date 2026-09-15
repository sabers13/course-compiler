"""Immutable versioned association between course and workflow identities."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .course import CourseReference


COURSE_WORKFLOW_ASSOCIATION_VERSION = "course-workflow-association/v1"

_WORKFLOW_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")


def _valid_workflow_id(value: object) -> bool:
    return (
        type(value) is str
        and value not in (".", "..")
        and _WORKFLOW_ID_RE.fullmatch(value) is not None
    )


def _valid_course_reference(value: object) -> bool:
    if type(value) is not CourseReference:
        return False
    try:
        CourseReference(value.reference_version, value.course_id)
    except Exception:
        return False
    return True


@dataclass(frozen=True, slots=True)
class CourseWorkflowAssociation:
    """Content-free value asserting one course-to-workflow relation."""

    association_version: Literal["course-workflow-association/v1"]
    course_reference: CourseReference
    workflow_id: str

    def __post_init__(self) -> None:
        if (
            type(self.association_version) is not str
            or self.association_version != COURSE_WORKFLOW_ASSOCIATION_VERSION
        ):
            raise ValueError("course-workflow association version is unsupported")
        if not _valid_course_reference(self.course_reference):
            raise ValueError("course-workflow association course reference is invalid")
        if not _valid_workflow_id(self.workflow_id):
            raise ValueError("course-workflow association workflow ID is invalid")


__all__ = [
    "COURSE_WORKFLOW_ASSOCIATION_VERSION",
    "CourseWorkflowAssociation",
]
