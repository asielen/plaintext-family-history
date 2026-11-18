# fha command reference

> **Generated - do not edit.** Written by `build_commands_doc.py` from the
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
| `ID` | ID to locate (e.g. P-de957bcda1) |
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
| `--source` `S-ID` | Upsert only this source (incremental mode) |
| `-v`, `--verbose` | Show progress (full rebuilds only; ignored with --source) |

## `fha lint`

Check the archive for problems and report them (alias: `fha check`)

```text
Check the archive for problems and report them (a spell-check for your data).

  fha lint                Walk and check the archive; report only
  fha check               The same command, plainer name
  fha lint --fix-ids      Give IDs to records you named in plain English
  fha lint --mint-stubs   Create placeholder records for people named but not filed

Catches broken links, missing dates, contradictions, and people referenced but
never filed. Run it any time something feels off. Exit: 0 clean, 1 warnings,
2 errors, 3 tool failure.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (overrides auto-detection) |
| `--spec-root` `PATH` | Spec docs root (when separate from archive root) |
| `--with-exif` | Also verify embedded SOURCE: keywords and stray documents-root person keywords (W131) via exiftool (slow) |
| `--json` | Machine-readable JSON output |
| `--format-check` | Check formatting only (no fixes) |
| `--format-write` | Apply conservative formatting fixes |
| `--dry-run` | Preview mutating operations without writing |
| `--mint-stubs` | Create person stubs for E005 set |
| `--spawn-questions` | Append questions to notes/questions.md for E009 contradictions |
| `--fix-claims-fence` | Wrap hand-written claims that forgot the ```yaml fence (with --dry-run to preview) |
| `--fix-ids` | Mint IDs for hand-authored id-less records (rename, keep the old name as an alias) and for id-less claims inside sources (with --dry-run to preview) |
| `--fix-reciprocal` | Add the missing mirror edge for each W116 (with --dry-run to preview) |
| `--fix-surnames` | Write the surname: line each W138 file name implies, so the record keeps the surname its file name was sorted under (with --dry-run to preview) |

## `fha stubs`

Mint person stubs for unresolved P-id references

```text
Create placeholder records for people who are named but not yet filed.

  fha stubs                          Stub every unresolved person reference
  fha stubs --from-names "A; B; C"   Stub these named people
  fha stubs --dry-run                Preview without writing

Like dropping a blank labeled folder in the cabinet for a name you've heard but
not yet researched. --from-names runs INSTEAD of the reference scan, not with it.
A name that already has a record is refused (nothing is created) so the same
person is never filed twice; add --force when it really is someone else.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root |
| `--from-names` `NAMES` | Semicolon-separated names to mint IDs and stubs for. Runs INSTEAD of the unresolved-reference scan, not in addition to it. |
| `--dry-run` | Preview without writing |
| `--force` | With --from-names: mint even when a name already has a record (two different people who share a name, e.g. a father and son). |

## `fha views`

Generate view files from the index (timeline, sources-index, draft-queue, …).

```text
Build always-current summary pages from your records.

  fha views timeline <P-id>        A person's life chronology
  fha views sources-index <P-id>   Every source about a person
  fha views summary <P-id>         Born / Died / Married / Parents / Children
                                   lines under the person's name
  fha views draft-queue <P-id>     Accepted facts not yet written into the bio
  fha views refresh                Rebuild every view for all curated people

Add --format html (refresh also takes --format both) for a printable
single-file page under generated/views/ - private and unredacted, with a
visible not-for-publication banner; share through `fha site` instead.

These pages are generated and rebuildable; edit the records, not the pages.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha views timeline`

Generate per-person timeline view.

```text
Generate {name}_timeline_{P-id}.md in the person's folder.
Requires a fresh .cache/index.sqlite (run `fha index` first).
```

| Argument | What it does |
|---|---|
| `[P-id]` | Person to generate for. |
| `--all-curated` | Generate for every curated person. |
| `--format` `{md,html}` | md (default): the companion .md beside the profile. html: a standalone printable page under generated/views/ (private and unredacted - it carries a visible not-for-publication banner; share through `fha site`). |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha views sources-index`

Refresh a person's own ## Sources section.

```text
Rewrite the ## Sources GENERATED region inside a person's own
profile (works on a stub too, so it can be populated before
promotion). There is no separate sources-index file and no
--format html page - the list lives inside the profile.
Requires a fresh .cache/index.sqlite (run `fha index` first).
```

| Argument | What it does |
|---|---|
| `[P-id]` | Person whose ## Sources section to refresh (stub or curated). |
| `--all-curated` | Refresh every curated profile's ## Sources section. |
| `--format` `{md,html}` | md (default). html is refused: the list lives inside the profile, with no separate page. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha views summary`

Write the Born / Died / Married / Parents / Children lines under a person's name.

```text
Write the summary lines under the person's name in their own
profile, from accepted claims, between GENERATED summary markers.
Lines a person wrote by hand (no markers) are left alone. Works
on a stub too. Requires a fresh .cache/index.sqlite (run
`fha index` first).
```

| Argument | What it does |
|---|---|
| `[P-id]` | Person whose summary lines to write (stub or curated). |
| `--all-curated` | Write every curated profile's summary lines. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha views draft-queue`

Generate per-person draft-queue (uncited-claim backlog) view.

```text
Generate {name}_draft-queue_{P-id}.md in the person's folder.
Requires a fresh .cache/index.sqlite (run `fha index` first).
```

