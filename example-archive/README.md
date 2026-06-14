# How to Read This Archive (No Tools Required)

This is a family-history archive designed to outlive its software. Everything important is plain files you can open with any text editor and image viewer. This page is the label on the filing cabinet — the full rules live in `SPEC.md` at the repo root.

*This `example-archive/` holds a small, entirely fictional family (the Hartleys). It exists so the tools have something spec-conformant to run against. None of it is real.*

## Start here
1. `people/` holds numbered ancestral-couple folders; the readable biographies are the
`hartley__thomas_edward_P-….md` files.
2. In any biography, codes like `[S-1a2b3c4d5e]` are citations — search that code across
the archive to find the evidence behind the statement.
3. `sources/` describes each piece of evidence and lists its files.
4. `places/places.yaml` is the place registry.
5. `notes/` holds research-in-progress.

## What the codes mean
Permanent random IDs: `P-` people, `S-` sources, `C-` claims, `L-` places, `H-` hypotheses. A statement you can trust carries an `[S-…]` citation; uncited prose is story or context.
