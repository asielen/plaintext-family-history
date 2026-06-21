# Plainfile Family History

**An operating spec for a durable, file-first family-history archive with an AI research assistant layered on top.**

![status](https://img.shields.io/badge/status-milestones_1--4_complete-green) ![type](https://img.shields.io/badge/type-operating_spec-orange) ![works with](https://img.shields.io/badge/works_with-Claude_Code-8A2BE2) ![format](https://img.shields.io/badge/format-plain_text-green) ![license](https://img.shields.io/badge/license-MIT-lightgrey)

This project stemmed from one idea: **for a hundred years, genealogy lived in a filing cabinet, and anyone could open the drawer.** No login, no subscription, no schema migration. A century later a curious descendant could still pull the folder or open the book and read it. Modern genealogy software and workflows have lost that virtue.

Plainfile is that filing cabinet, built to last. Plain files at the foundation, with search, structured claims, and an AI research layer stacked *on top of* the files, never *instead of* them.
Delete every layer above and the archive still works, the way the drawer still works.

> NOTE: **This is a specification and scaffold, not finished software.** 

It is the blueprint for a simple future proof system for family research. The goal of this repo is to establish simple standards that can be maintained with or without tooling. It also provides the spec to create tooling from scratch if you so wish and sample tools you can you use to get you started. 

## Repo, tools, and your archive

Three things live at arm's length from each other, by design:

1. **This repo** (public): the spec (`SPEC.md`, `TOOLING.md`, `AGENTS.md`), the docs, the generic `fha` tools (once built), an empty `archive-template/`, and a fictional `example-archive/` fixture.
2. **The tools** (public, in `tools/`): generic — they operate on *any* conforming archive and hold no family data. Publishing them is the manifestation of the spec. Tools are replaceable glue, regenerable from the spec.
3. **Your archive** (private, separate repo): your real family's records, created from `archive-template/`, depending on this repo's spec and tools but never living inside it. Public examples stay fictional; your groceries don't go in the cookbook.

> **Public examples must remain fictional.** Do not open issues or PRs containing real records about living people, private family documents, raw DNA files, or identifying photos. See [PRIVACY.md](PRIVACY.md).

### How the two repos relate

In practice you end up with **two separate repositories** — one public, one private:

```
plainfile-family-history/   ← PUBLIC:  the spec + the generic tools (this repo)
my-family-archive/          ← PRIVATE: your real family's records
```

They are not technically linked.
The only relationship is that your private archive *uses the tools* that live in this public repo.
There are two ways to get those tools to your archive (decide once the tools are built — you don't need to now):

- **Vendor (copy them in).** Copy this repo's `tools/` folder into your private archive so the tools live *beside* your data. The archive becomes fully self-contained — it works on any machine, offline, forever, even if this repo disappears. Updating means re-copying `tools/` when they improve. *Recommended for a personal archive — it matches the "survives tool churn, usable from a USB stick" goal.*
- **Install (once packaging exists).** *Not available yet — the tools are specified, not built.* Once the `fha` suite is implemented and packaged, you'll be able to install it from this repo (`pip install git+https://github.com/YOURNAME/plainfile-family-history.git`) and call `fha` from anywhere. Cleaner day-to-day (tools live in one place), but your archive then depends on the tools being installed separately. Until then, use the vendored-copy model above.

Either way, **your private family data never enters this public repo.** The public repo is the cookbook and the appliances; your private repo is your kitchen with your food in it.


---

## Contents

  - [What this is](#what-this-is)
  - [What this is not](#what-this-is-not)
  - [How it works](#how-it-works)
  - [Repository layout](#repository-layout)
  - [Quick start](#quick-start)
  - [The documents](#the-documents)
  - [Design principles](#design-principles)
  - [Status \& roadmap](#status--roadmap)
  - [A complementary project](#a-complementary-project)
  - [Contributing](#contributing)
  - [License](#license)

---

## What this is

Plainfile is an **archive-first** system.
The durable archive — plain text and standard file formats on disk — is the source of truth.
Every other moving part (the search index, the AI assistant, any genealogy app or website) is an optional, replaceable helper built *from* the archive and rebuildable *from scratch*.

It is designed to be **operated with an AI coding agent** — a chat assistant that can read your files and run commands for you, the same way a human research assistant would, except it never gets tired of repetitive filing work.
You open the archive in Claude Code (or any agent that reads `AGENTS.md`), and the agent helps you process records, draft sourced claims, build family trees, and surface research leads — while a set of small deterministic tools (the `fha` command suite, specified in `TOOLING.md`) does the mechanical work: the boring, exact, repeatable parts (checking IDs, building search indexes, scanning for contradictions) that don't need judgment, just consistency.
The spec is written so that all of that tooling can be *regenerated* from the documents, in any language, if it is ever lost.

## What this is not

- **Not a finished app.** Milestones 1–4 (linting, IDs, views/discovery, the photo catalog, and cross-reference/connection detection — see `BUILD.md` for the full roadmap) are implemented; the full suite (`fha process`, `fha site`, `fha packet`, etc.) is still being built.
- **Not a database.** No server, no proprietary store. Files are the truth; the index is a disposable cache.
- **Not a genealogy app that happens to store documents.** It is the inverse: an archive that *may* feed a genealogy app via export.
- **Not a hosted service.** Your data lives on your disk, in formats you can read with a text editor.


## How it works

Your existing photos and documents plus FIVE record types, all plain Markdown/YAML on disk:

| Type | What it is |
|---|---|
| **Person** `P-` | A human — identity, flags, and prose. |
| **Source** `S-` | A piece of evidence: a record, document, photo, interview. |
| **Claim** `C-` | A single sourced assertion (a date, place, relationship) living inside its source record, moving through a `suggested → accepted` review lifecycle. |
| **Place** `L-` | A physical location, identified by coordinates, with a dated name/jurisdiction history. |
| **Hypothesis** `H-` | An unsourced working theory — a guess, never a fact, until evidence promotes it to a claim. |

Around those, a rebuildable **index** (SQLite, regenerated from the files) powers search, family-tree generation, contradiction detection, and a research report — none of it authoritative, all of it disposable.
The operating loop is simple: **capture → file → process → review → report**, with human review the only gate to an accepted fact.

## Repository layout

```
plainfile-family-history/
├── README.md            ← you are here
├── SPEC.md              ← the law: philosophy, data model, physical format, governance
├── TOOLING.md           ← implementation design for every supporting tool (the fha suite)
├── AGENTS.md            ← canonical operating instructions for the AI agent
├── CLAUDE.md            ← Claude Code entry point (defers to AGENTS.md)
├── docs/                ← supporting documentation
│   ├── GETTING_STARTED.md
│   ├── GLOSSARY.md
│   └── FAQ.md
├── archive-template/    ← empty skeleton (+ fha.yaml) to copy when starting your own (private) archive
├── example-archive/     ← a small, fully fictional worked example (+ its own fha.yaml)
├── tools/               ← the generic fha command suite (skeletal in v1; see TOOLING.md)
├── tests/               ← fixtures for the linter (skeletal in v1)
├── PRIVACY.md           ← example-data policy
└── .github/             ← issue templates, contributing guide
```

## Quick start

> You need an AI coding agent that can read project instructions and run shell commands — [Claude Code](https://www.anthropic.com/claude-code) is the reference harness. The spec is harness-agnostic; anything that reads `AGENTS.md` works.

1. **Clone this repo** and read `SPEC.md` end to end. It is the contract; everything else serves it.
2. **Open the folder in your agent.** It will read `CLAUDE.md` → `AGENTS.md` and know the rules before you say anything.
3. **Build the tools.** Declare *tool-building mode* and point the agent at the build order in `TOOLING.md` §15. The first milestone is the linter running clean on `example-archive/`.
4. **Start your own archive.** Copy the structure, drop your first scan or note into `inbox/`, and ask the agent to process it.

See [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) for the full walkthrough.

## The documents

| Document | Read it for |
|---|---|
| **[SPEC.md](SPEC.md)** | The complete specification — what exists, how it lives on disk, and the rules that never bend. Start here. |
| **[TOOLING.md](TOOLING.md)** | How every tool is built, in enough detail to rewrite it from scratch. The `fha` command suite, the index schema, the linter rules. |
| **[AGENTS.md](AGENTS.md)** | What an AI agent may and may not do inside the archive — the contract, the operating modes, the workflows. |
| **[docs/GETTING_STARTED.md](docs/GETTING_STARTED.md)** | A practical first-session walkthrough. |
| **[docs/GLOSSARY.md](docs/GLOSSARY.md)** | Every term and ID type defined. |
| **[docs/FAQ.md](docs/FAQ.md)** | Why files, why not a database, why AI, how durable is this really. |

## Design principles

1. **The archive is the source of truth; tools are replaceable.**
2. **Durable, plain formats.** `.md`, `.txt`, `.csv`, `.jsonl`, `.yaml`, `.jpg`, `.tiff`, embedded IPTC/XMP.
3. **Every important fact traces to a source.** Uncited prose is story or context, never fact.
4. **AI suggestions are not facts.** They enter a review queue and stay there until a human accepts them.
5. **Nothing generated is load-bearing.** Index, search, trees, the website — all rebuildable from the files.
6. **Folder location is for human browsing; metadata carries meaning.**
7. **Stay light.** Long-term durability beats short-term convenience.

## Status & roadmap

**Current: `spec v1.2` — milestones 1–5 complete.**

Everything through the research report is implemented and runs cleanly on
the example archive: the linting/indexing substrate, the view generators and
universal locator, the photo catalog, the candidate-finding tools
(contradiction/corroboration detection, person and place co-occurrence, and
`fha find --related`'s neighborhood queries), and the `fha report` session
feed. See `BUILD.md` for the detailed milestone breakdown. The intended
build sequence (detailed in `TOOLING.md` §15):

- [x] Shared foundations (`_lib`: parsing, dates, ID grammar, path resolution)
- [x] `fha id`, `fha index`, `fha lint`, `fha stubs` — the substrate (milestone 1: lint clean on the example archive)
- [x] `fha views timeline`, `fha views sources-index`, `fha views draft-queue` — view generators (milestone 2)
- [x] `fha views brackets` — folder maintenance: W103 bracket refresh, W110 Ahnentafel placement (milestone 2)
- [x] `fha views tree` — relationship tree traversal, neutral JSON + DOT output (milestone 2)
- [x] `fha views clean`, `fha views refresh` — generated-file lifecycle management (milestone 2)
- [x] `fha find` — universal ID locator and full-text search across records, notes, transcripts (milestone 2)
- [x] `fha doctor` — archive health report: index freshness, file integrity, privacy flags (milestone 2)
- [x] `fha photoindex` — photo catalog: scan/grouping, find, triage/report, reconcile/tag-person (milestone 3)
- [x] `fha xref`, `fha cooccur` — corroboration/contradiction and co-occurrence candidate detection (milestone 4)
- [x] `fha find --related` — the neighborhood query: people, places, sources, and time slices (milestone 4)
- [x] `fha report` — the session research feed: discoveries, review queue, vitals gaps, contradictions, search-log awareness, answerable questions, photo triage, hypotheses, possible connections (milestone 5)
- [ ] `fha process`, person packets, GEDCOM/WikiTree export (milestone 6+)
- [ ] The static-site generator
- [ ] Web-capture companion for record intake

## A complementary project

A related project worth studying: if your interest is the *research* half — autonomous AI research loops, archive guides for specific countries, prompt templates for pushing a family tree backward — see [**autoresearch-genealogy**](https://github.com/mattprusak/autoresearch-genealogy) by Matt Prusak.
It and Plainfile arrived independently at the same files-first, Claude-Code-driven philosophy from different angles: that project is a research *playbook*, this one is the *filing system* the findings live in.
They complement each other well.

## Contributing

This is an early-stage spec and feedback is genuinely useful — especially from genealogists and from anyone building the tools against it.
See [`.github/CONTRIBUTING.md`](.github/CONTRIBUTING.md).
Issues and discussion welcome.

## License

[MIT](LICENSE).
The spec, the documents, and any code built from them are free to use, adapt, and build on.
