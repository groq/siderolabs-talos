import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import release
from automation_gate import Gate, Halted, Snapshot


class LockTests(unittest.TestCase):
    def setUp(self):
        self.lock = release.read_lock(release.DEFAULT_LOCK)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "release.json"

    def check_changed(self, field, value):
        changed = copy.deepcopy(self.lock)
        changed[field] = value
        self.path.write_text(json.dumps(changed))
        with self.assertRaises(ValueError):
            release.read_lock(self.path)

    def test_source_and_patch_are_pinned(self):
        self.assertEqual(self.lock["upstream_tag"], "v1.13.11")
        self.assertEqual(len(self.lock["upstream_commit"]), 40)
        self.assertEqual(
            release.sha256(release.ROOT / self.lock["patch"]), self.lock["patch_sha256"]
        )

    def test_wrong_or_mutable_inputs_rejected(self):
        for field, value in (
            ("schema_version", True),
            ("upstream_tag", "v1.14.4"),
            ("upstream_tag", "v1.13.12-rc.1"),
            ("upstream_commit", "main"),
            ("candidate", "v1.14.4-groq-ramdisk.1"),
            ("patch", "../../bad.patch"),
            ("patch_sha256", "0" * 64),
            ("imager", "ghcr.io/siderolabs/imager:latest"),
            (
                "installer_base",
                self.lock["installer_base"].replace("v1.13.11", "v1.13.2"),
            ),
            ("extensions", []),
            ("extensions", self.lock["extensions"] * 2),
            ("extensions", ["ghcr.io/siderolabs/iscsi-tools:latest"]),
        ):
            with self.subTest(field=field, value=value):
                self.check_changed(field, value)

    def test_imager_uses_same_extensions_for_both_variants(self):
        for variant in ("standard", "ramdisk"):
            for kind in ("kernel", "initramfs", "installer"):
                command = release.imager_command(
                    self.lock, Path("/source"), Path("/out"), variant, kind
                )
                self.assertIn(self.lock["imager"], command)
                self.assertIn(
                    f"--base-installer-image={self.lock['installer_base']}", command
                )
                for image in self.lock["extensions"]:
                    self.assertIn(f"--system-extension-image={image}", command)
                mounts = " ".join(command)
                self.assertEqual(
                    "/source/_out/initramfs-amd64.xz" in mounts, variant == "ramdisk"
                )
                self.assertNotIn("--privileged", command)
                self.assertNotIn("docker.sock", mounts)

    def test_build_disabled_before_creating_workspace(self):
        workspace = Path(self.temp.name) / "build"
        gate = Gate(Mock(return_value=Snapshot("a" * 40, False, "disabled")))
        with self.assertRaises(Halted):
            release.build(release.DEFAULT_LOCK, workspace, gate)
        self.assertFalse(workspace.exists())


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        upstream = self.base / "upstream"
        upstream.mkdir()

        def git(*args):
            return subprocess.check_output(
                ["git", *args], cwd=upstream, text=True
            ).strip()

        git("init", "--quiet")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "test")
        git("config", "commit.gpgsign", "false")
        (upstream / "example").write_text("before\n")
        git("add", "example")
        git("commit", "--quiet", "-m", "feat: fixture")
        git("tag", "v1.13.11")
        self.commit = git("rev-parse", "HEAD")
        (upstream / "example").write_text("after\n")
        self.patch = self.base / "change.patch"
        self.patch.write_text(git("diff") + "\n")
        git("restore", "example")
        self.lock = {
            "upstream_tag": "v1.13.11",
            "upstream_commit": self.commit,
            "patch": str(self.patch),
        }
        self.override = patch.object(release, "UPSTREAM", str(upstream))
        self.override.start()
        self.addCleanup(self.override.stop)

    def test_clean_checkout_and_exact_patch(self):
        destination = self.base / "prepared"
        release.prepare(self.lock, destination)
        self.assertEqual((destination / "example").read_text(), "after\n")

    def test_moved_tag_fails_before_applying_patch(self):
        self.lock["upstream_commit"] = "0" * 40
        destination = self.base / "prepared"
        with self.assertRaisesRegex(ValueError, "tag moved"):
            release.prepare(self.lock, destination)
        self.assertEqual((destination / "example").read_text(), "before\n")

    def test_existing_workspace_never_reset(self):
        destination = self.base / "prepared"
        destination.mkdir()
        (destination / "user-work").write_text("keep this")
        with self.assertRaises(FileExistsError):
            release.prepare(self.lock, destination)
        self.assertEqual((destination / "user-work").read_text(), "keep this")

    def test_patch_conflict_fails(self):
        self.patch.write_text(
            self.patch.read_text().replace("-before", "-not-the-base")
        )
        with self.assertRaises(subprocess.CalledProcessError):
            release.prepare(self.lock, self.base / "prepared")


