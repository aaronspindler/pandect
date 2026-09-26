# Public-alpha validation — 2026-09-24

**Local implementation checks passed; not release approval.** This records
checks against the uncommitted readiness working tree based on
`85c28f7ecd8b7c9a1fc3c0d63a11c93c984748f1`, not a published tag or an immutable
release candidate. Rerun required checks on the final reviewed commit. The
repository remains private, with no release tag or published release.

## Implementation validation snapshot

The local Docker server is Linux arm64. The latest implementation-run results
reported for this working tree are:

| Gate | Current result and boundary |
| --- | --- |
| `make check` | **Passed: 896 tests**, with Ruff, Django checks, migration drift checks, Compose validation, builds, and native-runtime imports. This count is a working-tree snapshot; later changes require another complete run. |
| Security scans | **Passed**: redacted scanning of 15 reachable commits and 2.38 MB of current source; `uv audit --locked` checked 33 packages; Trivy checked runtime (311 OS packages plus Python) and maintenance (97 OS packages plus Python). No blocking findings or advisory exceptions. This does not establish absence of vulnerabilities. |
| Built-image isolation | **Passed** the actual adversarial harness against the built image, without a source bind overlay. Coverage is the checked-in harness, not proof against every parser/kernel attack. |
| Clean Compose startup | **Passed** clean setup, health/readiness, real queue/task execution, nine native-operation smoke cases, and operation with an empty optional-model directory. |
| Backup/restore integration | **Passed** the complete stopped-stack round trip in `make compose-smoke`: exact synthetic file hashes, both databases, generated secret, originals, derivatives, and model notices preserved; restored services became healthy and executed another queued task. Verification inspects separate private SQLite/WAL copies, never the bytes being restored. No migrations were applied by restore. |
| Full-size capacity | **Pending**. The current Docker Desktop allocation is 8 GiB, below the specified 12 GiB allocation on a 16 GiB host. Full 2 GiB/file and 8 GiB/batch representative-media acceptance has not been established. |
| Real optional-model inference | **Pending**. Full pinned weights are not provisioned in this validation environment. No download/license acceptance was performed implicitly; synthetic/stub tests and empty-model checks do not substitute for the explicit real-model smoke gate. |
| Final amd64 and arm64 CI | **Pending** for these changes. Local arm64 checks and historical CI do not establish fresh dual-architecture CI on the final candidate. Running hosted CI requires an authorized push. |

The final local commands were `COMPOSE_PROJECT_NAME=pandect-alpha-validation
make check`, `./scripts/compose-smoke.sh`, and `make security-check`, all with
exit status zero. Integration tests used disposable synthetic data, not the
operator's existing evidence, credentials, or model weights. Temporary smoke
containers, volumes, image tags, and fixture directories were removed; shared
build caches and the dedicated local validation images were retained.

The read-only-WAL and sidecar-preservation failures found during integration
were repaired and rerun successfully. Keep the
[release checklist](../releasing.md) authoritative: full-capacity tests,
explicit real-model tests, final dual-architecture CI, and owner publication
review remain open. This snapshot must not turn an unverified boundary into a
release-ready claim.

## Read-only publication-surface inventory

The following GitHub/Git surfaces were enumerated and screened without changing
visibility, refs, issue content, logs, artifacts, or releases:

| Surface | Observed scope |
| --- | --- |
| Repository | Private; default branch `main`. No wiki, Pages site, or discussions enabled. |
| Remote refs | Branches `main` and `agent/implement-foundation`; PR-head refs for PRs 12, 13, and 28. No remote tags. A stale local remote-tracking branch is not a current remote branch. |
| Git history | 15 locally reachable commits, 335 objects, and 226 blobs (about 2.2 MB) screened. The otherwise nonlocal PR 28 head tree had 83 files; all its blobs were already available in the screened local history. Relevant PR commit messages were also screened. |
| Issues and PRs | 25 issues (13 open, 12 closed), three closed PRs, two issue/PR comments, no review comments, and no review bodies. |
| Actions | All 17 completed run log sets read: 1,563,750 bytes / 12,311 lines. No retained Actions artifacts reported. |
| Releases | No draft/published releases, release assets, or release attachments reported. |

The bounded text screening looked for recognizable credential/private-key
formats, personal machine paths, email addresses where relevant, and GitHub
attachment URLs. Historical file inventory was also checked for obvious
database, secret-file, and media/archive paths. No new actionable pattern match
was found in the screened issue/PR/log content or historical file inventory.

Three historical `.codex/config.toml` blobs retain the already-approved personal
machine path. Human author contact metadata remains in Git history, as agreed.
Untracking current personal configuration does not remove that history, and no
history rewrite was performed. Actual path/contact values and any raw log
content are deliberately not copied into this report.

**This is not comprehensive confidential-content clearance.** Pattern searches
can miss arbitrary credentials, names, case details, copyrighted material,
nonstandard attachment links, and contextual sensitivity. The owner must review
the intended public material before changing visibility. Repeat the inventory
if refs, discussions, attachments, logs, or releases change before publication.

## Repository controls and publication boundary

Read-back verification of `main` protection showed:

- PR-based changes, strict required checks, and administrator enforcement;
- required `Test, lint, and validate (amd64)`,
  `Test, lint, and validate (arm64)`, and
  `Secret, dependency, and image audit` checks;
- force pushes and branch deletion blocked, conversation resolution required;
- zero mandatory approving reviews, avoiding a second-maintainer requirement.

The following controls could not be enabled for the current private repository
with the available GitHub configuration/entitlement:

| Control | Observed API result | Required follow-up |
| --- | --- | --- |
| Secret scanning and push protection | HTTP 422 | Recheck eligibility and enable/verify after authorized publication. |
| Private vulnerability reporting | HTTP 404 | Enable and test the reporting link when available before inviting reports. |
| CodeQL/default code scanning setup | HTTP 403 | Verify eligibility and the checked-in public-repository workflow after an authorized push/visibility change. |

Those responses are recorded as unavailable/unverified, not as enabled security
features. Checked-in workflow/configuration files also do not prove that they
have run on GitHub. No attempt was made to bypass a plan or permission limit.

Remaining owner actions include reviewing this working tree, authorizing any
commit/push needed for final CI, resolving the outstanding capacity/model
gates, reviewing publishable content, and separately authorizing
repository visibility, tag `v0.1.0a1`, and GitHub prerelease publication. Do not
publish a PyPI package or prebuilt container image under this release plan.