| Argument | What it does |
|---|---|
| `[P-id]` | Person to generate for. |
| `--all-curated` | Generate for every curated person. |
| `--format` `{md,html}` | md (default): the companion .md beside the profile. html: a standalone printable page under generated/views/ (private and unredacted - it carries a visible not-for-publication banner; share through `fha site`). |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha views brackets`

Check couple-folder bracket lists, Ahnentafel placement, and direct-line stubs (W103/W110/W119/W120).

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--fix` | Apply renames/moves after preview. |
| `--fix-promote` | Promote the direct-line stubs (W119) after a preview and an Apply? confirmation - tier flip, move into the couple folder, research file scaffold. |
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
| `P-id` | Seed person for traversal. |
| `--mode` `{ancestors,descendants,fan}` | ancestors: pedigree BFS; descendants: all descendants + in-law spouses; fan: all edge types (default 2 hops). |
| `--generations` `N` | Maximum generations / hops (default: unlimited; fan default: 2). |
| `--format` `{json,dot,html}` | Output format (default: json). html is not available yet - it arrives with the site-wide family tree; run `fha site` for a browsable tree today. |
| `--out` `FILE` | Write output to FILE instead of stdout. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha views clean`

Delete every generated view file (companion .md + generated/views/ HTML); they regenerate with `fha views refresh`.

```text
Delete all GENERATED-headed view files: companion .md files (timeline,
draft-queue, and any old couple-folder sources-index.md) from the
people/ tree, and single-file HTML views under generated/views/. Uses the <!-- GENERATED
… --> whole-file header as the sole signal, file by file; never touches
profiles or manually authored files - including a profile whose own
## Sources section is a GENERATED-BEGIN/END region (#76): that region is
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
(stubs included).
--format html regenerates the standalone generated/views/ pages
instead; --format both writes the two sets in one pass.
Requires a fresh .cache/index.sqlite (run `fha index` first).
```

| Argument | What it does |
|---|---|
| `--format` `{md,html,both}` | md (default): companion .md files as always. html: standalone pages under generated/views/. both: write the two sets in one pass. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

## `fha doctor`

Archive health check - what is wrong with this archive?

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
| `--related` `[ID]` | Neighborhood of an ID - people/places/sources adjacent. Combine with --date for a time slice, or pass --related --date EDTF alone for the standalone time-slice neighborhood. |
| `--date` `EDTF` | Time-slice filter. With --related <ID>, narrows that neighborhood to the given EDTF range; with --related and no ID, reports everything active in that range. |
| `--json` | Machine-readable result list instead of a human report. Valid with a bare ID/text query or --text; combine with --kind/--limit to narrow it. Not yet valid with --related. |
| `--kind` `KIND[,KIND...]` | With --json: only these result kinds - person, source, place, hypothesis, claim, text (comma list; photo-source = only sources that have photo files). Default: every kind. |
| `--limit` `N` | With --json: maximum number of results (default 20). |
| `[ID_OR_TEXT]` | Archive ID (P-/S-/C-/L-/H-) or text to search |

## `fha search`

Search everything for a word or phrase (same as `fha find --text`)

```text
Search everything for a word or phrase - records, notes, transcripts, photo captions.

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
  fha relate P-A P-B --json   Same answer, as machine-readable JSON

A read-only view over accepted relationship claims: nothing is written.
```

| Argument | What it does |
|---|---|
| `P-A` | First person ID |
| `P-B` | Second person ID |
| `--root` `PATH` | Archive root (overrides auto-detection) |
| `--json` | Machine-readable JSON output |

## `fha metrics`

How well documented is each person? Completeness checks + archive totals.

```text
How well documented is each person?

  fha metrics               The archive in numbers, then the direct line by generation
  fha metrics p-xxxxxxxxxx  One person's five checks, each with what is missing
  fha metrics --all         Every person, stubs included
  fha metrics --found       Count suggested claims too (records found, not yet reviewed)
  fha metrics --json        The same answer as machine-readable JSON

The five checks: a birth record; every census they lived through; both parents
named by a sourced claim; a marriage record for each partner; and, for anyone
born over 130 years ago, a death or burial. Read-only: nothing is written.
```

| Argument | What it does |
|---|---|
| `[P-ID]` | One person to show in detail (default: the direct line) |
| `--all` | List every person, stubs included |
| `--found` | Count suggested claims as well as accepted ones |
| `--json` | Machine-readable JSON output |
| `--root` `PATH` | Archive root (overrides auto-detection) |

## `fha photoindex`

Scrape photo metadata into .cache/photos.sqlite

```text
Make your photo library searchable without opening Lightroom.

  fha photoindex                       Scan the photos root into the catalog
  fha photoindex find --person <P-id>  Every photo of someone
  fha photoindex gallery --person <P-id>  A clickable HTML page of the same photos
  fha photoindex triage --top 20       Un-filed photos worth processing next
  fha photoindex tag-person <P-id>     Tag a face across every copy
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
| `--person` `P-ID` | Photos resolved to this P-id (any confidence tier) |
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
| `--person` `P-ID` | Photos resolved to this P-id (any confidence tier) |
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

Re-match moved photos by SOURCE: keyword; flag the rest as missing

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (overrides auto-detection) |
| `--with-exif` | Read embedded SOURCE: keywords from untracked files to re-match missing paths (requires exiftool) |
| `--dry-run` | Report the rematch/missing/new-file plan without changing the cache |

### `fha photoindex tag-person`

Write a bare P-id keyword onto matched or explicit photos

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (overrides auto-detection) |
| `P-ID` | Person to tag |
| `--from-face-tag` `TAG` | Tag every photo whose cached face region carries this name |
| `--paths` `PATH [PATH ...]` | Tag these specific catalog paths |
| `--dry-run` | Preview the candidate list without writing or prompting |

### `fha photoindex set-summary`

Write an AI summary into a photo's embedded comment, preserving human text

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (overrides auto-detection) |
| `[PATH]` | One catalog photo path to write (as printed by fha photoindex find) |
| `--group` `GROUP-ID` | Write every variant in this photo group instead of one file (e.g. "SOURCE:S-ab12cd34ef") |
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
Find people who keep showing up together - possible relatives or associates.

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
Your session briefing: what's new, what's worth a look, what to work on next.

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

  fha packet <P-id>              Build the packet (profile, timeline, sources, photos)
  fha packet <P-id> --dry-run    Preview what's included and what's withheld

A private family export, not a public website. Living people and restricted
material are withheld by default; opt in per export with the --include flags.
```

| Argument | What it does |
|---|---|
| `P-id` | Curated person to export. |
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
  fha places candidates              Recurring place-text worth a registry entry
  fha places geocode (--place L-id | --all)  Fill in coordinates (offline, confirmed one by one)
  fha places set L-id [--coords "LAT, LON"] [--aka NAME]... [--history "PERIOD | HIERARCHY"]...
  fha places note L-id --text "..."  Append a dated research note to the place
  fha places edit-note L-id --old-text "..." --text "..."  Rewrite one existing note
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
| `--include-suggested` | Also cluster place_text on status: suggested claims (normally excluded) - a one-time pre-review backlog scan, e.g. right after a fresh `fha gedcom import`, not the everyday view. |

### `fha places geocode`

Backfill coords/alt-names from the offline GeoNames dump

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--place` `L-id` | Geocode only this place. |
| `--all` | Geocode every registry place lacking coordinates. |
| `--offline` | Never download; match only against an already-cached GeoNames dump. |
| `--dry-run` | Preview proposed changes without writing. |

### `fha places set`

Set a place's coordinates, alt-names, or names-over-time (full replace per field)

| Argument | What it does |
|---|---|
| `L-id` | The place to update. |
| `--coords` `"LAT, LON"` | New coordinates, latitude first - e.g. "39.8000, -95.6000". |
| `--aka` `NAME` | One also-known-as name, taken verbatim (commas and all - "Washington, D.C." is one name); repeat the flag for more. The given set replaces the old list; a lone `--aka -` clears it. |
| `--history` `"PERIOD | HIERARCHY"` | One names-over-time entry; repeat the flag for more (replaces the old list; a lone `--history -` clears it). |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the change without writing. |

### `fha places note`

Append a dated research note to a place

