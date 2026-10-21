import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import buildkite_runner as runner
import release
from automation_gate import Gate, Halted, Snapshot

SHA = "a" * 40
REVISION = "b" * 40


def context():
    return {
        "PATH": "/usr/bin:/bin",
        "BUILDKITE": "true",
        "BUILDKITE_ORGANIZATION_SLUG": "groq",
        "BUILDKITE_PIPELINE_SLUG": "siderolabs-talos",
        "BUILDKITE_BRANCH": "main",
        "BUILDKITE_SOURCE": "ui",
        "BUILDKITE_PULL_REQUEST": "false",
        "BUILDKITE_STEP_KEY": "groq-talos-candidate",
        "GROQ_TALOS_BUILD": "candidate",
        "BUILDKITE_UNBLOCKER_ID": "abcd1234-1234-1234-1234-123456789abc",
        "BUILDKITE_COMMIT": SHA,
        "BUILDKITE_JOB_ID": "12345678-1234-1234-1234-123456789abc",
    }


def artifacts(path):
    path.mkdir(parents=True)
    for variant in ("standard", "ramdisk"):
        (path / variant).mkdir()
        for filename in ("vmlinuz-amd64", "initramfs-amd64.xz", "installer-amd64.tar"):
            (path / variant / filename).write_bytes(b"fixture")
    tool = path / "talosctl-linux-amd64"
    tool.write_bytes(b"fixture-tool")
    records = release.artifact_records(path)
    records.append(
        {
            "path": tool.name,
            "sha256": release.sha256(tool),
            "bytes": tool.stat().st_size,
        }
    )
    manifest = {
        "status": "built-unqualified",
        "production_approved": False,
        "automation_policy_revision": REVISION,
        "artifacts": records,
    }
    (path / "manifest.json").write_text(json.dumps(manifest))
    (path / "SHA256SUMS").write_text(
        "".join(f"{r['sha256']}  {r['path']}\n" for r in records)
    )
    return manifest


