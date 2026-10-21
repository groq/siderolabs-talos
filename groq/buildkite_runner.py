"""Buildkite-only preflight and manually approved, non-deploying candidates."""

import argparse
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import release
from automation_gate import Gate, GitHubPolicyReader, Halted, decode_json

ROOT = Path(__file__).resolve().parents[1]
SECRET = "GROQ_TALOS_GITHUB_READ_TOKEN"
DOCKER_HOST = "unix:///var/run/docker.sock"
BUILDKIT_IMAGE = (
    "moby/buildkit:v0.34.1@sha256:"
    "b651bf250c2805569f1db385ad1ea8fb73737dfc851de305f274ca02690fa93f"
)
MIN_DISK = 40 * 1024**3
FORBIDDEN_PREFIXES = (
    "AWS_",
    "AZURE_",
    "ARM_",
    "GOOGLE_",
    "GCLOUD_",
    "GCP_",
    "VAULT_",
    "KUBE",
    "TALOSCONFIG",
    "SSH_AUTH_SOCK",
    "GROQKITE_",
    "BUILDKITE_S3_",
    "BUILDKITE_GS_",
    "BUILDKITE_ARTIFACT_UPLOAD_DESTINATION",
    "BUILDKITE_TOKEN",
    "BUILDKITE_API_TOKEN",
    "BUILDKITE_GITHUB_TOKEN",
)
SENSITIVE = re.compile(r"TOKEN|PASSWORD|SECRET|CREDENTIAL|PRIVATE_KEY|ACCESS_KEY")
CREDENTIAL_FILES = (
    ".aws/credentials",
    ".aws/config",
    ".config/gcloud/application_default_credentials.json",
    ".config/gcloud/credentials.db",
    ".kube/config",
    ".talos/config",
    ".netrc",
)


def credential_findings(env, home):
    """Report names only, never values. This is not an IAM/network audit."""
    findings = []
    for key, value in env.items():
        if not value:
            continue
        # Namespace injects this into the hosted job, even with a clean image.
        # Inventory it separately; candidate execution still requires a reviewed
        # isolation receipt. Never read the file or pass it to build commands.
        if key == "NSC_TOKEN_FILE":
            continue
        if key.startswith(FORBIDDEN_PREFIXES) or (
            SENSITIVE.search(key) and not key.startswith("BUILDKITE_")
        ):
            findings.append(f"environment:{key}")
    for key in (
        "BUILDKITE_AGENT_DEBUG_HTTP",
        "BUILDKITE_AGENT_TRACE_HTTP",
        "BUILDKITE_AGENT_SECRET_GET_SKIP_SECRET_REDACTION",
    ):
        if env.get(key, "false").lower() not in ("", "false", "0"):
            findings.append(f"unsafe-agent-option:{key}")
    if env.get("BUILDKITE_ADDITIONAL_HOOKS_PATHS"):
        findings.append("custom-agent-hooks")
    if Path("/custom/hooks/environment").exists():
        findings.append("shared-agent-credential-hook")
    for name in CREDENTIAL_FILES:
        if (home / name).exists():
            findings.append(f"credential-file:{name}")
    return sorted(findings)


def child_environment(home, builder=None):
    """Upstream commands get neither CI agent tokens nor the policy token."""
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "DOCKER_HOST": DOCKER_HOST,
        "DOCKER_CONFIG": str(home / ".docker"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "BUILDX_CONFIG": str(home / ".buildx"),
    }
    if builder:
        env["BUILDX_BUILDER"] = builder
    return env


