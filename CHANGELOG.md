# Changelog

## 0.1.0a1 — Unreleased

First public-alpha candidate. The corresponding tag will be `v0.1.0a1`;
this entry does not mean a tag or GitHub release has been published.

- Source-and-Compose distribution under Apache-2.0, with third-party and
  artifact-specific optional-model license/provenance disclosures.
- Source-neutral catalogue for images, video, audio, messages, notes, and events;
  immutable original-media pointers and versioned processing results.
- Local extraction, explicit model provisioning, closed reference-gallery
  observations, and optional per-item-consented Cloudflare processing.
- Isolated native media execution, bounded upload/processing resources,
  protected evidence-parent relationships, and explicit failure states.
- Safer data-path setup, checkout-specific Compose identities, stopped-stack
  backup/restore, and separately network-enabled security validation.
- Locked dependency freshness, dual-architecture CI, secret/image/dependency
  scans, and public security/contribution guidance.

### Compatibility and migration

- Supported runtime remains one trusted local operator, Docker Compose 2.24+,
  and Linux amd64/arm64 containers. There is no authentication or public-network
  deployment support.
- Existing installations must stop the legacy `pandect` Compose project before
  setup introduces a checkout-specific project. Back up the entire data
  directory first; do not run two projects against the same SQLite stores.
  The legacy-transition procedure builds new backup tooling without migrating,
  and uses `BACKUP_REVISION` to retain the previous source revision for recovery.
- New migrations protect immutable evidence against deletion through parents;
  formerly accepted deletions can now raise a protected-deletion error.
- The new processing execution-policy version marks older runs as historical
  where their provenance no longer matches current processing. Existing results
  remain intact; an explicit rerun is optional and appends a new run.
- Setup/provisioning makes only digest-verified public model and license files
  readable to sandbox jobs. The surrounding data and annotated notices stay
  private. Unexpected public model-directory contents require operator review.
- Upload defaults are 2 GiB/file, 8 GiB/batch, 50 files, and a 2 GiB disk reserve.
  A file may still exceed decoding/scratch/output limits. One processing slot
  means intake requiring probing may return busy.
- Backup archives do not include `.env` or an explicit secret/token override;
  retain those separately and securely. Rollback restores the full backup
  together with its matching source revision, not an in-place database downgrade.

### Alpha limitations

No forensic certification, accuracy guarantee, evidence encryption, open-world
identity resolution, automatic source imports, search, or multi-user support.
Model output is uncertain and may be biased. Optional-model licensing does not
establish commercial clearance or training-data rights. Publication requires
all gates in [the release checklist](docs/releasing.md), including explicit
capacity and real-model checks that unit tests do not replace.
