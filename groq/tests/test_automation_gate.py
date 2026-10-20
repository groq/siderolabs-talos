import base64
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automation_gate import (
    Gate,
    GitHubPolicyReader,
    Halted,
    Snapshot,
    decode_json,
    validate_policy,
)

REVISION = "a" * 40
NEXT_REVISION = "b" * 40


def enabled(revision=REVISION):
    return Snapshot(revision, True, "approved candidate build")


class PolicyTests(unittest.TestCase):
    def test_checked_in_policy_is_valid(self):
        path = Path(__file__).resolve().parents[1] / "automation.json"
        # Both states must pass CI so a one-field enable/disable PR is possible.
        validate_policy(decode_json(path.read_bytes()))

    def test_strict_boolean(self):
        for value in ("false", "true", 0, 1, None, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_policy(
                    {"schema_version": 1, "enabled": value, "reason": "test"}
                )

    def test_invalid_policy(self):
        for policy in (
            [],
            {},
            {"enabled": True},
            {"schema_version": True, "enabled": True, "reason": "test"},
            {"schema_version": 2, "enabled": True, "reason": "test"},
            {"schema_version": 1, "enabled": True, "reason": ""},
            {"schema_version": 1, "enabled": True, "reason": "ok", "override": True},
        ):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                validate_policy(policy)

    def test_duplicate_keys_are_rejected(self):
        with self.assertRaises(ValueError):
            decode_json('{"enabled":false,"enabled":true}')

    def test_live_main_then_immutable_contents(self):
        raw = json.dumps(
            {"schema_version": 1, "enabled": True, "reason": "approved"}
        ).encode()
        request = Mock(
            side_effect=[
                [{"sha": REVISION}],
                {
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.encodebytes(raw).decode(),
                },
            ]
        )
        snapshot = GitHubPolicyReader(request)()
        self.assertEqual(snapshot, Snapshot(REVISION, True, "approved"))
        self.assertIn(
            "sha=main&path=groq/automation.json&per_page=1",
            request.call_args_list[0].args[0],
        )
        self.assertEqual(
            request.call_args_list[1].args[0],
            f"contents/groq/automation.json?ref={REVISION}",
        )

    def test_missing_policy_or_bad_revision_stops(self):
        for first in ([], [{"sha": "main"}], [{"sha": None}], [{"oops": REVISION}]):
            with self.subTest(first=first), self.assertRaises(Halted):
                Gate(GitHubPolicyReader(Mock(return_value=first))).check()

    def test_symlink_and_invalid_encoding_stop(self):
        for entry in (
            {"type": "symlink"},
            {"type": "file", "encoding": "raw"},
            {"type": "file", "encoding": "base64", "content": "not%base64"},
        ):
            with self.subTest(entry=entry), self.assertRaises(Halted):
                Gate(
                    GitHubPolicyReader(Mock(side_effect=[[{"sha": REVISION}], entry]))
                ).check()

    @patch.dict("os.environ", {}, clear=True)
    def test_no_credential_fails_closed(self):
        with self.assertRaises(Halted):
            Gate().check()


class GateTests(unittest.TestCase):
    def test_disabled_never_starts_a_command(self):
        reader = Mock(return_value=Snapshot(REVISION, False, "incident"))
        with (
            patch("automation_gate.subprocess.Popen") as popen,
            self.assertRaises(Halted),
        ):
            Gate(reader).run_build_command(["make", "initramfs"])
        popen.assert_not_called()

    def test_rechecks_every_call_without_cached_allow(self):
        reader = Mock(side_effect=[enabled(), Snapshot(NEXT_REVISION, False, "kill")])
        gate = Gate(reader)
        gate.check()
        with self.assertRaises(Halted):
            gate.check()
        self.assertEqual(reader.call_count, 2)

    def test_network_failure_latches_stop(self):
        reader = Mock(
            side_effect=[enabled(), TimeoutError("secret must not appear"), enabled()]
        )
        gate = Gate(reader)
        gate.check()
        with self.assertRaisesRegex(Halted, "TimeoutError") as caught:
            gate.check()
        self.assertNotIn("secret", str(caught.exception))
        with self.assertRaisesRegex(Halted, "latched"):
            gate.check()
        self.assertEqual(reader.call_count, 2)

    def test_reenable_does_not_resume_old_run(self):
        gate = Gate(Mock(side_effect=[enabled(), enabled(NEXT_REVISION)]))
        gate.check()
        with self.assertRaisesRegex(Halted, "policy changed"):
            gate.check()

    def test_old_publication_receipt_rejected(self):
        with self.assertRaisesRegex(Halted, "policy changed"):
            Gate(
                Mock(return_value=enabled(NEXT_REVISION)), expected_revision=REVISION
            ).check()

    def test_successful_command_checked_before_and_after(self):
        reader = Mock(return_value=enabled())
        process = Mock()
        process.wait.return_value = 0
        with patch("automation_gate.subprocess.Popen", return_value=process):
            Gate(reader).run_build_command(["make", "kernel"])
        self.assertEqual(reader.call_count, 2)

    def test_kill_during_wait_stops_build_client_and_latches(self):
        reader = Mock(side_effect=[enabled(), Snapshot(NEXT_REVISION, False, "kill")])
        process = Mock(pid=999)
        process.wait.side_effect = [subprocess.TimeoutExpired("make", 10), 0]
        process.poll.return_value = None
        gate = Gate(reader)
        with (
            patch("automation_gate.subprocess.Popen", return_value=process),
            patch("automation_gate.os.killpg") as kill,
            self.assertRaises(Halted),
        ):
            gate.run_build_command(["make", "initramfs"])
        kill.assert_called_once()
        self.assertTrue(gate.halted)

    def test_kill_after_command_prevents_next_command(self):
        reader = Mock(side_effect=[enabled(), Snapshot(NEXT_REVISION, False, "kill")])
        process = Mock()
        process.wait.return_value = 0
        process.poll.return_value = 0
        gate = Gate(reader)
        with patch("automation_gate.subprocess.Popen", return_value=process) as popen:
            with self.assertRaises(Halted):
                gate.run_build_command(["make", "kernel"])
            with self.assertRaises(Halted):
                gate.run_build_command(["make", "initramfs"])
            self.assertEqual(popen.call_count, 1)

    def test_command_failure_propagates(self):
        process = Mock()
        process.wait.return_value = 2
        process.poll.return_value = 2
        with (
            patch("automation_gate.subprocess.Popen", return_value=process),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            Gate(Mock(return_value=enabled())).run_build_command(["make", "kernel"])


if __name__ == "__main__":
    unittest.main()
