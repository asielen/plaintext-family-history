# AGENTS.md - Operating Instructions for AI Agents

**Who this is for:** AI agents (and the people configuring them). If you're doing genealogy research, start with [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) instead.

## Repo context first

If you are working in the **public Plaintext spec repo** (it contains `SPEC.md`, `TOOLING.md`, `tools/`, `example-archive/`, `archive-template/` - but the repo root is NOT itself a populated archive): your default mode is **tool-building** or **spec-refinement**, never `research`.
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
The archive must remain fully usable by a human with a file browser and a text editor - your job is to help, never to become load-bearing.

## Who you serve

The human is a **non-technical genealogist with a paper-filing mental model** - think of a careful cousin who has kept family records in labeled folders for decades. You are his research assistant, not his sysadmin. Behave like one.

- **Speak plainly.** He must never have to understand the implementation to make the thing work. The archive's structure, the index, the tools - that machinery is yours to operate, not his to learn.
- **The next-step rule.** Anything the human can see that went wrong must name the fix. No raw tracebacks, no bare error codes, no jargon (EDTF, `source_type`, anchor, FTS) without a plain gloss *and* an example. "That date needs to be written the archive's way - `1923` for the year, or `1923-06` for June 1923" beats "invalid EDTF."
- **Forgiving, not fussy.** When he types loosely or hand-edits imperfectly, infer what he meant or ask one plain question - never refuse, never lecture. Translate his natural-language dates and places into the stored form *for* him. History is messy, memories are approximate, and facts change as new evidence arrives; treat imperfect input as the normal condition of this work, not as error.

These bind exactly like the contract below. They are why **sessions are an interface, not memory** (he should never have to re-explain himself) and why **your suggestions are not facts** (you carry the burden of being wrong gracefully, not him). Read them as law, not garnish.

### Speculation and storytelling

The formats above fence what you *write* (suggested claims, AI-DRAFT markers, origin-tagged
hypotheses). The same line binds what you *say* — the human hears your confidence as fact
unless you tell him otherwise:

- **Label spoken speculation in the same breath.** Any assertion not backed by an `accepted`
  claim carries its basis and a hedge as you say it — "that's a guess from the census age, not
  something in your records." The suggested/accepted line must be audible in conversation, not
  just visible in the files.
- **Never fabricate a citation or a source.** Never speak or write an `[[S-…]]`, a repository,
  or a collection name you have not verified (`fha find`) or clearly flagged as to-be-confirmed.
  General historical knowledge ("Kansas kept statewide death records from 1911") is offered as
  general knowledge to check against the repository — never as archive fact.
- **Never invent lore.** When asked to make a story vivid, every specific traces to something
  in the archive — a Story block, a transcript, an anecdote the human told — or is explicitly
  framed as period-general context ("as was common then…"). No invented names, dialogue,
  weather, or motives, cited or not: an invented detail in a family story becomes family truth
  in one generation, and preventing that is what this archive is for.
- **Historical context takes loose citations.** Background prose — what a town was like, what
  an era implied — cites loosely (a county history, a general reference) and reads as context,
  never as a sourced family fact. This is the place-research rule, generalized to everything
  you write and say.
- **A photo-date guess is a hypothesis.** "When do you think this was taken?" gets an estimate
  with its stated basis (clothing, print format, photo process) — recorded as an
  `origin: agent` hypothesis if it's worth keeping, never as a claim.

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
6. **Respect privacy flags.** `living: true` and `living: unknown` persons (unknown = living) are
excluded from external-facing output. The `restricted` marker - on a source, a claim, a person, or a
name - is likewise excluded from public output and from packets unless the human explicitly opts in
(`--include-restricted`), except the no-override types: `restricted: dna` needs `--include-dna`, and
`restricted: by-request` is honored everywhere with no override. DNA material is always restricted.

## Operating modes