| Argument | What it does |
|---|---|
| `L-id` | The place the note is about. |
| `--text` `TEXT` | The note, in your own words (loose citations welcome). |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the change without writing. |

### `fha places edit-note`

Rewrite one existing research note on a place (named by its whole current paragraph)

```text
Rewrite ONE existing research note on a place - the other notes are untouched. --old-text names the note by its exact current text: give the whole paragraph, word for word, not one sentence of it. If it is only part of a note, is not found, or appears more than once, nothing is written and the message says which.
```

| Argument | What it does |
|---|---|
| `L-id` | The place whose note is being corrected. |
| `--old-text` `TEXT` | The note's current text - the whole paragraph, exactly as it stands. |
| `--text` `TEXT` | The corrected note text. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the change without writing. |

## `fha gedcom`

Derive a GEDCOM 5.5.1 export from relationships + accepted vitals.

```text
Export your tree to a GEDCOM file for another genealogy app.

  fha gedcom --all                        Export everyone
  fha gedcom <P-id> --mode descendants    Export from one person, down the line
  fha gedcom <P-id> --mode ancestors      Export from one person, up the line
  fha gedcom --all --out family.ged       Choose the output file

Living people are redacted by default (use --include-living to override). GEDCOM
is the portable format Ancestry, RootsMagic, and others read; it is a one-way
export, never re-imported as truth.

Importing a GEDCOM from Ancestry or another program? That is the separate
`fha gedcom import <file.ged>` - this command is the exporter.
```

| Argument | What it does |
|---|---|
| `[P-id]` | Seed person for traversal (omit with --all). |
| `--mode` `{descendants,ancestors,connected}` | Traversal from the seed (default: descendants). |
| `--generations` `N` | Depth cap for descendants/ancestors (default: unlimited). |
| `--all` | Export every person in the index. Do not combine with a P-id (they conflict); --all needs no --mode. |
| `--include-living` | Export living/unknown persons in full (default: redacted). |
| `--out` `FILE` | Write to FILE (default: stdout). |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha gedcom import`

```text
Bring a tree from Ancestry (or any genealogy program) into your archive.

  fha gedcom import family-tree.ged            Show the plan - writes nothing
  fha gedcom import family-tree.ged --apply    Do the import

Download your tree as a GEDCOM (.ged) file, then run this. The file itself is
filed as one source (your original untouched); every person becomes a record
with their dates; every statement becomes a SUGGESTED fact for you to review
later, family by family - nothing imported is treated as proven. Re-importing
the same file is refused, and a failed import cleans up after itself.

  --plan-out FILE    Also write the full, uncut plan text to FILE.
```

| Argument | What it does |
|---|---|
| `FILE.ged` | The GEDCOM file to import (e.g. an Ancestry tree download). |
| `--apply` | Write the records (default: print the dry-run plan only). |
| `--plan-out` `FILE` | Also write the FULL (uncapped) plan text to FILE - refused inside the archive except its out/ folder. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

## `fha wikitree`

Render a curated profile to the WikiTree dialect (refs, spacetime spans, links).

```text
Render a person's profile into WikiTree's markup for a family-tree wiki.

  fha wikitree <P-id>              Print the WikiTree-dialect profile
  fha wikitree <P-id> --out FILE   Write it to a file

Turns a curated person's profile prose and its citations into the wiki markup
WikiTree expects. Privacy rules are enforced: restricted material is withheld.
```

| Argument | What it does |
|---|---|
| `P-id` | Curated person to export. |
| `--out` `FILE` | Write to FILE (default: stdout). |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

## `fha process`

Process an original asset into a Source (mint + mark + scaffold)

```text
File a new document or photo into the archive with a permanent ID.

  fha process <file>                        File one document or photo
  fha process <photo> --more <file> ROLE    Attach another file to its source
  fha process refile <S-id> --to photos|documents [--dest SUB]
                                            Move a filed file to the other root
  fha process <photo> --variants one        A photo with its back beside it: file
                                            both as ONE source without being asked
                                            (or --variants separate)
  fha process <photo> --dest "Cemetery Visit 2021"
                                            File an inbox photo into that folder of
                                            your photo library (default: its top)
  fha process <file> --dry-run              Preview, write nothing

Documents are renamed with their new ID (the old name is kept as provenance);
photos are never renamed. This is the deterministic step; drafting claims and
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
| `--dest` `SUBPATH` | The folder of your photo library to file a photo into, when filing it out of the inbox - e.g. --dest "Cemetery Visit 2021". Without it the photo is filed at the top of the library. Also applies to the photos of a notes.md bundle folder |
| `--people` `P-IDS` | Comma-separated P-ids of people in this photo - e.g. "P-de957bcda1,P-ab3c8f0e12". Writes each as a bare keyword in the photo file and populates the source record's people: list. Photos only; use `fha photoindex tag-person` to tag photos already processed. |
| `--physical-location` `HOLDER` | Who keeps the paper original of a scanned document, e.g. "Aunt Ruth" - recorded on the source with the current month as the date. Without it you are asked when filing a scan (press Enter to skip) |
| `--variants` `{one,separate}` | When the photo has variations beside it (a back, a second print, a crop), process them as ONE source or as separate sources without being asked - the answer to the [one / separate / skip] question, for scripts and agents |
| `--dry-run` | Preview without writing |

### `fha process refile`

```text
Move a filed source file to the other asset root - the filing correction.

  fha process refile S-2b3c4d5e6f --to photos --dest 1880s    records drawer -> photo library
  fha process refile S-2b3c4d5e6f --to documents --type census   photo library -> records
  fha process refile S-2b3c4d5e6f --file scan-back.jpg --to photos --dest 1880s

Fixes a filing decision after the fact: a scan processed as a document that
belongs in the photo library, or a photo that is really a record. The file is
moved, renamed for its new root's rules, and the source record updated, all in
one transaction. Reorganizing WITHIN a root is not this command - move files
freely and run `fha reconcile`. Preview with --dry-run first.
```

| Argument | What it does |
|---|---|
| `S-ID` | The source whose file is filed in the wrong root, e.g. S-2b3c4d5e6f |
| `--to` `{photos,documents}` | Which root the file belongs in |
| `--file` `NAME` | Which of the record's files to move, by name - needed only when the record lists more than one |
| `--dest` `SUBPATH` | Destination folder under the target root. Required with --to photos (e.g. "1880s" or "Family/Hartley"); with --to documents it defaults to documents/{type}/ |
| `--type` `TYPE` | What kind of source this really is, e.g. census. Rewrites source_type: and moves the record to sources/{type}/ in the same transaction. Required with --to documents when the record is still typed photo - a scan leaving the photo library is not a family photo any more |
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
into a real record. It reads only the page you already have - never logs in.
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
| `--path` `PATH` | Register a file that must stay exactly where it is (a photo library is never reorganized) - writes a pointer stub only, never moves, renames, or reads the file itself |
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

  fha convert-mining            Print the conversion plan (dry run - writes nothing)
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

