# Release Checklist

Before tagging a spec release or pushing a significant change:

- [ ] README status badge matches the SPEC version.
- [ ] TOOLING version matches the SPEC version.
- [ ] Local Markdown links resolve.
- [ ] No real personal data in examples, docs, issues, or fixtures (see PRIVACY.md).
- [ ] `example-archive/` is lint-clean under the current rules.
- [ ] AGENTS.md / CLAUDE.md reference current command and skill names (no stale `promote`, `PERSON:`, `add-source`).
- [ ] SPEC decision log includes the change.
- [ ] Repo/tools/template/fixture distinction stays clear (no doc treats the repo root as a real archive).
