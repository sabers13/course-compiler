from __future__ import annotations

from dataclasses import replace
import unittest

import course_compiler.workflow as workflow_module
from course_compiler.workflow import (
    WORKFLOW_STATE_VERSION,
    ApproveLectureMap,
    ApproveMapReopen,
    ApprovePriorityBasis,
    ClearBlocker,
    DismissNewSource,
    MarkBlocked,
    RecordCandidateValidation,
    RecordLectureMap,
    RecordNewSource,
    RecordOperationFailure,
    RecordSourceAssessment,
    ReopenLecturesForCorrection,
    RejectLectureMap,
    RejectPriorityBasis,
    RetryFailed,
    SubmitLectureCandidate,
    ValidationRecord,
    WorkflowAdvanced,
    WorkflowBlocked,
    WorkflowFailed,
    WorkflowIdempotentRepeat,
    WorkflowRejected,
    WorkflowState,
    WorkflowTransitionRequest,
    apply_workflow_request,
    candidate_subject_sha256,
    derive_workflow_handoff,
    map_subject_sha256,
    ordered_accepted_documents,
    priority_subject_sha256,
    replay_workflow,
    resume_workflow,
    validate_workflow_state,
)

from tests.test_workflow_contract import (
    HOSTILE_SENTINEL,
    HostileInt,
    HostileStr,
    HostileTuple,
    digest,
    initialize_request,
    make_artifact,
    make_document,
    make_source,
    transition,
)

WORKFLOW_STATE_VERSION = "course-workflow-state/v1"


def advance(result: object) -> WorkflowState:
    if not isinstance(result, (WorkflowAdvanced, WorkflowBlocked, WorkflowFailed)):
        raise AssertionError(f"state-mutating result required, got {type(result).__name__}")
    return result.state


class Scenario:
    def __init__(self, lecture_count: int = 2) -> None:
        self.initialization = initialize_request()
        initial = apply_workflow_request(None, self.initialization)
        assert isinstance(initial, WorkflowAdvanced)
        self.state = initial.state
        self.requests = []
        self.states = [initial.state]
        self.receipts = [initial.receipt]
        self.lecture_count = lecture_count

    def apply(self, operation_id: str, payload: object) -> object:
        request = transition(self.state, operation_id, payload)
        result = apply_workflow_request(self.state, request)
        if isinstance(result, (WorkflowAdvanced, WorkflowBlocked, WorkflowFailed)):
            self.requests.append(request)
            self.state = result.state
            self.states.append(result.state)
            self.receipts.append(result.receipt)
        return result

    def source_assessed(self) -> "Scenario":
        result = self.apply(
            "op-assess",
            RecordSourceAssessment(
                "record_source_assessment",
                make_artifact("assessment", "source_assessment", "source-assessment-policy/v1"),
                "exam_driven",
                make_artifact("proposal", "priority_proposal", "priority-basis-policy/v1"),
                make_artifact("hierarchy", "evidence_hierarchy", "priority-basis-policy/v1"),
            ),
        )
        assert isinstance(result, WorkflowAdvanced)
        return self

    def priority_approved(self) -> "Scenario":
        self.source_assessed()
        assert self.state.priority_basis is not None
        subject = priority_subject_sha256(self.state.priority_basis, self.state.policies.priority_basis)
        result = self.apply("op-priority", ApprovePriorityBasis("approve_priority_basis", subject))
        assert isinstance(result, WorkflowAdvanced)
        return self

    def map_proposed(self) -> "Scenario":
        self.priority_approved()
        ids = tuple(f"l{i}" for i in range(1, self.lecture_count + 1))
        result = self.apply(
            "op-map",
            RecordLectureMap(
                "record_lecture_map",
                make_artifact("map", "lecture_map", "lecture-map-policy/v1"),
                ids,
            ),
        )
        assert isinstance(result, WorkflowAdvanced)
        return self

    def production_ready(self) -> "Scenario":
        self.map_proposed()
        assert self.state.lecture_map is not None
        subject = map_subject_sha256(self.state.lecture_map, self.state.policies.lecture_mapping)
        result = self.apply("op-map-approve", ApproveLectureMap("approve_lecture_map", subject))
        assert isinstance(result, WorkflowAdvanced)
        return self

    def submit(self, lecture_id: str, order: int, operation_id: str, source: str = "invented candidate") -> object:
        return self.apply(
            operation_id,
            SubmitLectureCandidate(
                "submit_lecture_candidate",
                lecture_id,
                make_document(lecture_id, order, source),
            ),
        )

    def validate(self, lecture_id: str, operation_id: str, disposition: str = "passed", subject: str | None = None) -> object:
        progress = next(item for item in self.state.lecture_progress if item.lecture_id == lecture_id)
        assert progress.candidate is not None
        actual = candidate_subject_sha256(progress.candidate)
        candidate_subject = subject or actual
        record = ValidationRecord(
            candidate_subject,
            "lecture-validation-policy/v1",
            self.state.policies.lecture_validation.content_sha256,
            disposition,  # type: ignore[arg-type]
            make_artifact(f"validation-{operation_id}", "validation", "lecture-validation-policy/v1"),
        )
        return self.apply(
            operation_id,
            RecordCandidateValidation(
                "record_candidate_validation",
                lecture_id,
                candidate_subject,
                record,
            ),
        )


class InitializationAndGateTests(unittest.TestCase):
    def test_initialization_creates_exact_revision_zero_state(self) -> None:
        result = apply_workflow_request(None, initialize_request())
        self.assertIsInstance(result, WorkflowAdvanced)
        assert isinstance(result, WorkflowAdvanced)
        self.assertEqual((result.state.revision, result.state.stage, result.state.disposition), (0, "source_assessment", "ready"))
        self.assertEqual((result.receipt.from_revision, result.receipt.to_revision), (-1, 0))
        self.assertEqual(validate_workflow_state(result.state), ())

    def test_priority_and_map_gates_cannot_be_bypassed(self) -> None:
        scenario = Scenario()
        request = transition(
            scenario.state,
            "op-illegal-map",
            RecordLectureMap(
                "record_lecture_map",
                make_artifact("map", "lecture_map", "lecture-map-policy/v1"),
                ("l1",),
            ),
        )
        result = apply_workflow_request(scenario.state, request)
        self.assertIsInstance(result, WorkflowRejected)
        self.assertEqual(result.state, scenario.state)
        self.assertEqual(result.diagnostics[0].code, "illegal_transition")

    def test_approval_subject_mismatch_is_non_mutating(self) -> None:
        scenario = Scenario().source_assessed()
        result = scenario.apply("op-wrong-priority", ApprovePriorityBasis("approve_priority_basis", "0" * 64))
        self.assertIsInstance(result, WorkflowRejected)
        self.assertEqual(result.state.revision, 1)
        self.assertEqual(result.diagnostics[0].code, "approval_subject_mismatch")

    def test_map_rejection_returns_to_mapping_without_progress(self) -> None:
        scenario = Scenario().map_proposed()
        assert scenario.state.lecture_map is not None
        subject = map_subject_sha256(scenario.state.lecture_map, scenario.state.policies.lecture_mapping)
        result = scenario.apply("op-map-reject", RejectLectureMap("reject_lecture_map", subject))
        self.assertIsInstance(result, WorkflowAdvanced)
        self.assertEqual(scenario.state.stage, "lecture_mapping")
        self.assertIsNone(scenario.state.lecture_map)
        self.assertEqual(scenario.state.lecture_progress, ())

    def test_priority_rejection_records_decision_and_restarts_assessment(self) -> None:
        scenario = Scenario().source_assessed()
        assert scenario.state.priority_basis is not None
        subject = priority_subject_sha256(scenario.state.priority_basis, scenario.state.policies.priority_basis)
        result = scenario.apply(
            "op-priority-reject",
            RejectPriorityBasis("reject_priority_basis", subject),
        )
        self.assertIsInstance(result, WorkflowAdvanced)
        self.assertEqual(scenario.state.stage, "source_assessment")
        self.assertIsNone(scenario.state.source_assessment)
        self.assertIsNone(scenario.state.priority_basis)
        self.assertEqual(scenario.state.approval_history[-1].decision, "rejected")


