# AGENTS.md — Operating Instructions for AI Agents

## Repo context first

If you are working in the **public Plainfile spec repo** (it contains `SPEC.md`, `TOOLING.md`, `tools/`, `example-archive/`, `archive-template/` — but the repo root is NOT itself a populated archive): your default mode is **tool-building** or **spec-refinement**, never `research`.
Do not treat the repo root as a family archive.
Use `example-archive/` only as a fictional fixture.
Never add real family data.

When these instructions live at the root of a **real archive** (created from `archive-template/`, containing actual `sources/`, `people/`, etc.), the rules below apply in full and `research` is the default mode.

---

## Inside an archive

You are working inside a durable family-history archive. **`SPEC.md` is the law of this repository; `TOOLING.md` is the design of its tools.** When these instructions are not enough, read those.
When anything here conflicts with SPEC.md, SPEC.md wins.

## What this place is

Plain files are the source of truth.
Everything you generate must be rebuildable from them.
The archive must remain fully usable by a human with a file browser and a text editor — your job is to help, never to become load-bearing.

## The contract (non-negotiable)

1. **Your suggestions are not facts.** Every claim you draft gets `status: suggested`.
Only the human moves a claim to `accepted`, and only with a `reviewed:` date.
2. **Sessions are an interface, never memory.** Anything worth keeping is written into
archive records in SPEC formats before the session ends.
Never rely on conversation history as a store of record.
3. **Never modify original content.** The only allowed original-file changes are the
spec-defined documents-root processing rename (via `fha process`) and embedded metadata writes performed through `fha` tools; photos are NEVER renamed.
4. **Never edit generated content.** Files (or sections) beginning
`<!-- GENERATED ... -->` are rebuilt by tools; regenerate, don't patch.
5. **Mark your work as AI** where formats allow, and never overwrite human-written text.
6. **Respect privacy flags.** `living: true` AND `living: unknown` persons (unknown = living) and `restricted: true` sources are
excluded from any export or external-facing output unless the human explicitly says otherwise.
DNA material is always restricted.

## Operating modes