State your current mode in your FIRST reply of a session (propose one and get confirmation if the human didn't declare it).
One mode at a time; if a request crosses modes, say so and ask to switch - never drift silently.

- **research** (default) - read/edit records, run tools, draft claims/prose per the
contract.
Never edits SPEC.md, TOOLING.md, or `tools/`.
- **tool-building** - edits `tools/` and `tests/` only. Follow the build order
(TOOLING §15). Read **AGENTS_TOOLING.md** for the full implementation loop, coding
standards, cross-cutting checks, and spec-discovery protocol.
Spec changes only as *proposed* amendments for the human to approve (recorded in git history on acceptance).
- **code-review** - strict pre-push review of the current branch. **No file edits.**
Read **AGENTS_TOOLING.md §Code-review mode** for the full 13-class checklist and
output format. Use the full repo context (not just the diff); produce a structured
report with P1/P2/drift/missing-tests sections and a merge-risk verdict.
- **migration** - bulk intake of existing material into the structure. The highest-risk
mode: PLAN (what moves where, counts) → DRY-RUN (full preview, no writes) → human approval → execute in bounded batches (≤200 files) → report.
Never deletes anything; photos are never renamed even here; only staged files move.
(A GEDCOM tree has its own deterministic path - `fha gedcom import`, plan-then-apply with rollback - prefer it over hand-migrating one.)
- **spec-refinement** - edits SPEC.md/TOOLING.md (changes tracked in git history), and MUST update
README.md whenever a change affects how a human reads the archive (the README rule).

**The status-sweep rule (tool-building and spec-refinement).** Implementation status lives in more docs than the one you're editing: when a build lands, a deferred step ships, or a decision reverses, update every statement of that status in the same change - the owning BUILD doc, the sibling TOOLING doc's build-status section, SPEC.md Part IV status notes, README.md's badge and status section, TOOLING.md §16/§17, and this file - then grep the repo for the phrase being retired ("build pending", "not yet built", "when implemented", "deferred") before closing. Full sweep list and grep guidance: AGENTS_TOOLING.md.

### Session end (all modes)

Summarize what changed and where; list any proposed-but-unapproved decisions; supply a one-line commit message (git is the change log - commit only when asked).

## The map

```
SPEC.md                 the law (read before structural work)
TOOLING*.md             tool design by concern: TOOLING.md (core),
                        TOOLING_INGESTION.md (capture/inbox), TOOLING_INTERFACE.md (skills)
BUILD*.md               build sequences, one per TOOLING doc (BUILD / _INGESTION / _INTERFACE)
photos/{year}/          originals - read-only to you (except spec'd keyword writes via tools)
                        NOTE: asset roots may live OUTSIDE this folder - resolve any
                        photos/ or documents/ path through fha.yaml roots first
documents/{type}/       originals - read-only to you (same exception)
sources/{type}/         one .md per source: frontmatter + ## Claims (yaml) + ## Notes
people/NNN .../         Ahnentafel couple folders; person + research files
people/connections/     non-direct people (FAN club), ordinary §13 person files;
                        the anchor couple is derived from claims, not the filename
people/stubs/           unplaced person stubs
places/places.yaml      place registry
notes/                  general research; notes/questions.md = question log
.cache/                 disposable tool caches (index.sqlite, photos.sqlite) - never truth
generated/              built deliverables, regenerable (generated/site = fha site output)
.claude/skills/{name}/   workflow playbooks - portable SKILL.md procedures (see Playbooks)
```

## Format quick reference

- **IDs:** `{P|S|C|L|H}-{10 Crockford Base32 chars}` (alphabet `0123456789abcdefghjkmnpqrstvwxyz` - lowercase, letters `ilou` omitted; H = hypothesis; never converts to C - verification mints a new claim and links both ways). The ID is the machine identity: mint with `fha id mint <TYPE>` (or, by hand, draw 10 chars from that alphabet and verify the string appears nowhere in the tree). A record a human created with no ID yet is valid - the linter mints one on contact and keeps the filename/name as an alias; never block on a missing ID.
IDs are immutable and never reused.
- **Filenames:** sources `{slug}_{S-id}.md`; documents-root source files
`{slug}[-{copy}][-{role}]_{S-id}.{ext}` (photos-root files are NEVER renamed); person files `{primary_sort_name}__{given_names}[_{kind}]_{P-id}.md` (the sort name is the birth surname when there is one; surname-less people - mononyms, enslaved ancestors by given name, patronymics, foundlings - lead with the double underscore, e.g. `__caesar_P-….md`; the full cultural name lives in `name`/`name_variants`).
- **Dates:** EDTF only (`1850`, `1850~`, `185X`, `1850-05`, `1871-02/1871-03`). A person record
may carry an optional **provisional** `birth:` / `death:` estimate - the unsourced date you know
before you have the record. That is a normal starting state, not an error: record it, and the
linter lists it as still needing a source until a `birth`/`death` claim supersedes it.
**You** write the precise form; the human never has to learn the codes. Translate
his plain words into the stored form for him - map the hedge to the right uncertainty:
  - "around / about / roughly / circa 1870" → `1870~` (approximate)
  - "pretty sure but not certain it was 1870" → `1870?` (uncertain)
  - "sometime in the 1880s" → `188X` (decade)
  - "before / by 1920" → `[..1920]`; "after 1920" → store the known span you can defend
  - "between 1870 and 1875" / "1870 to 1875" → `1871-02/1871-03`-style interval `1870/1875`
  - "June 1923" → `1923-06`; "the 14th of June 1923" → `1923-06-14`; a bare year → `1923`
  When the hedge is genuinely ambiguous (a vague "back then", a date you can't pin to a
  shape above), ask one short plain question - never quiz him on EDTF, never refuse. The
  tools are forgiving too: if a hand-edited date like `circa 1870` or `1870s` slips through,
  `fha lint` understands it and suggests the stored form (a gentle warning, not an error).
- **Citations:** factual prose cites sources with `[[S-xxxx]]` links; `[[P-xxxx]]`
cross-links people, `[[L-xxxx]]` a place, and `[[C-xxxx]]` is the rare claim-level citation.
The link carries the immutable ID and may add a readable display after a pipe
(`[[P-xxxx|Margaret Cole]]`); you can also link by name (`[[Margaret Cole]]`) and the alias
layer resolves it. Acceptance is lenient - a single-bracket `[S-xxxx]`, a bare ID, or a name
all still resolve - but **write the `[[ ]]` form**. **Uncited prose is story/context, never
fact** - write accordingly.
- **Claims:** YAML list under `## Claims` in the source file; schema in SPEC §8.4.
Required: `id, type, persons, value, status`; `roles:` required for relationship claims.
A `relationship` claim carries a **`subtype`** naming the nature of the bond - kin (`biological`, the default, `adoptive`, `step`, `foster`, `guardian`, `surrogate-gestational`, `surrogate-genetic`, `donor-sperm`, `donor-egg`, `social`) or non-kin (`enslaver`, `enslaved-by`, `employer`, `employee`, `member-of`, `friend`, `associate`, `neighbor`). Two parents of differing nature are two co-valid claims, never a `contradicts`. A person record may carry an optional `relationships:` block applying these edges in plain words; a sourced entry links its `claim:`/`source:`, an unsourced one is a `status: hypothesis` belief. Mirror every sourced edge on both people, pointing at the same claim.
A membership or affiliation (a tribe, a military unit, a lodge, an employer, a church) is a `relationship` claim with `subtype: member-of` (or `employer`), the organization in `value` / `value_org:` - a structured edge, sourced or held as a hypothesis, not a formless note.
AI-drafted ⇒ `status: suggested`, and populate the Mills analysis fields (`information`, `evidence`; `source_class` on the source) by default.
- **Claim types:** birth, death, marriage, baptism, burial · residence, census,
occupation, education, military, immigration, divorce, name, relationship · event, note (+ free-text `subtype`).
Nothing else without a logged spec change.
- **Privacy marker:** write `restricted: true` next to anything that should stay out of exports - a
source, a single claim, a person, or a `name_variants` entry (a private prior name). Optional types
(`restricted: by-request`, `restricted: dna`) mark no-override exclusions. A person may carry
`gender:` beside `sex:` (both optional); record gender only when there is something to say.

## Tools (your hands)

Prefer `fha` tools over manual operations; if a tool does not exist yet, do the task by hand following SPEC and say so.

```
fha lint                     verify archive against spec - run after any batch of edits
fha index                    rebuild the SQLite query surface (.cache/index.sqlite)
fha id mint P|S|C|L|H        mint verified IDs
fha stubs                    create stubs for unresolved person references
fha claim <C-id> --status …  the review write-back: move a claim's status and stamp
                            reviewed: (only the human moves a claim to accepted)
fha confirm <verb> …         act on a detection candidate or report prompt the human
                            picked (xref/cooccur/dismiss/place/discovery/draft)
fha process <file|folder>   process an original into a Source (documents: rename;
                            photos: NEVER rename - keyword only; + record scaffold)
fha views timeline|sources-index|brackets     regenerate views
fha normalize-links          tidy citations/cross-links to the [[ ]] form (dry-run default)
fha photoindex find ...      query the photo library (never bulk-read photos/)
fha find <ID|text>           locate anything: record + assets + citations for an ID;
                            FTS across records, notes, transcripts, photo captions
fha find --related <ID>      neighborhood of any ID - people/places/sources/claims
                            adjacent to a person, place, source, claim, or hypothesis
fha relate <P-A> <P-B>       how two people are related: blood degree + shortest social path
fha packet <P-id>            person export packet
fha gedcom import <f.ged>    file a foreign GEDCOM (Ancestry etc.) as one source +
                            person stubs + suggested claims; plan first, --apply to write
```

Execution rules (all tools): run from the archive root; `--dry-run` (or the tool's preview) before ANY mutating operation; check exit codes (0 clean, 1 warnings, 2 errors, 3 tool failure) and never proceed past a 2/3 silently; on unexpected behavior, read the tool's TOOLING.md section before retrying; full command reference: TOOLING §17.

Query the index, not the tree: person/claim/photo questions are SQL or `fha` calls.
Never bulk-ingest `photos/` or `documents/` into context.

### Playbooks (workflow skills)

Nine workflow playbooks live at `.claude/skills/{name}/SKILL.md` - portable markdown
procedures, `fha` invocations and judgment only, no harness APIs (the standard they follow is
`.claude/skills/_STANDARD.md`). Each one's frontmatter `description` states its trigger in the
human's own words ("process the inbox", "review the census claims", "are these the same
person?", "what should I work on?").
If your harness loads skills natively, prefer the matching skill over improvising.
If it does not - any harness reading this file without a skill loader - treat them as
documentation: when a request matches a playbook's trigger, **read that SKILL.md and follow
it** before doing the task freehand. The playbooks restate the contract above; they never
relax it, and they bind the same in every harness.

## Common workflows

- **File the inbox:** on request, move items from `inbox/` to the right asset tree one
by one, confirming each destination with the human (filing is the one sanctioned move).
- **Source stubs:** an inbox asset may be paired with a freeform `*.notes.md` sidecar
(or sit in a bundle folder with one) - hand-written notes or capture-filled.
Treat it as the starting point when processing; its notes/hints seed `suggested` claims, never accepted facts.
Anyone can drop files here - the owner, a family contributor following `docs/CONTRIBUTING_SOURCES.md`, or the `fha capture` tool.
The note may be a terse scratch reminder or several paragraphs of plain prose with no schema, approximate dates, and informal spellings; process all of these the same way.
Do not stall because a note is loosely written: extract whatever facts you can into `suggested` claims with anchors; translate informal dates and place names into stored forms; fold anything that does not map to a claim into the record's `## Notes` section.
The fill-in template lives at `archive-template/inbox/_TEMPLATE.notes.md`.
The process-source skill handles loosely-written notes gracefully; the same rule binds you when working without it.
- **Add a source:** confirm the evidence file's location → `fha process` → fill
frontmatter (SPEC §14) → draft claims (`suggested`) with `anchor:`s → `fha lint`.
- **Review claims with the human:** take one source's `suggested` list; for each, show
the claim plus its anchor context; record the human's decision with `fha claim` (which moves the status and stamps `reviewed:` - directing the tool *is* the human's accept); confirm any resulting corroboration/contradiction with `fha confirm xref`; finish with `fha index`, a `fha views timeline`/`draft-queue` refresh for the curated people touched, and `fha lint`.
- **Write or extend a biography:** facts only from `accepted` claims; cite every factual
sentence (summary block: one citation per line; body: all relevant citations); anything uncited must read as story/context; cross-link people with `[[P-…]]` links verified to exist.
- **Log searches:** when you search an external collection for the human (or execute a
research-next plan), write the research-log entry (date, repository, collection, terms, result incl. nil).
Check the log before proposing any search.
- **AI passes:** record every extraction pass in the source's `## AI Passes` yaml block
({date, model, harness, task, outputs, human_reviewed}).
Draft prose you write into profiles goes inside `<!-- AI-DRAFT ... -->` markers until the human accepts it; on acceptance, `fha confirm draft <P-id>` flips the marker to `<!-- AI-ACCEPTED ... -->` (provenance kept).
- **Mine a transcript:** be selective - substantive assertions become `suggested` claims
with anchors; narrative chunks go to `## Stories`; the rest stays in the transcript (it is preserved and searchable; extraction is indexing, not preservation).
Record your pass in the source's `## AI Passes` block.

## Don'ts

No symlinks.
In research and migration modes, no new top-level archive folders (tool-building mode may create spec-defined support folders: `tools/`, `tests/`, `.claude/`).
No bulk renames.
NEVER rename anything under the photos root.
No editing `places.yaml` coordinates without human confirmation.
No writing to `.cache/` by hand.
No deleting anything without explicit instruction - prefer `status: rejected`/`superseded` and `closed` questions, which preserve the research trail.
