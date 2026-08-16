# Using the archive in Obsidian

A Plaintext archive is a plain folder of Markdown files with YAML frontmatter and
`[[wikilinks]]` - which is exactly what Obsidian reads. Point Obsidian at your
archive folder and it opens as a vault, no conversion and no import. Everything
below is optional polish; the archive works the same whether or not you ever open
Obsidian.

## What you can do entirely in the vault (no command line)

Day-to-day genealogy needs no `fha` typing at all:

- **Create** a person or source note. Copy one of the `_TEMPLATE.*` files your
  archive already ships (`people/_TEMPLATE.person.md`,
  `sources/_TEMPLATE.source.md`, and friends), or install the optional
  [Templater pack](https://github.com/asielen/plaintext-family-history/tree/master/obsidian-templater)
  from the project repo into your own Obsidian setup for a one-click new note
  with the right frontmatter. Leave the ID blank - it is filled in
  later (a record with no ID is a valid starting state).
- **Link** people, sources, and places by name: type `[[` and pick. The link
  resolves to the right record even before any ID exists, and Obsidian draws the
  connection in its graph.
- **Cite** a fact by putting its source's name in double brackets in your prose.

When you want machine work done - IDs minted, the archive checked, a shareable
site or packet built - run the `fha` tools, or just ask the assistant. The
expected daily interaction is not the command line: it is writing in the vault
and letting an assistant (Claude today, or another interface later) drive the
tools when needed. Each tool is a headless engine, so any front door - a
terminal, an assistant, a future Obsidian plugin - reaches the same behavior.

## Querying your archive

Person-level frontmatter is the queryable surface: vitals, the `relationships:`
block, `gender:`, and `restricted:` are all ordinary frontmatter that Dataview
and Bases read natively. "Everyone without a death date" or "everyone born in
Breton County" are plain frontmatter queries.

The detailed, per-claim data lives in fenced ` ```yaml ` blocks under
`## Claims`. That fence is deliberate - an unfenced YAML list renders as garbled
Markdown - but it also means Dataview cannot see inside it. Query claims through
the assistant or `fha` (which build the index), not Dataview. There is no
generated "claim mirror" note: it would duplicate the claims and add a file to
keep in sync, and the person frontmatter already answers the common questions.

## The graph view

At a couple thousand people the *global* graph is a hairball - that is true of
any large vault, and it is not the useful view. The wins are Obsidian's **local
graph** (one person and their immediate connections) and the household clusters
that form around couples and sources. The optional pack ships a sample
`graph.json` that dims `people/stubs/` and brightens curated people so those
clusters stand out; merge its `colorGroups` into your vault's `.obsidian/graph.json`.

## Mobile and desktop

The files read and edit fine on a phone, so capture and reading work on the go.
The `fha` tools (processing, indexing, export) are desktop work. The natural
rhythm matches that split: **capture and read on mobile, process at the desk.**

## A footgun to know about

The privacy flags (`living`, `restricted`) protect what the *export* tools
publish - the static site and packets. They do **not** filter a raw vault sync.
If you sync or share the vault folder itself (a synced drive, a shared folder),
you are sharing everything in it, living people and restricted material included.
Share the generated site or a packet, not the raw vault.

Two smaller notes: `fha lint` catches alias clashes (two same-named people)
that Obsidian will not warn you about, so run it now and then; and a `[[C-…]]`
claim link resolves to the source file that holds the claim, since claims are
not their own notes.

## What this is not

Plaintext anchors on Obsidian among PKM tools. The vault opens in other Markdown
apps too (it is just Markdown), but it is not tuned for outliner tools, and
org-mode is out of scope.