class CandidateValidationTests(unittest.TestCase):
    def test_candidate_id_order_and_t002_validation_fail_closed(self) -> None:
        scenario = Scenario().production_ready()
        attempts = (
            SubmitLectureCandidate("submit_lecture_candidate", "l1", make_document("l2", 1)),
            SubmitLectureCandidate("submit_lecture_candidate", "l1", make_document("l1", 2)),
            SubmitLectureCandidate("submit_lecture_candidate", "l1", make_document("l1", 1, "bad\r\n")),
        )
        expected = ("candidate_id_mismatch", "candidate_order_mismatch", "invalid_candidate_document")
        for index, (payload, code) in enumerate(zip(attempts, expected), 1):
            with self.subTest(code=code):
                result = scenario.apply(f"op-invalid-{index}", payload)
                self.assertIsInstance(result, WorkflowRejected)
                self.assertEqual(result.diagnostics[0].code, code)
                self.assertEqual(scenario.state.revision, 4)

    def test_equal_markdown_different_identity_cannot_reuse_validation(self) -> None:
        scenario = Scenario().production_ready()
        source = "same invented markdown"
        first_submit = scenario.submit("l1", 1, "op-l1-submit", source)
        self.assertIsInstance(first_submit, WorkflowAdvanced)
        first_ref = scenario.state.lecture_progress[0].candidate
        assert first_ref is not None
        first_subject = candidate_subject_sha256(first_ref)
        first_validation = scenario.validate("l1", "op-l1-valid")
        self.assertIsInstance(first_validation, WorkflowAdvanced)
        second_submit = scenario.submit("l2", 2, "op-l2-submit", source)
        self.assertIsInstance(second_submit, WorkflowAdvanced)
        second_ref = scenario.state.lecture_progress[1].candidate
        assert second_ref is not None
        self.assertEqual(first_ref.content_sha256, second_ref.content_sha256)
        self.assertNotEqual(first_subject, candidate_subject_sha256(second_ref))
        wrong = scenario.validate("l2", "op-l2-wrong", subject=first_subject)
        self.assertIsInstance(wrong, WorkflowRejected)
        self.assertEqual(wrong.diagnostics[0].code, "candidate_subject_mismatch")
        matching = scenario.validate("l2", "op-l2-valid")
        self.assertIsInstance(matching, WorkflowAdvanced)
        self.assertEqual(scenario.state.stage, "completed")
        self.assertEqual(len(ordered_accepted_documents(scenario.state)), 2)

    def test_policy_digest_mismatch_cannot_authorize_candidate(self) -> None:
        scenario = Scenario(1).production_ready()
        scenario.submit("l1", 1, "op-submit")
        progress = scenario.state.lecture_progress[0]
        assert progress.candidate is not None
        subject = candidate_subject_sha256(progress.candidate)
        validation = ValidationRecord(
            subject,
            "lecture-validation-policy/v1",
            "0" * 64,
            "passed",
            make_artifact("validation-wrong-policy", "validation", "lecture-validation-policy/v1"),
        )
        result = scenario.apply(
            "op-wrong-policy",
            RecordCandidateValidation("record_candidate_validation", "l1", subject, validation),
        )
        self.assertIsInstance(result, WorkflowRejected)
        self.assertEqual(result.diagnostics[0].code, "validation_policy_mismatch")

    def test_rejected_candidate_retry_archives_safe_history(self) -> None:
        scenario = Scenario(1).production_ready()
        scenario.submit("l1", 1, "op-submit-old")
        result = scenario.validate("l1", "op-reject", disposition="rejected")
        self.assertIsInstance(result, WorkflowAdvanced)
        self.assertEqual(scenario.state.lecture_progress[0].status, "retry_required")
        scenario.submit("l1", 1, "op-submit-new", "replacement invented candidate")
        self.assertEqual(len(scenario.state.historical_candidates), 1)
        historical = scenario.state.historical_candidates[0]
        self.assertEqual(historical.historical_disposition, "rejected")
        self.assertEqual(historical.validation.disposition, "rejected")

    def test_replacement_removes_stale_candidate_warning(self) -> None:
        scenario = Scenario(1).production_ready()
        scenario.submit("l1", 1, "op-submit-old", "```text\ninvented")
        self.assertTrue(any(item.code == "candidate_unclosed_code_fence" for item in scenario.state.diagnostics))
        scenario.validate("l1", "op-reject-old", disposition="rejected")
        scenario.submit("l1", 1, "op-submit-new", "closed invented candidate")
        self.assertFalse(any(item.code == "candidate_unclosed_code_fence" for item in scenario.state.diagnostics))

    def test_state_and_results_never_retain_source_text(self) -> None:
        scenario = Scenario(1).production_ready()
        secret = "never-persist-this-invented-source"
        result = scenario.submit("l1", 1, "op-submit-secret", secret)
        self.assertIsInstance(result, WorkflowAdvanced)
        self.assertNotIn(secret, repr(result))
        self.assertNotIn(secret, repr(scenario.state))


class FailureAndBlockerTests(unittest.TestCase):
    def test_external_failure_and_explicit_retry_from_every_ready_stage(self) -> None:
        scenarios = [
            (Scenario(), "record_source_assessment", "source_assessment_operation_failed"),
            (Scenario().priority_approved(), "record_lecture_map", "lecture_mapping_operation_failed"),
            (Scenario().production_ready(), "submit_lecture_candidate", "lecture_production_operation_failed"),
        ]
        validation = Scenario().production_ready()
        validation.submit("l1", 1, "op-active")
        scenarios.append((validation, "record_candidate_validation", "lecture_validation_operation_failed"))
        for index, (scenario, failed_action, code) in enumerate(scenarios):
            with self.subTest(stage=scenario.state.stage):
                evidence = make_artifact(f"failure-{index}", "failure", WORKFLOW_STATE_VERSION)
                result = scenario.apply(
                    f"op-failure-{index}",
                    RecordOperationFailure("record_operation_failure", failed_action, code, None, evidence),  # type: ignore[arg-type]
                )
                self.assertIsInstance(result, WorkflowFailed)
                self.assertEqual(scenario.state.disposition, "failed")
                retry = scenario.apply(
                    f"op-retry-{index}",
                    RetryFailed("retry_failed", code, evidence.content_sha256),  # type: ignore[arg-type]
                )
                self.assertIsInstance(retry, WorkflowAdvanced)
                self.assertEqual(scenario.state.disposition, "ready")

    def test_failure_registry_rejects_wrong_action_code_pair(self) -> None:
        scenario = Scenario()
        evidence = make_artifact("failure", "failure", WORKFLOW_STATE_VERSION)
        result = scenario.apply(
            "op-invalid-failure",
            RecordOperationFailure(
                "record_operation_failure",
                "record_lecture_map",
                "lecture_mapping_operation_failed",
                None,
                evidence,
            ),
        )
        self.assertIsInstance(result, WorkflowRejected)
        self.assertEqual(result.diagnostics[0].code, "illegal_transition")

    def test_generic_blocker_creation_and_matching_clear(self) -> None:
        scenario = Scenario().source_assessed()
        evidence = make_artifact("blocker", "blocker", WORKFLOW_STATE_VERSION)
        result = scenario.apply(
            "op-block",
            MarkBlocked("mark_blocked", "private_artifact_unavailable", None, digest("subject"), evidence),
        )
        self.assertIsInstance(result, WorkflowBlocked)
        self.assertEqual(scenario.state.disposition, "blocked")
        mismatch = scenario.apply(
            "op-clear-wrong",
            ClearBlocker("clear_blocker", "private_artifact_unavailable", "0" * 64),
        )
        self.assertIsInstance(mismatch, WorkflowRejected)
        cleared = scenario.apply(
            "op-clear",
            ClearBlocker("clear_blocker", "private_artifact_unavailable", digest("subject")),
        )
        self.assertIsInstance(cleared, WorkflowAdvanced)
        self.assertEqual(scenario.state.disposition, "awaiting_approval")
        self.assertFalse(any(item.category == "blocked" for item in scenario.state.diagnostics))

    def test_generic_blocker_cannot_replace_active_issue(self) -> None:
        scenario = Scenario()
        evidence = make_artifact("blocker", "blocker", WORKFLOW_STATE_VERSION)
        scenario.apply(
            "op-block",
            MarkBlocked("mark_blocked", "external_prerequisite_unavailable", None, digest("subject"), evidence),
        )
        request = transition(
            scenario.state,
            "op-block-again",
            MarkBlocked("mark_blocked", "external_prerequisite_unavailable", None, digest("other"), evidence),
        )
        result = apply_workflow_request(scenario.state, request)
        self.assertIsInstance(result, WorkflowRejected)


