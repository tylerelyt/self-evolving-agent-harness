"""Native review evidence survives rejection even with SessionDB disabled."""

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class ReviewEvidenceTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("openai") and importlib.util.find_spec("yaml"),
                         "Requires the pinned native Hermes dependencies")
    def test_rejected_native_review_still_preserves_raw_transcript(self):
        code = r'''
import json,sys
from pathlib import Path
root=Path(sys.argv[1]);state=Path(sys.argv[2]).resolve()
sys.path.insert(0,str(root))
from apps.oncall.paths import prepare
prepare()
from fact_review import native_review,review_connection
from recovery_skill_review import verified_result
from fleet import SEED
with review_connection(True,lambda messages:(None,{'status':'keep'})) as url:
 captured=native_review(state,'Saved historical evidence.',{'base_url':url,'api_key':'local-fixture','model':'review-fixture'},initial_skill=SEED.read_bytes())
raw=json.loads((state/'native-review.json').read_text())
assert raw['result']['messages']==captured[2]['messages']
assert raw['session_id']==captured[0]
assert raw['worker_flags']['persist_disabled'] is True
assert raw['worker_flags']['memory_write_origin']=='background_review'
assert raw['worker_flags']['valid_tool_names']==['skill_manage','skill_view','skills_list']
assert 'api_key' not in raw and 'options' not in raw
try: verified_result(state,{},SEED.read_bytes(),captured)
except RuntimeError: pass
else: raise AssertionError('Review without successful reads must remain rejected')
assert (state/'native-review.json').is_file()
assert (state/'native-review.json').stat().st_mode & 0o777 == 0o600
'''
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-B", "-c", code, str(ROOT), directory],
                                    cwd=directory, capture_output=True, text=True, timeout=35)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
