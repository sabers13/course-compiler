"""Executable browser identity comparison plus semantic-only presentation contract."""
import json
from pathlib import Path
import subprocess
import unittest

from tests.toolchain_support import NODE_MISSING_REASON, node_available

ROOT = Path(__file__).resolve().parents[1]

class BrowserSemanticTests(unittest.TestCase):
    @unittest.skipUnless(
        node_available(), f"shipped JS comparison cannot execute ({NODE_MISSING_REASON})"
    )
    def test_request_binding_comparison_is_app_owned_and_stale_safe(self):
        source = (ROOT/'course_compiler/app/static/app.js').read_text()
        start = source.index('  const GPT_REQUEST_IDENTITY_FIELDS')
        end = source.index('  function gptHolderId',start)
        comparison = source[start:end]
        program = comparison + '''
const assert = require('node:assert/strict');
const identity = {request_id:'r', job_id:'j', workflow_id:'w', expected_revision:3,
  operation_id:'o', kind:'lecture_generation', input_refs:['l2']};
assert.equal(gptIdentityMismatch(identity, {...identity, lease_expires_at:'later'}), null);
for (const field of GPT_REQUEST_IDENTITY_FIELDS) {
  const changed = {...identity, [field]: field === 'input_refs' ? ['l3'] : 'different'};
  assert.equal(gptIdentityMismatch(identity, changed), field);
}
assert.equal(gptIdentityMismatch(null, identity), 'request_id');
'''
        result = subprocess.run(['node','-e',program],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('parsed.', comparison)
        self.assertIn('gptHandoffText.value = data.handoff.prompt',source)
        self.assertNotIn('JSON.parse(raw)',source)
        self.assertNotIn('expectedModelKeys',source)
        self.assertIn('text: raw',source)
        self.assertIn('plan.value !== plan.dataset.original',source)
        html = (ROOT/'course_compiler/app/static/index.html').read_text()
        self.assertNotIn('semantic-work-result/v1',html)
        self.assertNotIn('result JSON',html)
        self.assertIn('course-guidance',html)