class NewSourceAndReopenTests(unittest.TestCase):
    def test_new_source_dismissal_preserves_active_validation_candidate(self) -> None:
        scenario = Scenario().production_ready()
        scenario.submit("l1", 1, "op-submit")
        pending = make_source("source-2", "new-source")
        evidence = make_artifact("new-source-blocker", "blocker", WORKFLOW_STATE_VERSION)
        blocked = scenario.apply("op-new-source", RecordNewSource("record_new_source", pending, evidence))
        self.assertIsInstance(blocked, WorkflowBlocked)
        self.assertEqual(scenario.state.active_lecture_id, "l1")
        dismissed = scenario.apply(
            "op-dismiss",
            DismissNewSource("dismiss_new_source", pending.content_sha256),
        )
        self.assertIsInstance(dismissed, WorkflowAdvanced)
        self.assertEqual((scenario.state.stage, scenario.state.active_lecture_id), ("lecture_validation", "l1"))
        self.assertIn(pending, scenario.state.reviewed_source_evidence)

    def test_duplicate_new_sources_are_rejected_without_mutation(self) -> None:
        scenario = Scenario().production_ready()
        evidence = make_artifact("new-source-blocker", "blocker", WORKFLOW_STATE_VERSION)
        duplicate = make_source("source-1", "different-bytes")
        result = scenario.apply("op-duplicate-source", RecordNewSource("record_new_source", duplicate, evidence))
        self.assertIsInstance(result, WorkflowRejected)
        self.assertEqual(result.diagnostics[0].code, "duplicate_source_id")

    def test_map_reopen_archives_all_progress_and_preserves_baseline(self) -> None:
        scenario = Scenario().production_ready()
        scenario.submit("l1", 1, "op-l1-submit")
        scenario.validate("l1", "op-l1-valid")
        scenario.submit("l2", 2, "op-l2-submit")
        pending = make_source("source-2", "new-source")
        evidence = make_artifact("new-source-blocker", "blocker", WORKFLOW_STATE_VERSION)
        scenario.apply("op-new-source", RecordNewSource("record_new_source", pending, evidence))
        reopened = scenario.apply("op-reopen", ApproveMapReopen("approve_map_reopen", pending.content_sha256))
        self.assertIsInstance(reopened, WorkflowAdvanced)
        self.assertEqual(scenario.state.stage, "source_assessment")
        self.assertIsNotNone(scenario.state.map_reopen_context)
        self.assertEqual(tuple(item.status for item in scenario.state.lecture_progress), ("pending", "pending"))
        self.assertEqual(len(scenario.state.historical_candidates), 2)
        self.assertTrue(all(item.historical_disposition == "invalidated_by_map_reopen" for item in scenario.state.historical_candidates))
        self.assertFalse(any(item.code.startswith("candidate_") for item in scenario.state.diagnostics))

        # Renew assessment and priority approval, then reject one append-only proposal.
        result = scenario.apply(
            "op-reassess",
            RecordSourceAssessment(
                "record_source_assessment",
                make_artifact("assessment-2", "source_assessment", "source-assessment-policy/v1"),
                "exam_driven",
                make_artifact("proposal-2", "priority_proposal", "priority-basis-policy/v1"),
                make_artifact("hierarchy-2", "evidence_hierarchy", "priority-basis-policy/v1"),
            ),
        )
        self.assertIsInstance(result, WorkflowAdvanced)
        assert scenario.state.priority_basis is not None
        priority_subject = priority_subject_sha256(scenario.state.priority_basis, scenario.state.policies.priority_basis)
        scenario.apply("op-reapprove-priority", ApprovePriorityBasis("approve_priority_basis", priority_subject))
        scenario.apply(
            "op-remap-one",
            RecordLectureMap(
                "record_lecture_map",
                make_artifact("map-2", "lecture_map", "lecture-map-policy/v1"),
                ("l1", "l2", "l3"),
            ),
        )
        assert scenario.state.lecture_map is not None
        map_subject = map_subject_sha256(scenario.state.lecture_map, scenario.state.policies.lecture_mapping)
        scenario.apply("op-reject-remap", RejectLectureMap("reject_lecture_map", map_subject))
        self.assertIsNotNone(scenario.state.map_reopen_context)
        self.assertEqual(tuple(item.lecture_id for item in scenario.state.lecture_progress), ("l1", "l2"))

        scenario.apply(
            "op-remap-two",
            RecordLectureMap(
                "record_lecture_map",
                make_artifact("map-3", "lecture_map", "lecture-map-policy/v1"),
                ("l1", "l2", "l3"),
            ),
        )
        assert scenario.state.lecture_map is not None
        map_subject = map_subject_sha256(scenario.state.lecture_map, scenario.state.policies.lecture_mapping)
        approved = scenario.apply("op-approve-remap", ApproveLectureMap("approve_lecture_map", map_subject))
        self.assertIsInstance(approved, WorkflowAdvanced)
        self.assertIsNone(scenario.state.map_reopen_context)
        self.assertEqual(tuple(item.lecture_id for item in scenario.state.lecture_progress), ("l1", "l2", "l3"))

    def test_map_reopen_rejects_non_append_subject(self) -> None:
        scenario = Scenario().production_ready()
        pending = make_source("source-2", "new-source")
        evidence = make_artifact("new-source-blocker", "blocker", WORKFLOW_STATE_VERSION)
        scenario.apply("op-new-source", RecordNewSource("record_new_source", pending, evidence))
        mismatch = scenario.apply("op-reopen-wrong", ApproveMapReopen("approve_map_reopen", "0" * 64))
        self.assertIsInstance(mismatch, WorkflowRejected)
        self.assertEqual(mismatch.diagnostics[0].code, "new_source_subject_mismatch")

    def test_reopened_map_rejects_removal_of_reserved_ids(self) -> None:
        scenario = Scenario().production_ready()
        pending = make_source("source-2", "new-source")
        evidence = make_artifact("new-source-blocker", "blocker", WORKFLOW_STATE_VERSION)
        scenario.apply("op-new-source", RecordNewSource("record_new_source", pending, evidence))
        scenario.apply("op-reopen", ApproveMapReopen("approve_map_reopen", pending.content_sha256))
        scenario.apply(
            "op-reassess",
            RecordSourceAssessment(
                "record_source_assessment",
                make_artifact("assessment-2", "source_assessment", "source-assessment-policy/v1"),
                "exam_driven",
                make_artifact("proposal-2", "priority_proposal", "priority-basis-policy/v1"),
                make_artifact("hierarchy-2", "evidence_hierarchy", "priority-basis-policy/v1"),
            ),
        )
        assert scenario.state.priority_basis is not None
        subject = priority_subject_sha256(scenario.state.priority_basis, scenario.state.policies.priority_basis)
        scenario.apply("op-reapprove", ApprovePriorityBasis("approve_priority_basis", subject))
        result = scenario.apply(
            "op-remove-reserved",
            RecordLectureMap(
                "record_lecture_map",
                make_artifact("map-short", "lecture_map", "lecture-map-policy/v1"),
                ("l1",),
            ),
        )
        self.assertIsInstance(result, WorkflowRejected)
        self.assertEqual(result.diagnostics[0].code, "map_reopen_violation")


class IdempotencyReplayAndHandoffTests(unittest.TestCase):
    def test_repeated_initialization_and_normal_request_are_idempotent(self) -> None:
        initial_request = initialize_request()
        initial = apply_workflow_request(None, initial_request)
        assert isinstance(initial, WorkflowAdvanced)
        repeated = apply_workflow_request(initial.state, initial_request)
        self.assertIsInstance(repeated, WorkflowIdempotentRepeat)
        request = transition(
            initial.state,
            "op-assess",
            RecordSourceAssessment(
                "record_source_assessment",
                make_artifact("assessment", "source_assessment", "source-assessment-policy/v1"),
                "exam_driven",
                make_artifact("proposal", "priority_proposal", "priority-basis-policy/v1"),
                make_artifact("hierarchy", "evidence_hierarchy", "priority-basis-policy/v1"),
            ),
        )
        applied = apply_workflow_request(initial.state, request)
        assert isinstance(applied, WorkflowAdvanced)
        repeat = apply_workflow_request(applied.state, request)
        self.assertIsInstance(repeat, WorkflowIdempotentRepeat)
        self.assertEqual(repeat.state, applied.state)

    def test_operation_reuse_precedes_stale_revision_check(self) -> None:
        scenario = Scenario().source_assessed()
        changed = WorkflowTransitionRequest(
            scenario.requests[0].contract_version,
            scenario.requests[0].workflow_id,
            0,
            scenario.requests[0].operation_id,
            RecordSourceAssessment(
                "record_source_assessment",
                make_artifact("assessment-other", "source_assessment", "source-assessment-policy/v1"),
                "custom",
                make_artifact("proposal-other", "priority_proposal", "priority-basis-policy/v1"),
                make_artifact("hierarchy-other", "evidence_hierarchy", "priority-basis-policy/v1"),
            ),
        )
        result = apply_workflow_request(scenario.state, changed)
        self.assertIsInstance(result, WorkflowRejected)
        self.assertEqual(result.diagnostics[0].code, "operation_id_reuse")

    def test_new_operation_with_stale_revision_is_rejected(self) -> None:
        scenario = Scenario().source_assessed()
        stale = WorkflowTransitionRequest(
            scenario.requests[0].contract_version,
            scenario.state.workflow_id,
            0,
            "op-new-stale",
            ApprovePriorityBasis("approve_priority_basis", "0" * 64),
        )
        result = apply_workflow_request(scenario.state, stale)
        self.assertIsInstance(result, WorkflowRejected)
        self.assertEqual(result.diagnostics[0].code, "stale_revision")

    def test_replay_compares_initialization_and_every_revision(self) -> None:
        scenario = Scenario().production_ready()
        replayed = replay_workflow(
            scenario.initialization,
            tuple(scenario.requests),
            tuple(scenario.receipts),
            tuple(scenario.states),
        )
        self.assertEqual(replayed, scenario.state)
        rejected = replay_workflow(
            scenario.initialization,
            tuple(scenario.requests),
            tuple(scenario.receipts[:-1]),
            tuple(scenario.states),
        )
        self.assertIsInstance(rejected, WorkflowRejected)

    def test_replay_recomputes_candidate_subject_and_validation_state(self) -> None:
        scenario = Scenario(1).production_ready()
        scenario.submit("l1", 1, "op-submit")
        scenario.validate("l1", "op-valid")
        replayed = replay_workflow(
            scenario.initialization,
            tuple(scenario.requests),
            tuple(scenario.receipts),
            tuple(scenario.states),
        )
        self.assertEqual(replayed, scenario.state)

    def test_resume_is_snapshot_only_and_rejects_corruption(self) -> None:
        scenario = Scenario().production_ready()
        self.assertIs(resume_workflow(scenario.state), scenario.state)
        object.__setattr__(scenario.state, "stage", "unknown")
        diagnostics = validate_workflow_state(scenario.state)
        self.assertEqual(diagnostics[0].code, "invalid_workflow_state")
        self.assertIsInstance(resume_workflow(scenario.state), WorkflowRejected)

    def test_handoff_projection_deduplicates_codes_in_first_occurrence_order(self) -> None:
        scenario = Scenario().production_ready()
        scenario.submit("l1", 1, "op-l1-submit", "short one")
        scenario.validate("l1", "op-l1-valid")
        scenario.submit("l2", 2, "op-l2-submit", "short two")
        warnings = tuple(item for item in scenario.state.diagnostics if item.code == "candidate_short_source")
        self.assertEqual(tuple(item.subject_id for item in warnings), ("l1", "l2"))
        handoff = derive_workflow_handoff(scenario.state)
        self.assertEqual(handoff.diagnostic_codes.count("candidate_short_source"), 1)
        self.assertEqual(
            handoff.next_actions,
            ("record_candidate_validation", "record_operation_failure", "mark_blocked", "record_new_source"),
        )

    def test_completed_handoff_contains_only_safe_ordered_references(self) -> None:
        scenario = Scenario(1).production_ready()
        scenario.submit("l1", 1, "op-submit")
        scenario.validate("l1", "op-valid")
        handoff = derive_workflow_handoff(scenario.state)
        self.assertEqual(handoff.stage, "completed")
        self.assertEqual(handoff.accepted_documents, ordered_accepted_documents(scenario.state))
        self.assertEqual(handoff.next_actions, ())
        self.assertFalse(hasattr(handoff, "source_text"))