State your current mode in your FIRST reply of a session (propose one and get confirmation if the human didn't declare it).
One mode at a time; if a request crosses modes, say so and ask to switch — never drift silently.

- **research** (default) — read/edit records, run tools, draft claims/prose per the
contract.
Never edits SPEC.md, TOOLING.md, or `tools/`.
- **tool-building** — edits `tools/` and `tests/` only. Follow the build order
(TOOLING §15) and the implementation loop below.
Spec changes only as *proposed* decision-log entries for the human to approve.
- **migration** — bulk intake of existing material into the structure. The highest-risk
mode: PLAN (what moves where, counts) → DRY-RUN (full preview, no writes) → human approval → execute in bounded batches (≤200 files) → report.
Never deletes anything; photos are never renamed even here; only staged files move.
- **spec-refinement** — edits SPEC.md/TOOLING.md + the decision log, and MUST update
README.md whenever a change affects how a human reads the archive (the README rule).

### Tool-building: the implementation loop (per tool)

1. **Read** the tool's TOOLING section and every SPEC section it cites.
2. **Restate the contract** to the human before coding: inputs, outputs, flags, exit
codes, what it must never do.
Mismatch caught here is cheap.
3. **Implement** within the guardrails: Python ≥3.10; dependencies ONLY PyYAML, Jinja2
(site), exiftool-as-binary — adding any other is a proposed decision, not a choice; one file per tool under `tools/`, shared code only in `_lib.py`, tools never import tools; no network access (geocoder's gazetteer download excepted).
4. **Fixtures, not the archive.** Develop and test ONLY against `tests/fixtures/`
copies.
The real archive is never a test bed; destructive paths are exercised on fixtures exclusively.
5. **Definition of done:** `fha lint` runs clean on the clean pilot fixture; each of
the tool's error codes fires on its broken fixture; `--dry-run` previews every mutating operation; help text exists; TOOLING still describes the tool accurately.
6. **Handoff:** demo the commands, note any deviation (there should be none unlogged).

**Spec-discovery protocol:** when implementation reveals that TOOLING/SPEC is ambiguous, contradictory, or wrong — STOP.
Do not improvise past the spec (the docs must remain able to regenerate the tools).
Present the gap, propose the amendment as a decision-log entry, and proceed only after the human's call.

### Session end (all modes)

Summarize what changed and where; list any proposed-but-unapproved decisions; supply a one-line commit message (git is the change log — commit only when asked).

## The map

```
SPEC.md TOOLING.md      law + tool design (read before structural work)
photos/{year}/          originals — read-only to you (except spec'd keyword writes via tools)
                        NOTE: asset roots may live OUTSIDE this folder — resolve any
                        photos/ or documents/ path through fha.yaml roots first
documents/{type}/       originals — read-only to you (same exception)
sources/{type}/         one .md per source: frontmatter + ## Claims (yaml) + ## Notes
people/NNN .../         Ahnentafel couple folders; person + research files
people/connections/     non-direct people, "{anchor} {Surname}, {Given}"
people/stubs/           unplaced person stubs
places/places.yaml      place registry
notes/                  general research; notes/questions.md = question log
.cache/                 disposable tool caches — never treat as truth
```

## Format quick reference

- **IDs:** `{P|S|C|L|H}-{10 lowercase hex}` (H = hypothesis; never converts to C — verification mints a new claim and links both ways). Never invent one ad hoc — mint with
`fha id mint <TYPE>` (or generate 10 random hex digits and verify the string appears nowhere in the tree).
IDs are immutable and never reused.
- **Filenames:** sources `{slug}_{S-id}.md`; documents-root source files
`{slug}[-{copy}][-{role}]_{S-id}.{ext}` (photos-root files are NEVER renamed); person files `{surname}__{given_names}[_{kind}]_{P-id}.md` (birth surname, double underscore).
- **Dates:** EDTF only (`1850`, `1850~`, `185X`, `1850-05`, `1871-02/1871-03`).
- **Citations:** factual prose cites sources with bare `[S-xxxx]` tokens; `[P-xxxx]`
cross-links people. **Uncited prose is story/context, never fact** — write accordingly.
- **Claims:** YAML list under `## Claims` in the source file; schema in SPEC §8.4.
Required: `id, type, persons, value, status`; `roles:` required for relationship claims.
AI-drafted ⇒ `status: suggested`, and populate the Mills analysis fields (`information`, `evidence`; `source_class` on the source) by default.
- **Claim types:** birth, death, marriage, baptism, burial · residence, census,
occupation, education, military, immigration, divorce, name, relationship · event, note (+ free-text `subtype`).
Nothing else without a logged spec change.

## Tools (your hands)

Prefer `fha` tools over manual operations; if a tool does not exist yet, do the task by hand following SPEC and say so.

```
fha lint                     verify archive against spec — run after any batch of edits
fha index                    rebuild the SQLite query surface (.cache/index.sqlite)
fha id mint P|S|C|L|H        mint verified IDs
fha stubs                    create stubs for unresolved person references
fha process <file|folder>   process an original into a Source (documents: rename;
                            photos: NEVER rename — keyword only; + record scaffold)
fha views timeline|sources-index|brackets     regenerate views
fha photoindex find ...      query the photo library (never bulk-read photos/)
fha find <ID|text>           locate anything: record + assets + citations for an ID
fha packet <P-id>            person export packet
```

Execution rules (all tools): run from the archive root; `--dry-run` (or the tool's preview) before ANY mutating operation; check exit codes (0 clean, 1 warnings, 2 errors, 3 tool failure) and never proceed past a 2/3 silently; on unexpected behavior, read the tool's TOOLING.md section before retrying; full command reference: TOOLING §17.

Query the index, not the tree: person/claim/photo questions are SQL or `fha` calls.
Never bulk-ingest `photos/` or `documents/` into context.

## Common workflows

- **File the inbox:** on request, move items from `inbox/` to the right asset tree one
by one, confirming each destination with the human (filing is the one sanctioned move).
- **Source stubs:** an inbox asset may be paired with a freeform `*.notes.md` sidecar
(or sit in a bundle folder with one) — hand-written notes or capture-filled.
Treat it as the starting point when processing; its notes/hints seed `suggested` claims, never accepted facts.
The human can create one by hand anytime; honor whatever's there.
- **Add a source:** confirm the evidence file's location → `fha process` → fill
frontmatter (SPEC §14) → draft claims (`suggested`) with `anchor:`s → `fha lint`.
- **Review claims with the human:** take one source's `suggested` list; for each, show
the claim plus its anchor context; record the human's accept/dispute/edit; set `reviewed:`; finish with `fha lint`.
- **Write or extend a biography:** facts only from `accepted` claims; cite every factual
sentence (summary block: one citation per line; body: all relevant citations); anything uncited must read as story/context; cross-link people with `[P-]` tokens verified to exist.
- **Log searches:** when you search an external collection for the human (or execute a
research-next plan), write the research-log entry (date, repository, collection, terms, result incl. nil).
Check the log before proposing any search.
- **AI passes:** record every extraction pass in the source's `## AI Passes` yaml block
({date, model, harness, task, outputs, human_reviewed}).
Draft prose you write into profiles goes inside `<!-- AI-DRAFT ... -->` markers until the human accepts it.
- **Mine a transcript:** be selective — substantive assertions become `suggested` claims
with anchors; narrative chunks go to `## Stories`; the rest stays in the transcript (it is preserved and searchable; extraction is indexing, not preservation).
Record your pass in the source's `## AI Passes` block.

## Don'ts

No symlinks.
In research and migration modes, no new top-level archive folders (tool-building mode may create spec-defined support folders: `tools/`, `tests/`, `.claude/`).
No bulk renames.
NEVER rename anything under the photos root.
No editing `places.yaml` coordinates without human confirmation.
No writing to `.cache/` by hand.
No deleting anything without explicit instruction — prefer `status: rejected`/`superseded` and `closed` questions, which preserve the research trail.
