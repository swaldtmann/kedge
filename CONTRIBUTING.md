# Contributing — Kedge

Thanks for your interest in Kedge! Contributions are welcome.

## How to Contribute

This repository is a read-only mirror; the source of truth lives elsewhere.
There is **no public issue tracker**. Bug reports, feature ideas and patches go
by e-mail to **security@waldtmann.de** (the same address as in
[SECURITY.md](SECURITY.md); for vulnerabilities, follow that file instead).

1. **Report or discuss** — Found a bug or have a feature idea? Send an e-mail first.
2. **Prepare a change** — Create a feature branch (`feature/short-description` or
   `fix/short-description`), write code, add tests.
3. **Send the patch** — Attach the output of `git format-patch` (or a link to your
   branch) and describe what and why.

## Git Conventions

### Branches

`feature/short-description` or `fix/short-description`

### Commit Messages

A short, imperative subject line, optionally prefixed with a type such as
`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `ci:` or `release:`
(optionally with a scope, e.g. `docs(readme):`). The body explains the why.

## QA

Test the full backup → restore cycle before submitting:

```bash
# Roundtrip test on Hetzner Cloud (creates ephemeral boxes)
export HCLOUD_TOKEN=<your-token>
./test.sh
```

For smaller changes, verify with `discover` + `backup` + `restore` on a local stack.

## Release Checklist

Before tagging a new release:

1. Update `CHANGELOG.md` — move Unreleased items to the new version
2. Update `SECURITY.md` — Supported Versions table matches the new release
3. Create tag: `git tag v<version>`
4. Push the tag (the public mirror follows the source of truth)
5. Verify: tag, CHANGELOG, SECURITY.md all consistent

## What We Don't Accept

- Changes that violate privacy principles
- Dependencies on proprietary cloud services
- Code without tests (for new features)

## License

By contributing, you agree that your contributions will be licensed under the Apache-2.0 License (see [LICENSE](LICENSE)).
