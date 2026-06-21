# Getting Started

Plainfile is operated with an AI coding agent.
This walks you from a fresh clone to your first processed record.

## Prerequisites

- An AI coding agent that reads project instructions and runs shell commands. [Claude Code](https://www.anthropic.com/claude-code) is the reference; the spec is harness-agnostic (anything reading `AGENTS.md` works).
- Python 3.10+ (the `fha` tools are Python; minimal dependencies).
- `exiftool` for embedded photo/document metadata.
- Optional: a photo library (Lightroom or similar) — it can live anywhere; `fha.yaml` maps to it.

## Step 1 — Read the spec

`SPEC.md` is the contract.
Read it fully before building anything.
The five record types, the claim lifecycle, and the four-layer model are the concepts everything else rests on. `TOOLING.md` is the implementation design; `AGENTS.md` is what the agent is allowed to do.

## Step 2 — Open the repo in your agent

The agent reads `CLAUDE.md`, which defers to `AGENTS.md`, and now knows the rules: files are truth, AI suggestions enter a review queue, photos are never renamed, every fact cites a source.
State your **mode** at the start of a session — `research`, `tool-building`, `migration`, or `spec-refinement`.

## Step 3 — Use (or extend) the tools

Milestones 1–5 are complete, plus `fha packet` (milestone 6.1): `fha lint`, `fha index`,
`fha id`, `fha stubs`, `fha views` (timeline, sources-index, draft-queue, brackets, tree),
`fha doctor`, `fha find` (including `--related` and `--text`), `fha photoindex`
(scan/find/triage/report/reconcile/tag-person), `fha xref`, `fha cooccur`, `fha report`,
and `fha packet` are all implemented — see `tools/README.md` for the authoritative
per-tool status table. Run them with Python 3.10+ from the repo root:

```
python tools/fha.py lint --root example-archive          # exits 1 (one W101 warning, no errors)
python tools/fha.py id mint P                             # mint a fresh person ID
python tools/fha.py index --root example-archive          # build the SQLite index
python tools/fha.py views timeline --root example-archive --all-curated
python tools/fha.py views sources-index --root example-archive --couple-folders
python tools/fha.py views draft-queue --root example-archive --all-curated
python tools/fha.py views brackets --root example-archive          # check W103/W110; add --fix to apply
python tools/fha.py views tree P-de957bcda1 --mode descendants --root example-archive
python tools/fha.py views tree P-de957bcda1 --mode ancestors --format dot --root example-archive
python tools/fha.py doctor --root example-archive
python tools/fha.py find P-de957bcda1 --root example-archive
python tools/fha.py find --related P-de957bcda1 --root example-archive
python tools/fha.py report --root example-archive
python tools/fha.py packet P-de957bcda1 --root example-archive --no-photos
```

To build further tools (process, places, gedcom, wikitree, site, …), declare
**tool-building mode** and follow the build order in `BUILD.md` (which itself implements
the design in `TOOLING.md` §15).
Each new tool follows the same implementation loop: read TOOLING, state contract, implement, test on fixtures, README review.

## Step 4 — Start your own archive

> **Your archive is a separate, private repository from this public one.** This public
> repo holds the spec and the generic tools; your real family records live in their own
> private repo and never enter the public one. The only link between them is that your
> archive *uses* the tools published here.

**Setting up the two repos (one time):**

1. **This public repo** already exists once you've pushed it (spec + tools).
2. **Your private archive:** on GitHub, create a new **private** repo (e.g. `my-family-archive`).
Copy the contents of `archive-template/` into it as the starting skeleton.
3. **Get the tools into your archive.** `fha install`/`fha update-tools` (TOOLING.md §13c) are
   the planned vendoring path — copy the operating layer into a fresh archive once, then pull
   improvements later, backing up anything you've customized and never touching your data — but
   they are not built yet (milestone 9, `BUILD.md` M9.1–M9.2). Until then, vendor by hand:
   copy `tools/` plus `SPEC.md`, `TOOLING.md`, `AGENTS.md`, `CLAUDE.md` onto the skeleton from
   `archive-template/`. There is no packaged `fha` install yet either; call it as
   `python tools/fha.py <command>` from your clone of this repo (or add it to your `PATH`
   yourself).

**Then, the actual research:**

1. Copy the structure (`sources/`, `people/`, `places/`, `notes/`, and an `inbox/`).
2. Point `fha.yaml` at where your photos and documents live.
3. Drop a scan, a downloaded record, or a quick note into `inbox/` — optionally with a freeform `*.notes.md` beside it (a "source stub").
4. Ask the agent to process it. It mints IDs, drafts sourced claims as `suggested`, and hands them to you for review. Nothing becomes a fact until you accept it.

## Step 5 — The daily loop

A session looks like: run the report (`fha report`, narrated by the future `today` skill —
TOOLING.md §16), see your review queue and research leads, process new inbox items, review
drafted claims, and let the index regenerate. `fha process` (intake) and the workflow skills
themselves are still milestone 7 work; until they ship, intake is manual (`fha id mint` +
hand-editing the source record) and review/report are run directly from the CLI.
Capture → file → process → review → report.

## A note on the example archive

`example-archive/` is a small, **entirely fictional** family (the Hartleys).
It exists to give the linter and tools something spec-conformant to run against, and to show what processed records look like.
None of it is real genealogy.
