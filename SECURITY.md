# Security policy

## Supported releases and reporting

Security fixes target the latest published alpha release; older tags and
unreleased forks are not supported. `0.1.0a1` is currently in preparation, not a
claim that a release has been published. This is a best-effort, single-maintainer
project with no response-time or remediation SLA.

Report vulnerabilities privately through
[GitHub's private vulnerability reporting](https://github.com/aaronspindler/pandect/security/advisories/new).
If that page is unavailable, do not open a public issue containing exploit or
sensitive details. Contact the maintainer through the contact information on
their [GitHub profile](https://github.com/aaronspindler) to arrange a private
channel, or wait until private reporting is enabled. Private reporting is a
publication prerequisite, not an invitation to send investigation evidence.

Include the affected tag/commit, host/container architecture, a concise impact
description, and reproduction steps using synthetic material. Redact tokens,
names, paths, metadata, and logs. Do not attach databases, originals, identity
features, backups, `.env`, or `secret.key`. Coordinate disclosure with the
maintainer; do not test against another person's installation.

## Intended security boundary

Pandect runs on a trusted machine for one trusted operator. There is no login,
authorization, tenancy, or public-network deployment mode. Keep the published
port on loopback. A reverse proxy, changed host allowlist, or TLS alone does not
make an internet-facing installation supported. Host users with Docker access
may control containers and read application data.

The media runner is a defense-in-depth boundary for untrusted decoding and
inference. It has no IP network or application-data/credential mounts, fixed
operations, one bounded job at a time, and disposable unprivileged parser
identities. See [its architecture](docs/architecture/overview.md#media-boundary).
It is not a virtual machine or proof against kernel/runtime vulnerabilities.
The trusted web and worker still access databases and evidence, and the worker
can contact configured remote services. Keep Docker, the host, and locked
dependencies patched. Do not disable isolation to make a failed file process.

## Evidence and privacy

- Files, SQLite databases, embeddings, transcripts, and backups are plaintext.
  Use host/disk encryption and an encrypted backup destination; gzip is not
  encryption. Restrictive permissions do not protect an unlocked or compromised
  host. Temporary files and swap may also retain sensitive material.
- Original files remain canonical; derived artifacts and interpretations are
  separately recorded. ORM append-only and protected-deletion controls do not
  stop the trusted operator editing SQLite or files directly. Hashes support
  consistency checks, not external attestation or a complete chain of custody.
- Local operation does not automatically download models, send telemetry, or
  upload investigation content. Initial builds, explicit model provisioning,
  dependency scans, and explicitly requested remote processing use the network.
- Cloudflare processing is disabled by default. Consent binds exact inputs,
  recipients, models, and requests. Originals may contain location or other
  hidden metadata. Read the manifest, provider terms, and
  [transfer details](docs/processing.md#optional-cloudflare-transfer) before
  accepting. Disabling Gateway payload logging/cache is not a guarantee of
  zero provider retention; billing and operational metadata may remain.
- Model outputs and identity similarities may be wrong or biased. Review them
  against original evidence. The project does not establish training-data
  rights, consent, fitness for high-stakes decisions, or commercial clearance.

Use `make check` for hermetic tests and `make security-check` for explicit
network-enabled vulnerability/secret scans. Passing scans does not establish
that software is vulnerability-free. Publication also requires the manual
review and capacity gates in [the release checklist](docs/releasing.md).
