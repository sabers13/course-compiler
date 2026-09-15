from __future__ import annotations

import ast
import hashlib
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import course_compiler.workflow as workflow_module
from course_compiler import DocumentReference, LectureDocument, SourceProvenance
from course_compiler.workflow import (
    DIAGNOSTIC_REGISTRY,
    SOURCE_EVIDENCE_REFERENCE_VERSION,
    WORKFLOW_ARTIFACT_REFERENCE_VERSION,
    WORKFLOW_STATE_VERSION,
    WORKFLOW_TRANSITION_VERSION,
    ApprovalRecord,
    ApproveLectureMap,
    ApproveMapReopen,
    ApprovePriorityBasis,
    ClearBlocker,
    DismissNewSource,
    HistoricalCandidateRecord,
    InitializeWorkflow,
    InitializeWorkflowRequest,
    LectureMapRecord,
    LectureProgress,
    MapReopenContext,
    MarkBlocked,
    OperationReceipt,
    PendingSourceRecord,
    PolicyReference,
    PriorityBasisRecord,
    RecordCandidateValidation,
    RecordLectureMap,
    RecordNewSource,
    RecordOperationFailure,
    RecordSourceAssessment,
    RejectLectureMap,
    RejectPriorityBasis,
    RetryFailed,
    SourceAssessmentRecord,
    SourceEvidenceReference,
    SubmitLectureCandidate,
    ValidationRecord,
    WorkflowAdvanced,
    WorkflowArtifactReference,
    WorkflowBlocked,
    WorkflowBlocker,
    WorkflowDiagnostic,
    WorkflowFailed,
    WorkflowFailure,
    WorkflowHandoff,
    WorkflowIdempotentRepeat,
    WorkflowPolicySet,
    WorkflowRejected,
    WorkflowState,
    WorkflowTransitionRequest,
    apply_workflow_request,
    candidate_subject_sha256,
    canonical_encode,
    map_subject_sha256,
    priority_subject_sha256,
    request_fingerprint,
    source_set_subject_sha256,
)

# The inherited T003 vectors are durable v1 compatibility fixtures. New
# workflows are covered separately by the T053 v2 lifecycle tests.
WORKFLOW_STATE_VERSION = "course-workflow-state/v1"
WORKFLOW_TRANSITION_VERSION = "course-workflow-transition/v1"


ZERO = "0" * 64
HOSTILE_SENTINEL = "private-hostile-method-sentinel"


