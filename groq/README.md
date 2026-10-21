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

The repository's active Buildkite integration loads `.buildkite/pipeline.yml`.
It uses mise-pinned Python 3.12.11 and runs `bash groq/check.sh`: tooling tests,
lock validation, and a fresh exact-source/patch check. The GitHub Actions
validation job uses the same script if Actions is enabled. These checks do not
start container builds or acquire deployment credentials, and remain usable with
the global switch off. No check is skipped just to satisfy the CI status.
Existing dirty Talos checkouts are never modified. Patch conflicts stop the job.

```sh
bash groq/check.sh
```

Candidate execution uses the **existing Buildkite `github` cluster's `large`
queue** (Linux amd64, 8 vCPU / 32 GiB), not a new GitHub Actions runner. GitHub
Actions is validation-only. The preflight and candidate steps override the
shared agent image with a digest-pinned public Python/Debian image; they do not
inherit the shared image's embedded Groq credential-broker hook or change any
queue configuration. Buildkite supplies its agent and Docker tools.

PR and main builds run a non-building preflight for tooling, workspace disk,
Docker resources and known credential indicators. It stays usable with the
global switch off. **Passing preflight is not proof of IAM/network isolation or
privileged BuildKit support.** Candidate execution repeats it on its own worker.
The first actual build must still qualify image assembly and hardware behavior.

Before enabling, follow [Buildkite setup and operator procedure](BUILDKITE.md)
to review isolation and provision the restricted read-only GitHub credential.
Do not copy a personal/admin token or grant production/registry-write access.
Then enable the switch by reviewed PR, create a fresh UI build on `main` with
`GROQ_TALOS_BUILD=candidate`, and explicitly unblock the candidate approval step.
An ordinary merge, PR, schedule, API build, tag, or retry cannot start a candidate.
The builder:

1. Re-checks the live switch and verifies the source/patch identity.
2. Runs relevant upstream machined tests and builds the patched initramfs and
   matching talosctl. It exports the matching packaged kernel, not a new kernel.
3. Uses a digest-pinned upstream imager to assemble **both** standard and RAMdisk
   kernel/initramfs/installer sets with the same pinned AMD microcode and iSCSI
   extensions. Raw custom assets are mounted into the imager for the RAMdisk set.
4. Requires both variants to have the same kernel hash and all outputs to exist.
5. Creates SHA256 checksums and a manifest marked `built-unqualified`, with
   `production_approved=false`, source/patch identity, and policy revision.
6. Removes only its job-owned local BuildKit instance, verifies artifact checksums
   and the original policy receipt, then uploads only the enumerated candidate
   files to that Buildkite job. No automatic `artifact_paths`, GitHub Release,
   registry push, or deployment is performed.

The upstream Makefile supplies its versioned package/tool dependencies. The
initial pipeline does **not** promise bit-for-bit reproducibility of those
transitive inputs; complete dependency digest locking/provenance is follow-up
work before treating builds as reproducible production releases.

Buildkite artifacts are CI outputs subject to organization retention policy, not
durable release storage. **Do not point PXE at them.** Permanent,
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
7. Qualify YKA1 first. MSP1 requires separate approval and the Brake exclusions
   below; a general MSP1 approval never authorizes touching Brake nodes. All
   other sites are denied unless explicitly admitted; **DMM1 is excluded** until
   its model/SLO policy is implemented and approved. Infra/control-plane upgrades
   are separate from IEI worker maintenance and require their own quorum/backup
   checks.

### MSP1 Brake protection (required before any MSP1 rollout)

**Nodes running or reserved for Brake are excluded from the general rollout.**
They require a separate owner-reviewed maintenance procedure and explicit
approval. Neither a site-wide approval nor enabling the global switch overrides
this exclusion. This is a contract for the future rollout runner, not a claim
that a production exclusion mechanism exists in this build-only PR.

- Keep all MSP1 rollout actions blocked until a reviewed, authoritative Brake
  node inventory and dependency scope are established. Record stable node
  identities, not just rack names. The absence of a running pod or a label is
  not proof that a node is safe; a stopped Brake workload remains protected.
- Combine that protected inventory with fresh workload/allocation checks before
  acquiring repair and before each disruptive operation. Missing, stale,
  ambiguous, or conflicting evidence fails closed. Inventory or allocation
  changes invalidate the approved plan and require reconciliation, not a retry
  against stale node names.
- Check the **entire IEI allocation and maintenance group**, not just the selected
  rack or node. If any member is protected or cannot be classified safely, block
  the whole group before acquiring repair. Do not upgrade the remaining nodes
  of a mixed group or move/evict Brake merely to make that group eligible.
- Exclusion covers repair/disruption, cordon/drain/eviction, reboot, OS or
  firmware changes, and boot/provisioning configuration. Shared PXE defaults,
  boot selectors, or infrastructure changes that could affect Brake, including
  its next boot, require the separate Brake procedure too.
- If a conflict is discovered after work starts, stop further mutations and
  retain existing owned repair holds for operator reconciliation. Do not clear
  repair, automatically roll back, or blindly repeat an in-flight operation.
- Before enabling MSP1, add regression tests for protected nodes, mixed IEI
  allocations, unknown/stale inventory, allocation changes, and shared boot
  configuration impact. Verify that none can reach a mutation call. The Brake
  procedure must separately define owner approval, maintenance scope, serving
  health/SLO checks, recovery steps, and explicit stop conditions.

No lab or upstream RAMdisk feature project is required. Keep the small patch
for the remaining hardware lifetime; do not freeze security updates for it.
