# Core-tool gap — `fha merge` (spec-discovery from `merge-identities`)

**Surfaced by:** building the `merge-identities` skill (interface-skills step 07).
**Type:** missing deterministic tool verb. **Not a SPEC change** — SPEC §9 already defines the merge/split
mechanics fully; the tool that *enacts* them was never built.

## The gap

The design (TOOLING_INTERFACE.md §2.2, BUILD_INTERFACE.md MI3.1) says the mechanical merge write is "the
deterministic tool's job — never the skill's silent action." **No such verb exists.** Verified against the
shipped suite:

- `fha claim` writes a claim's `status:` only; `fha confirm` verbs are `xref`, `cooccur`, `dismiss`,
  `place`, `discovery`, `draft` — none merge persons.
- Tools *read through* `merged_into` (`packet`, `index`, `site`) and *warn* on it (lint **E016** new claim
  on a merged person, **W107** direct references to a merged person), but nothing *performs* the merge.

So the SPEC §9 merge write — set `status: merged` / `merged_into` / `merge_reason` / `merged_date`, rename
the tombstone to `MERGED-INTO-P-survivor__…`, fold name-variants/external-IDs into the survivor, relink
direct references — has no deterministic owner.

## Interim path (in use now)

Per the owner's decision (and AGENTS.md §"Tools": *"if a tool does not exist yet, do the task by hand
following SPEC and say so"*), `merge-identities/SKILL.md` enacts a **human-confirmed** merge by a careful
SPEC §9 hand-edit, and this note records that the hand-edit is **temporary** — to be replaced by the verb
below. The split (conflation) case is already a guided human task in SPEC §9, so it stays hand-guided
regardless.

## Proposed core work (BUILD.md / TOOLING.md)

A deterministic merge verb in the `fha confirm` family (the write floor under the read-only detectors),
e.g.:

```
fha confirm merge <P-merged> --into <P-survivor> --reason "<why>" --dry-run
```

- writes `status: merged` / `merged_into` / `merge_reason` / `merged_date:` onto `<P-merged>`;
- renames the tombstone file to the `MERGED-INTO-P-survivor__…` grammar (SPEC §9);
- folds `<P-merged>`'s `name_variants`/external IDs into `<P-survivor>`;
- relinks direct references it can (claims' `persons:`/`roles:`, frontmatter `people:`), leaving the rest
  for the W107 gradual-cleanup list;
- is **surgical** (sibling data/keys/comments survive), locates records by scanning `people/` directly
  (works when the index is stale), ships `--dry-run`, and returns a `Result` whose `changed[]` lists every
  file written — matching the other `fha confirm` verbs (TOOLING §14a3).

A sibling `fha confirm separate` (or leaving split hand-guided) can follow. This is a **core PR**, not skill
work; when it lands, update `merge-identities/SKILL.md` to direct the verb instead of hand-editing, and
delete this note's "interim path."
