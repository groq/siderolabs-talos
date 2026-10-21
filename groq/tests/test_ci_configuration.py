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

    def test_buildkite_has_clean_preflight_and_explicit_manual_candidate(self):
        pipeline = (ROOT / ".buildkite/pipeline.yml").read_text()
        for required in (
            "key: groq-talos-preflight",
            "queue: large",
            "image: &talos_agent docker.io/library/python:3.12.11-bookworm@sha256:",
            "command: python3 groq/buildkite_runner.py preflight",
            'build.branch == "main"',
            'build.source == "ui"',
            'build.env("GROQ_TALOS_BUILD") == "candidate"',
            'block: "Approve unqualified Talos candidate"',
            "depends_on: groq-talos-approval",
            "if: *candidate_request",
            "automatic: false",
            "allowed: false",
            "concurrency: 1",
        ):
            self.assertIn(required, pipeline)
        # No automatic post-command publication path bypassing the live gate.
        self.assertNotRegex(pipeline, r"(?m)^\s*artifact_paths:")
        self.assertNotIn("groqkite gh token", pipeline)

    def test_actions_is_validation_only(self):
        workflow = (ROOT / ".github/workflows/groq-release.yaml").read_text()
        self.assertNotIn("candidate:", workflow)
        self.assertNotIn("self-hosted", workflow)
        self.assertNotIn("upload-artifact", workflow)


if __name__ == "__main__":
    unittest.main()
