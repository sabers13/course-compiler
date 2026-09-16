"""Invented mathematical course through the actual HTTP semantic-text boundary."""
from __future__ import annotations
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from course_compiler.app.config import create_config
from course_compiler.app.server import create_server
from tests.toolchain_support import (
    POPPLER_AVAILABLE,
    POPPLER_MISSING_REASON,
    TEX_MISSING_REASON,
    TEX_TOOLCHAIN_AVAILABLE,
)

ROOT = Path(__file__).resolve().parents[1]
GUIDANCE = 'Prioritize parameter interpretation. Retain full coverage and solve exercises.\nUse explicit notation.'
PLAN = '''# Lecture plan
## l1 — Parameter estimation
Scope: Location, scale and a standardized observation.
Priority: HIGH
Priority reason: The invented exercise requires distinguishing location and scale.
Sources: src-synthetic, page 1.
Topics: HIGH: location and scale; MEDIUM: squared deviation; LOW PRIORITY / SKIM: notation variants.
Prerequisites: Arithmetic and squaring.
Coverage: Interpret theta, mu and sigma; derive a standardized value; solve the supplied exercise and show a source diagram.

## l2 — Revision and recognition
Scope: Recognize a standardization problem and check its assumptions.
Priority: MEDIUM
Priority reason: Reinforces the reusable problem form beyond one exercise.
Sources: src-synthetic, page 1.
Topics: HIGH: recognition recipe; MEDIUM: assumptions; LOW PRIORITY / SKIM: alternate labels.
Prerequisites: l1.
Coverage: Explain positive scale, distinguish parameter from data, and provide an exam checklist.
'''
LECTURE = r'''# Parameter estimation
Overall priority: HIGH. The source exercise tests location and scale.

## Parameters and interpretation
Section priority: HIGH. Source: src-synthetic, page 1.

The location is \(\theta=\mu\), the scale is \(\sigma>0\), and the error allowance is \(\alpha\).
Prose adjacent to math stays readable: before \(x_i^2\), after.
A standardized observation is \(z_i=\frac{x_i-\mu}{\sigma}\).

\[
z_i^2 = \frac{(x_i-\mu)^2}{\sigma^2}
\]

Interpretation: subtract the location, then express the distance in scale units.

## Reusable workflow and source diagram
Section priority: HIGH. Recognize this method when a problem asks for a distance in standard units.

\[
\boxed{\text{Identify the observation and the specified location parameter} \rightarrow \text{Subtract the location from the observation} \rightarrow \text{Divide by the strictly positive scale parameter} \rightarrow \text{Interpret the signed result in scale units}}
\]

The following invented source diagram locates a value on a number line.

[[SOURCE_SNIP source="src-synthetic" page=1 purpose="Location and one scale unit on the source number line"]]

## Worked example and solved exercise
Section priority: HIGH. Source: src-synthetic, page 1. Model-derived solution; no official answer was supplied.

For \(x_1=7\), \(\mu=5\), \(\sigma=2\), subtract to get \(7-5=2\), then divide: \(z_1=2/2=1\).
The observation lies one scale unit above the location. The squared standardized distance is \(z_1^2=1\).

## Common mistakes and edge cases
Do not divide before subtracting. A scale of zero makes this expression undefined.
The sign distinguishes above from below; squaring removes this directional information.

## What to remember for the exam
Identify observation, location and positive scale. Subtract, divide and interpret.

## One-paragraph revision version
Standardization expresses a deviation in scale units. State the parameter roles,
check the positive-scale condition, subtract location and divide by scale. Preserve
the sign until a problem explicitly asks for a squared distance.
'''


def synthetic_pdf():
    """Invented vector/text PDF; uses only stdlib, like extraction fixtures."""
    content = (
        b"BT /F1 17 Tf 30 220 Td (Invented source: location and scale) Tj ET\n"
        b"0.1 0.3 0.6 RG 2 w 65 130 m 365 130 l S\n"
        b"135 124 m 135 136 l S 285 124 m 285 136 l S\n"
        b"BT /F1 12 Tf 115 107 Td (mu = 5) Tj ET\n"
        b"BT /F1 12 Tf 265 107 Td (x = 7) Tj ET\n"
        b"BT /F1 12 Tf 155 152 Td (sigma = 2) Tj ET\n"
        b"BT /F1 11 Tf 30 58 Td (Exercise: express x = 7 in scale units about mu = 5.) Tj ET\n"
    )
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 440 260] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode('ascii') + b" >>\nstream\n" + content + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    )
    output = bytearray(b"%PDF-1.4\n% Invented public mathematical fixture\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode('ascii') + body + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects)+1}\n".encode('ascii') + b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode('ascii'))
    output.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode('ascii'))
    return bytes(output)