Review claims: move status (one C-id or several) and/or correct fields (human-directed write-back)

```text
Record your verdict on a suggested fact - the human decision point.

  fha claim <C-id> --status accepted   Confirm a fact (stamps today's date)
  fha claim <C-id> --status disputed|rejected|superseded
  fha claim <C-id> --status suggested  Looked at it, cannot settle it yet
                                       (stays suggested, dated today)
  fha claim C-a C-b C-c --status accepted   One verdict for several claims at once
  fha claim <C-id> --place L-baba9801fa   Correct a field without touching status
  fha claim <C-id> --clear place_text     Remove an optional field (repeatable)

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
C-ids at once move status only - every id is checked first, and one bad id
stops the whole batch before anything is written; field corrections take
exactly one claim. Preview any change first with --dry-run.

To mint a brand-new claim onto a source, use `fha claim new` (`fha claim new --help`).
```

| Argument | What it does |
|---|---|
| `C-id [C-id ...]` | The claim(s) to review (e.g. C-fd0000001a). Several C-ids apply one status move to each - batches are status-only, and any bad id stops the whole batch before anything is written. |
| `--status` `{accepted,disputed,rejected,superseded,suggested}` | The review status to set; `suggested` parks a claim you looked at but cannot settle yet (it stays suggested, dated today). At least one of --status/--value/--date/--type/--place/--place-text/--persons/--confidence/--information/--evidence/--anchor/--notes/--clear is required. |
| `--reviewed` `DATE` | Review date (YYYY-MM-DD); defaults to today. Only takes effect together with --status. |
| `--reviewed-by` `NAME` | Who made this review decision, written as reviewed_by: on the claim (SPEC §8.4). Defaults to "human"; give a name when several researchers share the archive. Only takes effect together with --status. |
| `--value` `TEXT` | Optionally correct the claim value (single-line scalar only). |
| `--date` `EDTF` | Optionally correct the claim's date (EDTF, e.g. 1880 or 1880-06-15). |
| `--type` `TYPE` | Optionally correct the claim type (SPEC §8.2). Changing TO relationship is refused - see `fha claim --help`. |
| `--place` `L-id` | Optionally set the normalized place link. An existing place_text: (the place as the source wrote it) is left untouched - the two coexist (SPEC §15). |
| `--place-text` `TEXT` | Optionally set the place as written in the source. An existing place: link is left untouched. |
| `--persons` `P-id[,P-id...]` | Optionally REPLACE the whole persons: list, comma-separated P-ids (every id must already have a person record). |
| `--confidence` `{high,medium,low}` | Optionally set the evidence-quality level (SPEC §8.5; separate from --status, the review state). |
| `--information` `{primary,secondary,undetermined}` | Optionally set the Mills information value (SPEC §8.5): was the informant behind this assertion there at the time (primary), reporting secondhand (secondary), or unknown. |
| `--evidence` `{direct,indirect,negative}` | Optionally set the Mills evidence value (SPEC §8.5): does the source answer the question directly (direct), only through correlation (indirect), or is it evidence of an absence (negative). |
| `--anchor` `TEXT` | Optionally set the position inside the source this claim rests on (a timestamp, page, or line - e.g. "00:14:32" or "p. 12"). |
| `--notes` `TEXT` | Optionally REPLACE the claim's notes: - the context/detail behind it. Safe even when the existing notes: is a multi-line block (notes: > or notes: \|); the whole field is replaced, never appended. |
| `--clear` `FIELD` | Remove an optional field from the claim (repeatable): one of place, place_text, date, information, evidence, anchor, notes. The field goes whole (a multi-line value with it); comments and the other keys stay as written. Required fields (value, type, persons, status, confidence) cannot be cleared - correct them with their own flag. |
| `--confirm-deceased` | Accept a death or burial claim for a person marked living: true, after you have checked the record really is their death. The claim is accepted and the person marked as no longer living. Without it such a claim is refused, so a record put on the wrong person never marks someone living as dead. |
| `--keep-living` | Accepting a death or burial claim normally marks the person who died as no longer living (living: false), so shared exports and the site can include them. Pass this to accept the claim and leave their living flag as it is. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the YAML change without writing. |

### `fha claim new`

```text
Mint a brand-new claim onto an existing source record.

  fha claim new --source S-… --type occupation --value "Bookkeeper, 1874 directory" \
      --persons P-… --date 1874 --status accepted

Defaults to --status accepted (typing this command IS the review). Pass
--status suggested for an AI-drafted claim awaiting human review.
--type relationship is refused - it needs a roles: map this command does not
build; use `fha person relate` (hypothesis) or `fha confirm cooccur` (sourced)
instead. --information/--evidence/--anchor/--notes (SPEC §8.4/§8.5) mint the
Mills analysis fields and context note in the same step, instead of needing a
hand-edit right after minting. Preview any change first with --dry-run.
```

| Argument | What it does |
|---|---|
| `--source` `S-id` | The source this claim is cited to (e.g. S-fa1234567b). |
| `--type` `TYPE` | The claim type (SPEC §8.2). relationship is refused - see below. |
| `--value` `TEXT` | The human-readable summary of the assertion. |
| `--date` `DATE` | When it happened - EDTF (1880-06-15) or plain words like "about 1880". |
| `--place` `L-id` | The place, by its registry id (e.g. L-baba9801fa). |
| `--place-text` `TEXT` | The place as written in the source - alone when it has no L-id yet, or beside --place to keep the wording too. |
| `--persons` `P-id[,P-id...]` | Who the claim is about, comma-separated P-ids. Optional, but lint will flag the claim until at least one is linked. |
| `--negated` | Mint a confirmed ABSENCE (SPEC §8.6): "we researched and it did not happen" - e.g. --type marriage --negated --value "no marriage found" records confirmed-never-married. Writes negated: true and its pairing evidence: negative on the claim. |
| `--subtype` `WORD` | Free-text refinement of the type (e.g. deacon for occupation). |
| `--status` `{suggested,accepted}` | Review state to mint at (default: accepted - a human directing this IS the review; AI callers must pass --status suggested). |
| `--confidence` `{high,medium,low}` | Evidence-quality level (SPEC §8.5). Defaults from the source's source_type (vital-record: high, interview: low, else medium). |
| `--information` `{primary,secondary,undetermined}` | Mills information value (SPEC §8.5): was the informant behind this assertion there at the time (primary), reporting secondhand (secondary), or unknown. Optional - omit if undetermined. |
| `--evidence` `{direct,indirect,negative}` | Mills evidence value (SPEC §8.5): does the source answer the question directly (direct), only through correlation (indirect), or is it evidence of an absence (negative). --negated sets this to negative automatically - --evidence must agree if both are given. |
| `--anchor` `TEXT` | Position inside the source this claim rests on (a timestamp, page, or line - e.g. "00:14:32" or "p. 12"). |
| `--notes` `TEXT` | The context/detail behind the claim - EXPECTED (not required) on a substantive or low-confidence-vital claim (lint W109 pings an accepted one with none). |
| `--confirm-deceased` | Accept a death or burial claim for a person marked living: true, after you have checked the record really is their death. The claim is accepted and the person marked as no longer living. Without it such a claim is refused, so a record put on the wrong person never marks someone living as dead. |
| `--keep-living` | Accepting a death or burial claim normally marks the person who died as no longer living (living: false), so shared exports and the site can include them. Pass this to accept the claim and leave their living flag as it is. |
| `--reviewed-by` `NAME` | Who accepted the claim, written as reviewed_by: (SPEC §8.4). Defaults to "human" on an accepted claim; give a name when several researchers share the archive. Refused with --status suggested. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the claim block that would be appended, without writing. |

