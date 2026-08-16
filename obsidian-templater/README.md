# Obsidian Templater pack (optional)

A small, **optional** set of [Templater](https://github.com/SilentVoid13/Templater)
templates for people who keep their Plaintext archive open as an Obsidian vault and
want a one-click "new person / new source" that emits spec-correct frontmatter.

This pack is a **convenience, not part of the durable archive.** It lives outside
the Python `fha` suite: it is never vendored by `fha install`, never recorded in
`manifest.json`, and nothing depends on it. (The browser capture extension used to
sit in this same category but now ships into archives at `.fha/browser-companion/`;
this pack deliberately does not - it targets one optional editor.) The archive is
fully usable without it; the templates just save typing.

## What's here

| File | What it makes |
|---|---|
| `new-person.md` | A person record (`name`, `living`, `created`, `tier: stub`) |
| `new-source.md` | A source record (`title`, `source_type`, `## Claims`, `## Notes`) |
| `graph.json` | A sample Obsidian graph config (dim `people/stubs/`, brighten curated) |

There is no `new-place.md`: places live in the single `places/places.yaml`
registry (SPEC §15), not one file per place, so there is nothing to template.

## Install

1. Install the Templater community plugin in Obsidian.
2. Copy `new-person.md` and `new-source.md` into your Templater templates folder.
3. (Optional) Merge `graph.json`'s `colorGroups` into your vault's
   `.obsidian/graph.json` to dim unplaced stubs and brighten curated people.

Apply a template to a new, empty note and answer the prompt. **Leave the ID
blank** - a record with no ID is a valid pre-machine state (SPEC §10/§13);
`fha lint` mints the ID on first contact and renames the file to the
`{sort-name}__{given}_P-id.md` grammar, keeping your filename as an alias so any
`[[links]]` you already made keep working. Name the note whatever you like.

## A note on querying

Vitals, the `relationships:` block, `gender:`, and `restricted:` all live in
**person-level frontmatter**, which Dataview and Bases read natively - so
"everyone without a death date" or "everyone born in Breton County" are ordinary
frontmatter queries. The structured per-claim data stays in fenced ` ```yaml `
blocks (so notes render cleanly) and is queried through `fha` / the index, not
Dataview. See `docs/USING_WITH_OBSIDIAN.md`.