class RecursiveBoundaryValidationTests(unittest.TestCase):
    def test_every_nested_state_record_category_is_revalidated(self) -> None:
        def reviewed_source_state() -> WorkflowState:
            scenario = Scenario().production_ready()
            source = make_source("source-2", "reviewed-source")
            evidence = make_artifact("reviewed-blocker", "blocker", WORKFLOW_STATE_VERSION)
            scenario.apply("op-new", RecordNewSource("record_new_source", source, evidence))
            scenario.apply("op-dismiss", DismissNewSource("dismiss_new_source", source.content_sha256))
            return scenario.state

        def reopen_state() -> WorkflowState:
            scenario = Scenario().production_ready()
            source = make_source("source-2", "reopen-source")
            evidence = make_artifact("reopen-blocker", "blocker", WORKFLOW_STATE_VERSION)
            scenario.apply("op-new", RecordNewSource("record_new_source", source, evidence))
            scenario.apply("op-reopen", ApproveMapReopen("approve_map_reopen", source.content_sha256))
            return scenario.state

        def pending_state() -> WorkflowState:
            scenario = Scenario().production_ready()
            source = make_source("source-2", "pending-source")
            evidence = make_artifact("pending-blocker", "blocker", WORKFLOW_STATE_VERSION)
            scenario.apply("op-new", RecordNewSource("record_new_source", source, evidence))
            return scenario.state

        def history_state() -> WorkflowState:
            scenario = Scenario(1).production_ready()
            scenario.submit("l1", 1, "op-submit")
            scenario.validate("l1", "op-reject", disposition="rejected")
            scenario.submit("l1", 1, "op-replacement", "replacement candidate")
            return scenario.state

        def failure_state() -> WorkflowState:
            scenario = Scenario()
            evidence = make_artifact("failure", "failure", WORKFLOW_STATE_VERSION)
            scenario.apply(
                "op-fail",
                RecordOperationFailure(
                    "record_operation_failure",
                    "record_source_assessment",
                    "source_assessment_operation_failed",
                    None,
                    evidence,
                ),
            )
            return scenario.state

        def blocker_state() -> WorkflowState:
            scenario = Scenario().source_assessed()
            evidence = make_artifact("blocker", "blocker", WORKFLOW_STATE_VERSION)
            scenario.apply(
                "op-block",
                MarkBlocked("mark_blocked", "private_artifact_unavailable", None, digest("subject"), evidence),
            )
            return scenario.state

        def validation_state() -> WorkflowState:
            scenario = Scenario().production_ready()
            scenario.submit("l1", 1, "op-submit", "short")
            return scenario.state

        cases = (
            ("policy", lambda: Scenario().state, lambda state: object.__setattr__(state.policies.workflow_handoff, "content_sha256", "bad"), "invalid_workflow_state"),
            ("current-source", lambda: Scenario().state, lambda state: object.__setattr__(state.source_evidence[0], "reference_version", "source-evidence-reference/v2"), "unsupported_reference_version"),
            ("reviewed-source", reviewed_source_state, lambda state: object.__setattr__(state.reviewed_source_evidence[0], "content_sha256", "bad"), "invalid_workflow_state"),
            ("assessment", lambda: Scenario().source_assessed().state, lambda state: object.__setattr__(state.source_assessment.assessment_reference, "content_sha256", "bad"), "invalid_workflow_state"),
            ("priority", lambda: Scenario().source_assessed().state, lambda state: object.__setattr__(state.priority_basis.proposal_reference, "content_sha256", "bad"), "invalid_workflow_state"),
            ("map", lambda: Scenario().map_proposed().state, lambda state: object.__setattr__(state.lecture_map.map_reference, "content_sha256", "bad"), "invalid_workflow_state"),
            ("reopen", reopen_state, lambda state: object.__setattr__(state.map_reopen_context.baseline_map_reference, "content_sha256", "bad"), "invalid_workflow_state"),
            ("pending", pending_state, lambda state: object.__setattr__(state.pending_source.source_reference, "content_sha256", "bad"), "invalid_workflow_state"),
            ("progress", validation_state, lambda state: object.__setattr__(state.lecture_progress[0].candidate, "content_sha256", "bad"), "invalid_workflow_state"),
            ("history", history_state, lambda state: object.__setattr__(state.historical_candidates[0].candidate, "content_sha256", "bad"), "invalid_workflow_state"),
            ("blocker", blocker_state, lambda state: object.__setattr__(state.active_issue.evidence_reference, "content_sha256", "bad"), "invalid_workflow_state"),
            ("failure", failure_state, lambda state: object.__setattr__(state.active_issue.evidence_reference, "content_sha256", "bad"), "invalid_workflow_state"),
            ("approval", lambda: Scenario().production_ready().state, lambda state: object.__setattr__(state.approval_history[0], "subject_sha256", "bad"), "invalid_workflow_state"),
            ("diagnostic", validation_state, lambda state: object.__setattr__(state.diagnostics[0], "category", "validation"), "invalid_workflow_state"),
            ("receipt-digest", lambda: Scenario().state, lambda state: object.__setattr__(state.operation_receipts[0], "request_sha256", "bad"), "invalid_workflow_state"),
            ("receipt-outcome", lambda: Scenario().state, lambda state: object.__setattr__(state.operation_receipts[0], "outcome", "failed"), "invalid_workflow_state"),
        )
        for name, state_factory, corrupt, expected_code in cases:
            with self.subTest(category=name):
                state = state_factory()
                corrupt(state)
                diagnostics = validate_workflow_state(state)
                self.assertEqual(tuple(item.code for item in diagnostics), (expected_code,))
                resumed = resume_workflow(state)
                self.assertIsInstance(resumed, WorkflowRejected)
                self.assertIs(resumed.state, state)

    def test_unsupported_state_version_has_specific_diagnostic(self) -> None:
        state = Scenario().state
        object.__setattr__(state, "contract_version", "course-workflow-state/v9")
        diagnostics = validate_workflow_state(state)
        self.assertEqual(diagnostics[0].code, "unsupported_workflow_contract_version")
        self.assertIsInstance(resume_workflow(state), WorkflowRejected)

    def test_incomplete_exact_public_values_are_rejected_without_uncontrolled_exceptions(self) -> None:
        forged_state = object.__new__(WorkflowState)
        self.assertEqual(validate_workflow_state(forged_state)[0].code, "invalid_workflow_state")
        resumed = resume_workflow(forged_state)
        self.assertIsInstance(resumed, WorkflowRejected)
        self.assertIsNone(resumed.state)
        applied = apply_workflow_request(forged_state, initialize_request())
        self.assertIsInstance(applied, WorkflowRejected)
        self.assertIsNone(applied.state)

        forged_request = object.__new__(workflow_module.InitializeWorkflowRequest)
        result = apply_workflow_request(None, forged_request)
        self.assertIsInstance(result, WorkflowRejected)
        self.assertEqual(result.diagnostics[0].code, "invalid_source_evidence")

    def test_hostile_subclasses_fail_closed_at_apply_state_resume_and_replay_boundaries(self) -> None:
        scenario = Scenario()
        original_state = scenario.state
        original_receipts = original_state.operation_receipts
        request = transition(
            original_state,
            "op-hostile-action",
            RecordSourceAssessment(
                "record_source_assessment",
                make_artifact("hostile-assessment", "source_assessment", "source-assessment-policy/v1"),
                "exam_driven",
                make_artifact("hostile-proposal", "priority_proposal", "priority-basis-policy/v1"),
                make_artifact("hostile-hierarchy", "evidence_hierarchy", "priority-basis-policy/v1"),
            ),
        )
        object.__setattr__(request.payload, "action", HostileStr("record_source_assessment"))
        result = apply_workflow_request(original_state, request)
        self.assertIsInstance(result, WorkflowRejected)
        self.assertIs(result.state, original_state)
        self.assertEqual(result.diagnostics[0].code, "illegal_transition")
        self.assertEqual(result.state.operation_receipts, original_receipts)

        hostile_digest_request = initialize_request()
        object.__setattr__(
            hostile_digest_request.payload.source_evidence[0],
            "content_sha256",
            HostileStr(hostile_digest_request.payload.source_evidence[0].content_sha256),
        )
        hostile_digest_result = apply_workflow_request(None, hostile_digest_request)
        self.assertIsInstance(hostile_digest_result, WorkflowRejected)
        self.assertIsNone(hostile_digest_result.state)
        self.assertEqual(hostile_digest_result.diagnostics[0].code, "invalid_source_evidence")

        corrupted_state = Scenario().state
        object.__setattr__(corrupted_state, "revision", HostileInt(0))
        diagnostics = validate_workflow_state(corrupted_state)
        self.assertEqual(diagnostics[0].code, "invalid_workflow_state")
        resumed = resume_workflow(corrupted_state)
        self.assertIsInstance(resumed, WorkflowRejected)

        corrupted_reference_state = Scenario().state
        object.__setattr__(
            corrupted_reference_state.source_evidence[0],
            "content_sha256",
            HostileStr(corrupted_reference_state.source_evidence[0].content_sha256),
        )
        self.assertEqual(validate_workflow_state(corrupted_reference_state)[0].code, "invalid_workflow_state")
        self.assertIsInstance(resume_workflow(corrupted_reference_state), WorkflowRejected)

        initialization = initialize_request()
        object.__setattr__(initialization, "contract_version", HostileStr(workflow_module.WORKFLOW_TRANSITION_VERSION))
        replayed = replay_workflow(initialization, (), (), ())
        self.assertIsInstance(replayed, WorkflowRejected)
        tuple_replay = replay_workflow(initialize_request(), HostileTuple(()), (), ())
        self.assertIsInstance(tuple_replay, WorkflowRejected)

        handoff = derive_workflow_handoff(Scenario().state)
        public_values = (
            result,
            hostile_digest_result,
            diagnostics,
            resumed,
            replayed,
            tuple_replay,
            original_state,
            original_receipts,
            handoff,
        )
        for value in public_values:
            self.assertNotIn(HOSTILE_SENTINEL, repr(value))

    def test_hostile_candidate_and_provenance_scalars_are_invalid_candidate_documents(self) -> None:
        def malformed_request(field: str) -> tuple[WorkflowState, WorkflowTransitionRequest]:
            scenario = Scenario().production_ready()
            request = transition(
                scenario.state,
                f"op-hostile-{field}",
                SubmitLectureCandidate("submit_lecture_candidate", "l1", make_document()),
            )
            if field in ("contract-version", "document-id", "source-text"):
                attribute = field.replace("-", "_")
                object.__setattr__(
                    request.payload.document,
                    attribute,
                    HostileStr(getattr(request.payload.document, attribute)),
                )
            elif field == "order":
                object.__setattr__(request.payload.document, "order", HostileInt(request.payload.document.order))
            elif field == "provenance-digest":
                object.__setattr__(
                    request.payload.document.provenance,
                    "content_sha256",
                    HostileStr(request.payload.document.provenance.content_sha256),
                )
            else:
                provenance_type = type(request.payload.document.provenance)
                hostile_provenance_type = type("HostileProvenance", (provenance_type,), {})
                object.__setattr__(
                    request.payload.document,
                    "provenance",
                    hostile_provenance_type(request.payload.document.provenance.content_sha256),
                )
            return scenario.state, request

        for field in ("contract-version", "document-id", "order", "source-text", "provenance-digest", "provenance-type"):
            with self.subTest(field=field):
                state, request = malformed_request(field)
                receipts = state.operation_receipts
                result = apply_workflow_request(state, request)
                self.assertIsInstance(result, WorkflowRejected)
                self.assertIs(result.state, state)
                self.assertEqual(result.diagnostics[0].code, "invalid_candidate_document")
                self.assertEqual(result.state.operation_receipts, receipts)
                self.assertNotIn(HOSTILE_SENTINEL, repr(result))

    def test_corrupted_pending_source_subject_fails_closed_everywhere(self) -> None:
        scenario = Scenario().production_ready()
        pending_source = make_source("source-2", "hostile-pending-source")
        evidence = make_artifact("hostile-pending-blocker", "blocker", WORKFLOW_STATE_VERSION)
        blocked = scenario.apply(
            "op-hostile-pending",
            RecordNewSource("record_new_source", pending_source, evidence),
        )
        self.assertIsInstance(blocked, WorkflowBlocked)
        safe_handoff = derive_workflow_handoff(scenario.state)
        decision_request = transition(
            scenario.state,
            "op-hostile-pending-decision",
            DismissNewSource("dismiss_new_source", pending_source.content_sha256),
        )
        receipts = scenario.state.operation_receipts
        assert scenario.state.pending_source is not None
        object.__setattr__(
            scenario.state.pending_source,
            "blocker_subject_sha256",
            HostileStr(pending_source.content_sha256),
        )

        diagnostics = validate_workflow_state(scenario.state)
        self.assertEqual(tuple(item.code for item in diagnostics), ("invalid_workflow_state",))
        resumed = resume_workflow(scenario.state)
        self.assertIsInstance(resumed, WorkflowRejected)

        applied = apply_workflow_request(scenario.state, decision_request)
        self.assertIsInstance(applied, WorkflowRejected)
        self.assertIs(applied.state, scenario.state)
        self.assertEqual(applied.state.operation_receipts, receipts)

        replayed = replay_workflow(
            scenario.initialization,
            tuple(scenario.requests),
            tuple(scenario.receipts),
            tuple(scenario.states),
        )
        self.assertIsInstance(replayed, WorkflowRejected)

        for value in (diagnostics, resumed, applied, replayed, receipts, scenario.state, safe_handoff):
            self.assertNotIn(HOSTILE_SENTINEL, repr(value))

    def test_every_request_envelope_and_nested_value_is_revalidated_without_mutation(self) -> None:
        initialization_cases = []
        request = initialize_request()
        object.__setattr__(request, "contract_version", "course-workflow-transition/v9")
        initialization_cases.append((request, "unsupported_transition_contract_version"))
        request = initialize_request()
        object.__setattr__(request, "workflow_id", "BAD")
        initialization_cases.append((request, "invalid_workflow_id"))
        request = initialize_request()
        object.__setattr__(request, "operation_id", "BAD")
        initialization_cases.append((request, "invalid_operation_id"))
        request = initialize_request()
        object.__setattr__(request.payload.source_evidence[0], "reference_version", "source-evidence-reference/v2")
        initialization_cases.append((request, "unsupported_reference_version"))
        request = initialize_request()
        object.__setattr__(request.payload.policies.source_assessment, "policy_version", "source-assessment-policy/v2")
        initialization_cases.append((request, "unsupported_policy_kind_version"))
        for malformed, expected_code in initialization_cases:
            with self.subTest(initialization=expected_code):
                result = apply_workflow_request(None, malformed)
                self.assertIsInstance(result, WorkflowRejected)
                self.assertIsNone(result.state)
                self.assertEqual(result.diagnostics[0].code, expected_code)

        scenario = Scenario()
        original_state = scenario.state
        original_receipts = original_state.operation_receipts
        payload_factory = lambda: RecordSourceAssessment(
            "record_source_assessment",
            make_artifact("assessment", "source_assessment", "source-assessment-policy/v1"),
            "exam_driven",
            make_artifact("proposal", "priority_proposal", "priority-basis-policy/v1"),
            make_artifact("hierarchy", "evidence_hierarchy", "priority-basis-policy/v1"),
        )

        transition_cases = []
        request = transition(original_state, "op-version", payload_factory())
        object.__setattr__(request, "contract_version", "course-workflow-transition/v2")
        transition_cases.append((request, "unsupported_transition_contract_version"))
        request = transition(original_state, "op-action", payload_factory())
        object.__setattr__(request.payload, "action", "approve_lecture_map")
        transition_cases.append((request, "illegal_transition"))
        request = transition(original_state, "op-reference", payload_factory())
        object.__setattr__(request.payload.assessment_reference, "reference_version", "workflow-artifact-reference/v2")
        transition_cases.append((request, "unsupported_reference_version"))
        request = transition(original_state, "op-revision", payload_factory())
        object.__setattr__(request, "expected_revision", -1)
        transition_cases.append((request, "illegal_transition"))
        for malformed, expected_code in transition_cases:
            with self.subTest(transition=expected_code):
                result = apply_workflow_request(original_state, malformed)
                self.assertIsInstance(result, WorkflowRejected)
                self.assertIs(result.state, original_state)
                self.assertEqual(result.diagnostics[0].code, expected_code)
                self.assertEqual(original_state.operation_receipts, original_receipts)

        candidate_scenario = Scenario().production_ready()
        candidate_request = transition(
            candidate_scenario.state,
            "op-invalid-document",
            SubmitLectureCandidate("submit_lecture_candidate", "l1", make_document()),
        )
        object.__setattr__(candidate_request.payload.document.provenance, "content_sha256", "bad")
        candidate_result = apply_workflow_request(candidate_scenario.state, candidate_request)
        self.assertIsInstance(candidate_result, WorkflowRejected)
        self.assertEqual(candidate_result.diagnostics[0].code, "invalid_candidate_document")
        self.assertEqual(candidate_result.state.revision, 4)

        validation_scenario = Scenario(1).production_ready()
        validation_scenario.submit("l1", 1, "op-submit")
        progress = validation_scenario.state.lecture_progress[0]
        assert progress.candidate is not None
        candidate_subject = candidate_subject_sha256(progress.candidate)
        validation = ValidationRecord(
            candidate_subject,
            "lecture-validation-policy/v1",
            validation_scenario.state.policies.lecture_validation.content_sha256,
            "passed",
            make_artifact("validation-boundary", "validation", "lecture-validation-policy/v1"),
        )
        validation_request = transition(
            validation_scenario.state,
            "op-invalid-validation-version",
            RecordCandidateValidation("record_candidate_validation", "l1", candidate_subject, validation),
        )
        object.__setattr__(validation, "validation_policy_version", "lecture-validation-policy/v2")
        validation_result = apply_workflow_request(validation_scenario.state, validation_request)
        self.assertIsInstance(validation_result, WorkflowRejected)
        self.assertEqual(validation_result.diagnostics[0].code, "unsupported_policy_kind_version")
        self.assertEqual(validation_result.state.operation_receipts, validation_scenario.state.operation_receipts)

    def test_all_stage_disposition_action_combinations_match_the_graph(self) -> None:
        normal_actions = {
            "source_assessment": {"record_source_assessment", "record_operation_failure", "mark_blocked"},
            "priority_approval": {"approve_priority_basis", "reject_priority_basis", "mark_blocked"},
            "lecture_mapping": {"record_lecture_map", "record_operation_failure", "mark_blocked"},
            "map_approval": {"approve_lecture_map", "reject_lecture_map", "mark_blocked"},
            "lecture_production": {"submit_lecture_candidate", "record_operation_failure", "mark_blocked", "record_new_source"},
            "lecture_validation": {"record_candidate_validation", "record_operation_failure", "mark_blocked", "record_new_source"},
            "completed": set(),
        }
        dispositions = ("ready", "awaiting_approval", "blocked", "failed", "completed")
        for stage in workflow_module.WORKFLOW_STAGES:
            for disposition in dispositions:
                if disposition == workflow_module.NORMAL_DISPOSITIONS[stage]:
                    expected = normal_actions[stage]
                    issue_code = None
                elif disposition == "failed" and stage in workflow_module.FAILURE_REGISTRY:
                    expected = {"retry_failed"}
                    issue_code = workflow_module.FAILURE_REGISTRY[stage][1]
                elif disposition == "blocked" and stage != "completed":
                    expected = {"clear_blocker"}
                    issue_code = "private_artifact_unavailable"
                else:
                    expected = set()
                    issue_code = None
                actual = set(workflow_module._actions_for(stage, disposition, issue_code))
                for action in workflow_module.WORKFLOW_ACTIONS:
                    with self.subTest(stage=stage, disposition=disposition, action=action):
                        self.assertEqual(action in actual, action in expected)

        for stage in workflow_module.NEW_SOURCE_STAGES:
            self.assertEqual(
                set(workflow_module._actions_for(stage, "blocked", "new_source_review_required")),
                {"dismiss_new_source", "approve_map_reopen"},
            )

    def test_every_public_illegal_stage_disposition_action_is_non_mutating(self) -> None:
        def payload_for(action: str, state: WorkflowState) -> object:
            artifact = lambda artifact_id, kind, producer: make_artifact(artifact_id, kind, producer)
            if action == "record_source_assessment":
                return RecordSourceAssessment(
                    action,
                    artifact("matrix-assessment", "source_assessment", "source-assessment-policy/v1"),
                    "exam_driven",
                    artifact("matrix-proposal", "priority_proposal", "priority-basis-policy/v1"),
                    artifact("matrix-hierarchy", "evidence_hierarchy", "priority-basis-policy/v1"),
                )
            if action in ("approve_priority_basis", "reject_priority_basis"):
                subject = priority_subject_sha256(state.priority_basis, state.policies.priority_basis) if state.priority_basis is not None else "0" * 64
                return ApprovePriorityBasis(action, subject) if action.startswith("approve") else RejectPriorityBasis(action, subject)
            if action == "record_lecture_map":
                return RecordLectureMap(action, artifact("matrix-map", "lecture_map", "lecture-map-policy/v1"), ("l1",))
            if action in ("approve_lecture_map", "reject_lecture_map"):
                subject = map_subject_sha256(state.lecture_map, state.policies.lecture_mapping) if state.lecture_map is not None else "0" * 64
                return ApproveLectureMap(action, subject) if action.startswith("approve") else RejectLectureMap(action, subject)
            if action == "submit_lecture_candidate":
                return SubmitLectureCandidate(action, "l1", make_document())
            if action == "record_candidate_validation":
                progress = next((item for item in state.lecture_progress if item.candidate is not None), None)
                subject = candidate_subject_sha256(progress.candidate) if progress is not None else "0" * 64
                validation = ValidationRecord(
                    subject,
                    "lecture-validation-policy/v1",
                    state.policies.lecture_validation.content_sha256,
                    "passed",
                    artifact("matrix-validation", "validation", "lecture-validation-policy/v1"),
                )
                return RecordCandidateValidation(action, "l1", subject, validation)
            if action == "record_operation_failure":
                failed_action, code = workflow_module.FAILURE_REGISTRY.get(
                    state.stage,
                    workflow_module.FAILURE_REGISTRY["source_assessment"],
                )
                return RecordOperationFailure(
                    action,
                    failed_action,
                    code,
                    None,
                    artifact("matrix-failure", "failure", WORKFLOW_STATE_VERSION),
                )
            if action == "retry_failed":
                return RetryFailed(action, "source_assessment_operation_failed", digest("matrix-failure"))
            if action == "mark_blocked":
                return MarkBlocked(
                    action,
                    "private_artifact_unavailable",
                    None,
                    digest("matrix-blocker"),
                    artifact("matrix-blocker", "blocker", WORKFLOW_STATE_VERSION),
                )
            if action == "clear_blocker":
                return ClearBlocker(action, "private_artifact_unavailable", digest("matrix-blocker"))
            if action == "record_new_source":
                return RecordNewSource(
                    action,
                    make_source("source-2", "matrix-source"),
                    artifact("matrix-new-source", "blocker", WORKFLOW_STATE_VERSION),
                )
            if action == "dismiss_new_source":
                return DismissNewSource(action, digest("matrix-source"))
            if action == "approve_map_reopen":
                return ApproveMapReopen(action, digest("matrix-source"))
            if action == "reopen_lectures_for_correction":
                return ReopenLecturesForCorrection(action, ("l1",), "review-request", "review-operation", state.revision)
            raise AssertionError(action)

        normal_scenarios = (
            Scenario(),
            Scenario().source_assessed(),
            Scenario().priority_approved(),
            Scenario().map_proposed(),
            Scenario().production_ready(),
        )
        validation = Scenario().production_ready()
        validation.submit("l1", 1, "op-active")
        completed = Scenario(1).production_ready()
        completed.submit("l1", 1, "op-complete-submit")
        completed.validate("l1", "op-complete-validation")

        failed = Scenario()
        failure_evidence = make_artifact("matrix-active-failure", "failure", WORKFLOW_STATE_VERSION)
        failed.apply(
            "op-active-failure",
            RecordOperationFailure(
                "record_operation_failure",
                "record_source_assessment",
                "source_assessment_operation_failed",
                None,
                failure_evidence,
            ),
        )
        blocked = Scenario()
        blocked.apply(
            "op-active-blocker",
            MarkBlocked(
                "mark_blocked",
                "private_artifact_unavailable",
                None,
                digest("matrix-active-blocker"),
                make_artifact("matrix-active-blocker", "blocker", WORKFLOW_STATE_VERSION),
            ),
        )
        new_source_blocked = Scenario().production_ready()
        new_source_blocked.apply(
            "op-active-new-source",
            RecordNewSource(
                "record_new_source",
                make_source("source-2", "matrix-active-source"),
                make_artifact("matrix-active-source", "blocker", WORKFLOW_STATE_VERSION),
            ),
        )

        scenarios = normal_scenarios + (validation, completed, failed, blocked, new_source_blocked)
        for scenario in scenarios:
            legal = set(workflow_module._legal_actions(scenario.state))
            for action in workflow_module.WORKFLOW_ACTIONS[1:]:
                if action in legal:
                    continue
                with self.subTest(stage=scenario.state.stage, disposition=scenario.state.disposition, action=action):
                    before = scenario.state
                    request = transition(before, f"op-illegal-{action}", payload_for(action, before))
                    result = apply_workflow_request(before, request)
                    self.assertIsInstance(result, WorkflowRejected)
                    self.assertIs(result.state, before)
                    self.assertEqual(result.state.operation_receipts, before.operation_receipts)

    def test_blocker_stage_entry_and_clear_matrix_is_exact(self) -> None:
        dispositions = ("ready", "awaiting_approval", "blocked", "failed", "completed")
        for stage in workflow_module.WORKFLOW_STAGES:
            for disposition in dispositions:
                for code in ("private_artifact_unavailable", "external_prerequisite_unavailable", "new_source_review_required"):
                    if code == "private_artifact_unavailable":
                        permitted = (stage, disposition) in workflow_module.PRIVATE_ARTIFACT_BLOCKER_STARTS
                    elif code == "external_prerequisite_unavailable":
                        permitted = stage in workflow_module.EXTERNAL_PREREQUISITE_BLOCKER_STAGES and disposition == "ready"
                    else:
                        permitted = stage in workflow_module.NEW_SOURCE_STAGES and disposition == "ready"
                    with self.subTest(stage=stage, disposition=disposition, code=code):
                        if permitted:
                            workflow_module.WorkflowBlocker(
                                code,
                                stage,
                                disposition,
                                None,
                                digest("subject"),
                                make_artifact("blocker", "blocker", WORKFLOW_STATE_VERSION),
                                1,
                            )
                        else:
                            with self.assertRaises(ValueError):
                                workflow_module.WorkflowBlocker(
                                    code,
                                    stage,
                                    disposition,
                                    None,
                                    digest("subject"),
                                    make_artifact("blocker", "blocker", WORKFLOW_STATE_VERSION),
                                    1,
                                )

    def test_replay_rejects_missing_extra_misordered_mismatched_and_corrupt_inputs(self) -> None:
        scenario = Scenario().source_assessed()
        valid = (scenario.initialization, tuple(scenario.requests), tuple(scenario.receipts), tuple(scenario.states))
        variants = (
            (valid[0], valid[1], valid[2][:-1], valid[3]),
            (valid[0], valid[1], valid[2] + (valid[2][-1],), valid[3]),
            (valid[0], valid[1], tuple(reversed(valid[2])), valid[3]),
            (valid[0], valid[1], valid[2], tuple(reversed(valid[3]))),
        )
        for arguments in variants:
            with self.subTest(kind="sequence"):
                self.assertIsInstance(replay_workflow(*arguments), WorkflowRejected)

        corrupted = Scenario().source_assessed()
        object.__setattr__(corrupted.receipts[-1], "request_sha256", "bad")
        result = replay_workflow(
            corrupted.initialization,
            tuple(corrupted.requests),
            tuple(corrupted.receipts),
            tuple(corrupted.states),
        )
        self.assertIsInstance(result, WorkflowRejected)

        malformed_request = Scenario().source_assessed()
        object.__setattr__(malformed_request.requests[0].payload, "action", "approve_lecture_map")
        result = replay_workflow(
            malformed_request.initialization,
            tuple(malformed_request.requests),
            tuple(malformed_request.receipts),
            tuple(malformed_request.states),
        )
        self.assertIsInstance(result, WorkflowRejected)

    def test_initialization_reuse_changed_payload_and_new_operation_are_rejected(self) -> None:
        request = initialize_request()
        initial = apply_workflow_request(None, request)
        assert isinstance(initial, WorkflowAdvanced)

        changed = initialize_request(request.operation_id)
        object.__setattr__(changed.payload.source_evidence[0], "content_sha256", digest("changed"))
        changed_result = apply_workflow_request(initial.state, changed)
        self.assertIsInstance(changed_result, WorkflowRejected)
        self.assertEqual(changed_result.diagnostics[0].code, "operation_id_reuse")

        new_operation = initialize_request("op-new-initialize")
        new_result = apply_workflow_request(initial.state, new_operation)
        self.assertIsInstance(new_result, WorkflowRejected)
        self.assertEqual(new_result.diagnostics[0].code, "illegal_transition")
        self.assertEqual(new_result.state.operation_receipts, initial.state.operation_receipts)

    def test_every_action_has_its_registered_outcome_and_exact_receipt_subject(self) -> None:
        observed: dict[str, tuple[str, str]] = {}

        def record(result: object, expected_subject: str) -> None:
            if isinstance(result, workflow_module.OperationReceipt):
                receipt = result
            else:
                self.assertIsInstance(result, (WorkflowAdvanced, WorkflowBlocked, WorkflowFailed))
                receipt = result.receipt
            observed[receipt.action] = (receipt.outcome, receipt.subject_sha256)
            self.assertEqual(receipt.outcome, workflow_module.RECEIPT_OUTCOMES[receipt.action])
            self.assertEqual(receipt.subject_sha256, expected_subject)

        initialization = initialize_request()
        initial = apply_workflow_request(None, initialization)
        assert isinstance(initial, WorkflowAdvanced)
        record(initial, workflow_module.source_set_subject_sha256(initialization.payload.source_evidence))

        assessed = Scenario()
        assessed.source_assessed()
        assert assessed.state.priority_basis is not None
        priority_subject = priority_subject_sha256(assessed.state.priority_basis, assessed.state.policies.priority_basis)
        record(assessed.receipts[-1], priority_subject)
        record(assessed.apply("op-approve", ApprovePriorityBasis("approve_priority_basis", priority_subject)), priority_subject)

        rejected_priority = Scenario().source_assessed()
        assert rejected_priority.state.priority_basis is not None
        subject = priority_subject_sha256(rejected_priority.state.priority_basis, rejected_priority.state.policies.priority_basis)
        record(rejected_priority.apply("op-reject", RejectPriorityBasis("reject_priority_basis", subject)), subject)

        mapped = Scenario().priority_approved()
        map_result = mapped.apply(
            "op-map-receipt",
            RecordLectureMap(
                "record_lecture_map",
                make_artifact("map-receipt", "lecture_map", "lecture-map-policy/v1"),
                ("l1", "l2"),
            ),
        )
        assert mapped.state.lecture_map is not None
        map_subject = map_subject_sha256(mapped.state.lecture_map, mapped.state.policies.lecture_mapping)
        record(map_result, map_subject)
        record(mapped.apply("op-map-approve-receipt", ApproveLectureMap("approve_lecture_map", map_subject)), map_subject)

        rejected_map = Scenario().map_proposed()
        assert rejected_map.state.lecture_map is not None
        subject = map_subject_sha256(rejected_map.state.lecture_map, rejected_map.state.policies.lecture_mapping)
        record(rejected_map.apply("op-map-reject-receipt", RejectLectureMap("reject_lecture_map", subject)), subject)

        candidate = Scenario(1).production_ready()
        submit_result = candidate.submit("l1", 1, "op-submit-receipt")
        candidate_reference = candidate.state.lecture_progress[0].candidate
        assert candidate_reference is not None
        candidate_subject = candidate_subject_sha256(candidate_reference)
        record(submit_result, candidate_subject)
        record(candidate.validate("l1", "op-validation-receipt"), candidate_subject)

        failed = Scenario()
        failure_evidence = make_artifact("failure-receipt", "failure", WORKFLOW_STATE_VERSION)
        record(
            failed.apply(
                "op-failure-receipt",
                RecordOperationFailure(
                    "record_operation_failure",
                    "record_source_assessment",
                    "source_assessment_operation_failed",
                    None,
                    failure_evidence,
                ),
            ),
            failure_evidence.content_sha256,
        )
        record(
            failed.apply(
                "op-retry-receipt",
                RetryFailed("retry_failed", "source_assessment_operation_failed", failure_evidence.content_sha256),
            ),
            failure_evidence.content_sha256,
        )

        blocked = Scenario().source_assessed()
        blocker_subject = digest("blocker-subject")
        blocker_evidence = make_artifact("blocker-receipt", "blocker", WORKFLOW_STATE_VERSION)
        record(
            blocked.apply(
                "op-block-receipt",
                MarkBlocked("mark_blocked", "private_artifact_unavailable", None, blocker_subject, blocker_evidence),
            ),
            blocker_subject,
        )
        record(
            blocked.apply(
                "op-clear-receipt",
                ClearBlocker("clear_blocker", "private_artifact_unavailable", blocker_subject),
            ),
            blocker_subject,
        )

        dismissed = Scenario().production_ready()
        pending_source = make_source("source-2", "dismiss-source")
        pending_evidence = make_artifact("dismiss-blocker", "blocker", WORKFLOW_STATE_VERSION)
        record(
            dismissed.apply("op-new-dismiss", RecordNewSource("record_new_source", pending_source, pending_evidence)),
            pending_source.content_sha256,
        )
        record(
            dismissed.apply("op-dismiss-receipt", DismissNewSource("dismiss_new_source", pending_source.content_sha256)),
            pending_source.content_sha256,
        )

        reopened = Scenario().production_ready()
        reopen_source = make_source("source-2", "reopen-receipt-source")
        reopen_evidence = make_artifact("reopen-receipt-blocker", "blocker", WORKFLOW_STATE_VERSION)
        reopened.apply("op-new-reopen", RecordNewSource("record_new_source", reopen_source, reopen_evidence))
        record(
            reopened.apply("op-reopen-receipt", ApproveMapReopen("approve_map_reopen", reopen_source.content_sha256)),
            reopen_source.content_sha256,
        )

        self.assertEqual(set(observed), set(workflow_module.WORKFLOW_ACTIONS) - {"reopen_lectures_for_correction"})

    def test_collection_order_uniqueness_and_disjointness_corruptions_are_rejected(self) -> None:
        def reviewed_state() -> WorkflowState:
            scenario = Scenario().production_ready()
            source = make_source("source-2", "reviewed-collection")
            evidence = make_artifact("reviewed-collection-blocker", "blocker", WORKFLOW_STATE_VERSION)
            scenario.apply("op-new", RecordNewSource("record_new_source", source, evidence))
            scenario.apply("op-dismiss", DismissNewSource("dismiss_new_source", source.content_sha256))
            return scenario.state

        def pending_state() -> WorkflowState:
            scenario = Scenario().production_ready()
            source = make_source("source-2", "pending-collection")
            evidence = make_artifact("pending-collection-blocker", "blocker", WORKFLOW_STATE_VERSION)
            scenario.apply("op-new", RecordNewSource("record_new_source", source, evidence))
            return scenario.state

        def history_state() -> WorkflowState:
            scenario = Scenario(1).production_ready()
            scenario.submit("l1", 1, "op-old")
            scenario.validate("l1", "op-rejected", disposition="rejected")
            scenario.submit("l1", 1, "op-new", "replacement")
            return scenario.state

        def diagnostic_state() -> WorkflowState:
            scenario = Scenario().production_ready()
            scenario.submit("l1", 1, "op-short", "short")
            return scenario.state

        cases = (
            (lambda: Scenario().state, lambda state: object.__setattr__(state, "source_evidence", state.source_evidence * 2)),
            (reviewed_state, lambda state: object.__setattr__(state, "reviewed_source_evidence", (state.source_evidence[0],))),
            (pending_state, lambda state: object.__setattr__(state.pending_source, "source_reference", state.source_evidence[0])),
            (lambda: Scenario().production_ready().state, lambda state: object.__setattr__(state, "lecture_progress", tuple(reversed(state.lecture_progress)))),
            (history_state, lambda state: object.__setattr__(state, "historical_candidates", state.historical_candidates * 2)),
            (lambda: Scenario().production_ready().state, lambda state: object.__setattr__(state, "approval_history", tuple(reversed(state.approval_history)))),
            (diagnostic_state, lambda state: object.__setattr__(state, "diagnostics", state.diagnostics * 2)),
            (lambda: Scenario().source_assessed().state, lambda state: object.__setattr__(state, "operation_receipts", tuple(reversed(state.operation_receipts)))),
        )
        for state_factory, corrupt in cases:
            state = state_factory()
            corrupt(state)
            self.assertEqual(validate_workflow_state(state)[0].code, "invalid_workflow_state")
            self.assertIsInstance(resume_workflow(state), WorkflowRejected)

    def test_v1_state_rejects_v2_only_progress_history_and_receipt_vocabulary(self) -> None:
        """T053 Block: v1 states (course-workflow-state/v1) must reject
        v2-only vocabulary injected into lecture_progress status,
        historical_candidates disposition, and operation_receipts action
        or transition version. v1 histories are byte-for-byte preserved.
        """
        scenario = Scenario(1).production_ready()
        first_submit = scenario.submit("l1", 1, "op-initial")
        self.assertIsInstance(first_submit, WorkflowAdvanced)
        rejected = scenario.validate("l1", "op-rejected", disposition="rejected")
        self.assertIsInstance(rejected, WorkflowAdvanced)
        retry_state = rejected.state
        self.assertEqual(retry_state.contract_version, "course-workflow-state/v1")
        # The retry_required lecture_progress is the v1 contract shape; an
        # injected correction_required record fails the v1 contract.
        first = retry_state.lecture_progress[0]
        forged_first = workflow_module.LectureProgress(
            lecture_id=first.lecture_id,
            map_position=first.map_position,
            status="correction_required",
            attempt=first.attempt,
            candidate=first.candidate,
            validation=workflow_module.ValidationRecord(
                candidate_subject_sha256=workflow_module.candidate_subject_sha256(first.candidate),  # type: ignore[arg-type]
                validation_policy_version="lecture-validation-policy/v1",
                validation_policy_sha256=retry_state.policies.lecture_validation.content_sha256,
                disposition="passed",
                validation_reference=workflow_module.WorkflowArtifactReference(
                    "workflow-artifact-reference/v1",
                    "validation-correction-forged",
                    "validation",
                    digest("forged-validation"),
                    "lecture-validation-policy/v1",
                ),
            ),
            accepted_document=first.candidate,
        )
        with self.assertRaises(ValueError):
            replace(retry_state, lecture_progress=(forged_first,))
        forged_snapshot = object.__new__(WorkflowState)
        for field in type(retry_state).__dataclass_fields__:
            object.__setattr__(forged_snapshot, field, getattr(retry_state, field))
        object.__setattr__(forged_snapshot, "lecture_progress", (forged_first,))
        # _state_violation surfaces the v1 + correction_required
        # combination as a version-category invariant violation.
        self.assertEqual(workflow_module._state_violation(forged_snapshot), "version")
        # validate_workflow_state collapses every category-level violation
        # to its concrete diagnostic (invalid_workflow_state by default;
        # unsupported_workflow_contract_version is reserved for wholly
        # unknown contract_version strings, NOT for v1+v2 vocabulary).
        self.assertEqual(
            validate_workflow_state(forged_snapshot)[0].code,
            "invalid_workflow_state",
        )
        self.assertIsInstance(resume_workflow(forged_snapshot), WorkflowRejected)

        # superseded_by_correction is a v2-only historical disposition.
        # The retry_required state has no historical_candidates yet, so
        # forge a candidate first to land a record on disk.
        second_submit = scenario.submit("l1", 1, "op-replacement", source="replacement")
        self.assertIsInstance(second_submit, WorkflowAdvanced)
        superseded_target = second_submit.state.historical_candidates[0]
        forged_record = workflow_module.HistoricalCandidateRecord(
            lecture_id=superseded_target.lecture_id,
            map_position=superseded_target.map_position,
            approved_map_subject_sha256=superseded_target.approved_map_subject_sha256,
            attempt=superseded_target.attempt,
            candidate=superseded_target.candidate,
            validation=superseded_target.validation,
            historical_disposition="superseded_by_correction",
        )
        with self.assertRaises(ValueError):
            replace(second_submit.state, historical_candidates=(forged_record,))
        forged_superseded_snapshot = object.__new__(WorkflowState)
        for field in type(second_submit.state).__dataclass_fields__:
            object.__setattr__(forged_superseded_snapshot, field, getattr(second_submit.state, field))
        object.__setattr__(forged_superseded_snapshot, "historical_candidates", (forged_record,))
        self.assertEqual(
            workflow_module._state_violation(forged_superseded_snapshot),
            "version",
        )
        self.assertEqual(
            validate_workflow_state(forged_superseded_snapshot)[0].code,
            "invalid_workflow_state",
        )

        # Forging the v2-only receipt action onto a v1 state must also fail.
        # Append a forged reopen_lectures_for_correction receipt at the
        # tail of the existing chain so structural invariants hold; the
        # v1 contract must still reject the v2-only action vocabulary.
        existing_receipts = retry_state.operation_receipts
        last = existing_receipts[-1]
        forged_receipt = workflow_module.OperationReceipt(
            "course-workflow-transition/v1",
            "op-reopen",
            last.to_revision,
            last.to_revision + 1,
            "reopen_lectures_for_correction",
            digest("forged-reopen"),
            "advanced",
            digest("forged-reopen-subject"),
        )
        forged_receipt_snapshot = object.__new__(WorkflowState)
        for field in type(retry_state).__dataclass_fields__:
            object.__setattr__(forged_receipt_snapshot, field, getattr(retry_state, field))
        object.__setattr__(
            forged_receipt_snapshot,
            "operation_receipts",
            existing_receipts + (forged_receipt,),
        )
        # Bump revision so the receipt chain aligns (chain validation
        # also checks operation_receipts[-1].to_revision == state.revision).
        object.__setattr__(
            forged_receipt_snapshot,
            "revision",
            last.to_revision + 1,
        )
        self.assertEqual(
            validate_workflow_state(forged_receipt_snapshot)[0].code,
            "invalid_workflow_state",
        )
        self.assertIsInstance(resume_workflow(forged_receipt_snapshot), WorkflowRejected)
        self.assertEqual(
            resume_workflow(forged_receipt_snapshot).diagnostics[0].code,  # type: ignore[union-attr]
            "invalid_workflow_state",
        )

        # course-workflow-transition/v2 against a v1 state must be rejected
        # by apply_workflow_request with a version-mismatch diagnostic.
        v2_transition = workflow_module.WorkflowTransitionRequest(
            "course-workflow-transition/v2",
            retry_state.workflow_id,
            retry_state.revision,
            "op-forbidden-v2",
            workflow_module.SubmitLectureCandidate(
                "submit_lecture_candidate", "l1", make_document("l1", 1, "forbidden")
            ),
        )
        applied = apply_workflow_request(retry_state, v2_transition)
        self.assertIsInstance(applied, WorkflowRejected)
        self.assertEqual(applied.diagnostics[0].code, "unsupported_transition_contract_version")


if __name__ == "__main__":
    unittest.main()
