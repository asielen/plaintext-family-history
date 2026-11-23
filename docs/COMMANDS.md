# fha command reference

> **Generated; do not edit.** Written by `build_commands_doc.py` from the
> commands' own definitions, so it always matches the tools. Every command
> also prints this help with `--help` (`fha lint --help`).

Most commands report by default and change nothing until given `--apply`
(or the command's own write flag). Every command accepts `--root PATH` to
name the archive; without it, the archive is found by walking up from the
current folder.

## Options for every command

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (default: auto-detect by walking up from CWD) |
| `--spec-root` `PATH` | Spec docs root when SPEC.md/TOOLING.md are not in the archive (e.g. running from the public spec repo). (reserved: only `fha lint` reads it yet) |
| `--debug` | Show Python tracebacks for tool-building diagnostics |

## Commands

- [`fha id`](#fha-id)
- [`fha index`](#fha-index)
- [`fha lint`](#fha-lint)
- [`fha stubs`](#fha-stubs)
- [`fha views`](#fha-views)
- [`fha doctor`](#fha-doctor)
- [`fha find`](#fha-find)
- [`fha search`](#fha-search)
- [`fha relate`](#fha-relate)
- [`fha metrics`](#fha-metrics)
- [`fha calendar`](#fha-calendar)
- [`fha photoindex`](#fha-photoindex)
- [`fha xref`](#fha-xref)
- [`fha cooccur`](#fha-cooccur)
- [`fha report`](#fha-report)
- [`fha packet`](#fha-packet)
- [`fha places`](#fha-places)
- [`fha gedcom`](#fha-gedcom)
- [`fha wikitree`](#fha-wikitree)
- [`fha process`](#fha-process)
- [`fha capture`](#fha-capture)
- [`fha convert-mining`](#fha-convert-mining)
- [`fha claim`](#fha-claim)
- [`fha confirm`](#fha-confirm)
- [`fha person`](#fha-person)
- [`fha source`](#fha-source)
- [`fha site`](#fha-site)
- [`fha serve`](#fha-serve)
- [`fha install`](#fha-install)
- [`fha update-tools`](#fha-update-tools)
- [`fha working-copy`](#fha-working-copy)
- [`fha normalize-links`](#fha-normalize-links)
- [`fha backup`](#fha-backup)
- [`fha reconcile`](#fha-reconcile)
- [`fha media`](#fha-media)
- [`fha reorganize`](#fha-reorganize)

## `fha id`

Mint and check archive IDs

```text
Mint and check the archive's ID codes.

  fha id mint P|S|C|L|H [-n N]   Print fresh IDs (P=person, S=source, C=claim,
                                 L=place, H=hypothesis)
  fha id check <ID>              Show everywhere an ID appears in the archive

You rarely need this by hand: `fha lint --fix-ids` assigns IDs to records you
named in plain English.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root |

### `fha id mint`

Mint fresh IDs (checked for non-existence)

```text
Generate one or more fresh IDs of the specified type.
```

| Argument | What it does |
|---|---|
| `TYPE` | ID type: P (person) S (source) C (claim) L (place) H (hypothesis) |
| `-n` `N` | How many IDs to mint (default: 1, max 100) |
| `--root` `PATH` | Archive root |

### `fha id check`

Find where an ID appears in the archive

| Argument | What it does |
|---|---|
| `ID` | ID to locate (e.g. p-de957bcda1) |
| `--root` `PATH` | Archive root |

## `fha index`

Rebuild the SQLite index from the archive tree

```text
Rebuild the search index so timelines, find, and reports see your latest edits.

  fha index                  Full rebuild (after adding or moving records)
  fha index --source S-xxxx  Update just one source (fast)

The index is a disposable cache, always rebuildable from your files.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root |
| `--source` `s-id` | Upsert only this source (incremental mode) |
| `-v`, `--verbose` | Show progress (full rebuilds only; ignored with --source) |

## `fha lint`

Check the archive for problems and report them (alias: `fha check`)

```text
Check the archive for problems and report them (a spell-check for your data).

  fha lint                Walk and check the archive; report only
  fha check               The same command, plainer name
  fha lint --fix-ids      Give IDs to records you named in plain English
  fha lint --mint-stubs   Create placeholder records for people named but not filed
  fha lint --changed      Quick check before a commit: report only on files
                          changed since the last commit and the records they
                          link to

Catches broken links, missing dates, contradictions, and people referenced but
never filed. Run it any time something feels off. A full `fha lint` (without
--changed) is still the check before a release and after `fha update-tools`.
Exit: 0 clean, 1 warnings, 2 errors, 3 tool failure.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (overrides auto-detection) |
| `--spec-root` `PATH` | Spec docs root (when separate from archive root) |
| `--with-exif` | Also verify embedded SOURCE: keywords and stray documents-root person keywords (W131) via exiftool (slow) |
| `--json` | Machine-readable JSON output |
| `--changed` `[REF]` | Report only on files changed since the git commit REF (default HEAD, so: since the last commit) and new files, plus the records they link to. Every check still runs, findings about scans and photos on disk are always shown, and a change to fha.yaml or places.yaml reports the whole archive. Before a release or after `fha update-tools`, run a full lint without it |
| `--timings` | After the report, print how many seconds each group of checks took (to stderr) |
| `--format-check` | Check formatting only (no fixes) |
| `--format-write` | Apply conservative formatting fixes |
| `--dry-run` | Preview mutating operations without writing |
| `--mint-stubs` | Create person stubs for E005 set |
| `--spawn-questions` | Append questions to notes/questions.md for E009 contradictions |
| `--fix-claims-fence` | Wrap hand-written claims that forgot the ```yaml fence (with --dry-run to preview) |
| `--fix-ids` | Mint IDs for hand-authored id-less records (rename, keep the old name as an alias) and for id-less claims inside sources (with --dry-run to preview) |
| `--fix-reciprocal` | Add the missing mirror edge for each W116 (with --dry-run to preview) |
| `--fix-applied` | Add the relationships: entry each W115 says an accepted claim is missing from, citing the claim (with --dry-run to preview) |
| `--fix-duplicates` | Remove each relationships: entry W160 names, one that cites no evidence beside an entry for the same person that does; an entry holding a note, another line or a comment is left for you to remove by hand (with --dry-run to preview) |
| `--fix-surnames` | Write the surname: line each W138 file name implies, so the record keeps the surname its file name was sorted under (with --dry-run to preview) |
| `--fix-aliases` | Bring each W111 record's aliases: into step: its own ID, and for a person their name and other public names (variants, nicknames, birth and married names), so typing [[ in Obsidian finds them by name (with --dry-run to preview) |

## `fha stubs`

Mint person stubs for unresolved p-id references

```text
Create placeholder records for people who are named but not yet filed.

  fha stubs                          Stub every unresolved person reference
  fha stubs --from-names "A; B; C"   Stub these named people
  fha stubs --dry-run                Preview without writing

Like dropping a blank labeled folder in the cabinet for a name you've heard but
not yet researched. --from-names replaces the reference scan; the two do not run together.
A name that already has a record is refused (nothing is created) so the same
person is never filed twice; add --force when it really is someone else.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root |
| `--from-names` `NAMES` | Semicolon-separated names to mint IDs and stubs for. Runs instead of the unresolved-reference scan, not in addition to it. |
| `--dry-run` | Preview without writing |
| `--force` | With --from-names: mint even when a name already has a record (two different people who share a name, e.g. a father and son). |

## `fha views`

Generate view files from the index (timeline, sources-index, draft-queue, …).

```text
Build always-current summary pages from your records.

  fha views timeline <p-id>        A person's life chronology
  fha views sources-index <p-id>   Every source about a person
  fha views summary <p-id>         Born / Died / Married / Parents / Children
                                   lines under the person's name
  fha views draft-queue <p-id>     Accepted facts not yet written into the bio
  fha views places                 One note per place, for Obsidian
                                   (only with links: obsidian in fha.yaml)
  fha views refresh                Rebuild every view for all curated people

Add --format html (refresh also takes --format both) for a printable
single-file page under generated/views/ (private and unredacted, with a
visible not-for-publication banner); share through `fha site` instead.

These pages are generated and rebuildable; edit the records, not the pages.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha views timeline`

Generate per-person timeline view.

```text
Generate {name}_timeline_{p-id}.md in the person's folder.
Requires a fresh .cache/index.sqlite (run `fha index` first).
```

| Argument | What it does |
|---|---|
| `[p-id]` | Person to generate for. |
| `--all-curated` | Generate for every curated person. |
| `--format` `{md,html}` | md (default): the companion .md beside the profile. html: a standalone printable page under generated/views/ (private and unredacted; it carries a visible not-for-publication banner; share through `fha site`). |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha views sources-index`

Refresh a person's own ## Sources section.

```text
Rewrite the ## Sources GENERATED region inside a person's own
profile (works on a stub too, so it can be populated before
promotion). There is no separate sources-index file and no
--format html page; the list lives inside the profile.
Requires a fresh .cache/index.sqlite (run `fha index` first).
```

| Argument | What it does |
|---|---|
| `[p-id]` | Person whose ## Sources section to refresh (stub or curated). |
| `--all-curated` | Refresh every curated profile's ## Sources section. |
| `--format` `{md,html}` | md (default). html is refused: the list lives inside the profile, with no separate page. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha views summary`

Write the Born / Died / Married / Parents / Children lines under a person's name.

```text
Write the summary lines under the person's name in their own
profile, from accepted claims, between GENERATED summary markers.
Lines a person wrote by hand (no markers) are left alone. Works
on a stub too. With `views: completeness_line: true` in
fha.yaml, the lines end with a Records line saying how well
documented the person is, as `fha metrics` says it. Requires a
fresh .cache/index.sqlite (run `fha index` first).
```

| Argument | What it does |
|---|---|
| `[p-id]` | Person whose summary lines to write (stub or curated). |
| `--all-curated` | Write every curated profile's summary lines. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha views draft-queue`

Generate per-person draft-queue (uncited-claim backlog) view.

```text
Generate {name}_draft-queue_{p-id}.md in the person's folder.
Requires a fresh .cache/index.sqlite (run `fha index` first).
```

| Argument | What it does |
|---|---|
| `[p-id]` | Person to generate for. |
| `--all-curated` | Generate for every curated person. |
| `--format` `{md,html}` | md (default): the companion .md beside the profile. html: a standalone printable page under generated/views/ (private and unredacted; it carries a visible not-for-publication banner; share through `fha site`). |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha views places`

Write one note per place in places/, for Obsidian (links: obsidian only).

```text
With links: obsidian in fha.yaml, write one generated note per place
in places/ (places/{name}_{l-id}.md), built from places/places.yaml,
so Obsidian can follow a place link and show what mentions the place.
A place renamed in places.yaml gets a note under its new name, the old
note is removed, and links to the old name are pointed at the new one.
places.yaml stays the record; edit the place there, never the note.
With links: id (the default) nothing is written.
Requires a fresh .cache/index.sqlite (run `fha index` first).
```

| Argument | What it does |
|---|---|
| `--dry-run` | Show what would be written, removed and re-pointed, without writing. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha views brackets`

Check couple-folder bracket lists, Ahnentafel placement, and direct-line stubs (W103/W110/W119/W120).

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--fix` | Apply renames/moves after preview. |
| `--fix-promote` | Promote the direct-line stubs (W119) after a preview and an Apply? confirmation: tier flip, move into the couple folder, research file scaffold. |
| `--realign` | Bring the whole promoted tree back in line with the current derivation in one previewed pass: the renames/moves AND the stub promotions together, one combined preview, one Apply? confirmation. Use after anything that shifts the derived tree (a re-anchored root_person, a corrected sex:, a new parent claim, a merge). |
| `--generations` `N` | Only consider direct-line stubs within N generations (1 = parents, 2 = grandparents, …). |
| `--dry-run` | Preview changes without writing. |
| `--yes` | Skip the Apply? confirmation (for scripted or non-interactive runs; the preview still prints). |

### `fha views tree`

Traverse relationships and emit an ancestor/descendant/fan tree.

```text
Traverse the relationships table from a seed person and emit the
result as neutral JSON (TOOLING §7) or GraphViz DOT.
Requires a fresh .cache/index.sqlite (run `fha index` first).
```

| Argument | What it does |
|---|---|
| `p-id` | Seed person for traversal. |
| `--mode` `{ancestors,descendants,fan}` | ancestors: pedigree BFS; descendants: all descendants + in-law spouses; fan: all edge types (default 2 hops). |
| `--generations` `N` | Maximum generations / hops (default: unlimited; fan default: 2). |
| `--format` `{json,dot,html}` | Output format (default: json). html is not available here; run `fha site` for a browsable tree. |
| `--out` `FILE` | Write output to FILE instead of stdout. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha views clean`

Delete every generated view file (companion .md + generated/views/ HTML); they regenerate with `fha views refresh`.

```text
Delete all GENERATED-headed view files: companion .md files (timeline,
draft-queue, and any old couple-folder sources-index.md) from the
people/ tree, single-file HTML views under generated/views/, and the
place notes Obsidian link mode writes in places/. Uses the <!-- GENERATED
… --> whole-file header as the sole signal, file by file; never touches
profiles or manually authored files, including a profile whose own
## Sources section is a GENERATED-BEGIN/END region: that region is
rewritten in place by `fha views sources-index`, never deleted by this
command, because ownership here is judged by a FILE's first line, and a
profile's first line is always its frontmatter fence, never a region
marker. --dry-run lists what would be removed without writing.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | List what would be removed without writing. |

### `fha views refresh`

Regenerate all content views for every curated person.

```text
Regenerate timeline and draft-queue for every curated person, and
refresh every person's own ## Sources section and summary lines
(stubs included). With links: obsidian in fha.yaml, the place notes
are rebuilt first, as `fha views places` does.
--format html regenerates the standalone generated/views/ pages
instead; --format both writes the two sets in one pass.
Requires a fresh .cache/index.sqlite (run `fha index` first).
```

| Argument | What it does |
|---|---|
| `--format` `{md,html,both}` | md (default): companion .md files as always. html: standalone pages under generated/views/. both: write the two sets in one pass. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

## `fha doctor`

Archive health check: what is wrong with this archive?

```text
Check that the tools, dependencies, and file paths are all wired correctly.

  fha doctor

Run this when something's broken, or when setting up on a new machine. Safe on a
fresh archive: missing caches are warnings, not errors.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root |

## `fha find`

Locate any ID, or full-text search across records and notes

```text
Locate anything in the archive, or search its full text.

  fha find <ID>              Everything about one ID (person, source, place...)
  fha find --text "phrase"   Full-text search across records, notes, captions
  fha find --related <ID>    Everything connected to an ID (add --date for a time slice)
  fha find --json <ID|text>  Machine-readable result list (add --kind/--limit to narrow it)

This is your search box. For plain word search, `fha search <words>` is the same
as `fha find --text`.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root |
| `--text` `PHRASE` | Full-text search across records, notes, transcripts |
| `--related` `[ID]` | Neighborhood of an ID: the people, places and sources next to it. Combine with --date for a time slice, or pass --related --date EDTF alone for the standalone time-slice neighborhood. |
| `--date` `EDTF` | Time-slice filter. With --related <ID>, narrows that neighborhood to the given EDTF range; with --related and no ID, reports everything active in that range. |
| `--json` | Machine-readable result list instead of a human report. Valid with a bare ID/text query or --text; combine with --kind/--limit to narrow it. Not yet valid with --related. |
| `--kind` `KIND[,KIND...]` | With --json: only these result kinds (person, source, place, hypothesis, claim, text (comma list; photo-source = only sources that have photo files)). Default: every kind. |
| `--limit` `N` | With --json: maximum number of results (default 20). |
| `[ID_OR_TEXT]` | Archive ID (P-/S-/C-/L-/H-) or text to search |

## `fha search`

Search everything for a word or phrase (same as `fha find --text`)

```text
Search everything for a word or phrase: records, notes, transcripts, photo captions.

Examples:
  fha search rose hartley
  fha search "1880 census"

Same as `fha find --text "..."`.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root |
| `WORD [WORD ...]` | Word or phrase to search for (joined with spaces if several words) |

## `fha relate`

How are two people related? Blood degree + shortest social path.

```text
Show how two people are related.

  fha relate P-A P-B          Blood degree plus the shortest social path
  fha relate "Ada Hart" P-B   Either person by full name instead of ID (a name
                              two people share is listed so you can pick one)
  fha relate P-A P-B --json   Same answer, as machine-readable JSON

A read-only view over accepted relationship claims: nothing is written.
```

| Argument | What it does |
|---|---|
| `P-A` | First person: ID or full name |
| `P-B` | Second person: ID or full name |
| `--root` `PATH` | Archive root (overrides auto-detection) |
| `--json` | Machine-readable JSON output |

## `fha metrics`

How well documented is each person? Completeness checks + archive totals.

```text
How well documented is each person?

  fha metrics               The archive in numbers, then the direct line by generation
  fha metrics p-xxxxxxxxxx  One person's five checks, each with what is missing
  fha metrics "Mary Smith"  The same, by full name (two people with that name:
                            it lists them so you can pick one by ID)
  fha metrics --all         Every person, stubs included
  fha metrics --include-suggested
                            Count suggested claims too (records found, not yet reviewed)
  fha metrics --json        The same answer as machine-readable JSON

The five checks: a birth record; every census they lived through; both parents
named by a sourced claim; a marriage record for each partner; and, for anyone
marked not living or born over 130 years ago, a death or burial. Read-only:
nothing is written.
```

| Argument | What it does |
|---|---|
| `[p-id]` | One person to show in detail, by ID or by full name (default: the direct line) |
| `--all` | List every person, stubs included (not with a p-id) |
| `--include-suggested` | Count suggested claims as well as accepted ones |
| `--json` | Machine-readable JSON output |
| `--root` `PATH` | Archive root (overrides auto-detection) |

## `fha calendar`

What happened in the family this month, or on this day?

```text
What happened in the family this month, or on this day?

  fha calendar                 This month: births, marriages, deaths, arrivals
  fha calendar june            Any month, by name or number
  fha calendar june 14         One day (also 06-14, 6/14, 14 june)
  fha calendar today           Today's anniversaries
  fha calendar --week          Today and the six days after it
  fha calendar --include-suggested
                               Count suggested claims too, each marked
  fha calendar --json          The same answer as machine-readable JSON

Only dates recorded to the month or day appear. Read-only: nothing is written.
```

| Argument | What it does |
|---|---|
| `[WHEN ...]` | A month or a day (default: this month) |
| `--week` | The week starting on the day given (default: today) |
| `--include-suggested` | Count suggested claims as well as accepted ones; each line resting only on suggested claims is marked |
| `--json` | Machine-readable JSON output |
| `--root` `PATH` | Archive root (overrides auto-detection) |

## `fha photoindex`

Scrape photo metadata into .cache/photos.sqlite

```text
Make your photo library searchable without opening Lightroom.

  fha photoindex                       Scan the photos root into the catalog
  fha photoindex find --person <p-id>  Every photo of someone
  fha photoindex gallery --person <p-id>  A clickable HTML page of the same photos
  fha photoindex triage --top 20       Un-filed photos to process next
  fha photoindex tag-person <p-id>     Tag a face across every copy
  fha photoindex set-summary <path> --text "..."   Update a photo's AI summary

Photos are never renamed; identity lives in the embedded metadata.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (overrides auto-detection) |
| `--full` | Rescan every file via exiftool, bypassing the incremental mtime/size check |

### `fha photoindex find`

Query the photo catalog

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (overrides auto-detection) |
| `--person` `p-id` | Photos resolved to this p-id (any confidence tier) |
| `--keyword` `TERM` | Case-insensitive substring match against cached keywords |
| `--edtf` `EDTF` | Bounds-overlap filter against each photo's resolved date |
| `--text` `TEXT` | Full-text search over title/caption/comment/keywords (photo_fts) |
| `--files` | Show every matching raw row instead of one path per group |
| `--under` `PATH` | Only groups with a variant under this folder (relative to the photos root) |
| `--not-under` `PATH` | Drop groups with any variant under this folder (relative to the photos root) |

### `fha photoindex gallery`

Build a clickable single-file HTML page of the matching photos

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (overrides auto-detection) |
| `--person` `p-id` | Photos resolved to this p-id (any confidence tier) |
| `--keyword` `TERM` | Case-insensitive substring match against cached keywords |
| `--edtf` `EDTF` | Bounds-overlap filter against each photo's resolved date |
| `--text` `TEXT` | Full-text search over title/caption/comment/keywords (photo_fts) |
| `--out` `FILE` | Write the page here instead of the default generated/gallery/ landing spot |
| `--under` `PATH` | Only groups with a variant under this folder (relative to the photos root) |
| `--not-under` `PATH` | Drop groups with any variant under this folder (relative to the photos root) |

### `fha photoindex triage`

Rank unprocessed photo groups by evidence signals

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (overrides auto-detection) |
| `--top` `N` | Show this many top candidates (default 10) |
| `--under` `PATH` | Only rank groups with a variant under this folder (relative to the photos root) |
| `--not-under` `PATH` | Skip groups with any variant under this folder (relative to the photos root) |

### `fha photoindex report`

List photo groups with conflicting variant dates

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (overrides auto-detection) |

### `fha photoindex reconcile`

Re-match moved photos by name and SOURCE: keyword; flag the rest as missing

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (overrides auto-detection) |
| `--with-exif` | Read embedded SOURCE: keywords from untracked files to re-match missing paths (requires exiftool) |
| `--dry-run` | Report the rematch/missing/new-file plan without changing the cache |

### `fha photoindex tag-person`

Write a bare p-id keyword onto matched or explicit photos

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (overrides auto-detection) |
| `p-id` | Person to tag |
| `--from-face-tag` `TAG` | Tag every photo whose cached face region carries this name |
| `--paths` `PATH [PATH ...]` | Tag these specific catalog paths |
| `--dry-run` | Preview the candidate list without writing or prompting |

### `fha photoindex set-summary`

Write an AI summary into a photo's embedded comment, preserving human text

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (overrides auto-detection) |
| `[PATH]` | One catalog photo path to write (as printed by fha photoindex find) |
| `--group` `GROUP-ID` | Write every variant in this photo group instead of one file (e.g. "SOURCE:s-ab12cd34ef") |
| `--text` `TEXT` | The new summary text (stored AI-marked, as "AI: <text>") |
| `--append` | Keep an existing AI summary and add this one below it instead of replacing it |
| `--dry-run` | Preview the old and new comment per file without writing or prompting |

## `fha xref`

Cross-reference accepted/parked claims for corroboration/contradiction candidates

```text
Find which of your facts back each other up and which ones conflict.

  fha xref

Read-only: it lists corroboration and contradiction candidates for you to act
on with `fha confirm xref`. It never writes to the archive.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (auto-detected if omitted). |

## `fha cooccur`

Find people who keep showing up together (shared sources, places, organizations)

```text
Find people who keep showing up together: possible relatives or associates.

  fha cooccur                  Pairs sharing sources, places, or organizations
  fha cooccur --threshold 3    Require more shared sources before suggesting a pair

Read-only: confirm a pair with `fha confirm cooccur`, or set it aside with
`fha confirm dismiss`.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--threshold` `N` | Minimum distinct shared sources for a person co-occurrence candidate (default: 2). |

## `fha report`

Generate the session research report (refresh, diff, render)

```text
Your session briefing: what's new and what to work on next.

  fha report                    The research feed (discoveries first, chores next)
  fha report --full             Everything, not just the highlights
  fha report --section NAME     Just one section

Refreshes the index, runs the checks, and compares against last session. Usually
you'll hear this narrated by asking "what should I work on?".
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (overrides auto-detection) |
| `--full` | Ignore the snapshot baseline (everything looks new) |
| `--section` `NAME` | Print only this section (still refreshes and records the full snapshot) |

## `fha packet`

Build a person export packet (profile, timeline, sources, files, photos) and zip it.

```text
Bundle everything about one person into a zip to share with family.

  fha packet <p-id>              Build the packet (profile, timeline, sources, photos)
  fha packet <p-id> --dry-run    Preview what's included and what's withheld

A private family export, not a public website. Living people and restricted
material are withheld by default; opt in per export with the --include flags.
```

| Argument | What it does |
|---|---|
| `p-id` | Curated person to export. |
| `-o`, `--out` `PATH` | Output directory (default: 'out/' under the archive root). |
| `--include-research` | Include the person's research.md alongside the profile. |
| `--include-restricted` | Include restricted (non-DNA) sources. Excluded by default. |
| `--include-dna` | Include DNA sources. Excluded even with --include-restricted. |
| `--no-photos` | Skip photo gathering entirely (no photoindex required). |
| `--dry-run` | Preview the packet path and checks without writing files. |
| `--overwrite` | Replace an existing same-name packet directory/zip. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

## `fha places`

Place registry hygiene (lint) and recurrence detection (candidates)

```text
Keep your places tidy, fill in their coordinates, and record what you learn.

  fha places lint                    Check the place registry for problems
  fha places candidates              Place text that recurs often enough for a registry entry
  fha places candidates --register   Register all of them at once (one list, one yes)
  fha places link [--apply]          Link claims to the registered places their text names
  fha places geocode (--place l-id | --all)  Fill in coordinates from GeoNames (one table, one yes)
  fha places set l-id [--coords "LAT, LON"] [--aka NAME]... [--history "PERIOD | HIERARCHY"]...
  fha places note l-id --text "..."  Append a dated research note to the place
  fha places edit-note l-id --old-text "..." --text "..."  Rewrite one existing note
                                     (--old-text is the note's whole paragraph)

"Am I spelling this town three different ways?", "which place should become a
real entry?", "fill in the coordinates for these towns", "move the pin",
"note what I found out about this town."
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha places lint`

Check places/places.yaml + claims.place_id for registry hygiene

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha places candidates`

Detect recurring unlinked place_text and GPS clusters

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--threshold` `N` | Minimum occurrences for a candidate cluster (default: 3). |
| `--include-suggested` | Also cluster place_text on status: suggested claims (normally excluded): a one-time scan of a backlog not yet reviewed, e.g. right after a fresh `fha gedcom import`, not the everyday view. |
| `--register` | Register a place for every cluster listed and link its claims: one list, one yes. Coordinates come from GeoNames data already saved; nothing is downloaded. |
| `--dry-run` | With --register: show the list without asking or writing. |

### `fha places link`

Link existing claims to the registered places their place text names

```text
Link claims written before their place was registered. A claim is linked when its place text names exactly one registered place: the first part is the name of the place or another name for it, and every other part is in its hierarchy ("Chicago, Cook, Illinois" for Chicago, Cook County, Illinois, USA). The place text itself is never changed. Shows what it would link; --apply writes it.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--apply` | Write the links (without it, nothing is written). |

### `fha places geocode`

Fill in coordinates, missing county/state/country and spellings from GeoNames

```text
Match places without coordinates against GeoNames, the free world gazetteer: towns, villages, counties, states and countries. The data is downloaded once into .cache/geonames/ (only the countries your places name). Every match is shown in one table and nothing is written until you answer yes. Places that could be more than one GeoNames entry are listed with the command that settles each.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--place` `l-id` | Geocode only this place. |
| `--all` | Geocode every registry place lacking coordinates. |
| `--offline` | Never download; match only against GeoNames data already saved. |
| `--dry-run` | Show the same table without asking or writing. |

### `fha places set`

Set a place's coordinates, alt-names, or names-over-time (full replace per field)

| Argument | What it does |
|---|---|
| `l-id` | The place to update. |
| `--coords` `"LAT, LON"` | New coordinates, latitude first, e.g. "39.8000, -95.6000"; degrees, minutes and seconds work too, as a gravestone gives them, e.g. "39°33'47"N, 95°07'18"W". |
| `--aka` `NAME` | One also-known-as name, taken verbatim, commas and all ("Washington, D.C." is one name); repeat the flag for more. The given set replaces the old list; a lone `--aka -` clears it. |
| `--history` `"PERIOD | HIERARCHY"` | One names-over-time entry; repeat the flag for more (replaces the old list; a lone `--history -` clears it). |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the change without writing. |

### `fha places note`

Append a dated research note to a place

| Argument | What it does |
|---|---|
| `l-id` | The place the note is about. |
| `--text` `TEXT` | The note, in your own words (loose citations welcome). |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the change without writing. |

### `fha places edit-note`

Rewrite one existing research note on a place (named by its whole current paragraph)

```text
Rewrite one existing research note on a place; the other notes are untouched. --old-text names the note by its exact current text: give the whole paragraph, word for word, not one sentence of it. If it is only part of a note, is not found, or appears more than once, nothing is written and the message says which.
```

| Argument | What it does |
|---|---|
| `l-id` | The place whose note is being corrected. |
| `--old-text` `TEXT` | The note's current text: the whole paragraph, exactly as it stands. |
| `--text` `TEXT` | The corrected note text. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the change without writing. |

## `fha gedcom`

Derive a GEDCOM 5.5.1 export from relationships + accepted vitals.

```text
Export your tree to a GEDCOM file for another genealogy app.

  fha gedcom --all                        Export everyone
  fha gedcom <p-id> --mode descendants    Export from one person, down the line
  fha gedcom <p-id> --mode ancestors      Export from one person, up the line
  fha gedcom --all --out family.ged       Choose the output file
  fha gedcom --all --basic --out dna.ged  For a DNA site: names, dates, places
                                          and family links only

Living people are redacted by default (use --include-living to override). GEDCOM
is the portable format Ancestry, RootsMagic, and others read; it is a one-way
export, never re-imported as truth.

Importing a GEDCOM from Ancestry or another program? That is the separate
`fha gedcom import <file.ged>`; this command is the exporter.
```

| Argument | What it does |
|---|---|
| `[p-id]` | Seed person for traversal (omit with --all). |
| `--mode` `{descendants,ancestors,connected}` | Traversal from the seed (default: descendants). |
| `--generations` `N` | Depth cap for descendants/ancestors (default: unlimited). |
| `--all` | Export every person in the index. Do not combine with a p-id (they conflict); --all needs no --mode. |
| `--include-living` | Export living/unknown persons in full (default: redacted). |
| `--basic` | For a DNA site: only names, sex, birth/death/marriage dates and places, and family links. No sources, citations or archive IDs leave the archive. |
| `--out` `FILE` | Write to FILE (default: stdout). |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha gedcom import`

```text
Bring a tree from Ancestry (or any genealogy program) into your archive.

  fha gedcom import family-tree.ged            Show the plan; writes nothing
  fha gedcom import family-tree.ged --apply    Do the import

Download your tree as a GEDCOM (.ged) file, then run this. The file itself is
filed as one source (your original untouched); every person becomes a record
with their dates; every statement becomes a suggested fact for you to review
later, family by family. Nothing imported is treated as proven. Photos your
old program linked are looked for in your photo folders. Re-importing
the same file is refused, and a failed import cleans up after itself.

  --plan-out FILE    Also write the full, uncut plan text to FILE.
```

| Argument | What it does |
|---|---|
| `FILE.ged` | The GEDCOM file to import (e.g. an Ancestry tree download). |
| `--apply` | Write the records (default: print the dry-run plan only). |
| `--plan-out` `FILE` | Also write the full (uncapped) plan text to FILE; refused inside the archive except its out/ folder. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

## `fha wikitree`

Render a curated profile to the WikiTree dialect (refs, spacetime spans, links).

```text
Render a person's profile into WikiTree's markup for a family-tree wiki.

  fha wikitree <p-id>              Print the WikiTree-dialect profile
  fha wikitree <p-id> --out FILE   Write it to a file

Turns a curated person's profile prose and its citations into the wiki markup
WikiTree expects. Privacy rules are enforced: restricted material is withheld.
```

| Argument | What it does |
|---|---|
| `p-id` | Curated person to export. |
| `--out` `FILE` | Write to FILE (default: stdout). |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

## `fha process`

Process an original asset into a Source (mint + mark + scaffold)

```text
File a new document or photo into the archive with a permanent ID.

  fha process <file>                        File one document or photo
  fha process <photo> --more <file> ROLE    Attach another file to its source
  fha process refile <s-id> --to photos|documents [--dest SUB]
                                            Move a filed file to the other root
  fha process <photo> --variants one        A photo with its back beside it: file
                                            both as one source without being asked
                                            (or --variants separate)
  fha process <photo> --dest "Cemetery Visit 2021"
                                            File an inbox photo into that folder of
                                            your photo library (default: its top)
  fha process <file> --dry-run              Preview, write nothing

Documents are renamed with their new ID (the old name is kept as provenance);
photos are never renamed. This step is mechanical; drafting claims and
reviewing them come after, through the process-source and review-claims skills.
```

| Argument | What it does |
|---|---|
| `FILE` | Asset file to process (or the processed asset, with --more); a folder triages its photos, or dissolves a notes.md bundle |
| `--root` `PATH` | Archive root |
| `--type` `TYPE` | What kind of source this is, e.g. census (default: other). Any type but photo files the asset in the documents root whatever its extension, so a scanned record supplied as a .jpg is filed as a record. A file already in the photo library is never moved out of it: the record is typed as asked and `fha process refile` is named as the way to move the file |
| `--title` `TITLE` | Source title (also seeds the slug) |
| `--slug` `SLUG` | Explicit filename slug |
| `--date` `DATE` | Source date, e.g. 1880, 1880-06-15, or 'about 1880' |
| `--more` `FILE ROLE` | Attach FILE to the existing source as ROLE[:copy] |
| `--dest` `SUBPATH` | The folder of your photo library to file a photo into, when filing it out of the inbox, e.g. --dest "Cemetery Visit 2021". Without it the photo is filed at the top of the library. Also applies to the photos of a notes.md bundle folder |
| `--people` `p-ids` | Comma-separated p-ids of people in this photo, e.g. "p-de957bcda1,p-ab3c8f0e12". Writes each as a bare keyword in the photo file and populates the source record's people: list. Photos only; use `fha photoindex tag-person` to tag photos already processed. |
| `--physical-location` `HOLDER` | Who keeps the paper original of a scanned document, e.g. "Aunt Ruth". It is recorded on the source with the current month as the date. Without it you are asked when filing a scan (press Enter to skip) |
| `--variants` `{one,separate}` | When the photo has variations beside it (a back, a second print, a crop), process them as one source or as separate sources without being asked: the answer to the [one / separate / skip] question, for scripts and agents |
| `--dry-run` | Preview without writing |

### `fha process refile`

```text
Move a filed source file to the other asset root to correct how it was filed.

  fha process refile s-2b3c4d5e6f --to photos --dest 1880s    records drawer -> photo library
  fha process refile s-2b3c4d5e6f --to documents --type census   photo library -> records
  fha process refile s-2b3c4d5e6f --file scan-back.jpg --to photos --dest 1880s

Fixes a filing decision after the fact: a scan processed as a document that
belongs in the photo library, or a photo that is really a record. The file is
moved, renamed for its new root's rules, and the source record updated, all in
one transaction. Reorganizing within a root is not this command: move files
freely and run `fha reconcile`. Preview with --dry-run first.
```

| Argument | What it does |
|---|---|
| `s-id` | The source whose file is filed in the wrong root, e.g. s-2b3c4d5e6f |
| `--to` `{photos,documents}` | Which root the file belongs in |
| `--file` `NAME` | Which of the record's files to move, by name; needed only when the record lists more than one |
| `--dest` `SUBPATH` | Destination folder under the target root. Required with --to photos (e.g. "1880s" or "Family/Hartley"); with --to documents it defaults to documents/{type}/ |
| `--type` `TYPE` | What kind of source this really is, e.g. census. Rewrites source_type: and moves the record to sources/{type}/ in the same transaction. Required with --to documents when the record is still typed photo, because a scan leaving the photo library is not a family photo any more |
| `--yes` | Skip the photo-catalog confirmation (--to documents) |
| `--dry-run` | Preview without writing |
| `--root` `PATH` | Archive root |

## `fha capture`

Capture an open web record page into an inbox source stub

```text
Clip an open web-record page into your inbox to process later.

  fha capture --url URL              Capture a page (HTML piped on stdin)
  fha capture --asset saved.html     Capture from a saved page file
  fha capture --path PATH            Register a file that must never move
  fha capture --ingest               Sweep staged capture bundles into the inbox

Writes a stub in inbox/, never a finished source; a later `fha process` turns it
into a real record. It reads only the page you already have and never logs in.
--path is different: it never reads anything, it just remembers where a file
(often a photo) already lives, so it enters the queue without being moved.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root |
| `--url` `URL` | Source page URL (the record being captured) |
| `--title` `TITLE` | Override the captured title |
| `--type` `TYPE` | Override the inferred source_type (controlled vocabulary) |
| `--date` `DATE` | Override the source's own date, e.g. 1880 or 'about 1880' |
| `--asset` `FILE` | Asset to stage alongside the stub (an image, or the saved page HTML) |
| `--path` `PATH` | Register a file that must stay exactly where it is (a photo library is never reorganized). Writes a pointer stub only; never moves, renames, or reads the file itself |
| `--note` `TEXT` | A note to record with --path (goes in the stub body) |
| `--ingest` `[DIR]` | Sweep staged capture bundles from DIR (default: the capture_staging folder or ~/Downloads/fha-inbox) into the inbox |
| `--host` | Run as the browser native-messaging host (stdin/stdout framed JSON); launched by the companion, not by hand |
| `--install-host` | Register the native-messaging host manifest for the browser |
| `--extension-id` `ID` | Companion extension id for --install-host allowed_origins |
| `--host-manifest-dir` `DIR` | Override where --install-host writes the manifest (default: the browser's native-messaging hosts dir) |
| `--browser` `{chrome,edge}` | Target browser for --install-host paths/registry (default: chrome) |
| `--dry-run` | Preview without writing |

## `fha convert-mining`

```text
Migrate a legacy transcript-mining project into archive records (one-time).

  fha convert-mining            Print the conversion plan (dry run, writes nothing)
  fha convert-mining --apply    Write the records

Dry-run by default. It reads a `mining/` folder of old text files and turns it
into sources, suggested claims, stories, and questions. Run it once: re-applying
mints fresh IDs and would duplicate everything, so review the plan, then apply.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (contains mining/) |
| `--apply` | Write the records (default: dry-run plan only) |

## `fha claim`

Review claims: move status (one c-id or several) and/or correct fields (your decision, written to the record)

```text
Record your verdict on a suggested fact.

  fha claim <c-id> --status accepted   Confirm a fact (stamps today's date)
  fha claim <c-id> --status disputed|rejected|superseded
  fha claim <c-id> --status suggested  Looked at it, cannot settle it yet
                                       (stays suggested, dated today)
  fha claim c-a c-b c-c --status accepted   One verdict for several claims at once
  fha claim <c-id> --place l-baba9801fa   Correct a field without touching status
  fha claim <c-id> --clear place_text     Remove an optional field (repeatable)

Only you move a claim to accepted. Nothing becomes a fact until you decide here.
Accepting a death or burial claim also marks the person who died as no longer
living, so shared exports and the site can include them (--keep-living to
leave the flag alone); someone marked as living is never changed.
At least one of --status/--value/--date/--type/--place/--place-text/--persons/
--confidence/--information/--evidence/--anchor/--notes/--clear is required.
--clear FIELD removes an optional field (place, place_text, date, information,
evidence, anchor, notes); an empty --place-text/--anchor/--notes is refused
rather than written as an empty line, and --value can be corrected but never
emptied (every claim needs one). Several
c-ids at once move status only: every id is checked first, and one bad id
stops the whole batch before anything is written; field corrections take
exactly one claim. Preview any change first with --dry-run.

To mint a brand-new claim onto a source, use `fha claim new` (`fha claim new --help`).
```

| Argument | What it does |
|---|---|
| `c-id [c-id ...]` | The claim(s) to review (e.g. c-fd0000001a). Several c-ids apply one status move to each. Batches are status-only, and any bad id stops the whole batch before anything is written. |
| `--status` `{accepted,disputed,rejected,superseded,suggested}` | The review status to set; `suggested` parks a claim you looked at but cannot settle yet (it stays suggested, dated today). At least one of --status/--value/--date/--type/--place/--place-text/--persons/--confidence/--information/--evidence/--anchor/--notes/--clear is required. |
| `--reviewed` `DATE` | Review date (YYYY-MM-DD); defaults to today. Only takes effect together with --status. |
| `--reviewed-by` `NAME` | Who made this review decision, written as reviewed_by: on the claim (SPEC §8.4). Defaults to "human"; give a name when several researchers share the archive. Only takes effect together with --status. |
| `--value` `TEXT` | Optionally correct the claim value (single-line scalar only). |
| `--date` `EDTF` | Optionally correct the claim's date (EDTF, e.g. 1880 or 1880-06-15). |
| `--type` `TYPE` | Optionally correct the claim type (SPEC §8.2). Changing TO relationship is refused (see `fha claim --help`). |
| `--place` `l-id` | Optionally set the normalized place link. An existing place_text: (the place as the source wrote it) is left untouched; the two coexist (SPEC §15). |
| `--place-text` `TEXT` | Optionally set the place as written in the source. An existing place: link is left untouched. |
| `--persons` `p-id[,p-id...]` | Optionally replace the whole persons: list, comma-separated p-ids (every id must already have a person record). |
| `--confidence` `{high,medium,low}` | Optionally set the evidence-quality level (SPEC §8.5; separate from --status, the review state). |
| `--information` `{primary,secondary,undetermined}` | Optionally set the Mills information value (SPEC §8.5): was the informant behind this assertion there at the time (primary), reporting secondhand (secondary), or unknown. |
| `--evidence` `{direct,indirect,negative}` | Optionally set the Mills evidence value (SPEC §8.5): does the source answer the question directly (direct), only through correlation (indirect), or is it evidence of an absence (negative). |
| `--anchor` `TEXT` | Optionally set the position inside the source this claim rests on (a timestamp, page, or line, e.g. "00:14:32" or "p. 12"). |
| `--notes` `TEXT` | Optionally replace the claim's notes: (the context or detail behind it). Safe even when the existing notes: is a multi-line block (notes: > or notes: \|); the whole field is replaced, never appended. |
| `--clear` `FIELD` | Remove an optional field from the claim (repeatable): one of place, place_text, date, information, evidence, anchor, notes. The field goes whole (a multi-line value with it); comments and the other keys stay as written. Required fields (value, type, persons, status, confidence) cannot be cleared; correct them with their own flag. |
| `--confirm-deceased` | Accept a death or burial claim for a person marked living: true, after you have checked the record really is their death. The claim is accepted and the person marked as no longer living. Without it such a claim is refused, so a record put on the wrong person never marks someone living as dead. |
| `--keep-living` | Accepting a death or burial claim normally marks the person who died as no longer living (living: false), so shared exports and the site can include them. Pass this to accept the claim and leave their living flag as it is. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the YAML change without writing. |

### `fha claim new`

```text
Mint a brand-new claim onto an existing source record.

  fha claim new --source s-… --type occupation --value "Bookkeeper, 1874 directory" \
      --persons p-… --date 1874 --status accepted

Defaults to --status accepted (typing this command is the review). Pass
--status suggested for an AI-drafted claim awaiting human review.
--type relationship is refused because it needs a roles: map this command does not
build; use `fha person relate` (hypothesis) or `fha confirm cooccur` (sourced)
instead. --information/--evidence/--anchor/--notes (SPEC §8.4/§8.5) mint the
Mills analysis fields and context note in the same step, instead of needing a
hand-edit right after minting. Preview any change first with --dry-run.
```

| Argument | What it does |
|---|---|
| `--source` `s-id` | The source this claim is cited to (e.g. s-fa1234567b). |
| `--type` `TYPE` | The claim type (SPEC §8.2). relationship is refused (see below). |
| `--value` `TEXT` | The human-readable summary of the assertion. |
| `--date` `DATE` | When it happened: EDTF (1880-06-15) or plain words like "about 1880". |
| `--place` `l-id` | The place, by its registry id (e.g. l-baba9801fa). |
| `--place-text` `TEXT` | The place as written in the source: alone when it has no l-id yet, or beside --place to keep the wording too. |
| `--persons` `p-id[,p-id...]` | Who the claim is about, comma-separated p-ids. Optional, but lint will flag the claim until at least one is linked. |
| `--negated` | Mint a confirmed absence (SPEC §8.6): "we researched and it did not happen". For example --type marriage --negated --value "no marriage found" records confirmed-never-married. Writes negated: true and its pairing evidence: negative on the claim. |
| `--subtype` `WORD` | Free-text refinement of the type (e.g. deacon for occupation). |
| `--status` `{suggested,accepted}` | Review state to mint at (default: accepted, because a person typing this command is the review; AI callers must pass --status suggested). |
| `--confidence` `{high,medium,low}` | Evidence-quality level (SPEC §8.5). Defaults from the source's source_type (vital-record: high, interview: low, else medium). |
| `--information` `{primary,secondary,undetermined}` | Mills information value (SPEC §8.5): was the informant behind this assertion there at the time (primary), reporting secondhand (secondary), or unknown. Optional; omit if undetermined. |
| `--evidence` `{direct,indirect,negative}` | Mills evidence value (SPEC §8.5): does the source answer the question directly (direct), only through correlation (indirect), or is it evidence of an absence (negative). --negated sets this to negative automatically, and --evidence must agree if both are given. |
| `--anchor` `TEXT` | Position inside the source this claim rests on (a timestamp, page, or line, e.g. "00:14:32" or "p. 12"). |
| `--notes` `TEXT` | The context or detail behind the claim. Expected, though not required, on a substantive or low-confidence vital claim (lint W109 flags an accepted one with none). |
| `--confirm-deceased` | Accept a death or burial claim for a person marked living: true, after you have checked the record really is their death. The claim is accepted and the person marked as no longer living. Without it such a claim is refused, so a record put on the wrong person never marks someone living as dead. |
| `--keep-living` | Accepting a death or burial claim normally marks the person who died as no longer living (living: false), so shared exports and the site can include them. Pass this to accept the claim and leave their living flag as it is. |
| `--reviewed-by` `NAME` | Who accepted the claim, written as reviewed_by: (SPEC §8.4). Defaults to "human" on an accepted claim; give a name when several researchers share the archive. Refused with --status suggested. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the claim block that would be appended, without writing. |

## `fha confirm`

Write back a decision the human made (xref/cooccur/dismiss/undismiss/place/discovery/draft/merge)

```text
Write down a suggestion you've decided to accept.

  fha confirm xref <c-a> <c-b> --as corroborates|contradicts
  fha confirm cooccur <p-a> <p-b> --source <s-id> --subtype friend|associate|neighbor
  fha confirm dismiss <p-a> <p-b> [--not-same-person]
  fha confirm undismiss <p-a> <p-b> [--not-same-person]   (or: --list)
  fha confirm place <c-id...> (--name NAME | --into <l-id>)
  fha confirm discovery "<text>"
  fha confirm draft <p-id>
  fha confirm merge <p-merged> --into <p-survivor> --reason "<why>"

Each verb writes one kind of decision you have made into the records: a
link, a connection, a place, a note, a draft, or an identity merge. Every verb
previews with --dry-run first.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha confirm xref`

Confirm an xref pair: write corroborates/contradicts links

| Argument | What it does |
|---|---|
| `c-id-a` | First claim (e.g. c-fd0000001a). |
| `c-id-b` | Second claim. |
| `--as` `{corroborates,contradicts}` | The link to write between the two claims. |
| `--dry-run` | Preview the change without writing. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha confirm cooccur`

Confirm a co-occurrence pair: mint a relationship claim

| Argument | What it does |
|---|---|
| `p-id-a` | First person. |
| `p-id-b` | Second person. |
| `--source` `s-id` | The source that supports this connection (cited on the claim). |
| `--subtype` `{friend,associate,neighbor}` | The social relationship subtype. |
| `--accept` | Mint the claim accepted (treat this confirm as the review) rather than suggested. |
| `--reviewed` `DATE` | Review date for --accept (YYYY-MM-DD); defaults to today. |
| `--dry-run` | Preview the change without writing. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha confirm dismiss`

Remember a pair: not connected, or (--not-same-person) two different people

| Argument | What it does |
|---|---|
| `p-id-a` | First person. |
| `p-id-b` | Second person. |
| `--not-same-person` | Record that these are two different people, so fha report stops listing them as a possible duplicate. |
| `--dry-run` | Preview the change without writing. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha confirm undismiss`

Take a remembered pair back out, or list every one the archive remembers

| Argument | What it does |
|---|---|
| `[p-id-a]` | First person. |
| `[p-id-b]` | Second person. |
| `--not-same-person` | Undo a "two different people" note instead of a "not connected" pair. |
| `--list` | List every remembered pair with the command that undoes it. |
| `--dry-run` | Preview the change without writing. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha confirm place`

Register a place-text cluster and relink its claims

| Argument | What it does |
|---|---|
| `c-id [c-id ...]` | Claim(s) to relink to the place. |
| `--name` `NAME` | Name for a new place (required unless --into). |
| `--hierarchy` `TEXT` | Optional hierarchy, e.g. "Topeka, Kansas, USA". |
| `--into` `l-id` | Merge into this existing place instead of minting. |
| `--dry-run` | Preview the change without writing. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha confirm discovery`

Append a dated entry to notes/discoveries.md

| Argument | What it does |
|---|---|
| `TEXT` | What was discovered. |
| `--refs` `IDS` | Comma-separated S-/P-/C-/L-/H- refs. |
| `--dry-run` | Preview the change without writing. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha confirm draft`

Accept AI-DRAFT prose markers in a person profile

| Argument | What it does |
|---|---|
| `p-id` | The person whose profile to accept drafts in. |
| `--dry-run` | Preview the change without writing. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha confirm merge`

Enact a human-confirmed identity merge: tombstone + rename + fold + relink (SPEC §9)

| Argument | What it does |
|---|---|
| `p-merged` | The duplicate record that becomes the tombstone. |
| `--into` `p-survivor` | The record that keeps the canonical home. |
| `--reason` `TEXT` | Why these are one person; stored as merge_reason: on the tombstone. |
| `--dry-run` | Preview every edit and the rename without writing. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

## `fha person`

Change a person record: new, promote, rename, set-living, set-profile-photo, set-sex, relate, estimate, edit, note

```text
Update a person's record directly, one field or section at a time.

  fha person new "Full Name" [--sex M|F|intersex|unknown] [--gender TEXT]
  fha person promote <p-id> [--into FOLDER]
  fha person rename <p-id> "New Name" [--keep-variant]
  fha person set-living <p-id> true|false|unknown
  fha person relate <p-id> --parent|--child|--sibling|--spouse <p-id2>
  fha person estimate <p-id> --birth DATE --death DATE
  fha person edit <p-id> --section biography|stories|research --text TEXT
  fha person note <p-id> --section stories|research --text TEXT
  fha person edit-note <p-id> --section stories|research --old-text TEXT --text TEXT

new mints a brand-new person and writes their stub record. promote graduates
a direct-line stub to curated (tier flip, couple-folder filing, research
file). rename corrects a name: filename, research companion, stale generated
companions, and (when unambiguous) the couple folder's own text all follow.
set-living marks a person as living, passed away, or unknown (drives
export privacy). relate jots an unsourced family-tie belief. estimate writes
a provisional, unsourced birth/death date. edit and note add or replace
prose in the curated profile's Biography, Stories, and Research Notes
sections.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha person new`

Mint a brand-new person and write their stub record.

```text
Mint a brand-new person in one command.

  fha person new "Jane Doe"
  fha person new "Jane Doe" --sex F --birth 1870 --death 1940
  fha person new "Cortez" --gender "two-spirit"
  fha person new "Maria Garcia Lopez" --surname "Garcia Lopez"

Mints one p-id, writes a stub under people/stubs/, and reports where it
landed. --sex accepts M, F, intersex, or unknown (case-insensitive for M/F).
--gender is free text; omit it unless there is something to record.
--birth/--death record provisional, unsourced estimates: the archive's exact
date form (1870, 1870-06) or plain words ("circa 1870", "before 1940") -
loose wording is translated for you. A generational suffix (Jr, Sr, II, III,
IV, V, Esq) in the name is recognised automatically and never taken as the
surname. --surname overrides the filename's surname split outright, for
names no automatic split can be expected to get right (Spanish double
surnames, particles like "van der", surname-first conventions), and
--surname "" files the person with no surname at all (__henry_tudor_...).
A regnal number VI-XXX in capitals ("Henry VII", "Louis XIV") is read as a
suffix too, not a surname, unless the whole name is in capitals. A surname
that is written in capitals and looks like one ("Jinping XI", "Thi VI")
needs --surname XI to be filed under it. name: is written exactly as given
either way. A name that already has a record (same spelling, ignoring case and accents) is refused so the same person is
never filed twice; add --force when it really is someone else who shares
the name. Use `fha person relate` next to tie the new person to family, and
`fha find` to confirm the record.

  fha person new --from-file people.tsv

mints a whole list at once: one person per line, tab-separated columns name,
sex (optional) and surname override (optional; - for no surname), a header
row "name sex surname" allowed. Every row is checked first, so one bad row
means nothing is minted, and the duplicate-name check covers the whole list.
```

| Argument | What it does |
|---|---|
| `[NAME]` | The person's full name, e.g. "Jane Doe". Omit with --from-file. |
| `--from-file` `PATH` | Mint everyone in a tab-separated list instead: one person per line, columns name, sex (optional), surname override (optional; - for no surname). A bad row refuses the whole file. |
| `--surname` `TEXT` | Override the filename's surname split, for names no automatic split can be expected to get right (Spanish double surnames, particles like "van der", surname-first conventions). --surname "" means no surname at all (the file name leads with __, e.g. __henry_tudor_P-....md). name: is written exactly as given either way. |
| `--sex` `M|F|intersex|unknown` | Birth-assigned sex, if known (case-insensitive for M/F). |
| `--gender` `TEXT` | Free-text gender/identity; omit unless there is something to record. |
| `--birth` `DATE` | A provisional, unsourced birth date or estimate. |
| `--birth-place` `PLACE` | A provisional, unsourced birth place (free text). |
| `--death` `DATE` | A provisional, unsourced death date or estimate. |
| `--death-place` `PLACE` | A provisional, unsourced death place (free text). |
| `--force` | Mint even when this name already has a record, for two different people who share a name (a father and son). |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the new record; write and mint nothing persistent. |

### `fha person promote`

Graduate a stub to curated: tier flip and filing (couple folder or connections/).

```text
Graduate a stub to a curated person: tier flip and filing in one step.

  fha person promote p-2b3c4d5e6f
  fha person promote p-2b3c4d5e6f --dry-run
  fha person promote p-2b3c4d5e6f --into "040 Thomas Hartley + Margaret Cole"
  fha person promote p-2b3c4d5e6f --into connections/

Flips the record's tier to curated and adds any of the standard profile
sections it is still missing. A direct-line ancestor moves out of people/stubs/ into their
Ahnentafel couple folder (derived from root_person in fha.yaml; the folder is
created if it does not exist yet). Anyone with no derived position (siblings,
in-laws, friends, associates and neighbors) can still be promoted: --into
connections/ files them flat in
people/connections/ instead (SPEC §12.3), no numbering. Call with no --into at
all and a non-direct person is refused plainly, naming that command (their
stub is a legitimate resting state either way). --into overrides the
destination for a direct-line person only, and still names a folder under
people/, never stubs/. An existing research companion travels with the record;
none is ever scaffolded fresh. Requires a fresh index (`fha index`).
Preview everything first with --dry-run; a failed write rolls itself back.
```

| Argument | What it does |
|---|---|
| `p-id` | The stub to promote (e.g. p-2b3c4d5e6f). |
| `--into` `FOLDER` | File the record in this folder under people/ instead of the derived couple folder: a couple-folder name, or "connections/" for a non-direct person (SPEC §12.3). |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the tier flip, move, and research scaffold without writing. |

### `fha person rename`

Correct a person's name: frontmatter, files, and (when safe) the couple folder.

```text
Correct a person's name everywhere it is mechanically derivable.

  fha person rename p-2b3c4d5e6f "Betty Marsh Vance"
  fha person rename p-2b3c4d5e6f "Betty Marsh Vance" --keep-variant
  fha person rename p-2b3c4d5e6f "Maria Garcia Lopez" --surname "Garcia Lopez"

Rewrites name: (and, with --keep-variant, files the old name into
name_variants: rather than losing it), and renames the profile and research
files to match (SPEC §13's filename grammar, the same deriver `fha person
new` uses). It deletes any stale-named generated companion (timeline,
draft-queue, and an older per-person sources-index file); regenerate them
with `fha views timeline|sources-index|draft-queue <p-id>` or `fha views
refresh`. A record's ## Sources section is an inline region and travels with
the rename. When the old name appears as exactly one unambiguous
partner-name segment of the couple folder's own text, the couple folder is
renamed to match too.
The couple-folder step is the one part that can fail on its own (a program
sitting inside the folder blocks a Windows rename); everything else has
already landed if it does, and the warning names the exact retry. --surname
overrides the filename's surname split, same escape hatch as `fha person
new --surname`. Preview everything first with --dry-run; a failed live
write (before the folder step) rolls itself back.
```

| Argument | What it does |
|---|---|
| `P-id` | The person to rename (e.g. P-2b3c4d5e6f). |
| `NAME` | The corrected full name, e.g. "Betty Marsh Vance". |
| `--keep-variant` | File the OLD name into name_variants: instead of losing it. |
| `--surname` `TEXT` | Override the renamed filename's surname split, for names no automatic split can be expected to get right (Spanish double surnames, particles like "van der", surname-first conventions). --surname "" means no surname at all. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview every rename without writing. |

### `fha person set-living`

Set a person's living flag (true/false/unknown), which decides export privacy.

```text
Mark one person as living, passed away, or unknown, which decides what exports show.

  fha person set-living p-2b3c4d5e6f false    Passed away: exports may include them
  fha person set-living p-2b3c4d5e6f true     Living: redacted from every export
  fha person set-living p-2b3c4d5e6f unknown  Not sure: treated as living (safe default)

Changes exactly one line of the person's record and touches nothing else.
Preview the change first with --dry-run.
```

| Argument | What it does |
|---|---|
| `p-id` | The person to update (e.g. p-2b3c4d5e6f). |
| `true|false|unknown` | The new value. unknown is treated as living (the safe default). |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the one-line change without writing. |

### `fha person set-profile-photo`

Set the portrait shown on a person's page and in the tree.

```text
Set a person's profile_photo: field (a filename, path, or s-id), resolved leniently against the photo catalog when the site is built.
```

| Argument | What it does |
|---|---|
| `p-id` | The person to update (e.g. p-2b3c4d5e6f). |
| `PHOTO` | A filename, path, or s-id identifying the photo. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the one-line change without writing. |

### `fha person set-sex`

Set or correct a person's sex: (M \| F \| intersex \| unknown).

```text
Set a person's sex: field. The numbered family-tree folders (Ahnentafel numbering) read it to place a parent in the father or mother slot. A change ends with the reminder to run `fha index`, then `fha views brackets` (`--realign` applies any folder-number shift). brackets reads the placement out of the index, so the rebuild comes first.
```

| Argument | What it does |
|---|---|
| `p-id` | The person to update (e.g. p-2b3c4d5e6f). |
| `SEX` | M, F, intersex, or unknown (case-insensitive). |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the one-line change without writing. |

### `fha person relate`

Jot an unsourced family-tie belief (parent/child/sibling/spouse).

```text
Jot an unsourced family-tie belief, always recorded as a hypothesis.

  fha person relate p-2b3c4d5e6f --parent p-de957bcda1
  fha person relate p-2b3c4d5e6f --spouse p-cd795c61e0 --reciprocal
  fha person relate p-2b3c4d5e6f --child p-c4b26bb4bc --subtype adoptive
  fha person relate p-2b3c4d5e6f --parent p-de957bcda1 --working-answer h-4f20aa91c3

Appends one entry to the first person's relationships: list, naming the
second person and the role (parent/child/sibling/spouse). --subtype names
the nature of the bond (biological is the default; SPEC §8.2 also lists
adoptive, step, foster, guardian, and more). --reciprocal also writes the
mirrored entry on the second person (parent<->child flips; sibling/spouse
mirror as themselves). --working-answer names the question entry that says
why the guess is believed and what would settle it. This is always an
unsourced belief; a sourced tie comes from an accepted relationship claim,
filed with `fha claim`.
```

| Argument | What it does |
|---|---|
| `p-id` | The person whose relationships: entry gets the new tie. |
| `--parent` `p-id2` | p-id2 is this person's parent. |
| `--child` `p-id2` | p-id2 is this person's child. |
| `--sibling` `p-id2` | p-id2 is this person's sibling. |
| `--spouse` `p-id2` | p-id2 is this person's spouse. |
| `--subtype` `WORD` | The nature of the bond (SPEC §8.2), e.g. biological (default), adoptive, step, foster, guardian. |
| `--reciprocal` | Also write the mirrored entry on p-id2's record. |
| `--working-answer` `h-id` | The working answer this guess rests on (the h- id of its question entry); written as the entry's hypothesis: line. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the change(s) without writing. |

### `fha person estimate`

Write a provisional, unsourced birth/death date and/or place estimate.

```text
Write a provisional, unsourced birth/death estimate: a starting guess for
the when, the where, or both.

  fha person estimate p-2b3c4d5e6f --birth 1870
  fha person estimate p-2b3c4d5e6f --birth "circa 1870" --death "before 1940"
  fha person estimate p-2b3c4d5e6f --birth-place "Kansas"   Place only, no date needed
  fha person estimate p-2b3c4d5e6f --birth -             Clear the birth estimate

DATE accepts the archive's exact date form (1870, 1870-06, 188X for "the
1880s") or plain words ("circa 1870", "before 1940", "the 1880s"); loose
wording is translated for you. --birth-place/--death-place record the where
as free text ("born in Kansas, no idea when" is real family knowledge), with
or without a date. Use - by itself to clear any field. A sourced birth/death
claim always supersedes this estimate; run `fha claim` once you have a
source. At least one of --birth/--death/--birth-place/--death-place is
required.
```

| Argument | What it does |
|---|---|
| `p-id` | The person to update (e.g. p-2b3c4d5e6f). |
| `--birth` `DATE` | A birth date/estimate, or - to clear it. |
| `--birth-place` `PLACE` | A provisional birth place (free text), or - to clear it. |
| `--death` `DATE` | A death date/estimate, or - to clear it. |
| `--death-place` `PLACE` | A provisional death place (free text), or - to clear it. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the change without writing. |

### `fha person edit`

Replace or append to a curated profile section (Biography/Stories/Research Notes).

```text
Replace (or append to) one prose section of a curated profile.

  fha person edit p-2b3c4d5e6f --section biography --text "..."
  fha person edit p-2b3c4d5e6f --section stories --file story.txt --append

--section picks the section: biography (## Biography), stories
(## Stories), or research (## Research Notes). Give the text with --text or
--file (exactly one; no reading from standard input). Replaces the whole
section by default; --append adds the text as a new paragraph at the end
instead. The section is created if it does not exist yet. Frontmatter and
every other section are left byte-for-byte alone.
```

| Argument | What it does |
|---|---|
| `p-id` | The person whose profile gets edited. |
| `--section` `{biography,stories,research}` | Which section to edit. |
| `--text` `TEXT` | The prose to write. |
| `--file` `PATH` | A file already holding the prose. |
| `--append` | Add as a new paragraph at the end instead of replacing. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the change without writing. |

### `fha person note`

Append a quick note to Stories or Research Notes.

```text
Append a quick note to Stories or Research Notes. Nothing you wrote is replaced.

  fha person note p-2b3c4d5e6f --section research --text "Check the 1880 census."

Adds TEXT as a new paragraph at the end of the section (creating it if
missing). Plain markdown, human-written; text you wrote is never rewritten
or removed. The first note replaces the section's "what goes here"
instructions, if they are still there unchanged. Refuses if the section's
<!-- private --> fencing is left unclosed, since it could not tell which
side of it the new text belongs on.
```

| Argument | What it does |
|---|---|
| `p-id` | The person whose profile gets the note. |
| `--section` `{stories,research}` | Which section to append to. |
| `--text` `TEXT` | The note to add. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the change without writing. |

### `fha person edit-note`

Rewrite one existing Stories/Research Notes entry.

```text
Rewrite one existing entry of Stories or Research Notes; the rest is untouched.

  fha person edit-note p-2b3c4d5e6f --section research \
      --old-text "Check the 1880 census." --text "Checked the 1880 census: nothing."

The entry is named by its exact current text (--old-text): give the whole
paragraph, word for word; one sentence of it is not enough. If --old-text is
only part of an entry, is not found, or appears more than once, nothing is
written and the message says which. This command only rewrites; deleting an
entry stays a hand edit to the record file.
```

| Argument | What it does |
|---|---|
| `p-id` | The person whose entry is being corrected. |
| `--section` `{stories,research}` | Which section the entry lives in. |
| `--old-text` `TEXT` | The entry's current text: the whole paragraph, exactly as it stands. |
| `--text` `TEXT` | The corrected entry text. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the change without writing. |

## `fha source`

Change a source record: note (append), edit-note (rewrite one), extract (PDF text layer), clear-keyword (fix a stray keyword), set-primary (choose the main image), merge (two records into one)

```text
Update a source record directly, one field or section at a time, or merge
two records of one piece of evidence into one.

  fha source note s-2b3c4d5e6f --text "..."
  fha source edit-note s-2b3c4d5e6f --old-text "..." --text "..."
  fha source extract s-2b3c4d5e6f [--pages 1-60] [--copy LABEL]
  fha source clear-keyword s-2b3c4d5e6f --keyword "..." [--replace-with "..."]
  fha source set-primary s-2b3c4d5e6f --file "front-scan.tif"
  fha source merge s-2b3c4d5e6f s-7q2x9c4m1k

note appends a hand-written paragraph to a source's ## Notes section;
edit-note rewrites one existing paragraph there (named by its exact current
text) and leaves the rest untouched; extract dumps the source PDF's embedded
text layer into a derived [Page N]-labeled companion file (needs the pypdf
helper package); clear-keyword removes (or corrects) one embedded keyword on
a documents-root file, the tool-mediated fix for a stray tag `fha lint
--with-exif` (W131) finds; set-primary chooses which photo is a source's main
image (its front, not its back), the fix `fha doctor` names; merge folds the
second source into the first (files, claims, notes) and removes its record.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha source note`

Append a paragraph to a source record's ## Notes section.

```text
Add a note to the end of a source's ## Notes section.

  fha source note s-2b3c4d5e6f --text "Found in Grandma's cedar chest, 2024."

Always append-only: an existing note is never edited or removed, and nothing
outside ## Notes changes (the frontmatter and ## Claims are never touched).
A source marked superseded still accepts notes.
Preview the change first with --dry-run.
```

| Argument | What it does |
|---|---|
| `s-id` | The source to update (e.g. s-2b3c4d5e6f). |
| `--text` `TEXT` | The note to add, in your own words. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the change without writing. |

### `fha source edit-note`

Rewrite one existing paragraph of a source's ## Notes.

```text
Rewrite one existing ## Notes paragraph; the rest of the section is untouched.

  fha source edit-note s-2b3c4d5e6f \
      --old-text "Found in the cedar chest." --text "Found in Grandma's cedar chest, 2024."

The note is named by its exact current text (--old-text): give the whole
paragraph, word for word; one sentence of it is not enough. If --old-text is
only part of a note, is not found, or appears more than once, nothing is
written and the message says which. This command only rewrites; deleting a
note stays a hand edit to the record file. Preview first with --dry-run.
```

| Argument | What it does |
|---|---|
| `s-id` | The source whose note is being corrected. |
| `--old-text` `TEXT` | The note's current text: the whole paragraph, exactly as it stands. |
| `--text` `TEXT` | The corrected note text. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the change without writing. |

### `fha source extract`

Dump a source PDF's embedded text layer into a derived companion file.

```text
Reads the source's PDF (its primary file, or the only PDF it lists; when a second digitization is attached as primary:LABEL, the one without a label unless --copy names another) and writes the embedded text layer, page by page, into a [Page N]-labeled companion the assistant can mine like a transcript, in seconds instead of reading hundreds of pages by eye. The PDF itself is never touched. Needs the pypdf helper package (python -m pip install pypdf).
```

| Argument | What it does |
|---|---|
| `s-id` | The source whose PDF to extract (e.g. s-2b3c4d5e6f). |
| `--pages` `RANGES` | Only these pages, e.g. `1-60` or `1-60,102,200-210` (default: every page). |
| `--copy` `LABEL` | Read the PDF whose files: entry carries this copy: label (e.g. `familysearch`, the label given to `fha process --more FILE primary:familysearch`) instead of the unlabeled primary. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the dump (page coverage, destination) without writing. |

### `fha source clear-keyword`

Clear or correct one embedded keyword on a source's documents-root file.

```text
Clear or correct one embedded keyword on a source's documents-root file.

  fha source clear-keyword s-2b3c4d5e6f --keyword "Margaret Hartley"
  fha source clear-keyword s-2b3c4d5e6f --keyword "Margaret Hartley" \
      --replace-with "Margaret Cole"

Reads the file's keyword fields (IPTC Keywords, XMP dc:subject and
Lightroom's hierarchical keywords), matches --keyword case-insensitively, and
removes the exact on-file value from every field it sits in, so a keyword
typed slightly differently on the command line refuses instead of silently
doing nothing. --replace-with writes the correction into every keyword field
the file takes, skipping one that already names it (a Lightroom "People|..."
entry counts). The preview shows exactly what each field will hold.
Documents-root only (`fha lint --with-exif` W131 finds candidates);
--file NAME picks among several files listed on the source. Preview first
with --dry-run.
```

| Argument | What it does |
|---|---|
| `s-id` | The source whose documents-root file to correct. |
| `--keyword` `TEXT` | The keyword to remove, exactly as it reads (matched case-insensitively against the file). |
| `--replace-with` `TEXT` | Add this keyword in place of the one removed, in every keyword field that does not already name it. Omit to just clear it. |
| `--file` `NAME` | Which documents-root file to correct, when the source lists more than one (a filename or its files: alias). |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the change without writing. |

### `fha source set-primary`

Choose which photo is a source's main image (e.g. its front, not its back).

```text
Choose which photo is the main image of a source (its front, not its back).

  fha source set-primary s-2b3c4d5e6f --file "Smith-Farm-01400.tif" --dry-run
  fha source set-primary s-2b3c4d5e6f --file "Smith-Farm-01400.tif"

Changes only the source record's files: list: the named photo becomes the
main image (is_primary: true), and the photo that was the main
image is listed by what its name says it is (role: back, say). No photo is
renamed or touched, and the record's title and filename stay as they are.
`fha doctor` names the records whose main image is a photo's back. Preview
first with --dry-run.
```

| Argument | What it does |
|---|---|
| `s-id` | The source whose main image to change (e.g. s-2b3c4d5e6f). |
| `--file` `NAME` | The photo to make the main image: its filename, or its full files: path when two photos share a name. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the change without writing. |

### `fha source merge`

Make two source records for one piece of evidence into one.

```text
Make two source records for one piece of evidence into one.

  fha source merge s-2b3c4d5e6f s-7q2x9c4m1k --dry-run
  fha source merge s-2b3c4d5e6f s-7q2x9c4m1k

The first id is the record to keep, the second the one folded into it: an
index entry and the document it indexes, or one record saved from two sites.
The second record's files join the first (documents are renamed for it,
photos keep their names), with their SOURCE: keyword changed to match; its
claims move over with their ids; its notes, people, places, web links and AI
passes are added; and its record file is removed. Its id stays in the kept
record's aliases:, so every old link still finds it, and a merged_from:
entry lists what moved.

Refused, with nothing written, when the two records' accepted claims are
about different people. Preview first with --dry-run, then run `fha index`.
```

| Argument | What it does |
|---|---|
| `keeper` | The source to keep (e.g. s-2b3c4d5e6f). |
| `retiree` | The source folded into it and then removed (e.g. s-7q2x9c4m1k). |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the merge without writing. |

## `fha site`

Generate the static HTML family explorer (standalone snapshot or linked preview).

```text
Build a browsable family website you can open in any browser.

  fha site                Build the shareable snapshot (redacted, self-contained)
  fha site --standalone   The same shareable snapshot, named explicitly
  fha site --linked       An unredacted local preview (for yourself, not to share)

The site opens from a plain file with no server: build it, then open it.
Living people and restricted material are redacted by default.
```

| Argument | What it does |
|---|---|
| `--out` `PATH` | Output directory (default: generated/site/, or generated/site-linked/ with --linked). |
| `--standalone` | Self-contained, redacted snapshot safe to share (default). |
| `--linked` | Local developer preview: real paths, no copies, no redaction. |
| `--dry-run` | Report how many pages would be built and what a rebuild would first remove from the output folder, without writing anything. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

## `fha serve`

Open the localhost workbench, a private, editable view of the archive.

```text
Start a local, private web workbench for this archive.

  fha serve                 open it in your browser on 127.0.0.1:8765
  fha serve --port 8766     use a different port
  fha serve --no-browser    start it but do not open a browser

It runs on this machine only, with no network and no login. Every button is an fha command, previewed before it writes. Stop it with Ctrl-C; nothing is lost.
```

| Argument | What it does |
|---|---|
| `--port` `PORT` | Port to listen on (default 8765). |
| `--no-browser` | Do not open a web browser on startup. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

## `fha install`

Bootstrap a new private archive with the plaintext operating layer.

```text
Copy the plaintext tools, rulebooks, and docs into a new archive and stamp it. Run this once from your clone (or unzipped download) of the public tools. Afterwards, refresh with `fha update-tools`.
```

| Argument | What it does |
|---|---|
| `ARCHIVE-PATH` | Folder for your new archive (created if it does not exist). |
| `--repo` `PATH` | Your copy of the plaintext tools (folder with manifest.json). Defaults to the tools you are running from. |
| `--dry-run` | Preview what would be installed; write nothing. |

## `fha update-tools`

Refresh an archive's tools/rulebooks from an updated public clone.

```text
Compare your archive against a newer copy of the public tools and pull in improvements. Never deletes and never overwrites your edits: anything you customized is backed up first. Run from inside your archive with --repo pointing at the updated tools.
```

| Argument | What it does |
|---|---|
| `--repo` `PATH` | Your updated copy of the plaintext tools (folder with manifest.json). |
| `--dry-run` | Preview the update plan; write nothing. |
| `--verbose` | Also list files that are already up to date. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

## `fha working-copy`

Manage working-copy mode (synced archive without asset files).

```text
Work on a synced copy of the archive that has your records but not the big
photo and document files.

  fha working-copy on       Turn on working-copy mode (asset features pause)
  fha working-copy off      Turn it back off (asset files must be present)
  fha working-copy status   Report whether this copy is in working-copy mode

Use this on a second machine that syncs the text records but not the originals.
Read-only commands work normally; commands that touch photos/documents pause.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root. |

### `fha working-copy on`

Activate working-copy mode.

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root. |

### `fha working-copy off`

Deactivate working-copy mode (prompts for confirmation).

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root. |
| `--yes` | Skip the confirmation prompt. |

### `fha working-copy status`

Report whether working-copy mode is active.

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root. |

## `fha normalize-links`

Settle prose citations to the canonical [[ID]] / [[ID\|name]] form

```text
Tidy the citations and cross-links in your prose to the archive's [[ ]] form,
and write each source's people:/places: and each claim's persons: one per line
with the name beside the ID, so a record says who it is about.

  fha normalize-links            Preview the rewrites (dry run, writes nothing)
  fha normalize-links --write    Apply them, showing the same diff
  fha normalize-links --obsidian --write
                                 Also point each person, source, claim and
                                 place link at its file, so Obsidian can
                                 follow it

Runs as a preview by default; a real write needs --write. Your original names
are kept as aliases, so the shortened links still resolve. With links: obsidian
in fha.yaml, every run also does what --obsidian does. A place link names its
place note once `fha views places` has written one; until then it keeps its
ID form.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root |
| `--write` | Apply the rewrites (default is a dry-run preview) |
| `--dry-run` | Preview without writing (already the default; accepted so the always-preview habit works here too) |
| `--quiet` | Suppress the per-file diff (show only the summary) |
| `--obsidian` | Point each person, source, claim and place link at the record's file (Obsidian link mode); already on when fha.yaml says links: obsidian |

## `fha backup`

Zip the archive into a dated backup beside it (restore = unzip).

```text
Copy your whole archive into one dated zip file, kept outside the archive.

  fha backup                      records-only zip in a folder beside the archive
  fha backup --include-assets     also pack the photos/documents roots
  fha backup --to D:/Backups      choose where the zip goes
  fha backup --dry-run            show the plan; write nothing

If any folder cannot be read, no zip is written at all, because a backup
missing part of your archive would look exactly like a good one. That covers
a folder that will not open, one that is a shortcut to somewhere else, and
(with --include-assets) a photos or documents folder that is not there.
Reconnect the drive or fix the folder and run it again; --allow-incomplete
writes it anyway, clearly marked, for a folder that is gone for good.

Photos and documents are not included unless you pass --include-assets (they
are often huge and often live on another drive; the output names them every
time). To restore a backup, unzip it.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root |
| `--to` `PATH` | Folder to write the zip into (default: a folder named {archive}-backups beside the archive; fha.yaml `backup: path:` also sets this) |
| `--include-assets` | Also pack the photos/documents roots into the zip (default: records only) |
| `--dry-run` | Print the full plan and write nothing |
| `--allow-incomplete` | Write the backup even if a folder could not be read (the zip is named ...-INCOMPLETE.zip and lists what is missing inside it) |

## `fha reconcile`

Re-tie moved documents (and photos) to their records

```text
Re-tie moved files to their records. Rearrange the documents folder any way you like, then run this: every filed document is found again by the ID in its name and its record updated. A filed photo moved within the photos folder is found again by its name and the SOURCE: keyword inside it (from the photo catalog, or read with --with-exif), and the photo catalog is reconciled too when one exists. Preview with --dry-run first.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root |
| `--dry-run` | Preview every re-tie without writing anything |
| `--with-exif` | Photos: read the SOURCE: keyword inside photos the catalog does not cover, to find moved photos (needs exiftool; see fha photoindex reconcile) |

## `fha media`

Content-hash dedupe and container-metadata probe for recordings

```text
Answer a question about an incoming recording before it is filed.

  fha media dedupe <file...> [--json PATH]
  fha media probe  <file> [--json]

Both are read-only. `dedupe` content-hashes incoming media against everything
already filed (size first, SHA-256 only on a size collision) so a phone
export renamed by the app never becomes a second source record for one
recording. `probe` reads a recording's true duration and container
creation_time, and derives its local start time without guessing the
timezone the recording was made in.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha media dedupe`

Content-hash incoming recording(s) against what is already filed

| Argument | What it does |
|---|---|
| `FILE_OR_FOLDER [FILE_OR_FOLDER ...]` | the recording(s) that arrived (files, or folders to walk) |
| `--json` `PATH` | also write the findings as JSON (a filename of its own; a path landing on a recording, an archived file, or fha.yaml is refused) |
| `--quiet` | print only warnings/errors |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha media probe`

Read a recording's true duration and container creation timestamp

| Argument | What it does |
|---|---|
| `FILE` | the recording to probe |
| `--json` | machine-readable JSON instead of plain-language lines |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

## `fha reorganize`

Bulk-tidy the documents/ root (never touches a human-organized folder)

```text
Tidy the documents/ root: group loose, machine-filed documents into documents/{type}/ (mirroring how sources/{type}/ is already organized), and give a source with many files its own subfolder. Every source's files: entry is updated in the same step as the physical move, so nothing ever points at the wrong place.

Safety, read this before running with --apply: a file inside any folder a person made by hand, however shallow, is never moved and never even proposed as a candidate. Only material still sitting exactly where `fha process` originally filed it (flat at the documents root, or one level down in its own type folder) is eligible. If you (or anyone) moved something by hand, this tool leaves it alone on purpose: moving a file someone organized deliberately would be far worse than leaving something untidy.

Always previews first: run with no flags (or --dry-run) to see the full plan and change nothing. Add --apply to actually move files, in bounded batches, re-checked with fha reconcile after each one. If anything looks wrong afterward, the run stops right there and reports it, without touching anything further. Photos are out of scope: SPEC identifies a photo by its embedded keyword, never by its path, so there is no folder to tidy; fha reconcile's own photo pass already re-verifies photo identity after any move you make by hand.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root |
| `--dry-run` | Preview the plan only and change nothing (also the default with no --apply) |
| `--apply` | Actually move files and update records (previews only without this) |
| `--yes` | Skip the [y/N] confirmation before applying |
| `--batch-size` `N` | Files per batch before re-checking with fha reconcile (default 25) |
| `--group-threshold` `N` | A source with more than N eligible files gets its own subfolder (default 3; 0 means every non-empty source gets its own subfolder, never a shared type folder) |
| `--limit` `N` | Only plan/apply the first N eligible files this run, whole records only: a record whose own files would cross N is skipped, never split (default: no cap) |
