# CLAUDE.md

Read and follow **AGENTS.md** - it is the canonical operating instruction for any AI agent in this archive, and it applies to you in full.
In **tool-building** or **code-review** mode, also read **AGENTS_TOOLING.md**.

Claude-Code-specific notes:
- Workflow skills live in `.claude/skills/` - process-source, review-claims,
mine-transcript, write-biography, today, research-next, merge-identities, place-research (as implemented).
Prefer them when they match the task. Their design is in `TOOLING_INTERFACE.md`, their build
sequence in `BUILD_INTERFACE.md`.
- `SPEC.md` is the law. The tool design is split by concern: `TOOLING.md` (core tools),
`TOOLING_INGESTION.md` (capture/inbox on-ramp), `TOOLING_INTERFACE.md` (workbench + skills) -
each with a matching build doc (`BUILD.md`, `BUILD_INGESTION.md`, `BUILD_INTERFACE.md`).
Cite sections when proposing structural changes.