## `fha confirm`

Write back a decision the human made (xref/cooccur/dismiss/place/discovery/draft/merge)

```text
Write down a suggestion you've decided to accept.

  fha confirm xref <C-a> <C-b> --as corroborates|contradicts
  fha confirm cooccur <P-a> <P-b> --source <S-id> --subtype friend|associate|neighbor
  fha confirm dismiss <P-a> <P-b>
  fha confirm place <C-id...> (--name NAME | --into <L-id>)
  fha confirm discovery "<text>"
  fha confirm draft <P-id>
  fha confirm merge <P-merged> --into <P-survivor> --reason "<why>"

Each verb turns one kind of decision you have made - a link, a connection, a
place, a note, a draft, an identity merge - into the records. Every verb
previews with --dry-run first.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha confirm xref`

Confirm an xref pair: write corroborates/contradicts links

| Argument | What it does |
|---|---|
| `C-id-a` | First claim (e.g. C-fd0000001a). |
| `C-id-b` | Second claim. |
| `--as` `{corroborates,contradicts}` | The link to write between the two claims. |
| `--dry-run` | Preview the change without writing. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha confirm cooccur`

Confirm a co-occurrence pair: mint a relationship claim

| Argument | What it does |
|---|---|
| `P-id-a` | First person. |
| `P-id-b` | Second person. |
| `--source` `S-id` | The source that supports this connection (cited on the claim). |
| `--subtype` `{friend,associate,neighbor}` | The social relationship subtype. |
| `--accept` | Mint the claim accepted (treat this confirm as the review) rather than suggested. |
| `--reviewed` `DATE` | Review date for --accept (YYYY-MM-DD); defaults to today. |
| `--dry-run` | Preview the change without writing. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha confirm dismiss`

Dismiss a co-occurrence pair (tombstone, not re-proposed)

| Argument | What it does |
|---|---|
| `P-id-a` | First person. |
| `P-id-b` | Second person. |
| `--dry-run` | Preview the change without writing. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha confirm place`

Register a place-text cluster and relink its claims

| Argument | What it does |
|---|---|
| `C-id [C-id ...]` | Claim(s) to relink to the place. |
| `--name` `NAME` | Name for a new place (required unless --into). |
| `--hierarchy` `TEXT` | Optional hierarchy, e.g. "Topeka, Kansas, USA". |
| `--into` `L-id` | Merge into this existing place instead of minting. |
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
| `P-id` | The person whose profile to accept drafts in. |
| `--dry-run` | Preview the change without writing. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha confirm merge`

Enact a human-confirmed identity merge: tombstone + rename + fold + relink (SPEC §9)

| Argument | What it does |
|---|---|
| `P-merged` | The duplicate record that becomes the tombstone. |
| `--into` `P-survivor` | The record that keeps the canonical home. |
| `--reason` `TEXT` | Why these are one person; stored as merge_reason: on the tombstone. |
| `--dry-run` | Preview every edit and the rename without writing. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

## `fha person`

Person-record write-backs: new, promote, rename, set-living, set-profile-photo, set-sex, relate, estimate, edit, note

```text
Update a person's record directly - the deterministic person-field write-backs.

  fha person new "Full Name" [--sex M|F|intersex|unknown] [--gender TEXT]
  fha person promote <P-id> [--into FOLDER]
  fha person rename <P-id> "New Name" [--keep-variant]
  fha person set-living <P-id> true|false|unknown
  fha person relate <P-id> --parent|--child|--sibling|--spouse <P-id2>
  fha person estimate <P-id> --birth DATE --death DATE
  fha person edit <P-id> --section biography|stories|research --text TEXT
  fha person note <P-id> --section stories|research --text TEXT
  fha person edit-note <P-id> --section stories|research --old-text TEXT --text TEXT

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
Mint a brand-new person - the one-command "+ add person" mint.

  fha person new "Jane Doe"
  fha person new "Jane Doe" --sex F --birth 1870 --death 1940
  fha person new "Cortez" --gender "two-spirit"
  fha person new "Maria Garcia Lopez" --surname "Garcia Lopez"

Mints one P-id, writes a stub under people/stubs/, and reports where it
landed. --sex accepts M, F, intersex, or unknown (case-insensitive for M/F).
--gender is free text - omit it unless there is something to record.
--birth/--death record PROVISIONAL, unsourced estimates: the archive's exact
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
needs --surname XI to be filed under it. name: is written exactly as given either way. A name that already has a record
(same spelling, ignoring case and accents) is refused so the same person is
never filed twice; add --force when it really is someone else who shares
the name. Use `fha person relate` next to tie the new person to family, and
`fha find` to confirm the record.

  fha person new --from-file people.tsv

mints a whole list at once: one person per line, tab-separated columns name,
sex (optional) and surname override (optional; - for no surname), a header
row "name sex surname" allowed. Every row is checked first - one bad row and
nothing is minted - and the duplicate-name check covers the whole list.
```

| Argument | What it does |
|---|---|
| `[NAME]` | The person's full name, e.g. "Jane Doe". Omit with --from-file. |
| `--from-file` `PATH` | Mint everyone in a tab-separated list instead: one person per line, columns name, sex (optional), surname override (optional; - for no surname). A bad row refuses the whole file. |
| `--surname` `TEXT` | Override the filename's surname split - for names no automatic split can be expected to get right (Spanish double surnames, particles like "van der", surname-first conventions). --surname "" means no surname at all (the file name leads with __, e.g. __henry_tudor_P-....md). name: is written exactly as given either way. |
| `--sex` `M|F|intersex|unknown` | Birth-assigned sex, if known (case-insensitive for M/F). |
| `--gender` `TEXT` | Free-text gender/identity - omit unless there is something to record. |
| `--birth` `DATE` | A provisional, unsourced birth date or estimate. |
| `--birth-place` `PLACE` | A provisional, unsourced birth place (free text). |
| `--death` `DATE` | A provisional, unsourced death date or estimate. |
| `--death-place` `PLACE` | A provisional, unsourced death place (free text). |
| `--force` | Mint even when this name already has a record - for two different people who share a name (a father and son). |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the new record; write and mint nothing persistent. |

