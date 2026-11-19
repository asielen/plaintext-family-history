# Obsidian Templater pack (optional)

A small, **optional** set of [Templater](https://github.com/SilentVoid13/Templater) templates for people who keep their Plaintext archive open as an Obsidian vault and want a one-click "new person / new source" that fills in the right fields at the top of the note.

This pack is a convenience and is **not part of the archive itself.** It lives outside the Python `fha` suite: `fha install` never copies it, `manifest.json` never lists it, and nothing depends on it. It is for one optional editor, so it stays out of archives (unlike the browser capture extension, which ships into every archive at `.fha/browser-companion/`). The archive is fully usable without it; the templates just save typing.

## What's here

| File | What it makes |
|---|---|
| `new-person.md` | A person record (`name`, `living`, `created`, `tier: stub`) |
| `new-source.md` | A source record (`title`, `source_type`, `## Claims`, `## Notes`) |
| `graph.json` | A sample Obsidian graph config (dim `people/stubs/`, brighten curated) |

There is no `new-place.md`: places live in the single `places/places.yaml` registry (SPEC §15) rather than one file per place, so there is nothing to template.

## Install

1. Install the Templater community plugin in Obsidian.
2. Copy `new-person.md` and `new-source.md` into your Templater templates folder.
3. (Optional) Merge `graph.json`'s `colorGroups` into your vault's `.obsidian/graph.json` to dim unplaced stubs and brighten curated people.

Apply a template to a new, empty note and answer the prompt. Name the note whatever you like and **leave the ID blank**: a record without an ID is fine (SPEC §10, §13). Running `fha lint --fix-ids` adds the ID and renames the file to the `{sort-name}__{given}_p-id.md` pattern, keeping your filename as an alias so any `[[links]]` you already made keep working.

## A note on querying

Vitals, the `relationships:` block, `sex:`, `gender:` and `restricted:` all live in **person-level frontmatter**, which Dataview and Bases read directly, so "everyone without a death date" or "everyone born in Breton County" are ordinary frontmatter queries. The structured per-claim data stays in fenced ` ```yaml ` blocks (so notes render cleanly) and is queried through `fha` and its index rather than Dataview. See `docs/USING_WITH_OBSIDIAN.md`.