class PreflightTests(unittest.TestCase):
    def test_platform_identity_is_not_a_blanket_token_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            findings = runner.credential_findings(
                {
                    "NSC_TOKEN_FILE": "/not/read",
                    "NSC_TOKEN": "private",
                    "BUILDKITE_TOKEN": "private",
                },
                Path(tmp),
            )
        self.assertNotIn("environment:NSC_TOKEN_FILE", findings)
        self.assertIn("environment:NSC_TOKEN", findings)
        self.assertIn("environment:BUILDKITE_TOKEN", findings)

    def test_sensitive_environment_fails_without_exposing_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            for key in (
                "GH_TOKEN",
                "GITHUB_TOKEN",
                "GROQKITE_OAUTH2_CLIENT_SECRET",
                "GOOGLE_APPLICATION_CREDENTIALS",
                "AWS_ACCESS_KEY_ID",
                "KUBECONFIG",
                "TALOSCONFIG",
                "SSH_AUTH_SOCK",
                "VAULT_TOKEN",
            ):
                with self.subTest(key=key):
                    findings = runner.credential_findings(
                        {key: "secret-value"}, Path(tmp)
                    )
                    self.assertIn(f"environment:{key}", findings)
                    self.assertNotIn("secret-value", str(findings))

    def test_known_credential_files_and_unsafe_debug_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for name in runner.CREDENTIAL_FILES:
                target = home / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("do not read me")
            findings = runner.credential_findings(
                {"BUILDKITE_AGENT_DEBUG_HTTP": "true"}, home
            )
            for name in runner.CREDENTIAL_FILES:
                self.assertIn(f"credential-file:{name}", findings)
            self.assertIn("unsafe-agent-option:BUILDKITE_AGENT_DEBUG_HTTP", findings)

    def test_upstream_commands_cannot_inherit_credentials_or_remote_builder(self):
        with patch.dict(
            os.environ,
            {
                "GH_TOKEN": "private",
                "BUILDKITE_AGENT_ACCESS_TOKEN": "private",
                "DOCKER_HOST": "tcp://shared",
                "BUILDX_BUILDER": "shared",
            },
            clear=True,
        ):
            os.environ["PATH"] = "/usr/bin"
            env = runner.child_environment(Path("/clean"), "job-owned")
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("BUILDKITE_AGENT_ACCESS_TOKEN", env)
        self.assertEqual(env["DOCKER_HOST"], runner.DOCKER_HOST)
        self.assertEqual(env["BUILDX_BUILDER"], "job-owned")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/dev/null")

    def test_resource_floors(self):
        info = {
            "OSType": "linux",
            "Architecture": "x86_64",
            "NCPU": 8,
            "MemTotal": 32 * 1024**3,
        }
        runner.validate_resources("Linux", "x86_64", runner.MIN_DISK, info)
        for system, machine, free, docker in (
            ("Darwin", "x86_64", runner.MIN_DISK, info),
            ("Linux", "aarch64", runner.MIN_DISK, info),
            ("Linux", "x86_64", runner.MIN_DISK - 1, info),
            ("Linux", "x86_64", runner.MIN_DISK, {**info, "NCPU": 4}),
            ("Linux", "x86_64", runner.MIN_DISK, {**info, "Architecture": "aarch64"}),
            ("Linux", "x86_64", runner.MIN_DISK, {**info, "MemTotal": 1024}),
        ):
            with (
                self.subTest(system=system, machine=machine, docker=docker),
                self.assertRaises(Halted),
            ):
                runner.validate_resources(system, machine, free, docker)

    def test_preflight_does_not_acquire_secrets_start_builders_or_build(self):
        info = {
            "OSType": "linux",
            "Architecture": "x86_64",
            "NCPU": 8,
            "MemTotal": 32 * 1024**3,
        }
        with (
            patch.object(runner, "credential_findings", return_value=[]),
            patch.object(runner.shutil, "which", return_value="/bin/tool"),
            patch.object(
                runner.shutil, "disk_usage", return_value=Mock(free=runner.MIN_DISK)
            ),
            patch.object(runner.platform, "system", return_value="Linux"),
            patch.object(runner.platform, "machine", return_value="x86_64"),
            patch.object(
                runner, "capture", side_effect=[json.dumps(info), "buildx version"]
            ) as capture,
            patch.object(runner, "policy_reader") as credential,
            patch.object(release, "build") as build,
        ):
            result = runner.preflight()
        credential.assert_not_called()
        build.assert_not_called()
        self.assertEqual(
            [c.args[0][:2] for c in capture.call_args_list],
            [["docker", "info"], ["docker", "buildx"]],
        )
        self.assertFalse(result["privileged_buildkit_smoke_tested"])
        self.assertFalse(result["production_access_audited"])