### `fha person promote`

Graduate a stub to curated: tier flip and filing (couple folder or connections/).

```text
Graduate a stub to a curated person - tier flip and filing in one step.

  fha person promote P-2b3c4d5e6f
  fha person promote P-2b3c4d5e6f --dry-run
  fha person promote P-2b3c4d5e6f --into "040 Thomas Hartley + Margaret Cole"
  fha person promote P-2b3c4d5e6f --into connections/

Flips the record's tier to curated and backfills any #16 body sections it is
still missing. A direct-line ancestor moves out of people/stubs/ into their
Ahnentafel couple folder (derived from root_person in fha.yaml; the folder is
created if it does not exist yet). Anyone with no derived position - siblings,
in-laws, the FAN club - is not stuck: --into connections/ files them flat in
people/connections/ instead (SPEC §12.3), no numbering. Call with no --into at
all and a non-direct person is refused plainly, naming that command (their
stub is a legitimate resting state either way). --into overrides the
destination for a direct-line person only, and still names a folder under
people/, never stubs/. An existing research companion travels with the record;
none is ever scaffolded fresh (#76). Requires a fresh index (`fha index`).
Preview everything first with --dry-run; a failed write rolls itself back.
```

| Argument | What it does |
|---|---|
| `P-id` | The stub to promote (e.g. P-2b3c4d5e6f). |
| `--into` `FOLDER` | File the record in this folder under people/ instead of the derived couple folder - a couple-folder name, or "connections/" for a non-direct person (SPEC §12.3). |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the tier flip, move, and research scaffold without writing. |

### `fha person rename`

Correct a person's name: frontmatter, files, and (when safe) the couple folder.

```text
Correct a person's name everywhere it is mechanically derivable.

  fha person rename P-2b3c4d5e6f "Betty Slamon Kelly"
  fha person rename P-2b3c4d5e6f "Betty Slamon Kelly" --keep-variant
  fha person rename P-2b3c4d5e6f "Maria Garcia Lopez" --surname "Garcia Lopez"

Rewrites name: (and, with --keep-variant, files the OLD name into
name_variants: rather than losing it), renames the profile and research
files to match (SPEC §13's filename grammar, the same deriver `fha person
new` uses), deletes any stale-named GENERATED companion (timeline,
draft-queue, and a per-person sources-index file if this record still has
one from before #76 - regenerate with `fha views timeline|sources-index|
draft-queue <P-id>` or `fha views refresh`; a current record's ## Sources
is an inline region that travels with the rename automatically), and -
only when the OLD name appears as exactly one unambiguous partner-name
segment of the couple folder's own text - renames that folder to match too.
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
| `NAME` | The corrected full name, e.g. "Betty Slamon Kelly". |
| `--keep-variant` | File the OLD name into name_variants: instead of losing it. |
| `--surname` `TEXT` | Override the renamed filename's surname split - for names no automatic split can be expected to get right (Spanish double surnames, particles like "van der", surname-first conventions). --surname "" means no surname at all. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview every rename without writing. |

### `fha person set-living`

Set a person's living flag (true/false/unknown) - drives export privacy.

```text
Mark one person as living, passed away, or unknown - the privacy switch.

  fha person set-living P-2b3c4d5e6f false    Passed away - exports may include them
  fha person set-living P-2b3c4d5e6f true     Living - redacted from every export
  fha person set-living P-2b3c4d5e6f unknown  Not sure - treated as living (safe default)

Changes exactly one line of the person's record and touches nothing else.
Preview the change first with --dry-run.
```

| Argument | What it does |
|---|---|
| `P-id` | The person to update (e.g. P-2b3c4d5e6f). |
| `true|false|unknown` | The new value. unknown is treated as living (the safe default). |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the one-line change without writing. |

### `fha person set-profile-photo`

Set the portrait shown on a person's page and in the tree.

```text
Set a person's profile_photo: field - a filename, path, or S-id, resolved leniently against the photo catalog when the site is built.
```

| Argument | What it does |
|---|---|
| `P-id` | The person to update (e.g. P-2b3c4d5e6f). |
| `PHOTO` | A filename, path, or S-id identifying the photo. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the one-line change without writing. |

### `fha person set-sex`

Set or correct a person's sex: (M \| F \| intersex \| unknown).

```text
Set a person's sex: field - the one fact the Ahnentafel derivation reads to place a parent in the father or mother slot. A change ends with the reminder to run `fha index`, then `fha views brackets` (`--realign` applies any folder-number shift) - brackets reads the placement out of the index, so the rebuild comes first.
```

| Argument | What it does |
|---|---|
| `P-id` | The person to update (e.g. P-2b3c4d5e6f). |
| `SEX` | M, F, intersex, or unknown (case-insensitive). |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the one-line change without writing. |

### `fha person relate`

Jot an unsourced family-tie belief (parent/child/sibling/spouse).

```text
Jot an unsourced family-tie belief - always recorded as a hypothesis.

  fha person relate P-2b3c4d5e6f --parent P-de957bcda1
  fha person relate P-2b3c4d5e6f --spouse P-cd795c61e0 --reciprocal
  fha person relate P-2b3c4d5e6f --child P-c4b26bb4bc --subtype adoptive

Appends one entry to the first person's relationships: list, naming the
second person and the role (parent/child/sibling/spouse). --subtype names
the nature of the bond (biological is the default; SPEC §8.2 also lists
adoptive, step, foster, guardian, and more). --reciprocal also writes the
mirrored entry on the second person (parent<->child flips; sibling/spouse
mirror as themselves). This is always an unsourced belief - a sourced tie
comes from an accepted relationship claim, filed with `fha claim`.
```

| Argument | What it does |
|---|---|
| `P-id` | The person whose relationships: entry gets the new tie. |
| `--parent` `P-id2` | P-id2 is this person's parent. |
| `--child` `P-id2` | P-id2 is this person's child. |
| `--sibling` `P-id2` | P-id2 is this person's sibling. |
| `--spouse` `P-id2` | P-id2 is this person's spouse. |
| `--subtype` `WORD` | The nature of the bond (SPEC §8.2), e.g. biological (default), adoptive, step, foster, guardian. |
| `--reciprocal` | Also write the mirrored entry on P-id2's record. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the change(s) without writing. |

### `fha person estimate`

Write a provisional, unsourced birth/death date and/or place estimate.

```text
Write a provisional, unsourced birth/death estimate - a starting guess for
the when, the where, or both.

  fha person estimate P-2b3c4d5e6f --birth 1870
  fha person estimate P-2b3c4d5e6f --birth "circa 1870" --death "before 1940"
  fha person estimate P-2b3c4d5e6f --birth-place "Kansas"   Place only - no date needed
  fha person estimate P-2b3c4d5e6f --birth -             Clear the birth estimate

