# Summary

Describe the problem, the user-visible result, and why this is the smallest sufficient change.

# Spec

- Issue: <!-- #<number>, or N/A for a direct change -->
- [ ] Acceptance criteria and explicit non-goals remain current.

# Verification

List exact commands and results. Check every command actually run:

- [ ] `uv sync --frozen` then `uv run pytest backend/tests -q`
- [ ] Focused backend/contract test: <!-- command -->
- [ ] Manual verification when startup, storage, retention, media, or exports changed

# Impact

- [ ] Backend / `/v1` API
- [ ] Models or engines
- [ ] Storage, retention, or exports
- [ ] Dependencies, third-party licenses, or release assets
- [ ] Documentation only

# Evidence

Add concise sanitized logs for runtime changes. Do not attach private media, transcripts, credentials, databases, backups, or unredacted diagnostic bundles.

# Checklist

- [ ] Tests reproduce the bug or cover the new behavior.
- [ ] Maintained API changes update the OpenAPI snapshot, typed consumers, and contract tests.
- [ ] User-facing behavior and `CHANGELOG.md` are current.
- [ ] Third-party notices, model licenses, source records, and checksums are current when affected.
- [ ] No model weights, generated packages, local data, or secrets are committed.
