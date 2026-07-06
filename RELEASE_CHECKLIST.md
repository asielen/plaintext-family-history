# Release Checklist

Before tagging a spec release or pushing a significant change:

- [ ] README status badge matches the SPEC version.
- [ ] TOOLING version matches the SPEC version.
- [ ] Local Markdown links resolve.
- [ ] No real personal data in examples, docs, issues, or fixtures (see PRIVACY.md).
- [ ] `example-archive/` lints with no errors under the current rules (warnings W101/W102 are intentional teaching states).
- [ ] `example-archive/` generated views are fresh (`fha views refresh --root example-archive`, or the per-person view command) so the committed views match the current records.
- [ ] Sample zips in `downloads/` rebuilt (`python build_sample_zips.py`) after any SPEC/TOOLING/docs/template change.
- [ ] AGENTS.md / CLAUDE.md reference current command and skill names (no stale `promote`, `PERSON:`, `add-source`).
- [ ] Significant design decisions are noted in the PR description or commit message.
- [ ] Repo/tools/template/fixture distinction stays clear (no doc treats the repo root as a real archive).
- [ ] Spec and Tooling files match implementation details if decisions were made differently than in the spec
- [ ] `tools/README.md` updated: any newly completed tool or flag marked ✓; any newly deferred item marked ⚑ with a note pointing at its BUILD.md layer
- [ ] Status summaries agree everywhere status is stated (`BUILD*.md` status tables/headers, the sibling TOOLING docs' build-status sections, SPEC.md Part IV status notes, README badge + "Status & roadmap", TOOLING.md §16/§17, AGENTS.md, `tools/README.md`); grep for retired status phrases ("build pending", "not yet built", "when implemented", "deferred") and fix any survivor
