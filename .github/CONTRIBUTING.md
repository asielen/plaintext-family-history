# Contributing to Plainfile Family History

Thanks for your interest.
Plainfile is an operating **spec**, so contributions look a little different from a typical code project.

## What's most useful right now

- **Feedback from genealogists.** Does the data model survive contact with real research problems - unusual relationships, conflicting records, brick walls, non-Western naming? Open an issue describing the case.
- **Spec clarity.** Ambiguities, contradictions, or gaps in `SPEC.md` / `TOOLING.md`. The documents are meant to be precise enough to regenerate the tooling; if something isn't, that's a bug.
- **Tooling implementations.** Building the `fha` suite against the spec? PRs and reference implementations are welcome, as are notes on where the spec was unclear during implementation.
- **Methodology cross-checks.** Where does the spec diverge from established genealogical practice (the Genealogical Proof Standard, evidence analysis)? Divergences should be intentional and documented.

## Ground rules

- **No real personal data in examples or issues.** Use the fictional Hartley family in `example-archive/`, or invent placeholders. Never commit identifying information about living people.
- **Decisions get recorded.** Significant design changes belong in the git history - commit messages and PR descriptions - so they aren't relitigated.
- **Keep the foundation boring.** The archive layer should stay simple, plain-text, and durable. Sophistication goes in optional layers on top.

## How to propose a change

1. Open an issue describing the problem before a large PR, so the design discussion happens first.
2. For spec changes, reference the section and propose the edit; note any knock-on effects (the README, the tooling design, the example archive).
3. Small fixes (typos, broken links, clarifications) can go straight to a PR.
