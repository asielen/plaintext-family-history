# How to Read This Archive (No Tools Required)

This is a family-history archive designed to outlive its software. Everything important is plain files you can open with any text editor and image viewer. This page is the label on the filing cabinet - the full rules live in `SPEC.md` (see *Where the rules live*, below).

*This `example-archive/` holds a small, entirely fictional family (the Hartleys). It exists so the tools have something spec-conformant to run against. None of it is real. The photos and scans it references are intentionally absent (marked `status: missing-fixture`) - this sample ships plain text only, no binaries.*

## Start here
1. `people/` holds numbered ancestral-couple folders; the readable biographies are the
`hartley__thomas_edward_P-….md` files.
2. In any biography, codes like `[S-1a2b3c4d5e]` are citations - search that code across
the archive to find the evidence behind the statement.
3. `sources/` describes each piece of evidence and lists its files (census, newspapers,
vital records, a photo, and more).
4. `people/connections/` holds the "FAN club" - friends, in-laws, and associates who
aren't direct ancestors but show up in the records around them.
5. `people/stubs/` is the holding pen for people not yet placed.
6. `places/places.yaml` is the place registry.
7. `notes/` holds research-in-progress.
8. `inbox/` is the staging area: new material (and rough notes about it) waiting to be
filed and processed into sources.

## What the codes mean
Permanent random IDs: `P-` people, `S-` sources, `C-` claims, `L-` places, `H-` hypotheses. A statement you can trust carries an `[S-…]` citation; uncited prose is story or context.

## Where the rules live
This sample lives inside the public Plaintext spec repository, so it does not duplicate the governing documents - they sit at the **repository root**, one level up:

- `SPEC.md` - the law of the archive (formats, the on-disk tree, what every field means).
- `TOOLING.md` - the design of the `fha` tools.
- `AGENTS.md` / `CLAUDE.md` - operating instructions for AI assistants.

A **real** archive is created from `archive-template/` and carries its own copies of these files at its root, so it travels as a self-contained, self-documenting bundle. If you share this `example-archive/` on its own, include the repo-root `SPEC.md` (or this note) so a reader knows where the rules are.
