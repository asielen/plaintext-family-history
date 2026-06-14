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

## Step 3 — Build the tools

This scaffold ships the tools as *specification*, not code.
Declare **tool-building mode** and point the agent at the build order in `TOOLING.md` §15:

```
_lib (foundations) → id → index → lint → stubs → views → photoindex → report → ...
```

The first milestone: **`fha lint` runs clean on `example-archive/`** (the fictional Hartley family).
That proves the foundations are correct before anything touches real data.

## Step 4 — Start your own archive

> **Your archive is a separate, private repository from this public one.** This public
> repo holds the spec and the generic tools; your real family records live in their own
> private repo and never enter the public one. The only link between them is that your
> archive *uses* the tools published here.

**Setting up the two repos (one time):**

1. **This public repo** already exists once you've pushed it (spec + tools).
2. **Your private archive:** on GitHub, create a new **private** repo (e.g. `my-family-archive`).
Copy the contents of `archive-template/` into it as the starting skeleton.
3. **Get the tools into your archive**, by whichever method you prefer:
   - *Vendor (recommended):* run `fha install ~/my-family-archive` once from your clone of
     this repo — it creates the archive with the operating layer (tools + the four docs).
     Later, `fha update-tools` (run from the archive) pulls improvements, backing up anything
     you've customized and never touching your data (TOOLING.md §13c). The archive stays
     self-contained even if the public repo vanishes, and you never memorize a file list.
     *(Until the tools are built, the manual equivalent is copying `tools/` plus `SPEC.md`,
     `TOOLING.md`, `AGENTS.md`, `CLAUDE.md` onto the skeleton — which is what install automates.)*
   - *Install (once packaging exists):* not available yet — the tools are specified, not
built.
When the `fha` suite is packaged, you'll install it from this repo and call `fha` from anywhere.
Until then, vendor the tools (above) or build them in your archive.

**Then, the actual research:**

1. Copy the structure (`sources/`, `people/`, `places/`, `notes/`, and an `inbox/`).
2. Point `fha.yaml` at where your photos and documents live.
3. Drop a scan, a downloaded record, or a quick note into `inbox/` — optionally with a freeform `*.notes.md` beside it (a "source stub").
4. Ask the agent to process it. It mints IDs, drafts sourced claims as `suggested`, and hands them to you for review. Nothing becomes a fact until you accept it.

## Step 5 — The daily loop

Once tools exist, a session looks like: run the report (`today`), see your review queue and research leads, process new inbox items, review drafted claims, and let the index regenerate.
Capture → file → process → review → report.

## A note on the example archive

`example-archive/` is a small, **entirely fictional** family (the Hartleys).
It exists to give the linter and tools something spec-conformant to run against, and to show what processed records look like.
None of it is real genealogy.
