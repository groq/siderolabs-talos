"""Prepare/build unqualified Talos candidates, without production access."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from automation_gate import Gate, Halted, decode_json, validate_policy

ROOT = Path(__file__).resolve().parent.parent
UPSTREAM = "https://github.com/siderolabs/talos.git"
DEFAULT_LOCK = ROOT / "groq/releases/v1.13.11.json"
SHA256 = r"[0-9a-f]{64}"
LOCK_FIELDS = {
    "schema_version",
    "upstream_tag",
    "upstream_commit",
    "candidate",
    "patch",
    "patch_sha256",
    "patch_origin",
    "imager",
    "installer_base",
    "extensions",
}


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_lock(path):
    lock = decode_json(path.read_bytes())
    if not isinstance(lock, dict) or set(lock) != LOCK_FIELDS:
        raise ValueError("invalid release lock fields")
    if type(lock["schema_version"]) is not int or lock["schema_version"] != 1:
        raise ValueError("unsupported release lock schema")
    for key in LOCK_FIELDS - {"schema_version", "extensions"}:
        if not isinstance(lock[key], str) or not lock[key]:
            raise ValueError(f"{key} must be a nonempty string")
    # Adjacent-minor support must be implemented/reviewed, not enabled by input.
    if not isinstance(lock["upstream_tag"], str) or not re.fullmatch(
        r"v1\.13\.[0-9]+", lock["upstream_tag"]
    ):
        raise ValueError("this builder currently admits stable Talos 1.13 patches only")
    if not re.fullmatch(r"[0-9a-f]{40}", lock["upstream_commit"]):
        raise ValueError("upstream commit must be a full SHA")
    if not re.fullmatch(
        re.escape(lock["upstream_tag"]) + r"-groq-ramdisk\.[1-9][0-9]*",
        lock["candidate"],
    ):
        raise ValueError("candidate must identify its upstream version and revision")
    patch = (ROOT / lock["patch"]).resolve()
    if not patch.is_relative_to(ROOT / "groq/patches") or patch.suffix != ".patch":
        raise ValueError("patch must be inside groq/patches")
    if (
        not re.fullmatch(SHA256, lock["patch_sha256"])
        or sha256(patch) != lock["patch_sha256"]
    ):
        raise ValueError("patch checksum mismatch")
    if not re.fullmatch(
        r"https://github.com/groq/siderolabs-talos/commit/[0-9a-f]{40}",
        lock["patch_origin"],
    ):
        raise ValueError("patch origin must identify a reviewed Groq commit")
    for key, image in (("imager", "imager"), ("installer_base", "installer-base")):
        pattern = (
            re.escape(f"ghcr.io/siderolabs/{image}:{lock['upstream_tag']}@sha256:")
            + SHA256
        )
        if not re.fullmatch(pattern, lock[key]):
            raise ValueError(
                f"{key} must be digest-pinned to the selected Talos release"
            )
    if not isinstance(lock["extensions"], list) or not lock["extensions"]:
        raise ValueError("explicit extensions are required")
    names = []
    for extension in lock["extensions"]:
        if not isinstance(extension, str) or not extension:
            raise ValueError("extension references must be nonempty strings")
        match = re.fullmatch(
            r"ghcr.io/siderolabs/(amd-ucode|iscsi-tools):[a-zA-Z0-9_.-]+@sha256:"
            + SHA256,
            extension,
        )
        if not match:
            raise ValueError("unsupported or unpinned extension")
        names.append(match[1])
    if sorted(names) != ["amd-ucode", "iscsi-tools"]:
        raise ValueError("each required extension must appear exactly once")
    return lock


def local_run(command, *, cwd=None):
    print("+", " ".join(map(str, command)), flush=True)
    subprocess.run(command, cwd=cwd, check=True, timeout=300)


def capture(command, *, cwd):
    return subprocess.check_output(command, cwd=cwd, text=True, timeout=60).strip()


def prepare(lock, source, run=local_run):
    # Never apply a patch to a user's checkout, reuse a partial tree, or reset it.
    source.mkdir(parents=True, exist_ok=False)
    run(["git", "init", "--quiet", str(source)])
    run(["git", "remote", "add", "origin", UPSTREAM], cwd=source)
    run(
        ["git", "fetch", "--depth=1", "origin", f"refs/tags/{lock['upstream_tag']}"],
        cwd=source,
    )
    run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=source)
    actual = capture(["git", "rev-parse", "HEAD"], cwd=source)
    if actual != lock["upstream_commit"]:
        raise ValueError(
            f"upstream tag moved: expected {lock['upstream_commit']}, got {actual}"
        )
    patch = str(ROOT / lock["patch"])
    run(["git", "apply", "--check", patch], cwd=source)
    run(["git", "apply", patch], cwd=source)
    # --check/apply intentionally do not use --3way or a fuzzy patch tool.
    run(["git", "diff", "--check"], cwd=source)


def imager_command(lock, source, destination, variant, kind):
    command = [
        "docker",
        "run",
        "--rm",
        "--platform=linux/amd64",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--mount",
        f"type=bind,src={destination},dst=/out",
    ]
    if variant == "ramdisk":
        for filename, target in (
            ("vmlinuz-amd64", "vmlinuz"),
            ("initramfs-amd64.xz", "initramfs.xz"),
        ):
            command += [
                "--mount",
                f"type=bind,src={source / '_out' / filename},dst=/usr/install/amd64/{target},readonly",
            ]
    elif variant != "standard":
        raise ValueError("unknown image variant")
    command += [
        lock["imager"],
        kind,
        "--arch=amd64",
        "--platform=metal",
        f"--base-installer-image={lock['installer_base']}",
    ]
    command += [f"--system-extension-image={image}" for image in lock["extensions"]]
    return command


def artifact_records(output):
    files = []
    for variant in ("standard", "ramdisk"):
        for name in ("vmlinuz-amd64", "initramfs-amd64.xz", "installer-amd64.tar"):
            path = output / variant / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
                raise ValueError(f"missing, empty, or symlinked artifact: {path}")
            files.append(
                {
                    "path": str(path.relative_to(output)),
                    "sha256": sha256(path),
                    "bytes": path.stat().st_size,
                }
            )
    return files


def normalize_imager_outputs(destination):
    # Exact names from profile.OutputPath with --platform=metal, --arch=amd64.
    (destination / "kernel-amd64").rename(destination / "vmlinuz-amd64")
    (destination / "initramfs-metal-amd64.xz").rename(
        destination / "initramfs-amd64.xz"
    )


def build(lock_path, workspace, gate):
    lock = read_lock(lock_path)
    gate.check()  # No build starts while the global switch is off.
    if sys.platform != "linux":
        raise ValueError(
            "candidate builds require a Linux amd64 Docker/BuildKit runner"
        )
    workspace.mkdir(parents=True, exist_ok=False)
    if shutil.disk_usage(workspace).free < 40 * 1024**3:
        raise ValueError("candidate build requires at least 40 GiB free disk space")
    source = workspace / "source"
    prepare(lock, source, run=gate.run_build_command)
    output = workspace / "artifacts"
    output.mkdir()
    build_revision = hashlib.sha256(
        (lock["upstream_commit"] + lock["patch_sha256"]).encode()
    ).hexdigest()
    make = [
        "make",
        "PLATFORM=linux/amd64",
        "ARCH=amd64",
        "PUSH=false",
        f"TAG={lock['candidate']}",
        f"ABBREV_TAG={lock['upstream_tag']}",
        f"SHA={build_revision[:12]}",
    ]
    # Upstream package/kernel versions come from the locked source's Makefile.
    # Do not rebuild a different kernel or reuse packages from our old fork main.
    gate.run_build_command(
        make
        + [
            "unit-tests",
            "TESTPKGS=github.com/siderolabs/talos/internal/app/machined/pkg/...",
        ],
        cwd=source,
    )
    for target in ("kernel", "initramfs", "talosctl-linux-amd64"):
        gate.run_build_command(make + [target], cwd=source)
    for variant in ("standard", "ramdisk"):
        destination = output / variant
        destination.mkdir()
        for kind in ("kernel", "initramfs", "installer"):
            gate.run_build_command(
                imager_command(lock, source, destination, variant, kind)
            )
        normalize_imager_outputs(destination)
    shutil.copy2(source / "_out/talosctl-linux-amd64", output / "talosctl-linux-amd64")
    records = artifact_records(output)
    if sha256(output / "standard/vmlinuz-amd64") != sha256(
        output / "ramdisk/vmlinuz-amd64"
    ):
        raise ValueError(
            "custom build kernel differs from the pinned upstream imager kernel"
        )
    tool = output / "talosctl-linux-amd64"
    records.append(
        {"path": tool.name, "sha256": sha256(tool), "bytes": tool.stat().st_size}
    )
    gate.check()
    manifest = {
        "schema_version": 1,
        "status": "built-unqualified",
        "production_approved": False,
        "automation_policy_revision": gate.revision,
        "pipeline_commit": capture(["git", "rev-parse", "HEAD"], cwd=ROOT),
        "release_lock_sha256": sha256(lock_path),
        "release": lock,
        "runtime_build_revision": build_revision[:12],
        "artifacts": records,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "SHA256SUMS").write_text(
        "".join(f"{record['sha256']}  {record['path']}\n" for record in records)
    )
    print(f"Built unqualified candidate: {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "prepare", "build", "gate"))
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    lock = read_lock(args.lock)
    if args.command == "validate":
        validate_policy(decode_json((ROOT / "groq/automation.json").read_bytes()))
        print(
            f"Validated lock and policy for {lock['candidate']}; no build or rollout authorized"
        )
    elif args.command == "gate":
        revision = None
        if args.manifest:
            revision = decode_json(args.manifest.read_bytes())[
                "automation_policy_revision"
            ]
            if not isinstance(revision, str) or not re.fullmatch(
                r"[0-9a-f]{40}", revision
            ):
                raise ValueError("invalid recorded automation policy revision")
        print(
            "Live gate permits this run at",
            Gate(expected_revision=revision).check().revision,
        )
    else:
        if not args.workspace:
            parser.error("--workspace is required and must not already exist")
        workspace = args.workspace.resolve()
        if args.command == "prepare":
            prepare(lock, workspace)
            print(
                "Source/patch check passed; no image built or production state changed"
            )
        else:
            build(args.lock.resolve(), workspace, Gate())


if __name__ == "__main__":
    try:
        main()
    except (Halted, ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"STOPPED: {error}", file=sys.stderr)
        sys.exit(1)
