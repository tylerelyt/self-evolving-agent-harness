"""Model settings stay local and are parsed as data."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbench_config import load_model_environment


class ModelConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / ".env"

    def test_existing_process_settings_win_and_unrelated_values_are_not_loaded(self):
        self.path.write_text('GLM_API_KEY=file-key\nGLM_MODEL="course-model" # comment\nUNRELATED=value\n')
        with patch.dict(os.environ, {"GLM_API_KEY": "process-key"}, clear=True):
            status = load_model_environment(self.path)
            self.assertTrue(status["configured"])
            self.assertEqual(os.environ["GLM_API_KEY"], "process-key")
            self.assertEqual(os.environ["GLM_MODEL"], "course-model")
            self.assertNotIn("UNRELATED", os.environ)
            self.assertNotIn("file-key", str(status))
            self.assertNotIn("process-key", str(status))

    def test_shell_substitution_is_not_executed(self):
        marker = self.directory / "unexpected"
        self.path.write_text(f'GLM_API_KEY="$(touch {marker})"\n')
        with patch.dict(os.environ, {}, clear=True):
            load_model_environment(self.path)
        self.assertFalse(marker.exists())

    def test_missing_explicit_file_is_reported(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            load_model_environment(self.path)

    def test_empty_key_does_not_count_as_configured(self):
        self.path.write_text('GLM_API_KEY=\n')
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(load_model_environment(self.path)["configured"])

    def test_remote_plain_http_and_embedded_credentials_are_rejected(self):
        for endpoint in ("http://example.com/v1", "https://user:password@example.com/v1"):
            with self.subTest(endpoint=endpoint), patch.dict(os.environ, {}, clear=True):
                self.path.write_text(f'GLM_BASE_URL={endpoint}\n')
                with self.assertRaisesRegex(ValueError, "GLM_BASE_URL"):
                    load_model_environment(self.path)


if __name__ == "__main__":
    unittest.main()
