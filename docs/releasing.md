# Release checklist

This repository prepares `0.1.0a1` / `v0.1.0a1` as a source-and-Compose alpha.
Do not treat this checklist, the changelog, or a version bump as proof that a
release has passed validation or been published. Keep the repository private
until preparation is complete and the owner separately authorizes publication.
Do not push a tag or publish a release as a side effect of another check.

Current preparation evidence is recorded in the
[2026-09-24 working-tree validation snapshot](releases/validation-2026-09-24.md).
Its pending gates remain pending until explicitly rerun and verified.

## Local acceptance record

Record the reviewed commit, commands, dates, architectures, environment limits,
outcomes, and remaining blockers in the release review. Use only synthetic
media and sanitized logs. Every box below must have evidence before release:

- [ ] Review all changes and preserve existing operator data/configuration.
  Ensure `.codex/config.toml` is untracked but its local content is retained.
- [ ] Run complete `make check` and successful Linux amd64/arm64 CI, including
  lock freshness, migrations, runtime builds, and native imports.
- [ ] Run `make security-check` on the exact candidate; no applicable unresolved
  audit finding, expired exception, or genuine secret may remain.
- [ ] Verify a clean source checkout can set up, become healthy, execute a real
  queued task, and operate locally without model weights or Cloudflare.
- [ ] Verify intake, all media/derived-media processing paths, model diagnostics,
  and retry splitting use the isolated runner with no direct fallback.
- [ ] Pass adversarial isolation tests: network/filesystem/control-socket denial,
  escaped descendants, unsafe file trees, malformed protocol data, disconnects,
  memory/process/scratch/inode/output limits, and successful recovery afterwards.
- [ ] On a 16 GiB host / 12 GiB Docker Desktop allocation, exercise representative
  large image/audio/video media, the exact 2 GiB file and 8 GiB batch boundaries,
  decoding expansion, low disk, and useful busy/failure messages. Record peaks
  and elapsed times. Tiny-limit unit fixtures are not this capacity test.
- [ ] With explicit model-license acceptance, provision pinned weights and run
  `make model-smoke` on supported architectures. Do not silently download weights
  in ordinary tests or substitute stub-model success for real inference.
- [ ] Test stopped-stack backup/restore end to end with synthetic originals,
  derivatives, both databases, secret, and model notices. Verify hashes,
  permissions, and the matching-revision rollback procedure.
- [ ] Verify protected parent deletion preserves immutable records/files and
  unprocessed catalogue items retain the intended deletion behavior.
- [ ] Verify remote processing against a fake gateway: current consent required,
  source/contract changes invalidate consent, and failures do not expose payloads
  or credentials. A real remote check is separately opt-in and may cost money.
- [ ] Reconcile README, configuration, command help, changelog, model legal
  inventory, and actual tested behavior. No unverified safety/accuracy claim.

## Repository and publication review

- [ ] Review all refs intended to become public, not just `main`, plus issues,
  PRs, Actions logs, attachments/artifacts, release drafts, and repository
  metadata. Redact/remove sensitive content deliberately; do not indiscriminately
  delete history or branches. Preserve the approved author metadata/history.
- [ ] Require PRs and successful CI on `main`; block force pushes and branch
  deletion. Do not require a second review while there is only one maintainer.
- [ ] Confirm automated dependency updates, pinned least-privilege workflows,
  Python code scanning where eligible, secret scanning, and push protection.
  Verify availability after visibility changes; record any unavailable control
  rather than claiming it is enabled.
- [ ] Enable and test private vulnerability reporting before inviting public
  reports. Confirm the link in `SECURITY.md` is usable.
- [ ] Obtain explicit owner authorization to change visibility and separately
  publish the reviewed commit as tag `v0.1.0a1` and a GitHub **prerelease**.
- [ ] Use the prepared [release notes](releases/0.1.0a1.md), replacing their
  draft banner and validation placeholders only with actual results.
- [ ] Verify source/tag install links after publication. Do not publish to PyPI
  or a container registry under this release workflow.

If any required check cannot run, leave the release blocked and state the exact
unverified boundary. No branch protection or tag publication should conceal a
failed test through an undocumented bypass.