DATE accepts the archive's exact date form (1870, 1870-06, 188X for "the
1880s") or plain words ("circa 1870", "before 1940", "the 1880s") - loose
wording is translated for you. --birth-place/--death-place record the WHERE
as free text ("born in Kansas, no idea when" is real family knowledge), with
or without a date. Use - by itself to clear any field. A sourced birth/death
claim always supersedes this estimate; run `fha claim` once you have a
source. At least one of --birth/--death/--birth-place/--death-place is
required.
```

| Argument | What it does |
|---|---|
| `P-id` | The person to update (e.g. P-2b3c4d5e6f). |
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

  fha person edit P-2b3c4d5e6f --section biography --text "..."
  fha person edit P-2b3c4d5e6f --section stories --file story.txt --append

--section picks the section: biography (## Biography), stories
(## Stories), or research (## Research Notes). Give the text with --text or
--file (exactly one - no reading from standard input). Replaces the whole
section by default; --append adds the text as a new paragraph at the end
instead. The section is created if it does not exist yet. Frontmatter and
every other section are left byte-for-byte alone.
```

| Argument | What it does |
|---|---|
| `P-id` | The person whose profile gets edited. |
| `--section` `{biography,stories,research}` | Which section to edit. |
| `--text` `TEXT` | The prose to write. |
| `--file` `PATH` | A file already holding the prose. |
| `--append` | Add as a new paragraph at the end instead of replacing. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the change without writing. |

### `fha person note`

Append a quick note to Stories or Research Notes.

```text
Append a quick note to Stories or Research Notes - never replaces anything.

  fha person note P-2b3c4d5e6f --section research --text "Check the 1880 census."

Adds TEXT as a new paragraph at the end of the section (creating it if
missing). Plain markdown, human-written - existing text is never rewritten
or removed. Refuses if the section's <!-- private --> fencing is left
unclosed, since it could not tell which side of it the new text belongs on.
```

| Argument | What it does |
|---|---|
| `P-id` | The person whose profile gets the note. |
| `--section` `{stories,research}` | Which section to append to. |
| `--text` `TEXT` | The note to add. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the change without writing. |

### `fha person edit-note`

Rewrite one existing Stories/Research Notes entry.

```text
Rewrite ONE existing entry of Stories or Research Notes - the rest is untouched.

  fha person edit-note P-2b3c4d5e6f --section research \
      --old-text "Check the 1880 census." --text "Checked the 1880 census - nothing."

The entry is named by its exact current text (--old-text): give the whole
paragraph, word for word - one sentence of it is not enough. If --old-text is
only part of an entry, is not found, or appears more than once, nothing is
written and the message says which. Deleting an entry stays a hand edit to
the record file - this only rewrites.
```

| Argument | What it does |
|---|---|
| `P-id` | The person whose entry is being corrected. |
| `--section` `{stories,research}` | Which section the entry lives in. |
| `--old-text` `TEXT` | The entry's current text - the whole paragraph, exactly as it stands. |
| `--text` `TEXT` | The corrected entry text. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the change without writing. |

## `fha source`

Source-record write-backs: note (append), edit-note (rewrite one), extract (PDF text layer), clear-keyword (fix a stray keyword), set-primary (choose the main image)

```text
Update a source record directly - the deterministic source-field write-backs.

  fha source note S-2b3c4d5e6f --text "..."
  fha source edit-note S-2b3c4d5e6f --old-text "..." --text "..."
  fha source extract S-2b3c4d5e6f [--pages 1-60] [--copy LABEL]
  fha source clear-keyword S-2b3c4d5e6f --keyword "..." [--replace-with "..."]
  fha source set-primary S-2b3c4d5e6f --file "front-scan.tif"

note appends a hand-written paragraph to a source's ## Notes section;
edit-note rewrites one existing paragraph there (named by its exact current
text) and leaves the rest untouched; extract dumps the source PDF's embedded
text layer into a derived [Page N]-labeled companion file (needs the pypdf
helper package); clear-keyword removes (or corrects) one embedded keyword on
a documents-root file, the tool-mediated fix for a stray tag `fha lint
--with-exif` (W131) finds; set-primary chooses which photo is a source's main
image (its front, not its back - the fix `fha doctor` names).
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root (auto-detected if omitted). |

### `fha source note`

Append a paragraph to a source record's ## Notes section.

```text
Add a note to a source - appended to the end of its ## Notes section.

  fha source note S-2b3c4d5e6f --text "Found in Grandma's cedar chest, 2024."

Always append-only: an existing note is never edited or removed, and nothing
outside ## Notes changes (the frontmatter and ## Claims are never touched).
A source marked superseded still accepts notes - the audit trail stays open.
Preview the change first with --dry-run.
```

| Argument | What it does |
|---|---|
| `S-id` | The source to update (e.g. S-2b3c4d5e6f). |
| `--text` `TEXT` | The note to add, in your own words. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the change without writing. |

### `fha source edit-note`

Rewrite one existing paragraph of a source's ## Notes.

```text
Rewrite ONE existing ## Notes paragraph - the rest of the section is untouched.

  fha source edit-note S-2b3c4d5e6f \
      --old-text "Found in the cedar chest." --text "Found in Grandma's cedar chest, 2024."

The note is named by its exact current text (--old-text): give the whole
paragraph, word for word - one sentence of it is not enough. If --old-text is
only part of a note, is not found, or appears more than once, nothing is
written and the message says which. Deleting a note stays a hand edit to the
record file - this only rewrites. Preview first with --dry-run.
```

| Argument | What it does |
|---|---|
| `S-id` | The source whose note is being corrected. |
| `--old-text` `TEXT` | The note's current text - the whole paragraph, exactly as it stands. |
| `--text` `TEXT` | The corrected note text. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the change without writing. |

### `fha source extract`

Dump a source PDF's embedded text layer into a derived companion file.

```text
Reads the source's PDF (its primary file, or the only PDF it lists; when a second digitization is attached as primary:LABEL, the one without a label unless --copy names another) and writes the embedded text layer, page by page, into a [Page N]-labeled companion the assistant can mine like a transcript - seconds instead of reading hundreds of pages by eye. The PDF itself is never touched. Needs the pypdf helper package (python -m pip install pypdf).
```

| Argument | What it does |
|---|---|
| `S-id` | The source whose PDF to extract (e.g. S-2b3c4d5e6f). |
| `--pages` `RANGES` | Only these pages, e.g. `1-60` or `1-60,102,200-210` (default: every page). |
| `--copy` `LABEL` | Read the PDF whose files: entry carries this copy: label (e.g. `familysearch`, the label given to `fha process --more FILE primary:familysearch`) instead of the unlabeled primary. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the dump (page coverage, destination) without writing. |

### `fha source clear-keyword`

Clear or correct one embedded keyword on a source's documents-root file.

```text
Clear or correct one embedded keyword on a source's documents-root file.

  fha source clear-keyword S-2b3c4d5e6f --keyword "Margaret Hartley"
  fha source clear-keyword S-2b3c4d5e6f --keyword "Margaret Hartley" \
      --replace-with "Margaret Cole"

