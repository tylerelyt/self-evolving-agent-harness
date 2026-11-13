"""Runtime independence, legacy delegation and credential transport bindings."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from apps.oncall.workbench_config import load_model_environment


class RuntimeBoundaryTests(unittest.TestCase):
    def test_maintained_runtime_imports_with_teaching_modules_forbidden(self):
        code = r'''
import importlib.abc,importlib.machinery,json,sys
from pathlib import Path
root=Path(sys.argv[1])
sys.path.insert(0,str(root))
class Guard(importlib.abc.MetaPathFinder):
 def find_spec(self,name,path=None,target=None):
  if name.split('.')[0] in ('workshop','examples'):
   raise ImportError('Teaching module forbidden: '+name)
  spec=importlib.machinery.PathFinder.find_spec(name,path)
  if spec and spec.origin and str(root) in spec.origin:
   origin=Path(spec.origin).resolve()
   if root/'workshop' in origin.parents or root/'examples' in origin.parents:
    raise ImportError('Teaching source forbidden: '+str(origin))
sys.meta_path.insert(0,Guard())
from apps.oncall import service,workflow
from apps.oncall.paths import APP
from apps.oncall.workbench import Workbench
assert Path(service.SEED).is_relative_to(APP)
assert Path(service.HERE)==APP
assert workflow.run is not None and Workbench is not None
print(json.dumps({'runtime':str(APP),'teaching_imports':False}))
'''
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-E", "-S", "-B", "-c", code, str(ROOT)],
                                    cwd=directory, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["teaching_imports"])

    def test_legacy_service_and_reference_delegate_to_same_runtime_objects(self):
        from apps.oncall import service, workflow
        import oncall_service
        self.assertIs(oncall_service, service)
        # Import the old reference by its actual file to test compatibility,
        # since canonical paths deliberately take precedence in sys.path.
        from importlib.util import module_from_spec, spec_from_file_location
        spec = spec_from_file_location("legacy_reference_identity", ROOT / "workshop/capstone/self_improvement_agent.py")
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIs(module.run, workflow.run)
        self.assertIs(module.learn, workflow.learn)


class ProviderBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.envfile = Path(temporary.name) / ".env"
        self.envfile.write_text("")

    def load(self):
        return load_model_environment(self.envfile)

    def test_inferred_deepseek_is_stable_across_repeated_loads(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fixture-deepseek-key"}, clear=True):
            self.assertEqual(self.load()["provider"], "deepseek")
            self.assertEqual(self.load()["provider"], "deepseek")
            self.assertEqual(os.environ["GLM_API_KEY"], "fixture-deepseek-key")
            self.assertEqual(os.environ["GLM_BASE_URL"], "https://api.deepseek.com")

    def test_explicit_deepseek_never_uses_existing_glm_key(self):
        with patch.dict(os.environ, {"ONCALL_PROVIDER": "deepseek", "GLM_API_KEY": "fixture-glm-key"}, clear=True):
            self.assertFalse(self.load()["configured"])
            self.assertEqual(os.environ["GLM_API_KEY"], "")
            self.assertEqual(os.environ["GLM_BASE_URL"], "https://api.deepseek.com")

    def test_process_provider_key_model_endpoint_override_file(self):
        self.envfile.write_text("ONCALL_PROVIDER=glm\nDEEPSEEK_API_KEY=fixture-file-key\nDEEPSEEK_MODEL=file-model\nDEEPSEEK_BASE_URL=https://file.example/v1\n")
        values = {"ONCALL_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "fixture-process-key",
                  "DEEPSEEK_MODEL": "process-model", "DEEPSEEK_BASE_URL": "https://process.example/v1"}
        with patch.dict(os.environ, values, clear=True):
            self.assertEqual(self.load()["provider"], "deepseek")
            self.assertEqual(os.environ["GLM_API_KEY"], "fixture-process-key")
            self.assertEqual(os.environ["GLM_MODEL"], "process-model")
            self.assertEqual(os.environ["GLM_BASE_URL"], "https://process.example/v1")

    def test_explicit_glm_keeps_glm_credentials_even_with_deepseek_present(self):
        values = {"ONCALL_PROVIDER": "glm", "GLM_API_KEY": "fixture-glm-key", "DEEPSEEK_API_KEY": "fixture-deepseek-key",
                  "GLM_MODEL": "glm-model", "GLM_BASE_URL": "https://glm.example/v4"}
        with patch.dict(os.environ, values, clear=True):
            self.assertEqual(self.load()["provider"], "glm")
            self.assertEqual(os.environ["GLM_API_KEY"], "fixture-glm-key")
            self.assertEqual(os.environ["GLM_BASE_URL"], "https://glm.example/v4")


if __name__ == "__main__":
    unittest.main()
