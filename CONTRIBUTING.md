# Contributing — Kedge

Thanks for your interest in Kedge! Contributions are welcome.

## How to Contribute

1. **Open an issue** — Found a bug or have a feature idea? Open an issue on Codeberg first.
2. **Fork + branch** — Fork the repo, create a feature branch (`feature/short-description`).
3. **Make changes** — Write code, add tests.
4. **Pull request** — Open a PR against `main`. Describe what and why.

## Git Conventions

### Branches

`feature/short-description` or `fix/short-description`

### Commit Messages

Conventional Commits: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`

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
4. Create Codeberg Release with CHANGELOG excerpt
5. Verify: tag, release, CHANGELOG, SECURITY.md all consistent

## What We Don't Accept

- Changes that violate privacy principles
- Dependencies on proprietary cloud services
- Code without tests (for new features)

## License

By contributing, you agree that your contributions will be licensed under the Apache-2.0 License (see [LICENSE](LICENSE)).
