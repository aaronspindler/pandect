## What and why

Describe the operator need, focused change, and related issue.

## Validation

List commands run and results, including `make check`. State checks not run and
their exact limitations; distinguish unit tests from runtime/capacity checks.

## Review checklist

- [ ] Tests cover meaningful behavior and failure paths; migrations are included where needed.
- [ ] Documentation matches configuration, operation, and security changes.
- [ ] Originals and immutable evidence remain preserved; new native parsing uses the isolated runner.
- [ ] No investigation evidence, credentials, personal paths, or unredacted logs are included.
- [ ] I have the right to contribute this material under Apache-2.0 and retained third-party notices.

Report suspected vulnerabilities through `SECURITY.md`, not this public PR.
