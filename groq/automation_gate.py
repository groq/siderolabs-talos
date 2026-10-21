"""Fail-closed, live GitHub-main kill switch. Never reads a checkout's policy."""

import base64
import json
import os
import re
import signal
import subprocess
import time
import urllib.request
from dataclasses import dataclass

REPOSITORY = "groq/siderolabs-talos"
POLICY_PATH = "groq/automation.json"
POLL_SECONDS = 10
REQUEST_TIMEOUT_SECONDS = 5
MAX_RESPONSE_BYTES = 262144


class Halted(RuntimeError):
    """Stop automation; never release repair or attempt rollback on this error."""


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def decode_json(data):
    return json.loads(data, object_pairs_hook=unique_object)


def validate_policy(policy):
    if not isinstance(policy, dict) or set(policy) != {
        "schema_version",
        "enabled",
        "reason",
    }:
        raise ValueError("invalid automation policy fields")
    if type(policy["schema_version"]) is not int or policy["schema_version"] != 1:
        raise ValueError("unsupported automation policy schema")
    if type(policy["enabled"]) is not bool:
        raise ValueError("enabled must be a JSON boolean, not a string or number")
    if not isinstance(policy["reason"], str) or not policy["reason"].strip():
        raise ValueError("automation policy needs a reason")
    return policy


@dataclass(frozen=True)
class Snapshot:
    revision: str
    enabled: bool
    reason: str


class GitHubPolicyReader:
    """Read the last policy change on main, then that exact immutable revision.

    Using the path's commit rather than its blob detects an off/on cycle even
    when the file is restored byte-for-byte. Neither a PR branch nor a local
    environment variable can override the policy source.
    """

    def __init__(self, request=None, *, token=None):
        self.request = request or self._request
        self.token = token

    def _request(self, path):
        token = (
            self.token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        )
        if not token:
            raise Halted("missing read-only GitHub credential for live kill switch")
        request = urllib.request.Request(
            f"https://api.github.com/repos/{REPOSITORY}/{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "groq-talos-automation",
                "Cache-Control": "no-cache, no-store",
            },
        )
        with urllib.request.urlopen(
            request, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise Halted("oversized policy response")
        return decode_json(data)

    def __call__(self):
        # Cache-bust the mutable lookup; contents are then read at an exact SHA.
        commits = self.request(
            f"commits?sha=main&path={POLICY_PATH}&per_page=1&nonce={time.time_ns()}"
        )
        revision = commits[0]["sha"]
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("invalid policy revision")
        entry = self.request(f"contents/{POLICY_PATH}?ref={revision}")
        if entry.get("type") != "file" or entry.get("encoding") != "base64":
            raise ValueError("policy is not a regular base64-encoded file")
        # GitHub inserts newlines in base64 content.
        raw = base64.b64decode("".join(entry["content"].split()), validate=True)
        policy = validate_policy(decode_json(raw))
        return Snapshot(revision, policy["enabled"], policy["reason"])


class Gate:
    def __init__(self, reader=None, expected_revision=None, *, command_env=None):
        self.reader = reader or GitHubPolicyReader()
        self.revision = expected_revision
        self.halted = False
        self.command_env = command_env

    def check(self):
        if self.halted:
            raise Halted("run is latched stopped; start a new approved run")
        try:
            snapshot = self.reader()
            if not snapshot.enabled:
                raise Halted(f"automation disabled: {snapshot.reason}")
            if self.revision is not None and snapshot.revision != self.revision:
                raise Halted("automation policy changed; start a new approved run")
            self.revision = snapshot.revision
            return snapshot
        except Exception as error:
            self.halted = True
            if isinstance(error, Halted):
                raise
            # Do not echo HTTP response bodies or credentials into CI logs.
            raise Halted(
                f"live kill switch unavailable or invalid ({type(error).__name__})"
            ) from error

    def run_build_command(self, command, *, cwd=None):
        """Watch a LOCAL build command. Not a production operation executor.

        Cancel local build clients when disabled; detached BuildKit work may
        finish, but no next command/publication is authorized. Production steps
        need their own durable receipts and must not infer delivery from exit.
        """
        self.check()
        print("+", " ".join(map(str, command)), flush=True)
        process = subprocess.Popen(
            command, cwd=cwd, start_new_session=True, env=self.command_env
        )
        try:
            while True:
                try:
                    result = process.wait(timeout=POLL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    self.check()
            self.check()
            if result:
                raise subprocess.CalledProcessError(result, command)
        except BaseException:
            self.halted = True
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=5)
                except ProcessLookupError:
                    pass
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
            raise
