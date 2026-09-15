"""T051 EvidenceAccess boundary tests: kind-specific manifests, digest-bound
reads, cross-request rejection. Invented synthetic bytes only."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from course_compiler.course_job_persistence import open_course_job_store
from course_compiler.course_operations import attach_source, create_course
from course_compiler.course_persistence import open_course_store
from course_compiler.course_workflow import COURSE_WORKFLOW_ASSOCIATION_VERSION, CourseWorkflowAssociation
from course_compiler.course_workflow_operations import reopen_course_workflow_continuation, reopen_course_workflow_context
from course_compiler.course_workflow_persistence import open_course_workflow_association_store
from course_compiler.evidence_access import (
    EvidenceAccessFailure,
    EvidenceItem,
    read_evidence_item,
    resolve_request_evidence,
)
from course_compiler.lecture_document_persistence import open_lecture_document_store
from course_compiler.policy_persistence import open_policy_content_store
from course_compiler.semantic_operations import request_semantic_work, start_generation, submit_owner_priority_decision, submit_semantic_result
from course_compiler.semantic_work import ScriptProvider
from course_compiler.semantic_work_persistence import open_semantic_work_store
from course_compiler.source_persistence import open_source_evidence_store
from course_compiler.workflow_artifact_persistence import open_workflow_artifact_store
from course_compiler.workflow_persistence import open_workflow_state_store

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_ISOLATED_BASE = REPOSITORY_ROOT / "local-data" / "test-tmp-evidence-access"
_ISOLATED_BASE.mkdir(parents=True, exist_ok=True)

_HOLDER = "holder0000000001"


class _Fixture:
    """Opens every store and drives the accepted T049 protocol to one exact stage."""

    def __init__(self, tmp: str) -> None:
        root = Path(tmp)
        self.course_store = open_course_store(root / "courses.sqlite3")
        self.assoc_store = open_course_workflow_association_store(root / "assoc.sqlite3")
        self.source_store = open_source_evidence_store(root / "sources.sqlite3")
        self.ws_store = open_workflow_state_store(root / "ws.sqlite3")
        self.job_store = open_course_job_store(root / "jobs.sqlite3")
        self.sem_store = open_semantic_work_store(root / "sem.sqlite3")
        self.policy_store = open_policy_content_store(root / "policy.sqlite3")
        self.artifact_store = open_workflow_artifact_store(root / "artifacts.sqlite3")
        self.doc_store = open_lecture_document_store(root / "docs.sqlite3")
        self.provider = ScriptProvider()

        ref = create_course(
            "Invented Evidence Course", ai_mode="gpt", quality_mode="fast",
            course_store=self.course_store, association_store=self.assoc_store,
        )
        self.course_id = ref.course_id
        self.src_a = attach_source(
            self.course_id, b"%PDF-1.4 invented A\n%%EOF", "srca",
            course_store=self.course_store, source_store=self.source_store,
        )
        self.src_b = attach_source(
            self.course_id, b"%PDF-1.4 invented B, longer bytes for size checks\n%%EOF", "srcb",
            course_store=self.course_store, source_store=self.source_store,
        )
        self.job_id = start_generation(
            self.course_id, course_store=self.course_store, job_store=self.job_store,
            workflow_store=self.ws_store, association_store=self.assoc_store,
            source_store=self.source_store, policy_store=self.policy_store,
            artifact_store=self.artifact_store, document_store=self.doc_store,
        )

    def close(self) -> None:
        for store in (
            self.course_store, self.assoc_store, self.source_store, self.ws_store,
            self.job_store, self.sem_store, self.policy_store, self.artifact_store, self.doc_store,
        ):
            try:
                store.close()
            except Exception:
                pass

    def request(self):
        return request_semantic_work(
            self.job_id, job_store=self.job_store, workflow_store=self.ws_store,
            semantic_store=self.sem_store, course_store=self.course_store, holder_id=_HOLDER,
        )

    def submit(self, req):
        result = self.provider.execute(req)
        outcome = submit_semantic_result(
            self.job_id, result, job_store=self.job_store, workflow_store=self.ws_store,
            semantic_store=self.sem_store, association_store=self.assoc_store,
            source_store=self.source_store, policy_store=self.policy_store,
            artifact_store=self.artifact_store, document_store=self.doc_store, holder_id=_HOLDER,
        )
        return result, outcome

    def approve_priority(self, subject_sha256: str):
        return submit_owner_priority_decision(
            self.job_id, approve=True, subject_sha256=subject_sha256,
            job_store=self.job_store, workflow_store=self.ws_store, semantic_store=self.sem_store,
            association_store=self.assoc_store, source_store=self.source_store,
            policy_store=self.policy_store, artifact_store=self.artifact_store, document_store=self.doc_store,
        )

    def continuation(self):
        job = self.job_store.load(self.job_id)
        association = CourseWorkflowAssociation(COURSE_WORKFLOW_ASSOCIATION_VERSION, job.course_reference, job.workflow_id)
        context = reopen_course_workflow_context(
            association, association_store=self.assoc_store, workflow_store=self.ws_store, policy_store=self.policy_store
        )
        return reopen_course_workflow_continuation(context, artifact_store=self.artifact_store, document_store=self.doc_store)


class EvidenceAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=str(_ISOLATED_BASE), prefix="ea-")
        self.fx = _Fixture(self._tmp.name)
        self.addCleanup(self.fx.close)
        self.addCleanup(self._tmp.cleanup)

    def test_source_assessment_evidence_is_exact_source_pdfs(self) -> None:
        req = self.fx.request()
        continuation = self.fx.continuation()
        items = resolve_request_evidence(req.kind, continuation, source_store=self.fx.source_store)
        self.assertIsInstance(items, tuple)
        self.assertEqual({i.evidence_kind for i in items}, {"source_pdf"})
        self.assertEqual({i.evidence_id for i in items}, {"src-srca", "src-srcb"})
        for item in items:
            self.assertEqual(item.media_type, "application/pdf")
            self.assertTrue(item.byte_length > 0)

    def test_evidence_bytes_round_trip_and_match_digest(self) -> None:
        req = self.fx.request()
        continuation = self.fx.continuation()
        items = resolve_request_evidence(req.kind, continuation, source_store=self.fx.source_store)
        for item in items:
            content, media_type = read_evidence_item(
                item.evidence_id, req.kind, continuation, source_store=self.fx.source_store
            )
            self.assertEqual(media_type, item.media_type)
            self.assertEqual(hashlib.sha256(content).hexdigest(), item.content_sha256)
            self.assertEqual(len(content), item.byte_length)

    def test_unknown_evidence_id_is_rejected(self) -> None:
        req = self.fx.request()
        continuation = self.fx.continuation()
        result = read_evidence_item("src-does-not-exist", req.kind, continuation, source_store=self.fx.source_store)
        self.assertIsInstance(result, EvidenceAccessFailure)
        self.assertEqual(result.diagnostics[0].code, "evidence_not_found")

    def test_map_generation_retains_source_assessment_evidence(self) -> None:
        # A fresh mapper needs the exact source assessment that produced the
        # approved basis; its digest-bound evidence must remain readable after
        # the priority decision advances the workflow.
        req1 = self.fx.request()
        self.fx.submit(req1)
        continuation_after_sa = self.fx.continuation()
        sa_items = resolve_request_evidence("exam_priority_assessment", continuation_after_sa, source_store=self.fx.source_store)
        sa_evidence_id = next(i.evidence_id for i in sa_items if i.label == "source_assessment")

        req2 = self.fx.request()
        result2, _ = self.fx.submit(req2)
        self.fx.approve_priority(result2.priority_subject_sha256)
        req3 = self.fx.request()
        self.assertEqual(req3.kind, "lecture_map_generation")
        continuation_map = self.fx.continuation()

        content, media_type = read_evidence_item(
            sa_evidence_id, req3.kind, continuation_map, source_store=self.fx.source_store
        )
        self.assertEqual(media_type, "text/plain; charset=utf-8")
        self.assertEqual(hashlib.sha256(content).hexdigest(), next(
            item.content_sha256 for item in resolve_request_evidence(
                req3.kind, continuation_map, source_store=self.fx.source_store
            ) if item.evidence_id == sa_evidence_id
        ))

    def test_map_generation_allows_no_optional_priority_review(self) -> None:
        # Read-only T030 continuations predate the relay-owned review
        # projection, so the optional field must not make those durable
        # workflows invalid or fabricate review evidence.
        req1 = self.fx.request()
        self.fx.submit(req1)
        req2 = self.fx.request()
        result2, _ = self.fx.submit(req2)
        self.fx.approve_priority(result2.priority_subject_sha256)
        req3 = self.fx.request()
        continuation = self.fx.continuation()

        self.assertEqual(req3.kind, "lecture_map_generation")
        labels = {item.label for item in resolve_request_evidence(
            req3.kind, continuation, source_store=self.fx.source_store
        )}
        self.assertNotIn("priority_evidence_review", labels)

    def test_priority_review_projection_requires_priority_proposal_artifact(self) -> None:
        # The semantic-review protocol reuses the priority-proposal artifact
        # kind.  A differently typed workflow artifact must never be
        # relabelled as review evidence in a map handoff.
        req1 = self.fx.request()
        self.fx.submit(req1)
        req2 = self.fx.request()
        result2, _ = self.fx.submit(req2)
        self.fx.approve_priority(result2.priority_subject_sha256)
        continuation = self.fx.continuation()

        with self.assertRaises(ValueError):
            replace(continuation, priority_evidence_review=continuation.source_assessment)

    def test_unsupported_kind_fails_closed(self) -> None:
        continuation = self.fx.continuation()
        result = resolve_request_evidence("not_a_real_kind", continuation, source_store=self.fx.source_store)
        self.assertIsInstance(result, EvidenceAccessFailure)
        self.assertEqual(result.diagnostics[0].code, "unsupported_kind")

    def test_exam_priority_assessment_evidence_includes_prior_artifacts_and_sources(self) -> None:
        req1 = self.fx.request()
        self.fx.submit(req1)
        req2 = self.fx.request()
        self.assertEqual(req2.kind, "exam_priority_assessment")
        continuation = self.fx.continuation()
        items = resolve_request_evidence(req2.kind, continuation, source_store=self.fx.source_store)
        kinds = {i.evidence_kind for i in items}
        self.assertEqual(kinds, {"artifact_text", "source_pdf"})
        labels = {i.label for i in items}
        self.assertTrue({"source_assessment", "priority_proposal", "evidence_hierarchy"}.issubset(labels))
        for item in items:
            content, _ = read_evidence_item(item.evidence_id, req2.kind, continuation, source_store=self.fx.source_store)
            self.assertEqual(hashlib.sha256(content).hexdigest(), item.content_sha256)

    def test_lecture_generation_evidence_names_map_and_priority_not_full_course(self) -> None:
        req1 = self.fx.request()
        self.fx.submit(req1)
        req2 = self.fx.request()
        result2, _ = self.fx.submit(req2)
        self.fx.approve_priority(result2.priority_subject_sha256)
        req3 = self.fx.request()
        self.assertEqual(req3.kind, "lecture_map_generation")
        result3, _ = self.fx.submit(req3)
        from course_compiler.semantic_operations import submit_owner_map_decision

        submit_owner_map_decision(
            self.fx.job_id, approve=True, subject_sha256=result3.map_subject_sha256,
            job_store=self.fx.job_store, workflow_store=self.fx.ws_store, semantic_store=self.fx.sem_store,
            association_store=self.fx.assoc_store, source_store=self.fx.source_store,
            policy_store=self.fx.policy_store, artifact_store=self.fx.artifact_store, document_store=self.fx.doc_store,
        )
        req4 = self.fx.request()
        self.assertEqual(req4.kind, "lecture_generation")
        continuation = self.fx.continuation()
        items = resolve_request_evidence(req4.kind, continuation, source_store=self.fx.source_store)
        labels = {i.label for i in items}
        self.assertIn("lecture_map", labels)
        # T051 deliberately never ships prior accepted lectures merely
        # because they exist: none are accepted yet, and none are named.
        self.assertFalse(any(i.evidence_kind == "document_text" for i in items))


class EvidenceItemValidationTests(unittest.TestCase):
    def test_valid_item_constructs(self) -> None:
        item = EvidenceItem(
            evidence_id="src-abc",
            evidence_kind="source_pdf",
            media_type="application/pdf",
            content_sha256="0" * 64,
            byte_length=10,
            label="source-1",
        )
        self.assertEqual(item.evidence_id, "src-abc")

    def test_mismatched_media_type_rejected(self) -> None:
        with self.assertRaises(ValueError):
            EvidenceItem(
                evidence_id="src-abc",
                evidence_kind="source_pdf",
                media_type="text/plain; charset=utf-8",
                content_sha256="0" * 64,
                byte_length=10,
                label="source-1",
            )

    def test_invalid_digest_rejected(self) -> None:
        with self.assertRaises(ValueError):
            EvidenceItem(
                evidence_id="src-abc",
                evidence_kind="source_pdf",
                media_type="application/pdf",
                content_sha256="not-hex",
                byte_length=10,
                label="source-1",
            )

    def test_zero_byte_length_rejected(self) -> None:
        with self.assertRaises(ValueError):
            EvidenceItem(
                evidence_id="src-abc",
                evidence_kind="source_pdf",
                media_type="application/pdf",
                content_sha256="0" * 64,
                byte_length=0,
                label="source-1",
            )


if __name__ == "__main__":
    unittest.main()
