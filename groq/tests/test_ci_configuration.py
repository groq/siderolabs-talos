"""Keep the active CI bootstrap wired to real, non-deploying validation."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class CIConfigurationTests(unittest.TestCase):
    def test_buildkite_bootstrap_has_validation_entrypoint(self):
        pipeline = (ROOT / ".buildkite/pipeline.yml").read_text()
        self.assertIn("key: groq-release-validation", pipeline)
        self.assertIn(
            "command: mise exec python@3.12.11 -- bash groq/check.sh", pipeline
        )
        self.assertNotIn("release.py build", pipeline)

    def test_both_ci_systems_use_shared_validation(self):
        workflow = (ROOT / ".github/workflows/groq-release.yaml").read_text()
        self.assertIn("run: bash groq/check.sh", workflow)
        script = (ROOT / "groq/check.sh").read_text()
        self.assertIn("python3 -m unittest discover -s groq/tests -v", script)
        self.assertIn("python3 groq/release.py validate", script)
        self.assertIn("python3 groq/release.py prepare", script)
        self.assertNotIn("release.py build", script)


if __name__ == "__main__":
    unittest.main()