class HostileStr(str):
    def _raise(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError(HOSTILE_SENTINEL)

    __eq__ = _raise
    __ne__ = _raise
    __hash__ = _raise
    encode = _raise
    __str__ = _raise


class HostileInt(int):
    def _raise(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError(HOSTILE_SENTINEL)

    __lt__ = _raise
    __le__ = _raise
    __gt__ = _raise
    __ge__ = _raise
    __str__ = _raise


class HostileTuple(tuple):
    def _raise(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError(HOSTILE_SENTINEL)

    __iter__ = _raise
    __len__ = _raise


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def make_policy_set() -> WorkflowPolicySet:
    pairs = (
        ("source_assessment", "source-assessment-policy/v1"),
        ("priority_basis", "priority-basis-policy/v1"),
        ("lecture_mapping", "lecture-map-policy/v1"),
        ("lecture_production", "lecture-production-policy/v1"),
        ("lecture_validation", "lecture-validation-policy/v1"),
        ("workflow_handoff", "workflow-handoff-policy/v1"),
    )
    return WorkflowPolicySet(
        *(PolicyReference(kind, version, digest(kind)) for kind, version in pairs)
    )


def make_source(source_id: str = "source-1", marker: str = "source-one") -> SourceEvidenceReference:
    return SourceEvidenceReference(
        SOURCE_EVIDENCE_REFERENCE_VERSION, source_id, digest(marker)
    )


def make_artifact(
    artifact_id: str,
    kind: str,
    producer: str,
) -> WorkflowArtifactReference:
    return WorkflowArtifactReference(
        WORKFLOW_ARTIFACT_REFERENCE_VERSION,
        artifact_id,
        kind,  # type: ignore[arg-type]
        digest(artifact_id),
        producer,  # type: ignore[arg-type]
    )


def make_document(lecture_id: str = "l1", order: int = 1, source: str = "invented candidate") -> LectureDocument:
    return LectureDocument(
        contract_version="lecture-document/v1",
        document_id=lecture_id,
        order=order,
        source_text=source,
        provenance=SourceProvenance(content_sha256=digest(source)),
    )


def initialize_request(operation_id: str = "op-0") -> InitializeWorkflowRequest:
    return InitializeWorkflowRequest(
        WORKFLOW_TRANSITION_VERSION,
        "workflow-1",
        operation_id,
        InitializeWorkflow("initialize_workflow", make_policy_set(), (make_source(),)),
    )


def transition(state: WorkflowState, operation_id: str, payload: object) -> WorkflowTransitionRequest:
    return WorkflowTransitionRequest(
        WORKFLOW_TRANSITION_VERSION,
        state.workflow_id,
        state.revision,
        operation_id,
        payload,  # type: ignore[arg-type]
    )


class WorkflowContractShapeTests(unittest.TestCase):
    def test_core_record_fields_are_exact(self) -> None:
        expected = {
            SourceEvidenceReference: ["reference_version", "source_id", "content_sha256"],
            WorkflowArtifactReference: ["reference_version", "artifact_id", "artifact_kind", "content_sha256", "producer_version"],
            PolicyReference: ["policy_kind", "policy_version", "content_sha256"],
            WorkflowPolicySet: ["source_assessment", "priority_basis", "lecture_mapping", "lecture_production", "lecture_validation", "workflow_handoff"],
            SourceAssessmentRecord: ["source_set_sha256", "assessment_reference", "recorded_revision"],
            PriorityBasisRecord: ["primary_mode", "proposal_reference", "evidence_hierarchy_reference", "status", "approval"],
            LectureMapRecord: ["map_reference", "lecture_ids", "status", "approval", "reserved_lecture_ids"],
            MapReopenContext: ["baseline_map_reference", "baseline_lecture_ids", "baseline_reserved_lecture_ids", "baseline_map_subject_sha256", "baseline_approval", "created_revision"],
            PendingSourceRecord: ["source_reference", "detected_stage", "detected_revision", "blocker_subject_sha256"],
            ApprovalRecord: ["gate", "decision", "subject_sha256", "state_revision", "operation_id"],
            ValidationRecord: ["candidate_subject_sha256", "validation_policy_version", "validation_policy_sha256", "disposition", "validation_reference"],
            LectureProgress: ["lecture_id", "map_position", "status", "attempt", "candidate", "validation", "accepted_document"],
            HistoricalCandidateRecord: ["lecture_id", "map_position", "approved_map_subject_sha256", "attempt", "candidate", "validation", "historical_disposition"],
            WorkflowBlocker: ["code", "stage", "resume_disposition", "subject_id", "subject_sha256", "evidence_reference", "recorded_revision"],
            WorkflowFailure: ["code", "stage", "failed_action", "subject_id", "evidence_reference", "recorded_revision"],
            OperationReceipt: ["contract_version", "operation_id", "from_revision", "to_revision", "action", "request_sha256", "outcome", "subject_sha256"],
            WorkflowDiagnostic: ["code", "severity", "category", "stage", "subject_id"],
        }
        for record_type, names in expected.items():
            with self.subTest(record=record_type.__name__):
                self.assertEqual([item.name for item in fields(record_type)], names)

    def test_request_payload_and_result_fields_are_exact(self) -> None:
        expected = {
            InitializeWorkflow: ["action", "policies", "source_evidence"],
            RecordSourceAssessment: ["action", "assessment_reference", "primary_mode", "priority_proposal_reference", "evidence_hierarchy_reference"],
            ApprovePriorityBasis: ["action", "priority_subject_sha256"],
            RejectPriorityBasis: ["action", "priority_subject_sha256"],
            RecordLectureMap: ["action", "map_reference", "lecture_ids"],
            ApproveLectureMap: ["action", "map_subject_sha256"],
            RejectLectureMap: ["action", "map_subject_sha256"],
            SubmitLectureCandidate: ["action", "lecture_id", "document"],
            RecordCandidateValidation: ["action", "lecture_id", "candidate_subject_sha256", "validation"],
            RecordOperationFailure: ["action", "failed_action", "failure_code", "subject_id", "evidence_reference"],
            RetryFailed: ["action", "failure_code", "failure_evidence_sha256"],
            MarkBlocked: ["action", "blocker_code", "subject_id", "subject_sha256", "evidence_reference"],
            ClearBlocker: ["action", "blocker_code", "subject_sha256"],
            RecordNewSource: ["action", "source_reference", "evidence_reference"],
            DismissNewSource: ["action", "source_content_sha256"],
            ApproveMapReopen: ["action", "source_content_sha256"],
            InitializeWorkflowRequest: ["contract_version", "workflow_id", "operation_id", "payload"],
            WorkflowTransitionRequest: ["contract_version", "workflow_id", "expected_revision", "operation_id", "payload"],
            WorkflowAdvanced: ["status", "state", "receipt", "diagnostics"],
            WorkflowRejected: ["status", "state", "diagnostics"],
            WorkflowBlocked: ["status", "state", "receipt", "blocker"],
            WorkflowFailed: ["status", "state", "receipt", "failure"],
            WorkflowIdempotentRepeat: ["status", "state", "original_receipt"],
        }
        for record_type, names in expected.items():
            with self.subTest(record=record_type.__name__):
                self.assertEqual([item.name for item in fields(record_type)], names)

    def test_state_and_handoff_fields_are_exact(self) -> None:
        self.assertEqual(
            [item.name for item in fields(WorkflowState)],
            ["contract_version", "workflow_id", "revision", "stage", "disposition", "policies", "source_evidence", "reviewed_source_evidence", "source_assessment", "priority_basis", "lecture_map", "map_reopen_context", "active_lecture_id", "pending_source", "lecture_progress", "historical_candidates", "active_issue", "approval_history", "diagnostics", "operation_receipts"],
        )
        self.assertEqual(
            [item.name for item in fields(WorkflowHandoff)],
            ["contract_version", "workflow_id", "revision", "stage", "disposition", "source_count", "priority_mode", "priority_approved", "map_approved", "lecture_ids", "active_lecture_id", "accepted_documents", "pending_source_sha256", "active_issue_code", "diagnostic_codes", "next_actions"],
        )

    def test_records_are_frozen_slotted_and_candidate_repr_is_redacted(self) -> None:
        source = make_source()
        self.assertFalse(hasattr(source, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            source.source_id = "changed"  # type: ignore[misc]
        secret = "invented-private-sentinel"
        payload = SubmitLectureCandidate("submit_lecture_candidate", "l1", make_document(source=secret))
        self.assertNotIn(secret, repr(payload))
        self.assertNotIn("source_text", repr(payload))

    def test_constructor_failures_are_content_safe(self) -> None:
        secret = "private-caller-value-sentinel"
        attempts = (
            lambda: SourceEvidenceReference(SOURCE_EVIDENCE_REFERENCE_VERSION, secret.upper(), ZERO),
            lambda: PolicyReference(secret, secret, ZERO),  # type: ignore[arg-type]
            lambda: WorkflowDiagnostic("illegal_transition", secret, "validation", "source_assessment", None),  # type: ignore[arg-type]
        )
        for attempt in attempts:
            with self.subTest():
                with self.assertRaises(ValueError) as caught:
                    attempt()
                self.assertNotIn(secret, str(caught.exception))

    def test_hostile_scalar_and_container_subclasses_have_fixed_constructor_failures(self) -> None:
        attempts = (
            (
                lambda: SourceEvidenceReference(HostileStr(SOURCE_EVIDENCE_REFERENCE_VERSION), "source", ZERO),
                "source evidence reference version is unsupported",
            ),
            (
                lambda: InitializeWorkflowRequest(
                    HostileStr(WORKFLOW_TRANSITION_VERSION),
                    "workflow-1",
                    "op-0",
                    InitializeWorkflow("initialize_workflow", make_policy_set(), (make_source(),)),
                ),
                "transition contract version is unsupported",
            ),
            (
                lambda: ApprovalRecord("priority_basis", "approved", ZERO, HostileInt(1), "op-1"),
                "approval revision is invalid",
            ),
            (
                lambda: InitializeWorkflow("initialize_workflow", make_policy_set(), HostileTuple((make_source(),))),
                "source evidence tuple is invalid",
            ),
        )
        for attempt, message in attempts:
            with self.subTest(message=message):
                with self.assertRaises(ValueError) as caught:
                    attempt()
                self.assertEqual(str(caught.exception), message)
                self.assertNotIn(HOSTILE_SENTINEL, str(caught.exception))

    def test_pending_source_subject_requires_an_exact_matching_digest(self) -> None:
        source = make_source("source-2", "pending-subject")
        record = PendingSourceRecord(
            source,
            "lecture_production",
            1,
            source.content_sha256,
        )
        record.__post_init__()

        object.__setattr__(record, "blocker_subject_sha256", HostileStr(source.content_sha256))
        with self.assertRaises(ValueError) as corrupted:
            record.__post_init__()
        self.assertEqual(str(corrupted.exception), "pending source subject is invalid")
        self.assertNotIn(HOSTILE_SENTINEL, str(corrupted.exception))

        for value in (HostileStr(source.content_sha256), "not-a-digest"):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ValueError) as caught:
                    PendingSourceRecord(source, "lecture_production", 1, value)
                self.assertEqual(str(caught.exception), "pending source subject is invalid")
                self.assertNotIn(HOSTILE_SENTINEL, str(caught.exception))

    def test_initialization_uses_source_references_not_documents(self) -> None:
        with self.assertRaisesRegex(ValueError, "source evidence tuple"):
            InitializeWorkflow(
                "initialize_workflow",
                make_policy_set(),
                (make_document(),),  # type: ignore[arg-type]
            )


class RegistryAndReferenceTests(unittest.TestCase):
    def test_policy_members_have_exact_kind_version_pairs(self) -> None:
        policies = make_policy_set()
        self.assertEqual(policies.lecture_mapping.policy_version, "lecture-map-policy/v1")
        with self.assertRaisesRegex(ValueError, "kind and version"):
            PolicyReference("lecture_mapping", "lecture-production-policy/v1", ZERO)  # type: ignore[arg-type]

    def test_artifact_producer_pairs_fail_closed(self) -> None:
        make_artifact("map", "lecture_map", "lecture-map-policy/v1")
        with self.assertRaisesRegex(ValueError, "producer"):
            make_artifact("map", "lecture_map", "lecture-production-policy/v1")

    def test_safe_id_digest_and_integer_rules_are_ascii_exact(self) -> None:
        for value in (".", "..", "A", "bad/path", "a" * 65, "١"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    SourceEvidenceReference(SOURCE_EVIDENCE_REFERENCE_VERSION, value, ZERO)
        with self.assertRaises(ValueError):
            SourceEvidenceReference(SOURCE_EVIDENCE_REFERENCE_VERSION, "source", "A" * 64)
        with self.assertRaises(ValueError):
            RecordLectureMap(
                "record_lecture_map",
                make_artifact("map", "lecture_map", "lecture-map-policy/v1"),
                ("L1",),
            )

    def test_lecture_map_bounds_and_contiguous_identity(self) -> None:
        reference = make_artifact("map", "lecture_map", "lecture-map-policy/v1")
        RecordLectureMap("record_lecture_map", reference, tuple(f"l{i}" for i in range(1, 10000)))
        for invalid in ((), ("l01",), ("l2",), tuple(f"l{i}" for i in range(1, 10001))):
            with self.subTest(length=len(invalid)):
                with self.assertRaises(ValueError):
                    RecordLectureMap("record_lecture_map", reference, invalid)

    def test_every_diagnostic_code_has_one_canonical_classification(self) -> None:
        for code, (severity, category, stages) in DIAGNOSTIC_REGISTRY.items():
            stage = next(iter(stages))
            with self.subTest(code=code):
                diagnostic = WorkflowDiagnostic(code, severity, category, stage, None)  # type: ignore[arg-type]
                self.assertEqual(diagnostic.message, "Workflow condition recorded.")
                wrong = "warning" if severity == "error" else "error"
                with self.assertRaisesRegex(ValueError, "classification"):
                    WorkflowDiagnostic(code, wrong, category, stage, None)  # type: ignore[arg-type]

    def test_every_diagnostic_permitted_stage_is_enforced(self) -> None:
        for code, (severity, category, permitted_stages) in DIAGNOSTIC_REGISTRY.items():
            for stage in workflow_module.DIAGNOSTIC_STAGES:
                with self.subTest(code=code, stage=stage):
                    if stage in permitted_stages:
                        WorkflowDiagnostic(code, severity, category, stage, None)  # type: ignore[arg-type]
                    else:
                        with self.assertRaisesRegex(ValueError, "classification"):
                            WorkflowDiagnostic(code, severity, category, stage, None)  # type: ignore[arg-type]

    def test_every_artifact_kind_has_only_its_registered_producer(self) -> None:
        producers = tuple(dict.fromkeys(workflow_module.ARTIFACT_PRODUCERS.values()))
        for kind, expected_producer in workflow_module.ARTIFACT_PRODUCERS.items():
            for producer in producers:
                with self.subTest(kind=kind, producer=producer):
                    if producer == expected_producer:
                        make_artifact(f"artifact-{kind}", kind, producer)
                    else:
                        with self.assertRaisesRegex(ValueError, "producer"):
                            make_artifact(f"artifact-{kind}", kind, producer)

    def test_every_failure_stage_action_code_classification_is_exact(self) -> None:
        actions = tuple(item[0] for item in workflow_module.FAILURE_REGISTRY.values())
        codes = tuple(item[1] for item in workflow_module.FAILURE_REGISTRY.values())
        evidence = make_artifact("failure", "failure", WORKFLOW_STATE_VERSION)
        for stage in workflow_module.WORKFLOW_STAGES:
            for action in actions:
                for code in codes:
                    valid = workflow_module.FAILURE_REGISTRY.get(stage) == (action, code)
                    with self.subTest(stage=stage, action=action, code=code):
                        if valid:
                            WorkflowFailure(code, stage, action, None, evidence, 1)  # type: ignore[arg-type]
                        else:
                            with self.assertRaisesRegex(ValueError, "classification"):
                                WorkflowFailure(code, stage, action, None, evidence, 1)  # type: ignore[arg-type]

    def test_forged_exact_nested_records_fail_with_fixed_messages(self) -> None:
        forged_policy = object.__new__(PolicyReference)
        with self.assertRaises(ValueError) as policy_error:
            WorkflowPolicySet(forged_policy, forged_policy, forged_policy, forged_policy, forged_policy, forged_policy)
        self.assertEqual(str(policy_error.exception), "workflow policy set member is invalid")

        forged_artifact = object.__new__(WorkflowArtifactReference)
        with self.assertRaises(ValueError) as artifact_error:
            PriorityBasisRecord("exam_driven", forged_artifact, make_artifact("hierarchy", "evidence_hierarchy", "priority-basis-policy/v1"), "proposed", None)
        self.assertEqual(str(artifact_error.exception), "priority proposal reference is invalid")

        secret = "private-forged-sentinel"
        object.__setattr__(forged_policy, "policy_kind", secret)
        with self.assertRaises(ValueError) as content_safe:
            InitializeWorkflow("initialize_workflow", forged_policy, (make_source(),))  # type: ignore[arg-type]
        self.assertEqual(str(content_safe.exception), "initialize policies are invalid")
        self.assertNotIn(secret, str(content_safe.exception))


class CanonicalEncodingTests(unittest.TestCase):
    def test_fixed_scalar_tuple_and_subject_vectors(self) -> None:
        self.assertEqual(canonical_encode("a").hex(), "393a737472696e672f7631313a61")
        self.assertEqual(
            canonical_encode(("a", 1, None)).hex(),
            "383a7475706c652f7631313a3331343a393a737472696e672f7631313a6131363a31303a696e74656765722f7631313a31393a373a6e6f6e652f7631",
        )
        source = SourceEvidenceReference(SOURCE_EVIDENCE_REFERENCE_VERSION, "source-1", "1" * 64)
        self.assertEqual(source_set_subject_sha256((source,)), "476ee6e27d07bada91a07ec4d5ad0b9ef101a28938f9eb384673adc536c1c708")
        reference = DocumentReference("lecture-document/v1", "l1", 1, "2" * 64)
        self.assertEqual(candidate_subject_sha256(reference), "0eb50808a2c585f94cf38fecb4ed78b35abf5adc8501d3394f73c0db2ad4142d")

    def test_fixed_priority_map_and_initialization_vectors(self) -> None:
        priority_policy = PolicyReference("priority_basis", "priority-basis-policy/v1", ZERO)
        map_policy = PolicyReference("lecture_mapping", "lecture-map-policy/v1", ZERO)
        basis = PriorityBasisRecord(
            "exam_driven",
            make_artifact("proposal", "priority_proposal", "priority-basis-policy/v1"),
            make_artifact("hierarchy", "evidence_hierarchy", "priority-basis-policy/v1"),
            "proposed",
            None,
        )
        lecture_map = LectureMapRecord(
            make_artifact("map", "lecture_map", "lecture-map-policy/v1"),
            ("l1", "l2"),
            "proposed",
            None,
            (),
        )
        self.assertEqual(priority_subject_sha256(basis, priority_policy), "ca27099a8d7543dd1e676d18f202c8e87fb3bd7c0607999cee8b504a9cd53a9a")
        self.assertEqual(map_subject_sha256(lecture_map, map_policy), "9dc29679a28e569f2fb3b04de1188faa6683c568e4ea297724b81f483b061387")
        self.assertEqual(request_fingerprint(initialize_request()), "65569a8f73e7e8c6f587160238e4982ff24509c162fbd222c09d2b2ac6f0fe7b")

    def test_candidate_subject_prevents_equal_markdown_identity_collision(self) -> None:
        source = "same invented markdown"
        first = DocumentReference("lecture-document/v1", "l1", 1, digest(source))
        second = DocumentReference("lecture-document/v1", "l2", 2, digest(source))
        self.assertEqual(first.content_sha256, second.content_sha256)
        self.assertNotEqual(candidate_subject_sha256(first), candidate_subject_sha256(second))
        variants = (
            DocumentReference("lecture-document/v1", "l2", 1, first.content_sha256),
            DocumentReference("lecture-document/v1", "l1", 2, first.content_sha256),
            DocumentReference("lecture-document/v1", "l1", 1, "3" * 64),
        )
        for variant in variants:
            self.assertNotEqual(candidate_subject_sha256(first), candidate_subject_sha256(variant))

    def test_candidate_subject_encoding_binds_contract_version_field(self) -> None:
        valid = DocumentReference("lecture-document/v1", "l1", 1, "2" * 64)
        forged = object.__new__(DocumentReference)
        object.__setattr__(forged, "contract_version", "lecture-document/v2")
        object.__setattr__(forged, "document_id", "l1")
        object.__setattr__(forged, "order", 1)
        object.__setattr__(forged, "content_sha256", "2" * 64)
        valid_bytes = workflow_module._record("candidate-subject/v1", (("candidate_reference", valid),))
        changed_bytes = valid_bytes.replace(b"lecture-document/v1", b"lecture-document/v2")
        self.assertNotEqual(hashlib.sha256(valid_bytes).digest(), hashlib.sha256(changed_bytes).digest())
        with self.assertRaisesRegex(ValueError, "candidate subject reference"):
            candidate_subject_sha256(forged)

    def test_framing_is_structurally_unambiguous(self) -> None:
        self.assertNotEqual(canonical_encode(("ab", "c")), canonical_encode(("a", "bc")))
        self.assertNotEqual(canonical_encode(("a", ("b", "c"))), canonical_encode((("a", "b"), "c")))

    def test_candidate_request_fingerprint_contains_no_source_text(self) -> None:
        initial = apply_workflow_request(None, initialize_request())
        assert isinstance(initial, WorkflowAdvanced)
        secret = "invented-source-text-sentinel"
        payload = SubmitLectureCandidate("submit_lecture_candidate", "l1", make_document(source=secret))
        request = transition(initial.state, "op-candidate", payload)
        fingerprint = request_fingerprint(request)
        self.assertEqual(len(fingerprint), 64)
        self.assertNotIn(secret, repr(request))

    def test_canonical_subject_and_fingerprint_boundaries_revalidate_recursively(self) -> None:
        secret = "private-corruption-sentinel"

        source = make_source()
        object.__setattr__(source, "content_sha256", secret)
        for operation in (
            lambda: canonical_encode(source),
            lambda: source_set_subject_sha256((source,)),
        ):
            with self.subTest(boundary="source"):
                with self.assertRaises(ValueError) as caught:
                    operation()
                self.assertNotIn(secret, str(caught.exception))

        basis = PriorityBasisRecord(
            "exam_driven",
            make_artifact("proposal", "priority_proposal", "priority-basis-policy/v1"),
            make_artifact("hierarchy", "evidence_hierarchy", "priority-basis-policy/v1"),
            "proposed",
            None,
        )
        object.__setattr__(basis.proposal_reference, "content_sha256", secret)
        with self.assertRaises(ValueError) as priority_error:
            priority_subject_sha256(basis, PolicyReference("priority_basis", "priority-basis-policy/v1", ZERO))
        self.assertEqual(str(priority_error.exception), "priority subject input is invalid")

        lecture_map = LectureMapRecord(
            make_artifact("map", "lecture_map", "lecture-map-policy/v1"),
            ("l1",),
            "proposed",
            None,
            (),
        )
        object.__setattr__(lecture_map.map_reference, "content_sha256", secret)
        with self.assertRaises(ValueError) as map_error:
            map_subject_sha256(lecture_map, PolicyReference("lecture_mapping", "lecture-map-policy/v1", ZERO))
        self.assertEqual(str(map_error.exception), "map subject input is invalid")

        reference = DocumentReference("lecture-document/v1", "l1", 1, ZERO)
        object.__setattr__(reference, "content_sha256", secret)
        with self.assertRaises(ValueError) as candidate_error:
            candidate_subject_sha256(reference)
        self.assertEqual(str(candidate_error.exception), "candidate subject reference is invalid")

        request = initialize_request()
        object.__setattr__(request.payload.policies.workflow_handoff, "content_sha256", secret)
        with self.assertRaises(ValueError) as request_error:
            request_fingerprint(request)
        self.assertEqual(str(request_error.exception), "request is invalid")
        self.assertNotIn(secret, str(request_error.exception))

    def test_hostile_scalar_container_and_nested_values_never_receive_digests(self) -> None:
        for hostile in (HostileStr("a"), HostileInt(1), HostileTuple(("a",))):
            with self.subTest(value_type=type(hostile).__name__):
                with self.assertRaises(ValueError) as caught:
                    canonical_encode(hostile)
                self.assertEqual(str(caught.exception), "canonical value type is unsupported")
                self.assertNotIn(HOSTILE_SENTINEL, str(caught.exception))

        source = make_source()
        object.__setattr__(source, "content_sha256", HostileStr(source.content_sha256))
        with self.assertRaises(ValueError) as source_error:
            source_set_subject_sha256((source,))
        self.assertEqual(str(source_error.exception), "source evidence tuple is invalid")

        basis = PriorityBasisRecord(
            "exam_driven",
            make_artifact("hostile-proposal", "priority_proposal", "priority-basis-policy/v1"),
            make_artifact("hostile-hierarchy", "evidence_hierarchy", "priority-basis-policy/v1"),
            "proposed",
            None,
        )
        object.__setattr__(basis, "primary_mode", HostileStr("exam_driven"))
        with self.assertRaises(ValueError) as priority_error:
            priority_subject_sha256(basis, PolicyReference("priority_basis", "priority-basis-policy/v1", ZERO))
        self.assertEqual(str(priority_error.exception), "priority subject input is invalid")

        lecture_map = LectureMapRecord(
            make_artifact("hostile-map", "lecture_map", "lecture-map-policy/v1"),
            ("l1",),
            "proposed",
            None,
            (),
        )
        object.__setattr__(lecture_map, "lecture_ids", HostileTuple(("l1",)))
        with self.assertRaises(ValueError) as map_error:
            map_subject_sha256(lecture_map, PolicyReference("lecture_mapping", "lecture-map-policy/v1", ZERO))
        self.assertEqual(str(map_error.exception), "map subject input is invalid")

        reference = DocumentReference("lecture-document/v1", "l1", 1, ZERO)
        object.__setattr__(reference, "content_sha256", HostileStr(ZERO))
        with self.assertRaises(ValueError) as candidate_error:
            candidate_subject_sha256(reference)
        self.assertEqual(str(candidate_error.exception), "candidate subject reference is invalid")

        request = initialize_request()
        object.__setattr__(
            request.payload.source_evidence[0],
            "content_sha256",
            HostileStr(request.payload.source_evidence[0].content_sha256),
        )
        with self.assertRaises(ValueError) as request_error:
            request_fingerprint(request)
        self.assertEqual(str(request_error.exception), "request is invalid")

        for caught in (source_error, priority_error, map_error, candidate_error, request_error):
            self.assertNotIn(HOSTILE_SENTINEL, str(caught.exception))


class ProductionBoundaryTests(unittest.TestCase):
    def test_workflow_modules_have_no_prohibited_imports(self) -> None:
        forbidden = {"legacy", "os", "pathlib", "shutil", "subprocess", "tempfile", "socket", "urllib", "requests", "json", "pickle"}
        for relative in ("course_compiler/workflow.py", "course_compiler/workflow_policy.py"):
            tree = ast.parse(Path(relative).read_text(encoding="utf-8"), filename=relative)
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            self.assertFalse(forbidden & imported, relative)

    def test_production_does_not_reference_local_prompt_or_private_content(self) -> None:
        for relative in ("course_compiler/workflow.py", "course_compiler/workflow_policy.py"):
            source = Path(relative).read_text(encoding="utf-8")
            self.assertNotIn("lecture-prompt-v2.txt", source)
            self.assertNotIn("legacy.convert_lectures", source)


if __name__ == "__main__":
    unittest.main()
