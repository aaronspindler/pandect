# Third-party notices

Pandect's original source and documentation are Apache-2.0, copyright 2026
Aaron Spindler. This does not relicense dependencies, optional weights, or
operator-supplied evidence.

## Vendored browser asset

HTMX **2.0.10** is served locally from
[`static/vendor/htmx/htmx-2.0.10.min.js`](../static/vendor/htmx/htmx-2.0.10.min.js).
Its exact upstream Zero-Clause BSD text remains in
[`static/vendor/htmx/LICENSE`](../static/vendor/htmx/LICENSE). Keep that file
when redistributing the vendored asset. No CDN request is required to use it.

## Installed software

[`pyproject.toml`](../pyproject.toml) and [`uv.lock`](../uv.lock) identify the
Python packages, including native runtimes, installed when an operator builds
the application. The Dockerfile also installs distribution packages such as
FFmpeg, ExifTool, and Tesseract. These components retain their upstream license
and copyright notices. Installed package metadata and `/usr/share/doc` inside
the built image provide corresponding license information.

Pandect distributes source and build instructions, not a prebuilt runtime
image. Anyone redistributing an image or dependency bundle must separately
review that exact bundle's notices and source-offer obligations; this page is
not a substitute for that review or a claim that every component is Apache-2.0.

## Optional models and remote services

No model weights are checked into the repository. Explicit provisioning
downloads pinned artifacts and exact upstream legal documents. See the
[artifact-specific model licensing inventory](model-licenses.md), the
source-controlled manifest, and the generated notice under your private model
directory. Preserve those notices and legal archives with the weights.

Cloudflare and model-provider terms apply separately to optional remote
processing. Any acceptance of Meta terms is an explicit operator action, not
an effect of installing Pandect or accepting Pandect's source license.