class CandidateTests(unittest.TestCase):
    def test_secret_name_is_buildkite_compatible(self):
        self.assertRegex(runner.SECRET, r"^[A-Z][A-Z0-9_]*$")

    def test_secret_lookup_uses_configured_name_and_accepts_response_case(self):
        for key in ("GROQ_TALOS_GITHUB_READ_TOKEN", "groq_talos_github_read_token"):
            result = Mock(returncode=0, stdout=json.dumps({key: "fixture-token"}))
            with (
                self.subTest(key=key),
                patch.object(runner.subprocess, "run", return_value=result) as call,
            ):
                reader = runner.policy_reader()
                self.assertEqual(reader.token, "fixture-token")
                call.assert_called_once_with(
                    [
                        "buildkite-agent",
                        "secret",
                        "get",
                        "--format=json",
                        "GROQ_TALOS_GITHUB_READ_TOKEN",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )

    def test_pending_isolation_review_blocks_before_secret_access(self):
        with (
            patch.dict(os.environ, context(), clear=True),
            patch.object(runner, "preflight"),
            patch.object(runner, "isolation_review", return_value={"approved": False}),
            patch.object(runner, "policy_reader") as credential,
            self.assertRaisesRegex(Halted, "isolation review"),
        ):
            runner.candidate()
        credential.assert_not_called()

    def test_only_fresh_approved_manual_main_context_is_allowed(self):
        runner.validate_candidate_context(context())
        for key, value in (
            ("BUILDKITE_ORGANIZATION_SLUG", "other"),
            ("BUILDKITE_PIPELINE_SLUG", "other"),
            ("BUILDKITE_SOURCE", "webhook"),
            ("BUILDKITE_SOURCE", "api"),
            ("BUILDKITE_BRANCH", "feature"),
            ("BUILDKITE_PULL_REQUEST", "25"),
            ("BUILDKITE_TAG", "v1.13.11"),
            ("BUILDKITE_RETRY_COUNT", "1"),
            ("BUILDKITE_UNBLOCKER_ID", ""),
            ("BUILDKITE_COMMIT", "HEAD"),
            ("GROQ_TALOS_BUILD", ""),
            ("BUILDKITE_JOB_ID", "bad"),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(Halted):
                runner.validate_candidate_context({**context(), key: value})

    def test_missing_or_bad_secret_is_redacted_and_stops(self):
        for result in (
            Mock(returncode=1, stdout="private", stderr="private"),
            Mock(returncode=0, stdout="private"),
            Mock(returncode=0, stdout=json.dumps({"wrong": "private"})),
            Mock(
                returncode=0,
                stdout=json.dumps({"groq-talos-github-read-token": "private"}),
            ),
            Mock(returncode=0, stdout=json.dumps({runner.SECRET: ""})),
        ):
            with (
                patch.object(runner.subprocess, "run", return_value=result),
                self.assertRaises(Halted) as caught,
            ):
                runner.policy_reader()
            self.assertNotIn("private", str(caught.exception))

    def test_secret_stays_out_of_environment(self):
        result = Mock(returncode=0, stdout=json.dumps({runner.SECRET: "private"}))
        before = dict(os.environ)
        with patch.object(runner.subprocess, "run", return_value=result) as call:
            reader = runner.policy_reader()
        self.assertEqual(reader.token, "private")
        self.assertEqual(dict(os.environ), before)
        self.assertNotIn("--skip-redaction", call.call_args.args[0])

    def test_main_must_still_match_checkout_and_approved_commit(self):
        reader = Mock()
        reader.request.return_value = {"sha": SHA}
        runner.require_current_main(reader, SHA, SHA)
        for expected, actual, remote in ((SHA, "c" * 40, SHA), (SHA, SHA, "c" * 40)):
            reader.request.return_value = {"sha": remote}
            with self.assertRaises(Halted):
                runner.require_current_main(reader, expected, actual)
        reader.request.side_effect = TimeoutError("private")
        with self.assertRaises(Halted) as caught:
            runner.require_current_main(reader, SHA, SHA)
        self.assertNotIn("private", str(caught.exception))

    def test_disabled_policy_prevents_builder_setup_and_release_build(self):
        reader = Mock(return_value=Snapshot(REVISION, False, "disabled"))
        with (
            patch.dict(os.environ, context(), clear=True),
            patch.object(runner, "preflight"),
            patch.object(runner, "require_isolation_review"),
            patch.object(runner, "policy_reader", return_value=reader),
            patch.object(Gate, "run_build_command") as command,
            patch.object(release, "build") as build,
            self.assertRaises(Halted),
        ):
            runner.candidate()
        command.assert_not_called()
        build.assert_not_called()

    def test_builder_is_job_local_pinned_and_never_uses_shared_builder(self):
        command = runner.builder_create_command("job-owned")
        self.assertIn("docker-container", command)
        self.assertIn("image=" + runner.BUILDKIT_IMAGE, command)
        self.assertIn("--allow-insecure-entitlement security.insecure", command)
        self.assertEqual(command[-1], runner.DOCKER_HOST)

    def test_failed_build_cleans_only_owned_builder_and_never_uploads(self):
        reader = Mock(return_value=Snapshot(REVISION, True, "approved"))
        reader.request.return_value = {"sha": SHA}
        with (
            patch.dict(os.environ, context(), clear=True),
            patch.object(runner, "preflight"),
            patch.object(runner, "require_isolation_review"),
            patch.object(runner, "policy_reader", return_value=reader),
            patch.object(runner, "capture", return_value=SHA),
            patch.object(Gate, "run_build_command"),
            patch.object(release, "build", side_effect=Halted("stopped")),
            patch.object(
                runner.subprocess, "run", return_value=Mock(returncode=0)
            ) as cleanup,
            patch.object(runner, "upload_candidate") as upload,
            self.assertRaises(Halted),
        ):
            runner.candidate()
        self.assertEqual(
            cleanup.call_args.args[0],
            [
                "docker",
                "buildx",
                "rm",
                "--force",
                "talos-" + context()["BUILDKITE_JOB_ID"],
            ],
        )
        self.assertNotIn(
            "BUILDKITE_AGENT_ACCESS_TOKEN", cleanup.call_args.kwargs["env"]
        )
        upload.assert_not_called()

    def test_success_cleans_builder_before_guarded_publication(self):
        reader = Mock(return_value=Snapshot(REVISION, True, "approved"))
        reader.request.return_value = {"sha": SHA}
        events = []

        def built(lock, workspace, gate):
            events.append("build")
            self.assertEqual(gate.revision, REVISION)
            self.assertNotIn("BUILDKITE_AGENT_ACCESS_TOKEN", gate.command_env)
            artifacts(workspace / "artifacts")

        def cleaned(*args, **kwargs):
            events.append("cleanup")
            return Mock(returncode=0)

        def command(gate, cmd, **kwargs):
            gate.check()
            if cmd[:3] == ["buildkite-agent", "artifact", "upload"]:
                events.append("upload")
                self.assertEqual(gate.revision, REVISION)
            gate.check()

        with (
            patch.dict(os.environ, context(), clear=True),
            patch.object(runner, "preflight"),
            patch.object(runner, "require_isolation_review"),
            patch.object(runner, "policy_reader", return_value=reader),
            patch.object(runner, "capture", return_value=SHA),
            patch.object(Gate, "run_build_command", command),
            patch.object(release, "build", side_effect=built),
            patch.object(runner.subprocess, "run", side_effect=cleaned),
        ):
            runner.candidate()
        self.assertEqual(events, ["build", "cleanup", "upload"])

    def test_cleanup_failure_blocks_publication_after_successful_build(self):
        reader = Mock(return_value=Snapshot(REVISION, True, "approved"))
        reader.request.return_value = {"sha": SHA}
        with (
            patch.dict(os.environ, context(), clear=True),
            patch.object(runner, "preflight"),
            patch.object(runner, "require_isolation_review"),
            patch.object(runner, "policy_reader", return_value=reader),
            patch.object(runner, "capture", return_value=SHA),
            patch.object(Gate, "run_build_command"),
            patch.object(release, "build"),
            patch.object(runner.subprocess, "run", return_value=Mock(returncode=1)),
            patch.object(runner, "upload_candidate") as upload,
            self.assertRaisesRegex(Halted, "cleanup failed"),
        ):
            runner.candidate()
        upload.assert_not_called()


class PublicationTests(unittest.TestCase):
    def test_exact_verified_files_only_are_uploaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "artifacts"
            artifacts(output)
            (output / "unexpected-secret").write_text("never upload")
            gate = Mock(revision=REVISION)
            runner.upload_candidate(output, gate)
            command = gate.run_build_command.call_args.args[0]
            self.assertEqual(command[:3], ["buildkite-agent", "artifact", "upload"])
            self.assertNotIn("*", command[-1])
            self.assertNotIn("unexpected-secret", command[-1])

    def test_bad_receipts_and_modified_artifacts_never_upload(self):
        for change in ("policy", "approved", "file", "checksums"):
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "artifacts"
                manifest = artifacts(output)
                if change == "policy":
                    manifest["automation_policy_revision"] = "c" * 40
                elif change == "approved":
                    manifest["production_approved"] = True
                elif change == "file":
                    (output / "standard/vmlinuz-amd64").write_bytes(b"changed")
                else:
                    (output / "SHA256SUMS").write_text("bad")
                (output / "manifest.json").write_text(json.dumps(manifest))
                gate = Mock(revision=REVISION)
                with self.subTest(change=change), self.assertRaises(Halted):
                    runner.upload_candidate(output, gate)
                gate.run_build_command.assert_not_called()

    def test_policy_changed_before_upload_is_not_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "artifacts"
            artifacts(output)
            gate = Gate(
                Mock(return_value=Snapshot("c" * 40, True, "new")),
                expected_revision=REVISION,
            )
            with (
                patch("automation_gate.subprocess.Popen") as popen,
                self.assertRaises(Halted),
            ):
                runner.upload_candidate(output, gate)
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