Reads the file's keyword fields (IPTC Keywords, XMP dc:subject and
Lightroom's hierarchical keywords), matches --keyword case-insensitively, and
removes the EXACT on-file value from every field it sits in - so a keyword
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
| `S-id` | The source whose documents-root file to correct. |
| `--keyword` `TEXT` | The keyword to remove, exactly as it reads (matched case-insensitively against the file). |
| `--replace-with` `TEXT` | Add this keyword in place of the one removed, in every keyword field that does not already name it. Omit to just clear it. |
| `--file` `NAME` | Which documents-root file to correct, when the source lists more than one (a filename or its files: alias). |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the change without writing. |

### `fha source set-primary`

Choose which photo is a source's main image (e.g. its front, not its back).

```text
Choose which photo is the main image of a source - its front, not its back.

  fha source set-primary S-2b3c4d5e6f --file "Smith-Farm-01400.tif" --dry-run
  fha source set-primary S-2b3c4d5e6f --file "Smith-Farm-01400.tif"

Changes only the source record's files: list: the named photo becomes the
main image (is_primary: true), and the photo that was the main
image is listed by what its name says it is (role: back, say). No photo is
renamed or touched, and the record's title and filename stay as they are.
`fha doctor` names the records that need this (#200: an older `fha process`
could make a photo's back its main image). Preview first with --dry-run.
```

| Argument | What it does |
|---|---|
| `S-id` | The source whose main image to change (e.g. S-2b3c4d5e6f). |
| `--file` `NAME` | The photo to make the main image: its filename, or its full files: path when two photos share a name. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |
| `--dry-run` | Preview the change without writing. |

## `fha site`

Generate the static HTML family explorer (standalone snapshot or linked preview).

```text
Build a browsable family website you can open in any browser.

  fha site                Build the shareable snapshot (redacted, self-contained)
  fha site --standalone   The same shareable snapshot, named explicitly
  fha site --linked       An unredacted local preview (for yourself, not to share)

Opens from a plain file, no server needed - want to see your tree? build the
site and open it. Living people and restricted material are redacted by default.
```

| Argument | What it does |
|---|---|
| `--out` `PATH` | Output directory (default: generated/site/, or generated/site-linked/ with --linked). |
| `--standalone` | Self-contained, redacted snapshot safe to share (default). |
| `--linked` | Local developer preview: real paths, no copies, no redaction. |
| `--dry-run` | Report how many pages would be built and what a rebuild would first remove from the output folder, without writing anything. |
| `--root` `PATH` | Archive root (auto-detected if omitted). |

## `fha serve`

Open the localhost workbench - a private, editable view of the archive.

```text
Start a local, private web workbench for this archive.

  fha serve                 open it in your browser on 127.0.0.1:8765
  fha serve --port 8766     use a different port
  fha serve --no-browser    start it but do not open a browser

It runs on this machine only - no network, no login. Every button is an fha command, previewed before it writes. Stop it with Ctrl-C; nothing is lost.
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
Compare your archive against a newer copy of the public tools and pull in improvements. Never deletes and never overwrites your edits - anything you customized is backed up first. Run from inside your archive with --repo pointing at the updated tools.
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

  fha normalize-links            Preview the rewrites (dry run - writes nothing)
  fha normalize-links --write    Apply them, showing the same diff

Runs as a preview by default; a real write needs --write. Your original names
are kept as aliases, so the shortened links still resolve.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root |
| `--write` | Apply the rewrites (default is a dry-run preview) |
| `--dry-run` | Preview without writing (already the default; accepted so the always-preview habit works here too) |
| `--quiet` | Suppress the per-file diff (show only the summary) |

## `fha backup`

Zip the archive into a dated backup beside it (restore = unzip).

```text
Copy your whole archive into one dated zip file, kept OUTSIDE the archive.

  fha backup                      records-only zip in a folder beside the archive
  fha backup --include-assets     also pack the photos/documents roots
  fha backup --to D:/Backups      choose where the zip goes
  fha backup --dry-run            show the plan; write nothing

If any folder cannot be read - it will not open, it is a shortcut to somewhere
else, or (with --include-assets) your whole photos or documents folder is not
there - no zip is written at all: a backup missing part of your archive would
look exactly like a good one. Reconnect the drive or fix the folder and run it
again; --allow-incomplete writes it anyway, clearly marked, for a folder that
is gone for good.

Photos and documents are NOT included unless you pass --include-assets (they
are often huge and often live on another drive - the output names them every
time). To restore a backup: unzip it. That's the whole procedure.
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
Re-tie moved files to their records. Rearrange the documents folder any way you like, then run this: every filed document is found again by the ID in its name and its record updated. A filed photo moved within the photos folder is found again by the SOURCE: keyword inside it (from the photo catalog, or read with --with-exif), and the photo catalog is reconciled too when one exists. Preview with --dry-run first.
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
creation_time, and derives its local start time - never guessing the
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
| `--json` `PATH` | also write the findings as JSON (a filename of its own - a path landing on a recording, an archived file, or fha.yaml is refused) |
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

SAFETY - read this before running with --apply: a file already sitting inside ANY folder a human created by hand - even a shallow one - is never touched, never even proposed as a candidate. Only material still sitting exactly where `fha process` originally filed it (flat at the documents root, or one level down in its own type folder) is eligible. If you (or anyone) moved something by hand, this tool leaves it alone on purpose - moving a person's deliberately-organized file would be far worse than leaving something merely untidy.

Always previews first: run with no flags (or --dry-run) to see the full plan and change nothing. Add --apply to actually move files, in bounded batches, re-checked with fha reconcile after each one - if anything looks wrong afterward, the run stops right there and reports it, without touching anything further. Photos are out of scope: SPEC identifies a photo by its embedded keyword, never by its path, so there is no folder to tidy - fha reconcile's own photo pass already re-verifies photo identity after any move you make by hand.
```

| Argument | What it does |
|---|---|
| `--root` `PATH` | Archive root |
| `--dry-run` | Preview the plan only and change nothing (also the default with no --apply) |
| `--apply` | Actually move files and update records (previews only without this) |
| `--yes` | Skip the [y/N] confirmation before applying |
| `--batch-size` `N` | Files per batch before re-checking with fha reconcile (default 25) |
| `--group-threshold` `N` | A source with more than N eligible files gets its own subfolder (default 3; 0 means every non-empty source gets its own subfolder, never a shared type folder) |
| `--limit` `N` | Only plan/apply the first N eligible files this run, whole records only - a record whose own files would cross N is skipped, never split (default: no cap) |
