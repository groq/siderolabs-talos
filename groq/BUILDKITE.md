# Buildkite candidate builder

## Scope and current state

This uses the existing `siderolabs-talos` pipeline, `github` Buildkite cluster,
and hosted `large` queue. No runner registration, queue change, additional VM,
cloud IAM change, or production access is required by this PR. Hosted compute
is still billed; this is reuse of the existing CI service, not free compute.

`groq/automation.json` remains disabled. PRs/merges run validation and a
**non-building preflight** only. Candidate builds are UI/main-only and require
an explicit block approval plus the live main policy. GitHub Actions no longer
has a candidate job, avoiding two independent candidate launch paths.

The pinned `python:3.12.11-bookworm` amd64 job image supplies Python, Git, Make,
Bash and CA certificates. Buildkite layers its agent and Docker tools onto this
image. The job-specific `image` overrides the shared `agent-github` image, which
contains a Groq credential-broker hook. Neither the queue default nor other
pipelines are changed. Repository checkout uses Buildkite's native GitHub access;
if it fails, have the CI owner verify that integration, not copy the shared broker.

## What preflight proves (and does not prove)

Preflight checks Linux amd64, required binaries, the explicit local Docker
socket, Docker host architecture/CPU/memory, Buildx availability, at least
40 GiB free on the workspace filesystem, and known cloud/cluster/broker
credential environment variables and files. It prints names, never secret values.
Unexpected credentials or additional image hooks stop the job.

The first live clean-image preflight found `NSC_TOKEN_FILE`, which Namespace uses
as a token-file location for its runtime identity. Its value/content was not read.
Preflight reports that platform identity separately rather than calling the worker
credential-free. Its effective scope is **not yet reviewed**. Candidate execution
is additionally blocked by `groq/buildkite-isolation.json` (`approved: false`),
even if someone enables the global switch. This is a one-time reviewed readiness
receipt, not a substitute for the global kill switch.

It does not start a builder/container, fetch the policy secret, build an image,
probe production endpoints, audit all IAM/network access, measure daemon-storage
free space, or prove `security.insecure` works. A fresh candidate repeats these
checks on its own worker. The first enabled candidate's upstream tests exercise
the privileged entitlement; passing preflight alone is not qualification.

The pipeline runs preflight on the `large` queue after normal validation. It is
safe to run while automation is off. A fresh main build without
`GROQ_TALOS_BUILD=candidate` runs preflight only, with no approval block/build.

## One-time CI-owner prerequisites (before enabling candidates)

1. Confirm that the `large` queue is still hosted/ephemeral, the per-step pinned
   image is honored, and native GitHub checkout works. Do not redirect this job
   to the shared GitLab/GCP agent pool or any inference/production host.
2. Review Buildkite pipeline permissions and secret policies. Approvers need
   pipeline write access; no broad `allowed_teams` override is added. Ensure
   untrusted PR jobs cannot obtain candidate secrets. Require normal protected
   main-branch review. Buildkite environment checks are interlocks, not a security
   boundary against someone allowed to replace the pipeline or run arbitrary code.
3. Provision `GROQ_TALOS_GITHUB_READ_TOKEN` in this Buildkite cluster's Secrets.
   Use underscores, not hyphens: Buildkite secret names permit letters, numbers,
   and underscores. The runner requests this fixed name; do not export the
   credential as a job environment variable.
   Use an approved CI identity, scoped **only** to `groq/siderolabs-talos`, with
   repository **Contents: read** (plus implicit Metadata: read). It needs to read
   commits and `groq/automation.json`; it must not have repository-write,
   administration, cloud, cluster, or registry-write privileges. Restrict the
   secret's access policy to this pipeline's approved main candidate job, not PRs
   or other pipelines. Verify effective policy with the CI/security owner. Set
   expiry/rotation; expiry or an unavailable credential stops builds safely.
4. Confirm the clean image has no production credentials or reachable privileged
   metadata identity, and that the job receives no deployment secrets/plugins.
   Review inherited agent hooks and Buildkite secret access, not just environment
   variables. Preflight's denylist cannot certify this on its own.
   Include the injected Namespace runtime identity and shared cache/registry
   scope in this review. Record the review URL and `approved: true` in
   `groq/buildkite-isolation.json` by reviewed PR only after these checks pass.
5. Review source/lock/patch identity and the first-build resource/time budget.
   Eight CPUs and 32 GiB RAM are a starting allocation, not a measured guarantee.
   Resolve blockers before merging a separate PR enabling the global switch.

No secret is created or read by validation/preflight. The candidate reads the
fixed secret through `buildkite-agent secret get` with built-in log redaction;
it holds the value in the supervisor process rather than exporting it. Public
upstream build commands get an allowlisted environment and an empty temporary
HOME/Docker configuration, excluding policy and Buildkite agent tokens.

## First enabled build

1. Merge the reviewed global-enable PR. Do not unblock an older build after
   changing main or enabling the policy; create a new build for the new commit.
2. In the `siderolabs-talos` Buildkite UI, choose **New Build**, branch `main`,
   and the explicit current main commit. Set `GROQ_TALOS_BUILD=candidate` in the
   build's environment. Ordinary UI builds with no flag only validate/preflight.
3. Wait for validation and preflight. Review their results and the exact commit,
   credential-isolation review, and enabled policy before manually unblocking
   **Approve unqualified Talos candidate**.
4. The candidate rechecks worker prerequisites, manual/main/approval context,
   and that its checkout is still current canonical main. It checks the live
   policy before any privileged builder setup. It creates a uniquely job-named,
   digest-pinned `docker-container` BuildKit on the local socket; it does not use
   the shared/remote default builder. Only `security.insecure` is enabled.
5. The original policy revision is checked before/after each build command and
   every ten seconds during waits. Stops latch. Cleanup removes only the owned
   builder, never shared builders/caches. Failed cleanup blocks artifact upload.
6. On success, the job verifies current main, the original policy receipt and
   every output checksum, and explicitly uploads the seven candidate files,
   manifest and checksum list. Upload is also guarded while waiting. An already
   submitted artifact upload may finish despite cancellation; its existence is
   never production approval. There is no automatic artifact upload on failure.

Candidates are globally serialized within this pipeline. Automatic and manual
job retries are disabled; start a fresh UI build with a new approval after a
failure, policy change, or CI interruption. Missing credential, ambiguous state,
changed main, changed policy or failed prerequisites stop instead of falling back
to local policy or an old artifact.

## After the first successful build

Inspect actual unit tests, resource use, matching kernels, extensions, checksums,
and the `built-unqualified` / `production_approved=false` manifest. No registry,
PXE, Talos, Kubernetes, repair, reboot, or inference operation is performed.
Permanent publishing and hardware qualification remain separate reviewed work.

The future multi-hop upgrade planner and production executor are not part of
this change. YKA1 qualification, MSP1 Brake exclusions, DMM1 exclusion, and the
separate GPC/control-plane procedures in README remain unchanged.
