# Groq Talos release automation

This is a **build-only foundation**, not permission to upgrade production.
There is no Kubernetes/Talos mutation client, repair writer, reboot operation,
registry publisher, automatic release promotion, or production credential here.
The first candidate is Talos 1.13.11. A successful build is not hardware qualification.

## Global kill switch

**Open a PR changing `groq/automation.json` on `groq/siderolabs-talos` main to
`"enabled": false`.** It is off by default. Updating the reason is recommended.

- Every controlled build step checks the **live main-branch policy**, not the
  checkout that started the job. PR branches cannot enable automation.
- A running build polls every 10 seconds, with a 5-second timeout per API call.
  Missing policy, deletion, invalid JSON, authentication failure, rate limiting,
  or network errors stop the run. There is no cached-allow fallback.
- The policy is pinned to its last modifying commit for the lifetime of a run.
  Any policy change stops the run; turning it back on does not resume old runs.
- Read-only checks, tests, and incident inspection remain usable while disabled.
- This is a **stop-further-work switch**, not an instantaneous emergency brake.
  Merge delay, GitHub propagation, API latency, and an already submitted operation
  remain. No PR-based switch can unsend a reboot or undo an OS installation.
- Local build clients are stopped on a kill-switch failure. Detached build work
  may finish; its output must not be published/promoted by a stopped run.

Future rollout code MUST use this same gate before acquiring repair, changing
provisioning, submitting each upgrade/reboot, releasing repair, or advancing to
another IEI, and during waits. A kill, lost credential, timeout, cancellation,
or CI restart MUST retain maintenance ownership/repair and record a stopped
state. No `finally`/exit cleanup may release repair, clear taints, or roll back.
Already-submitted production operations require read-only observation and durable
delivery receipts, not blind retries. Automatic release of repair is forbidden
until current-boot validation and ownership checks pass. Re-enabling the switch
must require a fresh approval and reconciliation of the recorded run state.

The switch is a cooperative interlock, not an authorization boundary: branch
protection, protected environments, minimal credentials, and runner access must
also be configured. Do not grant production credentials to this build workflow.

## Build workflow

`groq/releases/v1.13.11.json` pins the upstream tag **and commit**, the exact
reviewed RAMdisk patch and checksum, and upstream imager/base-installer/extension
image digests. A new release is a reviewed lock-file change, not `latest`.
Only stable 1.13 patch versions are admitted initially; adding another minor
requires an explicit compatibility review and code change.

PRs run tooling tests, validate the lock, fetch the exact upstream source, and
check patch application. They do not start container builds or acquire credentials.
Existing dirty Talos checkouts are never modified. Patch conflicts stop the job.

```sh
python3 -m unittest discover -s groq/tests -v
python3 groq/release.py validate
python3 groq/release.py prepare --workspace /tmp/talos-source-check
```

After merge, provision an **isolated, preferably ephemeral** Linux amd64 CI runner
with the `groq-talos-builder` label, Docker/BuildKit, git, make, Python 3.11+, at
least 40 GiB free disk, and outbound access to GitHub/GHCR. Measure build RAM/CPU
requirements during the first build before setting permanent runner sizing.
The unit-test target needs BuildKit's `security.insecure` entitlement; never run
this on a production node or a shared runner with production credentials. No
Kubernetes/Talos endpoint access, registry write credential, or new lab is needed.

Enable the switch by reviewed PR, then manually dispatch `groq talos release`
on `main` with `build_candidate=true`. The builder:

1. Re-checks the live switch and verifies the source/patch identity.
2. Runs relevant upstream machined tests and builds the patched initramfs and
   matching talosctl. It exports the matching packaged kernel, not a new kernel.
3. Uses a digest-pinned upstream imager to assemble **both** standard and RAMdisk
   kernel/initramfs/installer sets with the same pinned AMD microcode and iSCSI
   extensions. Raw custom assets are mounted into the imager for the RAMdisk set.
4. Requires both variants to have the same kernel hash and all outputs to exist.
5. Creates SHA256 checksums and a manifest marked `built-unqualified`, with
   `production_approved=false`, source/patch identity, and policy revision.
6. Rechecks that original policy revision before uploading a uniquely named
   Actions artifact. No GitHub Release, registry push, or deployment is performed.

The upstream Makefile supplies its versioned package/tool dependencies. The
initial pipeline does **not** promise bit-for-bit reproducibility of those
transitive inputs; complete dependency digest locking/provenance is follow-up
work before treating builds as reproducible production releases.

Actions artifacts expire after 30 days. **Do not point PXE at them.** Permanent,
verified publishing to an immutable internal location and provisioning changes
are a separate reviewed stage. No configuration or boot selector is changed by
this workflow. Image assembly, hardware boot behavior, and upgrade/recovery paths
still need successful CI execution and YKA1 qualification before use.

## Remaining rollout work (not implemented here)

1. Generate and validate matching provisioning/PXE changes from a qualified
   candidate manifest, preserving Kubernetes pins and unrelated site settings.
2. Discover IEIs intersecting YKA1 `c0r1`, record UID and full allocation, and
   reject unapproved cross-rack expansion. Scope by IEI, not rack names alone.
3. Integrate with existing repair/disruption ownership. Confirm actual workload
   quiescence, preserve allocation, and serialize the initial rollout globally.
4. Implement separate disk-installed and RAMdisk/PXE upgrade/recovery procedures.
5. Require fresh bring-up exit 0 on every allocated node for its current boot,
   expected OS/assets, node/network/storage health, and no conflicting repair.
   A listening TSPD SSH port or a previous boot's successful init is insufficient.
6. Release only this run's repair hold, then verify serving recovery before the
   next IEI. Restarted CI must resume from durable receipts, never start over.
7. Qualify YKA1 first, MSP1 next with separate approval. All other sites are
   denied unless explicitly admitted; **DMM1 is excluded** until its model/SLO
   policy is implemented and approved. Infra/control-plane upgrades are separate
   from IEI worker maintenance and require their own quorum/backup checks.

No lab or upstream RAMdisk feature project is required. Keep the small patch
for the remaining hardware lifetime; do not freeze security updates for it.
