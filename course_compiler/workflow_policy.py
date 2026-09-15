"""Exact v1 workflow registries and policy constants.

This module contains data-only policy.  It performs no I/O and imports no
private or legacy implementation.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Literal, Mapping, TypeAlias


WORKFLOW_STATE_VERSION = "course-workflow-state/v2"
WORKFLOW_TRANSITION_VERSION = "course-workflow-transition/v2"
SOURCE_EVIDENCE_REFERENCE_VERSION = "source-evidence-reference/v1"
WORKFLOW_ARTIFACT_REFERENCE_VERSION = "workflow-artifact-reference/v1"
WORKFLOW_HANDOFF_VERSION = "course-workflow-handoff/v2"
WORKFLOW_ARTIFACT_PRODUCER_VERSION = "course-workflow-state/v1"

_PAIRED_STATE_VERSIONS = {
    "course-workflow-transition/v1": "course-workflow-state/v1",
    "course-workflow-transition/v2": "course-workflow-state/v2",
}
_PAIRED_HANDOFF_VERSIONS = {
    "course-workflow-state/v1": "course-workflow-handoff/v1",
    "course-workflow-state/v2": "course-workflow-handoff/v2",
}


def paired_state_version(transition_version: str) -> str | None:
    return _PAIRED_STATE_VERSIONS.get(transition_version)


def paired_handoff_version(state_version: str) -> str | None:
    return _PAIRED_HANDOFF_VERSIONS.get(state_version)

WorkflowStage: TypeAlias = Literal[
    "source_assessment",
    "priority_approval",
    "lecture_mapping",
    "map_approval",
    "lecture_production",
    "lecture_validation",
    "completed",
]
WorkflowDisposition: TypeAlias = Literal[
    "ready", "awaiting_approval", "blocked", "failed", "completed"
]
WorkflowAction: TypeAlias = Literal[
    "initialize_workflow",
    "record_source_assessment",
    "approve_priority_basis",
    "reject_priority_basis",
    "record_lecture_map",
    "approve_lecture_map",
    "reject_lecture_map",
    "submit_lecture_candidate",
    "record_candidate_validation",
    "record_operation_failure",
    "retry_failed",
    "mark_blocked",
    "clear_blocker",
    "record_new_source",
    "dismiss_new_source",
    "approve_map_reopen",
    "reopen_lectures_for_correction",
]
BlockerCode: TypeAlias = Literal[
    "private_artifact_unavailable",
    "external_prerequisite_unavailable",
    "new_source_review_required",
]
FailureCode: TypeAlias = Literal[
    "source_assessment_operation_failed",
    "lecture_mapping_operation_failed",
    "lecture_production_operation_failed",
    "lecture_validation_operation_failed",
]
WorkflowResultStatus: TypeAlias = Literal[
    "advanced", "rejected", "blocked", "failed", "idempotent_repeat"
]
ArtifactKind: TypeAlias = Literal[
    "source_assessment",
    "priority_proposal",
    "evidence_hierarchy",
    "lecture_map",
    "lecture_candidate",
    "validation",
    "blocker",
    "failure",
    "decision",
]
PolicyKind: TypeAlias = Literal[
    "source_assessment",
    "priority_basis",
    "lecture_mapping",
    "lecture_production",
    "lecture_validation",
    "workflow_handoff",
]
ProducerVersion: TypeAlias = Literal[
    "source-assessment-policy/v1",
    "priority-basis-policy/v1",
    "lecture-map-policy/v1",
    "lecture-production-policy/v1",
    "lecture-validation-policy/v1",
    "workflow-handoff-policy/v1",
    "course-workflow-state/v1",
]
PriorityMode: TypeAlias = Literal[
    "exam_driven", "sheet_driven", "slide_driven", "custom"
]
ProgressStatus: TypeAlias = Literal[
    "pending", "candidate", "retry_required", "correction_required", "accepted"
]

WORKFLOW_STAGES: tuple[WorkflowStage, ...] = (
    "source_assessment",
    "priority_approval",
    "lecture_mapping",
    "map_approval",
    "lecture_production",
    "lecture_validation",
    "completed",
)
DIAGNOSTIC_STAGES = ("initialization",) + WORKFLOW_STAGES
WORKFLOW_ACTIONS: tuple[WorkflowAction, ...] = (
    "initialize_workflow",
    "record_source_assessment",
    "approve_priority_basis",
    "reject_priority_basis",
    "record_lecture_map",
    "approve_lecture_map",
    "reject_lecture_map",
    "submit_lecture_candidate",
    "record_candidate_validation",
    "record_operation_failure",
    "retry_failed",
    "mark_blocked",
    "clear_blocker",
    "record_new_source",
    "dismiss_new_source",
    "approve_map_reopen",
    "reopen_lectures_for_correction",
)

POLICY_VERSIONS: Mapping[str, str] = MappingProxyType(
    {
        "source_assessment": "source-assessment-policy/v1",
        "priority_basis": "priority-basis-policy/v1",
        "lecture_mapping": "lecture-map-policy/v1",
        "lecture_production": "lecture-production-policy/v1",
        "lecture_validation": "lecture-validation-policy/v1",
        "workflow_handoff": "workflow-handoff-policy/v1",
    }
)

ARTIFACT_PRODUCERS: Mapping[str, str] = MappingProxyType(
    {
        "source_assessment": "source-assessment-policy/v1",
        "priority_proposal": "priority-basis-policy/v1",
        "evidence_hierarchy": "priority-basis-policy/v1",
        "lecture_map": "lecture-map-policy/v1",
        "lecture_candidate": "lecture-production-policy/v1",
        "validation": "lecture-validation-policy/v1",
        "blocker": WORKFLOW_ARTIFACT_PRODUCER_VERSION,
        "failure": WORKFLOW_ARTIFACT_PRODUCER_VERSION,
        "decision": WORKFLOW_ARTIFACT_PRODUCER_VERSION,
    }
)

NORMAL_DISPOSITIONS: Mapping[str, str] = MappingProxyType(
    {
        "source_assessment": "ready",
        "priority_approval": "awaiting_approval",
        "lecture_mapping": "ready",
        "map_approval": "awaiting_approval",
        "lecture_production": "ready",
        "lecture_validation": "ready",
        "completed": "completed",
    }
)

FAILURE_REGISTRY: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        "source_assessment": (
            "record_source_assessment",
            "source_assessment_operation_failed",
        ),
        "lecture_mapping": (
            "record_lecture_map",
            "lecture_mapping_operation_failed",
        ),
        "lecture_production": (
            "submit_lecture_candidate",
            "lecture_production_operation_failed",
        ),
        "lecture_validation": (
            "record_candidate_validation",
            "lecture_validation_operation_failed",
        ),
    }
)

PRIVATE_ARTIFACT_BLOCKER_STARTS = frozenset(
    {
        ("source_assessment", "ready"),
        ("priority_approval", "awaiting_approval"),
        ("lecture_mapping", "ready"),
        ("map_approval", "awaiting_approval"),
        ("lecture_production", "ready"),
        ("lecture_validation", "ready"),
    }
)
EXTERNAL_PREREQUISITE_BLOCKER_STAGES = frozenset(FAILURE_REGISTRY)
NEW_SOURCE_STAGES = frozenset({"lecture_production", "lecture_validation"})

_ALL = frozenset(DIAGNOSTIC_STAGES)
_WORKFLOW = frozenset(WORKFLOW_STAGES)
_NON_COMPLETED = frozenset(WORKFLOW_STAGES[:-1])
_READY_WORK = frozenset(FAILURE_REGISTRY)
_APPROVAL_SUBJECT = frozenset(
    {"priority_approval", "map_approval", "lecture_production", "lecture_validation"}
)
_NEW_SOURCE = NEW_SOURCE_STAGES

# code -> (severity, category, permitted stages)
DIAGNOSTIC_REGISTRY: Mapping[str, tuple[str, str, frozenset[str]]] = MappingProxyType(
    {
        "unsupported_workflow_contract_version": ("error", "validation", _ALL),
        "unsupported_transition_contract_version": ("error", "validation", _ALL),
        "unsupported_reference_version": ("error", "validation", _ALL),
        "unsupported_policy_kind_version": ("error", "validation", _ALL),
        "invalid_workflow_id": ("error", "validation", _ALL),
        "invalid_operation_id": ("error", "validation", _ALL),
        "invalid_source_evidence": ("error", "validation", frozenset({"initialization", "source_assessment", "lecture_production", "lecture_validation"})),
        "duplicate_source_id": ("error", "validation", frozenset({"initialization", "source_assessment", "lecture_production", "lecture_validation"})),
        "duplicate_source_digest": ("error", "validation", frozenset({"initialization", "source_assessment", "lecture_production", "lecture_validation"})),
        "invalid_workflow_state": ("error", "validation", _ALL),
        "invalid_stage_disposition": ("error", "validation", _ALL),
        "illegal_transition": ("error", "validation", _ALL),
        "stale_revision": ("error", "validation", _ALL),
        "operation_id_reuse": ("error", "validation", _ALL),
        "approval_subject_mismatch": ("error", "validation", _APPROVAL_SUBJECT),
        "invalid_lecture_map": ("error", "validation", frozenset({"lecture_mapping", "map_approval"})),
        "invalid_candidate_document": ("error", "validation", frozenset({"lecture_production"})),
        "candidate_id_mismatch": ("error", "validation", frozenset({"lecture_production"})),
        "candidate_order_mismatch": ("error", "validation", frozenset({"lecture_production"})),
        "candidate_subject_mismatch": ("error", "validation", frozenset({"lecture_validation"})),
        "validation_policy_mismatch": ("error", "validation", frozenset({"lecture_validation"})),
        "invalid_validation_evidence": ("error", "validation", frozenset({"lecture_validation"})),
        "new_source_subject_mismatch": ("error", "validation", _NEW_SOURCE),
        "map_reopen_violation": ("error", "validation", frozenset({"lecture_mapping", "map_approval"})),
        "invalid_handoff": ("error", "validation", _WORKFLOW),
        "candidate_empty_source": ("warning", "warning", frozenset({"lecture_validation"})),
        "candidate_short_source": ("warning", "warning", frozenset({"lecture_validation"})),
        "candidate_unclosed_code_fence": ("warning", "warning", frozenset({"lecture_validation"})),
        "candidate_unclosed_display_math": ("warning", "warning", frozenset({"lecture_validation"})),
        "source_conflict_reported": ("warning", "warning", frozenset({"source_assessment"})),
        "priority_basis_provisional": ("warning", "warning", frozenset({"priority_approval"})),
        "private_artifact_unavailable": ("error", "blocked", _NON_COMPLETED),
        "external_prerequisite_unavailable": ("error", "blocked", _READY_WORK),
        "new_source_review_required": ("error", "blocked", _NEW_SOURCE),
        "source_assessment_operation_failed": ("error", "failure", frozenset({"source_assessment"})),
        "lecture_mapping_operation_failed": ("error", "failure", frozenset({"lecture_mapping"})),
        "lecture_production_operation_failed": ("error", "failure", frozenset({"lecture_production"})),
        "lecture_validation_operation_failed": ("error", "failure", frozenset({"lecture_validation"})),
    }
)

DIAGNOSTIC_MESSAGES: Mapping[str, str] = MappingProxyType(
    {code: "Workflow condition recorded." for code in DIAGNOSTIC_REGISTRY}
)

RECEIPT_OUTCOMES: Mapping[str, str] = MappingProxyType(
    {
        "initialize_workflow": "advanced",
        "record_source_assessment": "advanced",
        "approve_priority_basis": "advanced",
        "reject_priority_basis": "advanced",
        "record_lecture_map": "advanced",
        "approve_lecture_map": "advanced",
        "reject_lecture_map": "advanced",
        "submit_lecture_candidate": "advanced",
        "record_candidate_validation": "advanced",
        "record_operation_failure": "failed",
        "retry_failed": "advanced",
        "mark_blocked": "blocked",
        "clear_blocker": "advanced",
        "record_new_source": "blocked",
        "dismiss_new_source": "advanced",
        "approve_map_reopen": "advanced",
        "reopen_lectures_for_correction": "advanced",
    }
)
