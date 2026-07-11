# Core-tool gap — `fha confirm merge` (CLOSED: the verb shipped)

**Surfaced by:** building the `merge-identities` skill (interface-skills step 07).
**Type:** missing deterministic tool verb — **resolved**. This file is kept as the historical record of
the spec-discovery; there is no live gap and no interim path.

## What the gap was

The design (TOOLING_INTERFACE.md §2.2, BUILD_INTERFACE.md MI3.1) says the mechanical merge write is "the
deterministic tool's job — never the skill's silent action," but no `fha` verb enacted SPEC §9's merge
write (tombstone fields, `MERGED-INTO-P-survivor__` rename, folds, reference relinks). By the owner's
decision the skill enacted a human-confirmed merge by a careful SPEC §9 hand-edit in the interim, with
this note tracking the wanted verb.

## How it closed

`fha confirm merge <P-merged> --into <P-survivor> --reason "<why>" [--dry-run]` shipped as the seventh
`fha confirm` verb (2026-07, audit Wave 3 / plan 16): the full SPEC §9 enactment in one plan-then-apply
pass with rollback — tombstone + rename, name-variant/external-id/relationship folds, claim relinks
across **all** statuses, other-record `relationships:`/`people:` relinks, prose mentions left to lint
W107 by design. It is surgical, scans `people/`/`sources/` directly (stale-index-proof), previews with
`--dry-run`, and returns a `Result` whose `changed[]` lists every file — matching the other confirm
verbs (TOOLING §14a3; implementation status in `tools/README.md`).

`merge-identities/SKILL.md` now directs the verb; the interim hand-edit language was retired everywhere
in the same change. The split (`fha confirm separate`) remains hand-guided by design — dividing an
identity is research judgment (SPEC §9), not a mechanical write.