class ProductParityTests(unittest.TestCase):
    def setUp(self):
        base = ROOT/'local-artifacts'/'test-tmp-parity'
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=base)
        self.start()

    def start(self):
        self.server = create_server(create_config(host='127.0.0.1', port=0, data_root=Path(self.tmp.name)))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=3)

    def tearDown(self):
        self.stop(); self.tmp.cleanup()

    def call(self, method, path, data=None, expected=200):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=150)
        conn.request(method, path, json.dumps(data or {}) if method == 'POST' else None,
                     {'Content-Type':'application/json'} if method == 'POST' else {})
        response = conn.getresponse(); body = response.read(); conn.close()
        self.assertEqual(response.status, expected, body[:800])
        return body if body.startswith(b'%PDF') else json.loads(body)

    def acquire(self):
        self.request = self.call('POST', f'/api/jobs/{self.job}/semantic-request', {'holder_id':'synthetic-browser'})['request']
        handoff = self.call('GET', self.route+'/pending_work')['handoff']
        self.assertIn(GUIDANCE, handoff['prompt'])
        for token in ('request_id', 'operation_id', 'holder_id', 'subject_sha256', 'request_revision', 'SemanticWorkResult'):
            self.assertNotIn(token, handoff['prompt'])
        return handoff

    def submit(self, text, expected=200):
        return self.call('POST', self.route+'/result', {'request_id':self.request['request_id'], 'holder_id':'synthetic-browser', 'text':text}, expected)

    def evidence_bytes(self, handoff, label):
        item = next(item for item in handoff['evidence_manifest'] if item['label'] == label)
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=150)
        conn.request('GET', self.route+'/pending_work/'+handoff['request_id']+'/evidence/'+item['evidence_id'])
        response = conn.getresponse(); body = response.read(); conn.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(len(body), item['byte_length'])
        self.assertEqual(hashlib.sha256(body).hexdigest(), item['content_sha256'])
        return body

    def approve(self, kind):
        gate = self.call('GET', f'/api/jobs/{self.job}')['owner_approval']
        self.assertEqual(gate['kind'], kind)
        self.assertTrue(gate['text'])
        return self.call('POST', f'/api/jobs/{self.job}/owner/{kind}', {'approve':True,'subject_sha256':gate['subject_sha256']})

    @unittest.skipUnless(
        TEX_TOOLCHAIN_AVAILABLE,
        f"real XeLaTeX build unavailable ({TEX_MISSING_REASON})",
    )
    @unittest.skipUnless(
        POPPLER_AVAILABLE,
        f"real Poppler snippet rendering unavailable ({POPPLER_MISSING_REASON})",
    )
    def test_representative_product_e2e(self):
        course = self.call('POST','/api/courses', {'title':'Invented mathematical course','ai_mode':'gpt','quality_mode':'fast','course_guidance':''},201)['course']
        cid = course['course_id']
        self.assertEqual(course['course_guidance'], '')
        course = self.call('POST',f'/api/courses/{cid}/guidance', {'course_guidance':GUIDANCE,'expected_revision':course['metadata_revision']})['course']
        self.assertEqual(course['course_guidance'], GUIDANCE)
        self.call('POST',f'/api/courses/{cid}/sources', {'source_id':'src-synthetic','content_base64':base64.b64encode(synthetic_pdf()).decode()},201)
        self.job = self.call('POST',f'/api/courses/{cid}/start-generation',expected=201)['job_id']
        self.route = f'/api/courses/{cid}/jobs/{self.job}'
        current = next(c for c in self.call('GET','/api/courses')['courses'] if c['course_id']==cid)
        self.call('POST',f'/api/courses/{cid}/guidance', {'course_guidance':'Changed guidance','expected_revision':current['metadata_revision']},409)
        assessment = '# Source assessment\nOne invented official note and exercise.\n# Priority proposal\nEmphasize interpretation while retaining the source exercise.\n# Evidence hierarchy\nThe note supplies scope; no exam or official solution is supplied.'
        self.acquire()
        self.submit(assessment)
        self.acquire()
        self.assertIsNone(self.call('GET',f'/api/jobs/{self.job}')['owner_approval'])
        review = 'The proposed priorities match the source. No exam evidence establishes exclusions. No conflicts found.'
        self.submit(review)
        self.assertEqual(self.submit(review)['status'], 'idempotent_repeat')
        self.submit(review+' A conflicting edit.',409)
        self.assertIn(review, self.call('GET',f'/api/jobs/{self.job}')['owner_approval']['text'])
        self.approve('priority')
        map_handoff = self.acquire()
        labels = [item['label'] for item in map_handoff['evidence_manifest']]
        self.assertEqual(labels.count('source_assessment'), 1)
        self.assertEqual(labels.count('priority_proposal'), 1)
        self.assertEqual(labels.count('evidence_hierarchy'), 1)
        self.assertEqual(labels.count('priority_evidence_review'), 1)
        self.assertEqual(len(map_handoff['evidence_manifest']), 5)
        self.assertEqual(len({item['evidence_id'] for item in map_handoff['evidence_manifest']}), 5)
        self.assertEqual(sum(item['evidence_kind'] == 'source_pdf' for item in map_handoff['evidence_manifest']), 1)
        self.assertNotIn('/home/', map_handoff['prompt'])
        self.assertNotIn('SemanticWorkResult', map_handoff['prompt'])
        self.assertNotIn('approve', review.lower())
        self.assertIn('owner-approved priority basis', map_handoff['prompt'])
        self.assertIn('accepted semantic review of the approved priority basis', map_handoff['prompt'])
        self.assertIn('Incorporate its supported findings', map_handoff['prompt'])
        self.assertIn('does not grant owner approval', map_handoff['prompt'])
        self.assertIn('Return the plan only.', map_handoff['prompt'])
        self.assertEqual(self.evidence_bytes(map_handoff, 'source_assessment'), b'One invented official note and exercise.')
        self.assertEqual(self.evidence_bytes(map_handoff, 'priority_proposal'), b'Emphasize interpretation while retaining the source exercise.')
        self.assertEqual(self.evidence_bytes(map_handoff, 'evidence_hierarchy'), b'The note supplies scope; no exam or official solution is supplied.')
        self.assertEqual(self.evidence_bytes(map_handoff, 'priority_evidence_review'), review.encode())
        self.submit(PLAN)
        gate = self.call('GET',f'/api/jobs/{self.job}')['owner_approval']
        self.assertEqual(gate['text'],PLAN)
        # Owner may reject/edit/regenerate; prior subject cannot approve new text.
        self.call('POST',f'/api/jobs/{self.job}/owner/map', {'approve':False,'subject_sha256':gate['subject_sha256']})
        self.acquire(); self.submit(PLAN.replace('Arithmetic and squaring.', 'Arithmetic, division and squaring.'))
        self.call('POST',f'/api/jobs/{self.job}/owner/map', {'approve':True,'subject_sha256':gate['subject_sha256']},400)
        self.approve('map')
        self.stop(); self.start()
        handoff = self.acquire()
        self.assertIn('Arithmetic, division and squaring.',handoff['prompt'])
        self.assertIn('Coverage: Interpret theta',handoff['prompt'])
        self.submit(LECTURE.replace('page=1','page=99'),400)
        self.submit(LECTURE)
        self.assertEqual(self.submit(LECTURE)["status"], "idempotent_repeat")
        self.stop(); self.start()
        self.acquire()
        self.submit(r'''# Revision and recognition
Overall priority: MEDIUM. Reinforce the reusable problem form.

## Recognition recipe
Section priority: HIGH. Source: src-synthetic, page 1.
Use \(z=(x-\mu)/\sigma\) for a distance in positive scale units.

## Worked example
Model-derived solution: \((7-5)/2=1\). This is one unit above the location.

## Common mistakes
Do not confuse data with parameters; never use a zero scale.

## What to remember for the exam
Subtract, divide and interpret the sign.

## One-paragraph revision version
Recognize the requested units, identify data and parameters, check positive scale,
and follow the standardization recipe while retaining source notation.
''')
        built = self.call('POST',f'/api/jobs/{self.job}/build')
        self.assertEqual(built['status'],'succeeded')
        pdf = self.call('GET',f'/api/jobs/{self.job}/artifact')
        self.assertGreater(len(pdf),10000)
        # Discard only this test's memoized PDFs; durable inputs must rebuild.
        for cached in Path(self.tmp.name).rglob('*.pdf'):
            cached.unlink()
        self.stop(); self.start()
        again = self.call('GET',f'/api/jobs/{self.job}/artifact')
        self.assertEqual(pdf,again)
        if os.environ.get('RELAY_EXPORT_SYNTHETIC') == '1':
            out = ROOT/'local-artifacts'/'product-parity'
            out.mkdir(parents=True,exist_ok=True)
            (out/'representative.pdf').write_bytes(pdf)
        self.assertNotIn('lecture-map-semantic.sqlite3', [p.name for p in Path(self.tmp.name).rglob('*')])