def capture(command, *, env=None, cwd=None):
    result = subprocess.run(
        command,
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        # Do not echo arbitrary child output, which may contain authentication data.
        raise Halted(f"preflight command failed: {command[0]}")
    return result.stdout.strip()


def validate_resources(system, machine, free_bytes, docker_info):
    if system != "Linux" or machine not in ("x86_64", "amd64"):
        raise Halted("Linux amd64 is required")
    if free_bytes < MIN_DISK:
        raise Halted("at least 40 GiB of free workspace disk is required")
    if docker_info.get("OSType") != "linux" or docker_info.get("Architecture") not in (
        "x86_64",
        "amd64",
    ):
        raise Halted("the explicit local Docker endpoint must be Linux amd64")
    if docker_info.get("NCPU", 0) < 8 or docker_info.get("MemTotal", 0) < 16 * 1024**3:
        raise Halted("Docker host does not meet the large-queue resource floor")


def preflight():
    findings = credential_findings(os.environ, Path.home())
    if findings:
        raise Halted("credential isolation preflight failed: " + ", ".join(findings))
    for tool in ("git", "make", "docker", "buildkite-agent"):
        if not shutil.which(tool):
            raise Halted(f"missing prerequisite: {tool}")
    with tempfile.TemporaryDirectory(prefix="talos-preflight-", dir=ROOT) as tmp:
        env = child_environment(Path(tmp))
        info = json.loads(
            capture(["docker", "info", "--format", "{{json .}}"], env=env)
        )
        buildx = capture(["docker", "buildx", "version"], env=env)
        free = shutil.disk_usage(tmp).free
        validate_resources(platform.system(), platform.machine(), free, info)
    report = {
        "status": "read-only-preflight-passed",
        "workspace_free_bytes": free,
        "docker_cpus": info["NCPU"],
        "docker_memory_bytes": info["MemTotal"],
        "docker_server_version": info.get("ServerVersion"),
        "buildx": buildx,
        "credential_indicators_found": False,
        "platform_identity_indicators": ["NSC_TOKEN_FILE"]
        if os.environ.get("NSC_TOKEN_FILE")
        else [],
        "candidate_isolation_review_approved": isolation_review()["approved"],
        "privileged_buildkit_smoke_tested": False,
        "production_access_audited": False,
        "note": "No image build or privileged container started; IAM/network review remains mandatory.",
    }
    print(json.dumps(report, indent=2), flush=True)
    return report


def isolation_review():
    review = decode_json((ROOT / "groq/buildkite-isolation.json").read_bytes())
    if (
        not isinstance(review, dict)
        or set(review) != {"schema_version", "approved", "review_reference"}
        or type(review["schema_version"]) is not int
        or review["schema_version"] != 1
        or type(review["approved"]) is not bool
        or not isinstance(review["review_reference"], str)
        or (
            review["approved"] and not review["review_reference"].startswith("https://")
        )
    ):
        raise Halted("invalid Buildkite isolation review receipt")
    return review


def require_isolation_review():
    review = isolation_review()
    if not review["approved"]:
        raise Halted("hosted runner credential/IAM isolation review is not approved")
    return review


def validate_candidate_context(env):
    required = {
        "BUILDKITE": "true",
        "BUILDKITE_ORGANIZATION_SLUG": "groq",
        "BUILDKITE_PIPELINE_SLUG": "siderolabs-talos",
        "BUILDKITE_BRANCH": "main",
        "BUILDKITE_SOURCE": "ui",
        "BUILDKITE_PULL_REQUEST": "false",
        "BUILDKITE_STEP_KEY": "groq-talos-candidate",
        "GROQ_TALOS_BUILD": "candidate",
    }
    if any(env.get(k) != v for k, v in required.items()):
        raise Halted("candidate requires the canonical manually requested main build")
    if env.get("BUILDKITE_TAG") or env.get("BUILDKITE_RETRY_COUNT", "0") != "0":
        raise Halted(
            "tags and retries cannot reuse candidate approval; start a new build"
        )
    if not re.fullmatch(r"[0-9a-f-]{36}", env.get("BUILDKITE_UNBLOCKER_ID", "")):
        raise Halted("candidate requires a fresh manual unblock")
    if not re.fullmatch(r"[0-9a-f]{40}", env.get("BUILDKITE_COMMIT", "")):
        raise Halted("candidate must identify an immutable checkout commit")
    if not re.fullmatch(r"[0-9a-f-]{36}", env.get("BUILDKITE_JOB_ID", "")):
        raise Halted("candidate needs a valid job identity")


def policy_reader():
    result = subprocess.run(
        ["buildkite-agent", "secret", "get", "--format=json", SECRET],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise Halted("repository-scoped read-only policy credential is unavailable")
    try:
        data = json.loads(result.stdout)
        if not isinstance(data, dict) or len(data) != 1:
            raise ValueError("unexpected secret response")
        key, token = next(iter(data.items()))
        if key.upper() != SECRET or not isinstance(token, str) or not token.strip():
            raise ValueError("invalid credential")
    except (ValueError, AttributeError):
        raise Halted("invalid policy credential response") from None
    # Keep it in this process, not an environment variable inherited by builds.
    return GitHubPolicyReader(token=token.strip())


def require_current_main(reader, expected, actual):
    if actual != expected:
        raise Halted("checkout does not match the approved build commit")
    try:
        current = reader.request("commits/main")["sha"]
    except (OSError, ValueError, KeyError, TypeError, Halted):
        raise Halted("cannot verify canonical main") from None
    if current != expected:
        raise Halted("main changed; start a new manually approved build")


def builder_create_command(name):
    return [
        "docker",
        "buildx",
        "create",
        "--name",
        name,
        "--driver",
        "docker-container",
        "--driver-opt",
        f"image={BUILDKIT_IMAGE}",
        "--buildkitd-flags",
        "--allow-insecure-entitlement security.insecure",
        "--use",
        DOCKER_HOST,
    ]


def upload_candidate(output, gate):
    manifest = json.loads((output / "manifest.json").read_text())
    if (
        manifest.get("status") != "built-unqualified"
        or manifest.get("production_approved") is not False
        or manifest.get("automation_policy_revision") != gate.revision
    ):
        raise Halted("candidate manifest/policy receipt is invalid")
    records = release.artifact_records(output)
    tool = output / "talosctl-linux-amd64"
    if tool.is_symlink() or not tool.is_file() or not tool.stat().st_size:
        raise Halted("missing candidate talosctl")
    records.append(
        {
            "path": tool.name,
            "sha256": release.sha256(tool),
            "bytes": tool.stat().st_size,
        }
    )
    if records != manifest.get("artifacts"):
        raise Halted("candidate artifact receipt mismatch")
    checksums = "".join(f"{r['sha256']}  {r['path']}\n" for r in records)
    if (output / "SHA256SUMS").read_text() != checksums:
        raise Halted("candidate checksum list mismatch")
    # No automatic artifact_paths, recursive globs, registry push or release.
    paths = ["manifest.json", "SHA256SUMS"] + [r["path"] for r in records]
    gate.run_build_command(
        ["buildkite-agent", "artifact", "upload", ";".join(paths)], cwd=output
    )


def candidate():
    validate_candidate_context(os.environ)
    preflight()  # Recheck on THIS worker; the preceding job ran on another VM.
    require_isolation_review()  # Cannot bypass pending review by enabling builds.
    reader = policy_reader()
    gate = Gate(reader)
    gate.check()  # Stop before privileged setup if the global switch is off.
    expected = os.environ["BUILDKITE_COMMIT"]
    actual = capture(["git", "rev-parse", "HEAD"], cwd=ROOT)
    require_current_main(reader, expected, actual)
    name = "talos-" + os.environ["BUILDKITE_JOB_ID"]
    with tempfile.TemporaryDirectory(prefix="talos-candidate-", dir=ROOT) as tmp:
        root = Path(tmp)
        home = root / "home"
        home.mkdir()
        env = child_environment(home, name)
        gate.command_env = env
        try:
            gate.run_build_command(builder_create_command(name))
            gate.run_build_command(["docker", "buildx", "inspect", name, "--bootstrap"])
            # The upstream unit tests exercise security.insecure. Read-only
            # preflight deliberately does not claim that entitlement is tested.
            release.build(release.DEFAULT_LOCK, root / "build", gate)
        finally:
            # Only this job's local builder. Never prune shared builders/caches or
            # alter production state, even when the global gate is latched stopped.
            cleanup = subprocess.run(
                ["docker", "buildx", "rm", "--force", name],
                env=env,
                capture_output=True,
                timeout=60,
                check=False,
            )
            if cleanup.returncode:
                raise Halted("job-local builder cleanup failed; no artifacts uploaded")
        require_current_main(
            reader, expected, capture(["git", "rev-parse", "HEAD"], cwd=ROOT)
        )
        publication = Gate(reader, expected_revision=gate.revision)
        publication.check()
        upload_candidate(root / "build/artifacts", publication)


def interrupted(signum, frame):
    raise Halted("CI interrupted; no further build or publication authorized")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "candidate"))
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    if args.command == "preflight":
        preflight()
    else:
        candidate()


if __name__ == "__main__":
    try:
        main()
    except (Halted, ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"STOPPED: {error}", file=sys.stderr)
        sys.exit(1)