class ArtifactTests(unittest.TestCase):
    def test_platform_qualified_imager_output_names(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "kernel-amd64").write_bytes(b"kernel")
            (root / "initramfs-metal-amd64.xz").write_bytes(b"initramfs")
            release.normalize_imager_outputs(root)
            self.assertEqual((root / "vmlinuz-amd64").read_bytes(), b"kernel")
            self.assertEqual((root / "initramfs-amd64.xz").read_bytes(), b"initramfs")

    def test_complete_artifact_set_and_missing_member(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for variant in ("standard", "ramdisk"):
                (root / variant).mkdir()
                for name in (
                    "vmlinuz-amd64",
                    "initramfs-amd64.xz",
                    "installer-amd64.tar",
                ):
                    (root / variant / name).write_bytes(b"fixture")
            records = release.artifact_records(root)
            self.assertEqual(len(records), 6)
            self.assertTrue(all(len(record["sha256"]) == 64 for record in records))
            (root / "ramdisk/initramfs-amd64.xz").unlink()
            with self.assertRaises(ValueError):
                release.artifact_records(root)


class BuildTests(unittest.TestCase):
    def test_build_wiring_and_manifest(self):
        self.exercise_build()

    def test_kernel_mismatch_never_creates_manifest(self):
        self.exercise_build(mismatch=True)

    def exercise_build(self, mismatch=False):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "build"
            commands = []
            gate = Mock()
            gate.revision = "a" * 40

            def prepare(lock, source, run):
                (source / "_out").mkdir(parents=True)
                for filename in (
                    "vmlinuz-amd64",
                    "initramfs-amd64.xz",
                    "talosctl-linux-amd64",
                ):
                    (source / "_out" / filename).write_bytes(b"built")

            def run(command, **kwargs):
                commands.append(command)
                if command[0] != "docker":
                    return
                mount = next(arg for arg in command if arg.endswith(",dst=/out"))
                destination = Path(
                    mount.removeprefix("type=bind,src=").removesuffix(",dst=/out")
                )
                kind = command[command.index("--arch=amd64") - 1]
                filename = {
                    "kernel": "kernel-amd64",
                    "initramfs": "initramfs-metal-amd64.xz",
                    "installer": "installer-amd64.tar",
                }[kind]
                content = b"kernel" if kind == "kernel" else destination.name.encode()
                if mismatch and kind == "kernel" and destination.name == "ramdisk":
                    content = b"wrong-kernel"
                (destination / filename).write_bytes(content)

            gate.run_build_command.side_effect = run
            with (
                patch.object(release.sys, "platform", "linux"),
                patch.object(
                    release.shutil, "disk_usage", return_value=Mock(free=100 * 1024**3)
                ),
                patch.object(release, "prepare", side_effect=prepare),
            ):
                if mismatch:
                    with self.assertRaisesRegex(ValueError, "kernel differs"):
                        release.build(release.DEFAULT_LOCK, workspace, gate)
                    self.assertFalse((workspace / "artifacts/manifest.json").exists())
                    return
                release.build(release.DEFAULT_LOCK, workspace, gate)

            manifest = json.loads((workspace / "artifacts/manifest.json").read_text())
            self.assertEqual(manifest["status"], "built-unqualified")
            self.assertIs(manifest["production_approved"], False)
            self.assertEqual(manifest["automation_policy_revision"], gate.revision)
            self.assertEqual(len(manifest["artifacts"]), 7)
            self.assertEqual(len(commands), 10)
            self.assertTrue(
                all(
                    "PUSH=false" in command
                    for command in commands
                    if command[0] == "make"
                )
            )
            self.assertFalse(
                any(
                    command[0] in ("kubectl", "talosctl", "ssh") for command in commands
                )
            )


if __name__ == "__main__":
    unittest.main()
