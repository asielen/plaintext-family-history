# SPEC.md - Plaintext Family History

*The durable, file-first family-history archive - the specification.*

**Who this is for:** people defining or auditing the archive rules - spec authors, implementers, and anyone checking whether a tool or record conforms. If you just want to use the archive, start with [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md).

**Version 1.2 - 2026-06-12**

This document is the source of truth for the archive: its philosophy, its data model, and its physical format.
Its companion, **`TOOLING.md`**, is the design document for every supporting script - deep enough that all tooling can be rebuilt from scratch, in any language, from the two documents together.
When a tool and the spec disagree, the spec wins.
When the spec and the files disagree, fix one and log the decision.

This spec describes a **target system**.
Normative language - *must*, *should*, *is to be* - defines what the archive and its tools are required to do, whether or not they exist yet.
The T.E. Hartley pilot is the first conforming material.

**How to read this document.** Part I is the philosophy - why the system is shaped this way.
Part II is the data model - what exists.
Part III is the physical format - how it lives on disk.
Part IV states the requirements every tool must meet (the *what*; `TOOLING.md` is the *how*).
Sections marked `LOCKED` are settled; reopening one requires a logged decision.

---

# Part I - Philosophy

## 1. What this is `LOCKED`

For most of the history of genealogy, real research lived in filing cabinets: the document in the drawer, the post-it note on the document, the binder of typed family sheets, the shoebox of labeled photographs.
That system had a virtue modern software keeps losing - **anyone could open the drawer**.
No login, no subscription, no schema migration.
A century later, a curious descendant could still pull the folder and read it.

This project is that filing cabinet, rebuilt to last: the grounding of old-school research in a plain file system, with a 20th-century layer (durable digital formats, embedded metadata), a 21st-century layer (search, structured claims, generated indexes), and now an AI layer (assisted extraction, research feeds) stacked *on top of it* - never *instead of it*.
Strip away every layer above the files and the archive still works, the way the drawer still works.

It is an archive-first project that *may* use genealogy software - never the reverse.
The deliverable is not an app.
It is **this written spec** and **a kit of small, replaceable scripts**.
The format and the process are the durable assets; every tool is a borrowed engine or a piece of regenerable glue.

The standard is not perfect research practice.
It is *good-enough* research practice with maintainability and searchability for future generations - research that someone can pick up in fifty years, on whatever computers look like then, and continue.

## 2. Guiding principles `LOCKED`

1. **The archive is the source of truth; tools are replaceable.**
2. **Durable, plain formats.** First tier: `.txt`, `.md`, `.csv`, `.jsonl`, `.yaml`, `.jpg`, `.tiff`, embedded IPTC/XMP. Second tier (acceptable): static HTML/CSS/JS, PDF. Everything else (audio, video, raw DNA, tool exports) only when necessary.
3. **Every important fact is traceable to a source.**
4. **Folder location is for human browsing; metadata carries meaning.** Folder trees are projections - regenerable, rearrangeable, never the only carrier of a relationship.
5. **Stay light.** Long-term durability beats short-term interface convenience. The right answer is usually simple and boring at the archive layer, with optional sophistication above it.

## 3. The four layers `LOCKED`

The system is built around four layers, starting from the source material and working up to what gets shared with the world.
Each layer may only depend on the layers below it, and nothing above the first layer is ever allowed to become the truth.
This document defines layer 1 precisely and constrains how the other three are permitted to behave.

1. **Durable archive layer** - files, folders, embedded metadata, markdown records, YAML blocks, stable IDs. The drawer itself. *(Parts II-III.)*
2. **Working interface layer** - optional editors and apps over the files: Obsidian or the like for notes, photo management software (like Lightroom), an AI session for research, a genealogy app fed by export. **The working or ingesting interface is never the truth.** Tooling that supports research or ingestion exists only for those purposes and is never load-bearing - it owns no data, and the archive must not notice if it disappears.
3. **Generated intelligence layer** - the index, full-text search, generated views (timelines, sources-indexes, family trees), AI-built caches. Fully rebuildable from layer 1, on demand, every time.
4. **Publication/export layer** - the static HTML explorer, person packets, profile exports, share packages. Derived outputs, never stored objects.

## 4. From file to fact: the processing path `LOCKED`

All documents, photos, recordings, and scans start as just that: **a file in a file system**.
Most of them stay that way forever, identified by their filename and embedded metadata, browsable in their folders - and that is a complete, legitimate, permanent state, not a backlog.

A file earns more structure only when it earns attention:

- When a file turns out to carry evidence - names on the back of a photo, a household in a census page - it should be **processed** into a Source: it is assigned an ID, and a source record is created describing what it is, where it came from, and how to cite it (§14).
- The next level connects the dots between the file and the research. This is done through **Claims**: a claim is a single fact supported by the source - a birth, a residence, a marriage, a relationship. Claims should be created *inside the source record*, parallel to the evidence they rest on (§8).
- Once a fact exists as a claim, it can be **cited in research**: a person's profile states the fact in prose and cites the source, so any reader can walk from the sentence to the evidence (§18).

The same born-minimal pattern governs everything else.
A **person** mentioned in a census enters as a stub - an ID and a name - and graduates to curated only when actively researched; expect dozens of curated profiles among ~2,000 stubs.
An **assertion** in an interview transcript stays as searchable text in the preserved transcript until it is substantive enough to be processed into a claim.
Observed rates make the point: roughly 1 photo in 50 carries evidence worth a source record.
Hand-labor scales with *curiosity*, not with the size of the family - that is what makes a 2,000-person, 10,000-photo archive maintainable by one person.

## 5. Tooling philosophy `LOCKED`

Own the format and the glue; borrow the hard engines (exiftool, OCR, transcription, SQLite).
Specifics:

1. **Nothing generated is load-bearing.** Indexes, search, caches, generated views, embeddings - all rebuildable from the archive. Delete any of them and the next run restores them.
2. **The spec is the source of truth for tooling.** Every script is an executable copy of rules written in these documents; if a script cannot be regenerated from the spec, the spec is incomplete. If Python fades or every script dies, the archive remains fully usable **by hand** - a person names a file, fills in the shipped template (`archive-template/`), and links to it by name (`[[grandpas-letter]]`), with no software at all. The machine IDs are a convenience the tools add and maintain; a human never has to create one.
3. **Python is preferred** for scripts, minimal dependencies, small and single-purpose. No script may be *required*. The preferred language may change in the future.
4. **Tools report by default** and modify only when explicitly asked.
5. **The tools are a headless core.** Each tool is an engine that computes and returns a structured result; rendering that result for a human is a separate, replaceable layer. Whatever front door drives the tools - a terminal command, an AI agent shelling out, a batch pipeline, a future click in a UI - calls the same engine and reads the same result. The interface is glue; the engine is the tool. (This is also how the deterministic write-backs stay honest across front doors - see §6.)

**The archive test.** Any tool that touches the archive must pass:

1. **Original content untouched.** Tools never alter pixels, audio, scan content, or document text. Two sanctioned, controlled operations exist: processing renames a *documents-root* file (filename only, never content or location; photos are never renamed at all), and tools may write the spec'd non-destructive embedded metadata (§20: `SOURCE:` source keywords, bare `P-id` person keywords, date-confidence keywords, captions, AI markers) - preservation operations, not content edits.
2. **Rebuildable.** Anything the tool generates can be rebuilt from the archive.
3. **Links to stable IDs** - or at minimum does not block adding them later.
4. **Plain export.** No knowledge trapped in an opaque store.
5. **Privacy-aware.** Can respect the `living`/`restricted` flags (§19).
6. **Disposable.** If the tool dies, the archive survives without it.

A tool failing 1, 2, or 6 cannot own any part of the durable layer.

Tool Evaluation Rule of Thumb: *can data get in and out with a small script, or does the tool want to be the center?* 

## 6. Working with AI `LOCKED`

AI is potentially the most powerful layer of this system and the most dangerous to its integrity, so it gets its own rules.

**The AI contract (locked).** Any AI - any model, any harness, any vendor - that touches the archive must honor:

1. **AI suggestions are not facts.** Every AI-derived assertion enters the claim lifecycle at `status: suggested` and reaches `accepted` only through human review. AI-written text is marked as AI wherever it is stored (keywords, marker blocks); human-written content is never overwritten. The review gate is the human's *decision*, not any one editing method: a tool may perform the actual status change deterministically, but it does so only because a human directed it. Whatever issues that confirmation - a typed instruction today, a click later - is the gate; the tool never moves a claim to `accepted` on its own.
2. **AI sessions are an interface, never memory.** No conversation, chat history, or agent state is ever the store of record. Anything worth keeping is written into the archive in the formats of this spec, where it is reviewable, diffable, and durable.
3. **Extraction is indexing, not preservation.** Originals (transcripts especially) are preserved verbatim in the archive, so AI mining can be selective and can be *re-run* anytime as models improve. Each extraction pass over a source is recorded in that source's record (model, date).
4. AI tooling passes the archive test like everything else.

The day-to-day research workbench is an **agentic CLI harness opened on the archive root** - an AI that reads and edits the files directly, runs the `fha` tools as its hands, and sits beside a plain text editor so human and AI work the same files in the same place. **Claude Code is the current operating choice; it is not a required one.** Vendor lock-in is prevented structurally: the harness configuration lives in open formats at the archive root (`AGENTS.md` as the canonical agent instructions, with `CLAUDE.md` deferring to it; skills in the portable SKILL.md standard), no harness-only state is ever load-bearing, and any agent harness honoring the contract above is an acceptable drop-in.
Harness configuration and the workflow skills are specified in `TOOLING_INTERFACE.md` (summarized in `TOOLING.md` §16).
Other modes keep their own surfaces: chat projects for thought-partnership, headless pipelines for batch work, Obsidian/Lightroom/the generated site for human browsing.

---

# Part II - The Data Model

## 7. Record types and what each ID is for `LOCKED`

| Concept | What it is | Identity |
|---|---|---|
| **Person** | A human, living or dead. External profile IDs (WikiTree, Ancestry, FamilySearch) are fields on the person. | `P-` id |
| **Source** | An evidence-bearing item: a record, document, photo-as-evidence, interview, letter, clipping. | `S-` id |
| **Claim** | A single sourced assertion - a date, place, relationship, event, attribute. The heart of traceability. | `C-` id |
| **Place** | A location referenced by claims. | `L-` id |
| **Hypothesis** | An unsourced placeholder belief under investigation - a guess, never a fact. Lives in research files (§16). | `H-` id |
| **Asset** | An actual file on disk (photo, scan, recording, transcript). | **Shares its source's `S-` id** once processed - carried in the filename (documents) or embedded keyword (photos); *no ID before processing* |

**Assets share their source's identity.** All files of a processed source - fronts, backs, copy B, negatives, pages, transcripts - are *copies and facets of one piece of evidence*, so they all carry the source's ID - in their filenames for documents-root files (§13), in embedded keywords for photos-root files (§20, never renamed).
Searching a cited ID in filenames+keywords surfaces the record *and* the files together.
Unprocessed files (most photos) are **not** auto-assigned IDs: renaming ten thousand files would buy nothing, since filename plus embedded metadata already identifies them completely.
IDs are assigned at processing, on need - the processing path (§4) applied to identity itself.

**What each ID is for** - the usage map that justifies each one's existence:

- **`S-` ids** are the *citation and retrieval unit*: prose cites them (§17), filenames carry them, embedded keywords repeat them. If you only ever interact with the archive as a reader, S-ids are the only IDs you need.
- **`C-` ids** are the *assertion unit*, used almost entirely by tooling and active research: claim-to-claim links (`corroborates`, `contradicts`, supersession), review tracking and backlogs, the linter's cross-checks (summary block ↔ accepted claims), generated timelines, and the rare precision citation `[[C-…]]` for one disputed assertion. Readers never need them; the system does.
- **`P-` ids** make people unambiguously linkable across name changes, spelling variants, and duplicate names - in claims' `persons:` lists, in profile cross-links, in photo keywords (§20) - which is why a human-typed name resolves to a person only when it is unambiguous, and a shared name (two `John Smith`s) is pinned to its ID.
- **`L-` ids** keep places stable across renamings and spelling variants in claims.
- **`H-` ids** give hypotheses stable handles for the report (tracking across sessions), question references, and the discovery join ("a hypothesis from 2024 verified today"). **An H-id never converts to a C-id** - IDs are immutable and typed for life. Verification mints a *new* claim from the found source; hypothesis records `verified → C-…`, claim carries optional `hypothesis: H-…` back-pointer; both persist.

Relationships and events are not separate object types - they are **claim types** (§8.2), sourced like any other fact.
Organizations are claim values, not a dedicated record type: an organization's name lives in a claim's `value` (and an optional `value_org:` for grouping), never in an `Org-` record. A person's **membership** in one - a tribe, a military unit, a lodge, an employer, a church - is a structured `relationship` claim (`subtype: member-of` or `employer`, §8.2), sourced like any fact or held as a hypothesis until it is, so the belonging edge is queryable while the organization stays a value.

## 8. Claims `LOCKED`

A claim is a single sourced assertion.
Claims live in a fenced YAML list under the `## Claims` heading of their source's record - one Markdown file per source, frontmatter (the source's identity) on top, then the claims it supports (the full file layout is §14, Part III; this section defines the claims themselves).
A claim belongs to the *source*, never to an individual file copy - the back of copy B supports the same source's claims as the front of copy A.

### 8.1 Three orthogonal axes

**type** - *what* is asserted.
Controlled vocabulary (§8.2).

**status** - *how reviewed*.
The fact-safety lifecycle:

```
suggested → needs-review → accepted | disputed | rejected | superseded
```

Nothing reaches `accepted` without human review.
AI-generated claims always enter at `suggested`. `superseded` claims are kept, pointing forward via `notes`.

**significance** - *how much it defines a complete record*.
Optional per-claim override: a `significance` field wins over the table and **must** carry a `significance_reason` (one line).
Overrides are rare by design.

Resolution rule for any tool: `claim.significance if set, else SIGNIFICANCE[claim.type]`.

### 8.2 Type vocabulary and significance table

This table is ours and editable; editing it is a logged decision, never a per-claim choice.

| Significance | Types | Role |
|---|---|---|
| **vital** | `birth`, `death`, `marriage` (+ `baptism`, `burial` where they stand in for vital records) | Defines completeness: a person's record is complete when each applicable vital type has ≥1 `accepted` claim. *Applicability:* `death` is inapplicable while `living: true|unknown`; `marriage` is satisfied by a `no_known_marriages` flag or a `negated: true` marriage claim (§8.6) - a confirmed absence counts complete, not missing. |
| **substantive** | `residence`, `census`, `occupation`, `education`, `military`, `immigration`, `divorce`, `name`, `relationship` | Enriches the record; recurring; not required for completeness. `relationship` carries a **`subtype` naming the nature of the bond**, drawn from a standardized vocabulary. *Kin-nature:* `biological` (the default for an unqualified parent or child edge), `adoptive`, `step`, `foster`, `guardian`, `surrogate-gestational`, `surrogate-genetic`, `donor-sperm`, `donor-egg`, `social`. *Non-kin and power ties:* `enslaver`, `enslaved-by`, `employer`, `employee`, `member-of`, and the social ties `friend`, `associate`, `neighbor` - the latter, when sourced (a hunting-party clipping, a unit roster), is how the FAN network is built; unsourced ties live as hypotheses. Two parents of differing nature (a biological and an adoptive father) are two co-valid edges, never a contradiction. The list is mostly closed: an unlisted nature lands in `subtype` free text, so no relationship stalls for lack of a word. |
| **incidental** | `event`, `note` | Preserved, never scored - anecdotes, one-off moments. `subtype` free text carries detail. |

The vocabulary is mostly closed: new normalized types are added here deliberately; everything else lands in `event`/`note` + `subtype`, so no fact ever stalls for lack of a category.

### 8.3 Multi-subject claims

A claim may reference multiple subject persons (`persons:` list) - a marriage names two, a census household many. `persons:` is the index of who is involved; the optional `roles:` map carries the semantics (child/parent, spouse, head/household_member) and is **required for `relationship` claims** - positional convention alone is too fragile for exporters and tree regeneration.

A `relationship` claim may repeat a role with distinct subtypes - a child can carry several `parent` edges (`subtype: biological`, `adoptive`, `surrogate-gestational`, `donor-egg`), each a separate, equally valid bond rather than a conflict. Beyond `child`/`parent`/`spouse`/`head`/`household_member`, `roles:` also names directional power ties - `enslaved`/`enslaver`, `employer`/`employee` - and the affiliation role `member`, so an exporter never confuses victim and perpetrator or guesses a direction the source made explicit.

### 8.4 Claim field reference

```yaml
- value: bookkeeper, Plains Junction Railroad   # required, FIRST - the human-skimmable
                                 #   summary of the assertion; a claims block is read by value
  id: C-90ad2e11b7              # required; §10
  type: occupation               # required; §8.2 vocabulary
  persons: [P-de957bcda1]        # required; one or more P-ids
  date: 1869/1874                # EDTF (§11); omit only if truly undatable
  place: L-baba9801fa            # optional; L-id
  status: accepted               # required; §8.1 lifecycle
  confidence: high               # required; high | medium | low (§8.5). Tooling defaults by
                                 #   source_type (vital-record → high) and asks only when unclear
  reviewed: 2026-06-10           # date of last human review (required once past suggested)
  notes: >                       # optional but EXPECTED - the context/detail behind the claim,
                                 #   2-3 sentences typically, a "novel" if the claim is dense;
                                 #   linter warns (W109) when a non-vital/low-confidence claim
                                 #   lacks it. Provenance remarks, supersession pointers go here too.
    Listed as book-keeper for the Plains Junction RR in the 1874 directory; the 1869
    Champion item places him there from the railroad's early days.
  # ---- other optional fields, present only when used ----
  subtype: biological            # the NATURE of a relationship (kin: biological [default] | adoptive |
                                 #   step | foster | guardian | surrogate-gestational | surrogate-genetic |
                                 #   donor-sperm | donor-egg | social; non-kin: enslaver | enslaved-by |
                                 #   employer | employee | member-of | friend | associate | neighbor);
                                 #   also free-text refinement on event/note. Unlisted natures: free text.
  roles:                         # explicit semantics for multi-subject claims;
    child: P-de957bcda1          #   REQUIRED for type: relationship; repeated roles with distinct
    parent: [P-aaaaaaaaaa]       #   subtypes are valid (multiple parents of differing nature). Also:
                                 #   spouse:, head:/household_member:, enslaved:/enslaver:, employer:/
                                 #   employee:, member: for the matching subtype.
  negated: true                  # confirmed ABSENCE: "we researched and it did not happen"
                                 #   (e.g. type: marriage + negated: true = confirmed never married);
                                 #   pairs with evidence: negative. See §8.6.
  restricted: true               # optional; keeps this single claim out of public output and out of
                                 #   packets unless --include-restricted (§19). A free-text type
                                 #   (restricted: by-request) marks a no-override exclusion.
  place_text: "Fairview City, Breton Co., Kansas"   # the place AS WRITTEN in the source;
                                 #   `place:` is the normalized L-id interpretation
  value_org: "Cherokee Nation"   # optional; the organization a member-of/employer relationship names,
                                 #   as a string (no record type) - lets views group "everyone in Co. K"
  information: primary           # Mills analysis (optional): primary | secondary | undetermined
  evidence: direct               # direct | indirect | negative
  asset: b-back                  # pins claim to a copy/role suffix of the source's files
  anchor: "00:14:32"             # position inside the source: timestamp, page, or line
  corroborates: [C-xxxxxxxxxx]   # this claim independently supports those
  contradicts: [C-xxxxxxxxxx]    # conflict - tooling spawns an open question
  hypothesis: H-xxxxxxxxxx       # back-pointer: the hypothesis this claim verified
  significance: vital            # override only; requires significance_reason
  significance_reason: linchpin of the Marsh Creek identification
```

### 8.5 Evidence analysis (optional), and confidence

The full analytical vocabulary of the field (Mills, *Evidence Explained*) is available as optional fields, never required of a human: `source_class` on sources (original | derivative | authored), and `information` (primary | secondary | undetermined - judged per informant per assertion) and `evidence` (direct | indirect | negative - relative to the question) on claims. **AI-assisted research populates these by default**; the linter's informational pass pings accepted claims missing them so cleanup sessions can backfill.
Tentative identification ("a John Smith who may be ours") is expressed as low confidence + a hypothesis, never a separate mechanism.

### Confidence, distinct from status

Status is *review state*; confidence is *evidence quality* - **required on every claim**.
A hearsay claim can be `accepted` (as what was said) while remaining `low` confidence (as what happened).
Rubric: `high` = first-person/primary with specific date and place, ideally corroborated · `medium` = single source with moderate specificity · `low` = hearsay, vague time/place, or unresolved speaker. **Tooling defaults confidence from `source_type`** (vital-record → high, census/newspaper → medium, interview hearsay → low) and only asks the human when the source class is ambiguous; the human can always override.

### 8.6 Confirmed absences (negative facts)

Some of the most important genealogical findings are *absences*: a person who never married, had no children, or - for someone still living - has no death record.
"We researched and it did not happen" is a real, citable conclusion, represented two ways:

- **Negative-fact claim** (the researched case): a normal claim of the relevant `type` with **`negated: true`** and `evidence: negative`, e.g. `type: marriage, negated: true, value: "no marriage found", confidence: medium` citing the searches that justify it. It sits in the source's claims list like any claim and is fully sourced - typically by a proof-argument source (§14) assembling the negative searches.
- **Person-level convenience flags** (the common, low-ceremony case): optional booleans on the person record - `no_known_marriages: true`, `no_known_children: true` - for quickly recording a settled judgment without authoring a claim. They are *assertions of current knowledge*, not sourced facts; tooling treats them as "stop flagging this person's missing marriage/children in vitals gaps," and a later contradicting claim supersedes them.

This keeps completeness honest: a person isn't "missing" a marriage if we've confirmed there wasn't one. **Living persons** (`living: true|unknown`) are likewise never flagged for a missing death - the vitals-completeness check (§8.2) treats death as inapplicable while living.

The same low-ceremony pattern covers **provisional vital estimates**: optional `birth:` / `death:` fields on a person record (§9) hold the unsourced date you know before you have found the record. Like the absence flags, they are assertions of current knowledge, not sourced facts; the linter lists them as needing a source, and a sourced `birth`/`death` claim supersedes them. Recording what you know before you can prove it is a legitimate starting state, not an error.

### 8.7 Claims are a background layer

A human reading the archive never needs claims: prose cites *sources*, and the reader's path is profile → source record → file.
Claims exist to power tooling - timelines, completeness checks, sources-indexes, exports, review workflows, contradiction detection.
The source file is each claim's durable, human-readable home (the post-it on the document in the drawer); all *querying* happens against the generated index, rebuilt from disk on demand.
Tooling must abstract claims away from readers while depending on them completely - the full design implications live in `TOOLING.md`.
The cost of files-as-truth - edit, then reindex - is accepted.

Claims live in a fenced ` ```yaml ` block - not because the parser needs the fence but because it helps markdown tooling render cleanly instead of as a garbled mess, and keeps a value's stray `#` or `*` literal. (Frontmatter needs no fence only because it is treated as a special entity by markdown editors such as Obsidian, which is also why frontmatter links can be clickable and a fenced block's cannot - and why the cross-linking lives in the frontmatter `people:` / `places:` fields (§14), not the claims block.) Inside the claims block, IDs are written **bare** (`persons: [P-…]`, `place: L-…`) - structured data, not `[[ ]]` links. If you write claims by hand and forget the `yaml` fence, the tools still read claim-shaped content under `## Claims`, and the linter will later add the fence for you - the fence is the preferred form, not a trap.

## 9. Persons `LOCKED`

Two tiers (§4): **stub** - frontmatter only, script-mintable in bulk, a permanent legitimate state - and **curated** - the full file set of §16. **Rule:** every `P-id` referenced anywhere must resolve to at least a stub.

**Merging and separating identities.** When two person records prove to be one human: choose a survivor; the other record gains `status: merged`, `merged_into: P-survivor`, `merge_reason:`, `merged_date:` - and its file **persists forever, renamed with a `MERGED-INTO-P-survivor__` prefix** (e.g. `MERGED-INTO-P-de957bcda1__hartley__thomas_P-old.md`) so the tombstone is obvious on disk; IDs never die and every old reference still resolves through the pointer.
Name variants and external IDs fold into the survivor.
Tools resolve references *through* `merged_into`; the linter warns on new claims pointing at a merged person and lists remaining direct references for gradual cleanup.
When one record proves to be two people (conflation): mint a new P-id and reassign each claim's `persons:`/`roles:` entries deliberately - a guided human task, since dividing an identity is research judgment - with both records noting the split and date.
Source records get the parallel treatment: `status: superseded`, `superseded_by: S-…` (e.g. a better scan processed later), retained for the audit trail.

```yaml
name: Thomas Edward Hartley     # required; preferred display name
id: P-de957bcda1                 # required
aliases:                         # link-resolution handles, tool-maintained: the ID (so [[P-…]]
  - P-de957bcda1                 #   resolves in Obsidian) + the name & variants (so [[Thomas
  - Thomas Edward Hartley        #   Edward Hartley]] resolves) + any human stem you like.
  - T. E. Hartley                #   Distinct in purpose from name_variants (display) - see §18.
name_variants: [T. E. Hartley]    # optional; display variants (mirrored into aliases)
birth: 1840~                     # optional PROVISIONAL estimate (unsourced); EDTF; superseded by a
death: 1941                      #   birth/death claim. Linter tracks these toward a source (§8.6).
face_tags: ["Thomas Edward Hartley"]   # optional: EXACT face/people-tag strings meaning
                                 # this person in the photo library (§20) - the durable
                                 # name→P-id resolution; one line here vs retagging photos
profile_photo: hartley-portrait-1895.jpg   # optional; the person's main portrait - shown on their
                                 # profile page and as a small square in the family tree. A filename,
                                 # path, or the photo's S-id, resolved leniently through the photo
                                 # index (§20) so it survives Lightroom moves. Same export privacy
                                 # gate as the photo strip: a portrait that co-depicts a living person,
                                 # or whose source is withheld, is dropped from public output (§19).
sex: M                           # optional; birth-assigned where a record states it; M | F | intersex | unknown
gender: man                      # optional; identity, free text with suggested values; omit unless there is something to record
living: false                    # required; true | false | unknown - drives export redaction (§19)
restricted: false                # optional; keeps this person out of public output, and out of packets
                                 #   unless --include-restricted. A type (restricted: by-request) is
                                 #   a no-override exclusion honored by every export path (§19).
no_known_marriages: false        # optional; confirmed-absence convenience flag (§8.6)
no_known_children: false         # optional; confirmed-absence convenience flag (§8.6)
relationships:                   # optional; the working surface where relationship claims are
  - to: "[[P-075114a0f8|Caleb Comstock Hartley]]"   # forgiving target: name, [[name]], or [[P-id]]
    type: parent                 #   the ROLE; subtype names the NATURE (§8.2)
    subtype: biological          #   omit for biological (the default)
    claim: "[[C-90ad2e11b7]]"    #   link the backing claim (preferred) or source: [[S-…]]; optional to
                                 #   write, required for the entry to count complete. No link = a belief.
  - to: "[[Walter Doe]]"         # an unsourced belief: jotted before evidence is filed
    type: parent                 #   linter lists it as needing a source, never rejects it
    status: hypothesis           #   honestly marked as not yet a fact (§16)
external_ids:                    # optional
  wikitree: Hartley-6084
  ancestry: "382013742308"
created: 2026-06-10
tier: curated                    # stub | curated
```

The person record is identity, flags, and prose; sourced facts live with evidence, as claims. The record may carry optional **provisional** `birth:` / `death:` estimates - most genealogists expect to record those as core data points. Like any fact they want a source: add a `birth`/`death` claim tagged to the person, and that sourced claim supersedes the frontmatter estimate. When run, the linter matches the provisional `birth:`/`death:` values against sourced birth/death claims to keep them aligned, and lists any still needing a source (§8.6).

A person's `name` and each `name_variants` entry are mirrored into `aliases:`, so typing `[[Thomas Edward Hartley]]` resolves to that person without needing the unique ID. People can share a name, though: if two `John Smith`s exist, the linter flags an **alias clash**, and the clashing name is pinned to its ID (`[[P-…|John Smith]]`). You get the best of both worlds - the stable unique ID and an easy-to-read name. A `name_variants` entry may be written as a `{value:, restricted: true}` mapping rather than a bare string when the name should stay private - a prior name someone no longer uses, for instance. It still resolves internally (so existing links keep working) and is redacted on export (§18, §19); it is the general `restricted` marker (§19) applied to a name, not a separate mechanism.

The optional `relationships:` block is the human-writable surface where relationship claims are applied to the lives they concern. Each entry names the other person the forgiving way (a name, a `[[name]]` tag, or a pinned `[[P-id|Name]]`, settled by the tools), a `type` (the role: parent, child, spouse, sibling, …), and a `subtype` (the nature of the bond, §8.2). A **sourced** entry links its backing with `claim: [[C-…]]` or `source: [[S-…]]` - optional to write, required for the entry to count complete - and the linter reconciles it against the accepted `relationship` claim that is its durable home. An **unsourced** entry carries `status: hypothesis`: a belief you hold before you can cite it, listed as needing a source, never an error. This mirrors the provisional `birth:`/`death:` pattern (§8.6): most of what you know about a person begins as an unsourced jotting and graduates when the source is found and mints the claim.

Two parents of differing nature sit side by side as equally valid edges - a biological and an adoptive father, a gestational and a genetic and an intended mother - never a contradiction. The claim stays canonical: delete the block and a reindex rebuilds every sourced edge from claims, while the unsourced beliefs live here as hypotheses always have. The linter records the reciprocal edge on the other person automatically (a `child` edge here implies a `parent` edge there, pointing at the same claim) and offers to add a missing mirror rather than asking you to write both ends by hand.

## 10. Identifiers `LOCKED`

The system does not expect a human to create an ID. A person references records by **filename and name**; the **ID is the machine identity**, assigned by tools, not by people. A hand-written record may carry no ID until a tool first touches it - a valid pre-machine state (§4); on contact a tool mints the ID, records the filename/name as an alias so existing name-links keep resolving, and maintains it thereafter.

The ID is the more durable identity. A filename is mutable and can collide; an ID is immutable and unique (format below), so machine references hold across renames, spelling changes, and duplicate names. Name-based references work while names stay unique - IDs are stronger, which is why tools assign one to every record.

```
{TYPE}-{10 random Crockford Base32 characters}   e.g.  P-3kq9v8x2m1, S-7n4hp0wztb
```

- The type prefix (`P`/`S`/`C`/`L`/`H`) is the **only** meaning an ID carries - safe because a record never changes type. Nothing else is ever encoded: no dates, names, or sequence. Anything correctable (in genealogy: everything) lives in metadata.
- **Alphabet:** Crockford Base32 - `0123456789abcdefghjkmnpqrstvwxyz` (lowercase; the letters `i l o u` are deliberately omitted to avoid confusion with `1 0` and accidental words). Stored lowercase; matched case-insensitively, so an ID can never collide with itself across a case-insensitive filesystem (macOS, Windows).
- 10 Base32 chars ≈ 32¹⁰ ≈ 1.1 × 10¹⁵ values (~50 bits) - collision probability at family scale is vanishingly small, and the linter checks for duplicate IDs anyway (E001), so the rare collision is caught, not trusted away.
- **IDs are immutable** - never changed, never reused, including for deleted records.
- **No registry.** IDs are random, so no counter or ID database exists. **The archive itself is the registry**: every record carries its ID, so the set of used IDs is always derivable by walking the tree. Minting tools generate a candidate and check existence (against the tree, or the rebuildable index as a cache). Two machines mint independently. Sequential IDs are rejected precisely because they would require the registry this design avoids.
- IDs are assigned **on need** (at processing), never in bulk to unprocessed files (§7).
- **Minting is the machine's job.** `fha id mint` - or any tool, on first contact with an id-less record - generates a candidate and checks it appears nowhere in the tree. A human may mint one by hand (ten characters from the alphabet above, checked absent), but nothing requires it.

## 11. Dates: EDTF everywhere `LOCKED`

All dates in archive records use **EDTF (ISO 8601-2)**:

| Need | EDTF |
|---|---|
| Known year | `1850` |
| Circa | `1850~` |
| Decade | `185X` |
| Year + month | `1850-05` |
| Month approximate | `1850-~05` or `1850-05~` (tilde before or after month - both valid EDTF Level 1) |
| Uncertain | `1850?` |
| Before | `[..1920]` |
| Interval | `1871-02/1871-03` |

The one exception is embedded photo metadata, which cannot hold partial dates; §20 defines the bidirectional mapping to the keyword-pattern system used there.

**Calendar quirks:** a claim records the date *as written* in `value` (double dates like "11 Feb 1731/32", regnal or feast dates) with the best EDTF interpretation in `date:`.
Julian/Gregorian judgment goes in `notes`.

---

# Part III - Physical Format

## 12. The on-disk tree `LOCKED`

```
family_archive/              ← the root (default name; rename freely - nothing parses it)
  SPEC.md  TOOLING.md        ← the archive carries its own spec
  README.md                  ← plain-language how-to (§21a)
  AGENTS.md  CLAUDE.md       ← agent operating instructions
  fha.yaml                   ← config + root mapping (§12.4)
  ── plain-text core (git-versioned) ──────────────────────────
  sources/{type}/            ← RECORDS: one .md per source (census/, newspapers/, photos/, …)
  people/
    NNN <Couple folders>/    ← direct line, Ahnentafel-numbered (§12.2)
    connections/             ← everyone else, anchor-numbered (§12.3)
    stubs/                   ← holding pen for people not yet placed
  places/places.yaml         ← single-file place registry (§15)
  notes/                     ← general research workspace (§16)
  ── assets (mappable elsewhere via fha.yaml; not git-pushed if local) ──
  photos/{year}/             ← ASSET TREE: all photos, by year (§12.4 - often external)
  documents/{type}/          ← ASSET TREE: scans, clippings, recordings, transcripts
  inbox/                     ← STAGING: new scans/downloads before filing (§12.1; mappable)
```

The **plain-text core** (records, people, places, notes, the docs) is small and designed to be **git-versioned** - that is the change log (§ governance). **Assets** (photos, documents, inbox) are large and binary; they may live inside the root (then git-ignored) or, more often, on a separate drive mapped via `fha.yaml` (§12.4).
The root's default name is `family_archive`; rename it freely - no tool parses the root's name.

### 12.1 Assets and records are fully separated

**No asset ever lives inside a record folder.** All original files live in the asset trees; source records reference them in place by path.
Subdividing asset trees (by type, then decade) is free - folders are projection.

**Staging and filing.** New material (scanner output, downloads) lands in `inbox/`. **Filing** - moving a file from `inbox/` into the right asset tree - is the one sanctioned *move* of a file, performed by human or agent at intake.
The "originals never move" rule applies from the moment a file is filed.

**Source stubs (the half-formed middle state).** Between "raw file in the inbox" and "fully processed Source" sits a deliberate intermediate: a **source stub** - an asset (or no asset at all) paired with rough, unprocessed notes capturing *why this matters and what it is*.
A stub has two equally first-class origins: **created by hand** - you drop a scan in the inbox and write a plain notes file beside it ("Grandma's photo, that's her brother on the left, probably 1925, from Aunt Mary's album"), or jot a note with no asset yet at all - or **pre-filled by capture** (the browser companion; TOOLING.md §13b, designed in full in TOOLING_INGESTION.md).
The format is identical; the only difference is who typed the notes.

A stub is a plain Markdown notes file:

- **Lone sidecar** for a single asset: `photo.jpg` + `photo.notes.md` beside it (paired by basename).
- **Bundle folder** the moment there's **more than one file** (or none): `inbox/hartley-interview-2024/` holding `interview.mp3`, `interview-transcript.md`, and `notes.md` - or `inbox/2026-06-12-ancestry-census/`, or `inbox/grandmas-album-scan/`. A multi-version item - a recording plus its transcript, a document plus a translation, a photo plus its back - is **always a bundle folder in the inbox**, never a naming convention, because a stub has no S-id yet and the folder is the only thing grouping the files. The single `notes.md` is the stub for the whole bundle.

The notes file is **freeform-first** - the body is whatever you want to say, and a *light, optional* YAML frontmatter holds any structured hints that happen to exist (a captured recipe's citation fields, a parsed person, a source-type guess, and - for a bundle - optional per-file role hints like `recording` / `transcript`).
By hand you can skip the frontmatter entirely and just write prose; capture fills more in.
It carries **no S-id** - a stub is *pre-source*, exactly as an untagged photo is pre-source; processing is what mints the ID and promotes the stub into a real source record (§14).
Either way, **processing reads the stub as its starting point** rather than working from a blank page - your notes and any person/vital hints become `suggested` claims and scaffolding for review, never accepted facts.

**At processing the bundle folder dissolves:** one S-id is minted for the source; each file is filed into its asset tree carrying that shared S-id via the `[-role]` filename grammar (documents root - §13) or its `SOURCE:` keyword (photos root); the files' roles populate the source record's `files:` inventory; and the stub's notes flow into the record's `## Notes`. **Grouping migrates from the folder to the shared ID** - the folder was pre-ID scaffolding, not durable structure, and it goes away.
(After processing, the files no longer live together in a folder; the shared S-id is what binds them - §14.)
An unworked stub is a legitimate resting state, like any inbox item: "captured or jotted, not yet processed."

**Processing** = creating a source record for a file - the operation `fha process`.
Identity marking depends on the root:
- **Documents root:** the original is renamed to append `_{S-id}` - the one sanctioned touch of a filed original: *filename only, never content, never location*. Prior name preserved as `original_filename` (provenance).
- **Photos root: files are NEVER renamed** - renames break the Lightroom catalog (links, edits, collections). Identity carriers for photos are the embedded `SOURCE: S-xxxx` keyword (written via exiftool during processing) and the source record's `files:` inventory - two carriers instead of three; keyword search (photo index, Lightroom itself) replaces filename search for photos.

Reorganizing or rescanning assets must never orphan a source or claim; the record, not the path, is the identity.

### 12.2 People: Ahnentafel couple folders

- One folder per **direct-line ancestral couple**, numbered with the even (male-partner) Ahnentafel number; the wife is implicitly 2n+1. Root: **#1 = the children, collectively** - valid because full siblings share one ancestor tree. (#2 their father, #4 his father, … T.E. Hartley = 040.)
- Folder names are free-form human convenience - spaces, `+`, bracketed child lists: `040 Thomas Hartley + Margaret Cole [Ethel + Frances + Calvin + Edward]`. **Folder names carry no machine meaning**; scripts never parse them (files inside are self-identifying). Bracket lists may drift until a tool refreshes them from relationship claims.
- A couple folder contains both partners' person files and the stub/person files of their **non-direct children** - that is where a human looks for an ancestor's siblings.
- Direct-line children get their own numbered folder, never a subfolder.
- **A direct ancestor's non-ancestral marriages** get suffix folders sorting beside the ancestral one: `040b Thomas Hartley + (second spouse) [children]`. Occupants beyond the ancestor are connections-tier people; half-siblings of the line live here.
- Ahnentafel's even/odd convention is a sorting convenience, not an assumption - use one partner's even number consistently; nothing in the model requires opposite-sex couples.
- The whole tree is a projection, regenerable from relationship claims; Ahnentafel numbers are derivable, never stored in records. The derivation root - the person at position #1 - is declared in `fha.yaml` as `root_person` (§12.4); any direct-line descendant works as the anchor, since all full siblings share one ancestor tree. With that declaration, tools compute every ancestral couple's Ahnentafel number by following only the **genetic** parent edges among accepted `relationship` claims (`subtype: biological` - the default - `surrogate-genetic`, `donor-sperm`, `donor-egg`); social and legal parent edges (`adoptive`, `step`, `foster`, `guardian`, `surrogate-gestational`, `social`) are shown in the folder's bracket list and the relationship views, clearly marked, but are not numbered into the pedigree. Tools also verify and correct folder placement (see `fha views brackets`, `TOOLING.md §7`).
- A person who occupies **multiple Ahnentafel positions** - cousin marriage, endogamy, pedigree collapse - **homes to the lowest position number**; their files live in that one couple folder, and every higher position carries a cross-link entry in its folder's bracket list (`Thomas Hartley (also #128 - see 040)`). The lowest-number rule is deterministic, so placement and bracket refreshes are stable across runs. The folder is a projection, so the cross-link lives in the regenerated bracket text, not in an extra file.

### 12.3 Connections (everyone beyond)

Ancestor siblings' lines, in-laws, and non-family - friends, associates, neighbors (the genealogical "FAN club"; researching the people *around* a family is how brick walls fall) - live flat in `people/connections/`, as ordinary **§13 person files**:

```
cole__susan_P-s5a8g87at3.md          Margaret's sister - anchors through couple 040
layng__charley_P-ajnng40q36.md       Thomas's lodge brother - anchors through couple 040
```

The **anchor** is the nearest direct-line couple - every non-direct person anchors to the *family member they connect through*.
A friend of Thomas Hartley anchors through Thomas's couple; Margaret's sister's husband anchors through Margaret's.
The anchor is **derived, never encoded in the filename**: it follows from the person's relationship claims, and tooling answers "everyone around couple 040" as a query (`fha find --related`), not a filename prefix.
A file may carry the anchor as a plain comment for the browsing human, the way the example archive's connections do.
One filename grammar everywhere means connections sort by surname beside every other person file, survive the same renames, and need no second dialect for tools or search to learn.
Flat by design; the claims are where the anchor lives.

*Decision log:* an earlier revision of this section prescribed a distinct `{anchor} {Surname}, {Given}` filename style (browse-clustering by anchor prefix). Retired 2026-07 - it traded a second, tool-unknown naming dialect for a sort order the index answers better, and no tooling or fixture ever adopted it.

### 12.4 Asset roots (the records/assets split)

The plain-text core and the asset libraries are **physically separable by design**.
The photo library especially predates the archive, is managed by an external photo library tool (such as Lightroom), and warrants its own backup/sync policy; documents and the intake staging area may likewise live elsewhere.
Roots are configured in `fha.yaml`, never hard-coded:

```yaml
# fha.yaml - plain, hand-editable archive configuration
root_person: P-xxxxxxxxxx    # Ahnentafel anchor: this person is #1 (father #2, mother #3, …).
                              # Any direct-line descendant works - full siblings share one tree.
                              # Enables folder-number verification and person placement via
                              # `fha views brackets`. Omit to disable Ahnentafel tooling.
roots:
  photos: C:/Photos          # absolute path (external library), or "photos" to keep it internal
  documents: documents       # relative → under the archive root
  inbox: C:/Photos/_inbox    # staging may sit inside the photo library's own workflow
backup:                      # optional: where `fha backup` writes its dated zips (absolute, or
  path: D:/ArchiveBackups    # relative to the archive root - same tolerance as roots: values).
                              # Default: a "{root-folder-name}-backups" folder beside the archive.
                              # Must resolve outside the archive and its asset roots (TOOLING §13e).
```

Every record path keeps the alias form (`photos/1880/…`); tools resolve the first segment through the mapping (absolute → used as-is, relative → joined to the archive root, missing → an internal folder of that name).
Moving a library is a one-line edit and **no record changes**.
The spec's internal structure (`photos/{year}`, `documents/{type}`) describes the tree *under each root*, wherever it lives.

The design fact this establishes: **the archive is a records core plus mapped asset libraries.** The git-versioned core travels as plain text; the assets are referenced wherever they live.
Exports (packets, site) copy resolved files so *outputs* stay self-contained, and the backup policy must cover both the core and the mapped roots.
A human learns where assets live by reading `fha.yaml`.

**Working copies (the `WORKING_COPY` marker).** Because the records core and the asset libraries are separable, the core may be synced to a second machine - typically a git working copy carrying `sources/`, `people/`, `places.yaml`, `notes/`, and `fha.yaml` but **not** the binary assets (they are git-ignored or live on a drive that does not travel). Such a copy is flagged by the presence of a file named **`WORKING_COPY`** at the archive root, declaring the asset roots **populated elsewhere, not missing**.

The marker is a file, **not** a key in `fha.yaml`, by design. Working-copy status is a fact about *this copy on this machine*, not about the archive - so it must never travel: the marker is **git-ignored** (it can never be committed and synced back to flip the main archive into working-copy mode) and **machine-local**. It is a **human-managed control** (the human opts this copy in, opts out by removing it), so it is a **visible** file at the root - found by opening the folder in any file browser - carrying a plain-language explanation inside; only its *existence* matters to the tools, not its content. The human opts in or out most simply with **`fha working-copy on`** / **`fha working-copy off`**, which create or remove the marker (and keep it git-ignored) so nobody has to remember the filename or contents. Turning the mode *on* only ever withholds, so it needs no confirmation; turning it *off* re-enables the asset-aware behaviour, so it **must confirm first** - if the originals are not in fact on this machine, switching off would make the tools treat them as missing and could let a photo re-index prune the catalogue. The scaffolding commands (`fha install`/`fha update-tools`) never touch the marker - it is a statement about this machine's copy, not about the tools.

In this mode tools *must* treat absent assets as present-but-unavailable rather than lost:
- asset-on-disk checks are suppressed - a missing original is not flagged as an error;
- asset caches are never pruned for files that are simply not on this machine (a working copy must not be able to empty the photo catalogue);
- operations that need the originals - processing a new asset, embedding photo keywords, building an export packet - refuse with a plain pointer to the main archive rather than acting on absence.

The plain-text surface stays fully usable: rebuilding the query index from records, search, views, reports, narrative drafting against existing accepted claims, and adding new material to `inbox/` to carry back all work normally. Every command announces the mode, so behaviour stays obvious and nothing is silently rebuilt over absence. The round trip is safe by construction: `.cache/` and the `WORKING_COPY` marker are never synced, so a working copy's caches and mode flag cannot reach the main archive, and the return path is the ordinary `inbox/` workflow. Because a working copy never needs to edit `fha.yaml` (it ignores the asset roots), `fha.yaml` may itself keep syncing without conflict. (Tooling design: `TOOLING.md` §13d; build status: `BUILD.md` M10.)

## 13. Filenames `LOCKED`

Every record file is **self-identifying** - its ID is in its filename, so files survive separation from their folders, and searching an ID finds everything carrying it.

The ID-in-filename is the **formalized** form, maintained by tools. A record a person creates by hand may be named however they like - `john-smith.md`, `grandpas-letter.md` - with no ID suffix, a valid pre-machine state (§10): the human identifies it by name. On first tool contact the file is renamed to the grammar below, with the original name preserved both as the mutable `slug` and as an alias, so existing `[[name]]` links keep resolving. From then on the filename carries the ID and the self-identifying invariant holds.

- **Source records:** `{slug}_{S-id}.md` - slug lowercase hyphenated, mutable; ID immutable.
- **Source files (documents root):** `{slug}[-{copy}][-{role}]_{S-id}.{ext}` - the *source's* ID, shared by all versions. **Photos-root files are never renamed *by us*** (§12.1) - but another system (eg Lightroom, a cleanup pass) may rename or move them, so the filename is **not** a reliable identifier for photos. The durable identity is the embedded `SOURCE:` keyword; the record inventory stores the last-known path as a hint, reconciled by `fha photoindex reconcile` (§ tooling) when files move. Roles: `front`, `back`, `page-N`, `clipping`, `recording`, `transcript`, `translation`… Copies: `b`, `c`, `negative`… Derivative views: `-crop` stacks on any other suffix (`front-crop`, `back-crop`, `negative-crop`) marking supplementary detail images, never independent sources. Note: `-negative` is mutually exclusive with `-front`, `-back`, and `-pageN` - it is the physical film or glass-plate source material for the root image. Suffix parsing priority order: `-crop` stripped first, then part-kind (`-negative` before `-back`/`-front`/`-pageN`), then trailing variant letter; remaining stem = base id (see `TOOLING.md` §6 for the full algorithm). Rarely more than ~3 versions; skimmable by design. (The photo pipeline propagates text between versions - "text from alternate version" tags - so any copy reveals the others.)
- **Person files:** `{primary_sort_name}__{given_names}[_{kind}]_{P-id}.md` - **double underscore** after the sort name (families sort together), underscores within given names. The **primary sort name** is the birth surname when there is one (keeping women findable under the name in their early records; matching WikiTree practice); where a person has no surname - a mononym, an enslaved ancestor recorded by given name, a patronymic, a foundling - the slot is empty and the filename **leads with the double underscore** (`__caesar_P-….md`, `__jon_thorsson_P-….md`), a distinct no-surname sort group. Two-surname systems put the paternal (or chosen sort) surname in the slot and the full `Apellido1 Apellido2` form in `name`; surname-first cultures sort by the surname slot and display in cultural order via `name`. The full cultural name always lives in `name` / `name_variants` (§9); the filename is only a sort handle. `kind` ∈ `research` | `timeline` | `sources-index` | `draft-queue`.

The deliberate style difference - person files underscored, source files hyphenated - instantly distinguishes record kinds in search results.

## 14. The source record `LOCKED`

**One source = one file**: `sources/{type}/{slug}_{S-id}.md`.
Frontmatter carries metadata and the file inventory; `## Claims` carries all of this source's claims; `## Stories` (interviews especially) carries mined narrative chunks; `## Notes` carries prose. **Never one file per claim** - a rich interview yielding 50-100 claims in one block is expected; it is queried through the index and reviewed in filtered passes, never read linearly.

```markdown
---
id: S-b237895f31
aliases:                       # link-resolution handles: the ID (so [[S-…]] resolves) + any human
  - S-b237895f31               #   stem you used before processing (e.g. the inbox filename)
title: Campaign card for T. E. Hartley, Clerk of the District Court, 1880
source_type: photo            # census | vital-record | photo | interview | letter | newspaper | …
source_date: 1880-11~         # EDTF; the date OF the source itself
source_class: original        # optional: original | derivative | authored (§8.5; proofs: authored)
original_language: de         # optional; language of the evidence itself, only when not English (BCP-47/ISO-639)
repository: family collection # where the evidence came from / lives
citation: >
  Campaign card for T. E. Hartley, candidate for Clerk of the District
  Court, Fairview, Kansas, circa November 1880.
external_links:
  - https://www.wikitree.com/photo.php/f/f6/Hartley-6084-1.jpg
people:                        # who this source involves/depicts - the human cross-link surface,
  - "[[P-…|Thomas Hartley]]"    #   link-valued so it draws an Obsidian graph edge; hand-editable by
  - "[[P-…|Margaret Cole]]"     #   name ([[Margaret Cole]]), settled to [[P-…|Name]] by the tools
places:                        # optional; where the source is set - same link form; may be
  - "[[L-…|Fairview]]"          #   hand-added or synced from the claims' place: fields
restricted: true              # only when applicable (§19); DNA always
provenance: "Robert Hartley's collection, acquired 2025"   # optional: where the original came from
rights:                       # optional publication metadata (tooling flattens
                              #   rights.publication_ok → index sources.publication_ok)
  holder: family collection   #   who owns/holds copyright
  publication_ok: true        #   exporters honor this in addition to restricted/living
physical_location:            # optional: where the PHYSICAL original lives (changes over time)
  holder: Sam Rivera
  as_of: 2025-05
files:                        # inventory: roles + provenance
  - file: photos/1880/Hartley-6084-1.jpg          # PHOTOS ROOT: never renamed
    role: front                                   #   identity = SOURCE: keyword + this inventory
    language: en              # optional; the language of THIS file
    digitized: "Scanned by Sam Rivera, 2025-05" # optional per-file digitization provenance
  - file: photos/1880/Hartley-6084-1-back.jpg
    role: back
  - file: documents/interviews/…-transcript_S-….md  # DOCUMENTS ROOT: renamed at processing
    role: transcript
    language: en              # the transcription's language (verbatim, as written)
    derived: true             # hand-corrected derivative; an original in its own right
  - file: documents/interviews/…-translation_S-….md
    role: translation         # an English rendering of a non-English original
    language: en
    derived: true             # a derivative, an original in its own right
created: 2026-06-10
---

## Claims
(fenced YAML block - §8.4 schema)

## AI Passes
(optional - present only once a pass has run; structured yaml block:
`- {date, model, harness, task, outputs: […], human_reviewed: bool}`)

## Stories
(narrative chunks mined from the source, each with topics + [[P-…]] refs - feedstock
for profile Stories sections)

## Notes
(free prose: context, verification TODOs)
```

The `files:` inventory documents roles and provenance for humans.
Each file may carry an optional `status:` - omitted means present; **`missing-fixture`** marks a deliberately absent placeholder, allowed **only** under `example-archive/` and `tests/fixtures/` (warning-level there); a `missing` file in a real archive is an error (E011).
For documents-root files the link has three carriers (filename, inventory, embedded keyword where supported); for photos-root files, two (inventory + keyword - filenames are sacred).
Tooling verifies the carriers agree.
A source may carry an optional `original_language:` (the language of the evidence), and each `files:` entry an optional `language:` (the language of that file); a translated rendering is filed as a `role: translation` derivative beside the original, the same way a corrected transcript is. Language codes follow BCP-47 / ISO-639 (`de`, `en`, `la`) and are informational, never hard-validated.

The frontmatter `people:` / `places:` are the human cross-link surface: link-valued so an Obsidian-only editor can cross-link by name and draw the graph edges, hand-editable, normalized to the stable `[[P-…|Name]]` / `[[L-…|Name]]` form by the tools. The claims block (§8) instead requires **bare** IDs - `persons: [P-…]`, `place: L-…` - because it is structured data in a fenced block; links live in the frontmatter, not the claims (§8.7). `places:` is optional and may be hand-added or linter-synced from the claims' `place:` fields.

**Source type vocabulary** (controlled, expandable by logged decision - same pattern as claim types): `census` · `vital-record` · `newspaper` · `photo` · `interview` · `letter` · `military-record` · `land-record` · `probate` · `directory` · `dna` · `book` · `website` · `artifact` · `proof-argument` · `other` (+ free-text `subtype` when nothing fits).

**Proof-argument sources.** A conclusion resting on indirect or negative evidence is written as an **authored source**: `sources/proofs/{slug}_{S-id}.md`, `source_type: proof-argument`, `source_class: authored`.
The body *is* the argument, citing the contributing claims and sources with normal `[[C-…]]`/`[[S-…]]` links (the linter verifies them); the concluded claim(s) live in the proof's own `## Claims` block - the proof is their source - typically with `evidence: indirect`.
Biographies then cite the proof like any source.

**DNA sources.** `source_type: dna`, **always** `restricted: true`.
Fields: `tested_person:` (P-id), `provider:` (AncestryDNA, FamilyTreeDNA, …), `test_type:` (`autosomal` | `y-dna` | `mtdna`), optional kit notes; raw files live in `documents/dna/`.
Export rule: DNA is excluded from every packet, site, and export by default, and `--include-restricted` does **not** include it - DNA requires its own explicit `--include-dna`.

**Draft-prose markers.** `(TODO: import source)` is the recognized marker for useful factual prose awaiting its source; exporters treat marked sentences as context and exclude or flag them in public-facing output.

## 15. Places `LOCKED`

A single `places/places.yaml` holds all places - they are tiny and number in the hundreds.
Move to per-place files only if places start accumulating prose.

**One record per physical location.** Jurisdictions and names change; the dirt does not. `coords` anchor a place's identity - one L-id per physical place, forever - and a dated `history:` carries what it was called and governed by over time.
Claims always reference the single L-id (recording the source's wording in the claim's own `place_text`); the claim's date lets tools render the period-correct jurisdiction.

```yaml
- id: L-baba9801fa
  name: Fairview                     # modern/common name
  coords: [39.5631, -95.1216]        # lat, lon - the identity anchor; tooling backfills
  hierarchy: Fairview, Breton County, Kansas, USA   # modern hierarchy
  alt_names: [Fairview City]
  history:                           # optional, dated jurisdiction/name changes
    - {period: "1855/1861", hierarchy: "Fairview, Breton Co., Kansas Territory, USA"}
  notes: optional free text - brief place history; LOOSE citations (Wikipedia) are
         acceptable here, places are reference data, not genealogical conclusions
```

**Containment: physical links, political strings.** A micro-place (house, address, cemetery, church, building) may carry one optional `within: L-xxxx` link to the settlement physically containing it - stable because the dirt doesn't move.
Settlement→county→state is **never** linked: that is *jurisdiction*, which drifts, and it lives only in the dated `history:` strings.
Tooling recurses `within` so "claims in Fairview" includes its houses and cemeteries; coords serve proximity even without links.

```yaml
- id: L-9e2210ab44
  name: Hartley family home, 214 N 5th St
  within: L-baba9801fa               # physically inside Fairview; one hop
  coords: [39.5644, -95.1209]
```

Most addresses never become places at all - they live as `place_text` on claims.
A micro-place earns an L-record by the processing path like everything else: when it recurs and matters (the family home across decades of claims; the cemetery holding six relatives). **Recurrence is detected, not remembered:** the report surfaces unlinked `place_text` values that cluster past a threshold (and photo-GPS clusters near no known place) as place candidates; confirmed elevation mints the L-id and guides per-claim backfill of `place:` - `place_text` itself is never altered.

## 16. The curated person files `LOCKED`

Per the filename grammars of §13, a curated person has, in their couple folder:

| File (`{surname}__{given}…`) | Nature |
|---|---|
| `…_P-xxxx.md` | **Curated profile** - the "hand this to grandma" document. |
| `…_research_P-xxxx.md` | **Working file** - Research Notes, Open Questions, Hypotheses. |
| `…_timeline_P-xxxx.md` | **Generated** from claims, EDTF-sorted. Never hand-edited. |
| `…_sources-index_P-xxxx.md` | **Generated** list of sources mentioning this person. |
| `…_draft-queue_P-xxxx.md` | **Generated** uncited-claim backlog; consumed by write-biography. Never hand-edited. |

**Profile structure** - frontmatter (§9), then:

```markdown
# Thomas Edward Hartley (1840-1941)

**Born:** 3 Mar 1840 - Easton, Carrow Co., New York [[S-xxxx]]
**Died:** 19 Jan 1941 - Riverton, California [[S-xxxx]]
**Married:** [[P-cd795c61e0|Margaret A. Cole]] - Feb/Mar 1871, Fairview, Kansas [[S-ea61339378]]
**Parents:** [[P-075114a0f8|Caleb Comstock Hartley]] · [[P-d00c678c1a|Chastina Augusta Reed]]
**Children:** [[P-c4b26bb4bc|Ethel]] · [[P-83e768cacb|Frances]] · [[P-fa7541e871|Calvin]] · [[P-4b9d197ee4|Edward]]

## Biography
(chaptered by era/place)

## Stories
(the incidental long tail, each linking its source)

## Friends & Family
(non-relative connections and context - the FAN club)
```

**Citation density:** in the summary block, **one citation per line** is sufficient - it is a curated overview.
In the body sections, factual statements should carry **all relevant citations** - every source that supports the fact - since the body is where corroboration is shown.
(Tooling may suggest missing citations by matching prose against claims; see `TOOLING.md`.)

The summary block is hand-curated denormalization of claims: every line cites; cross-links use `[[P-xxxx]]` links (zero-hop - person filenames carry IDs, so searching the link's ID finds the person). **Tooling cross-checks the block against accepted claims and flags drift.**

The research file body: `## Research Notes`, `## Open Questions`, `## Hypotheses`, `## Research Log`.

**The research log** records searches performed - including empty ones - so no collection is fruitlessly re-searched, and so "reasonably exhaustive" is demonstrable.
Entries are **dated** (collections grow; a nil from 2024 is worth re-running in 2027) and **primarily tool-fed**: the capture flow, mining passes, and executed `research-next` plans log themselves; manual entries are welcome but never a required ritual.
Format:
```yaml
- date: 2026-06-12
  question: "[[H-…]] / [Q ref] / free text objective"
  repository: Ancestry
  collection: "Kansas State Census, 1875"
  terms: "Hartley, Breton Co."
  result: nil          # nil | found [[S-xxxx]] | partial (note)
```
Multi-person/locality searches log to `notes/research-log.md` with the same format. `research-next` and the report **check the log first** - "already searched (date)" is surfaced before any lead is proposed. **Hypotheses are where unsourced placeholder beliefs live** - a guess is never a claim (claims require sources by definition).
Structure per hypothesis: `id:` (`H-` per §10), `hypothesis:` (the belief), `basis:` (reasoning/context), `verify:` (what evidence would settle it), `origin:` (`human` | `agent`), `status:` (`open` · `verified → C-xxxx` · `abandoned`).
On verification, the found source yields a real claim and the hypothesis records the pointer - the guess's life preserved.
Sources sections are never hand-maintained; they are generated from cited claims.

## 17. Notes (general research) `LOCKED`

`notes/` is the **general** workspace - research strategy, todos, surname studies, multi-person narratives.
Person-specific research lives in that person's research file, never here.

- `notes/research/` - working notes spanning people or topics.
- `notes/narratives/` - formal multi-person write-ups; every factual claim cites a source or is explicitly marked context/speculation; exportable.
- `notes/questions.md` - single file of general open questions. Format per question: an `## Q:` heading, then `origin:` (`human` | `tool` | `agent` - machine questions are marked at birth), `status:` (`open` · `answered [[S-xxxx]]` · `closed (not pursuing)`), `refs:` (related `[[P-…]]`/`[[C-…]]` links), and a `context:` list of dated, origin-attributed findings appended over time. Closing without an answer is a legitimate, recordable research outcome. Tooling may propose answers/closures and append context; status changes require human confirmation.

Notes connect to the core elements through ID links in their text, and linked frontmatter `persons:` / `sources:` / `places:` lists. **A script reading only the IDs those links resolve to must be able to reconstruct every connection** - the bare ID is the load-bearing part; the double brackets are durable plain-text delimiters, and the editor's *resolution and graphing* of them is front-end sugar, never load-bearing.

## 18. Citations and linking `LOCKED`

Double-bracket ID links, greppable and tool-verifiable:

- **`[[S-xxxx]]` is the standard citation** on factual statements in any narrative body. It matches natural research practice - footnotes cite evidence - and is zero-hop: searching the ID surfaces the source record *and* its files together (the bare `S-xxxx` is a substring of the link). The double brackets are durable delimiters - plain text in any editor, and a live link in a wiki-style editor where the source record carries the ID as an alias (§14).
- The link always carries the **immutable ID**, never the slug or filename, so a citation survives a record rename. An optional display follows a pipe: `[[P-xxxx|Margaret Cole]]` reads as the name and links by ID.
- `[[C-xxxx]]` is permitted when claim-level precision matters (one disputed assertion and its status) - the exception; it resolves to the claim's source record (§8.7).
- `[[P-xxxx]]` cross-links people; `[[L-xxxx]]` a place. Both zero-hop via the record's ID/alias.
- **Forgiving by default.** A hand-typed single-bracket `[S-xxxx]`, a bare `S-xxxx`, a human alias (`[[grandmas-album]]`), or an Obsidian fragment (`[[S-xxxx#Notes]]`) all still resolve; `fha normalize-links` tidies them to the standard form on request, never rejecting them.
- **Uncited prose is by definition story/context, never fact.** The fact-safety rule, expressed as syntax.
- Exporters swap links for refs; the WikiTree exporter renders `[[S-…]]` as `<ref>` blocks from the source's `citation` field, and redacts the display name of any `living`/`unknown` person (§19).

**Resolution and the alias surface.** A `[[ ]]` link's target may be an ID or a name/stem; either resolves to a canonical ID through the record's `aliases:`. Three related fields stay distinct in purpose so they never conflate: **`aliases:`** are link-resolution handles - the canonical ID, any human stems, and (for persons *and* places) the `name` plus its `name_variants` / `alt_names` mirrored in, so a name-link resolves; **`name_variants:` / `alt_names:`** are the display/genealogical name variants a human curates - the *source* the tool mirrors into `aliases:`; **`face_tags:`** are exact photo people-tag strings (§20). The human edits `name` / `name_variants` / `alt_names`; the tool keeps `aliases:` in sync. Because mirrored names can collide (two `Springfield`s, two `John Smith`s), the alias-clash check (`TOOLING.md` §3) governs all of them, and a clashing name resolves only when pinned to its ID (§7). A name marked `restricted` (a `name_variants` entry carrying `restricted: true`) still resolves through the alias surface internally, so links and search keep working, but its display is redacted in any external export - the same internal-resolves / external-redacts rule that governs a `living` person's name (§19).

## 19. Privacy `LOCKED`

Two privacy flags; no tier system. Flags appear only where they apply.

- `living: true | false | unknown` (Person) - drives redaction in any external export, packet, or publication. **`unknown` is treated as living** for all external-facing output; stubs default to `unknown` (uncertainty is safe by default).
- `restricted` - a marker placed on whatever should stay out of public output: a **source**, a single **claim**, a whole **person**, or a **name** (a `name_variants` entry written as `{value:, restricted: true}` - how a private prior name is recorded). Plain `restricted: true` is excluded from public output and from packets unless `--include-restricted` is passed. It may carry a free-text **type** instead (`restricted: dna`, `restricted: by-request`, …) when types must be told apart; two are **no-override** - `dna` (always restricted; requires its own `--include-dna`) and `by-request` (a person who asked to be left out, honored by every export path with no opt-in). The marker keeps material in the archive and out of what leaves it; nothing is deleted, and a restricted name still resolves internally (§18).

## 20. Embedded metadata `LOCKED`

The AI photo-categorization pipeline (separately documented) is a curation-layer adapter writing IPTC/XMP: keywords, verbatim transcriptions, AI captions, date-confidence tags.
Embedded metadata is part of the durable layer - it travels with the bytes.
Integration rules:

1. **Date mapping is bidirectional.** Photo metadata cannot hold partial dates, so confidence-pattern keywords map to EDTF. Pattern grammar per component (Y/M/D): `!` confident, `~` best guess, `?`/omitted unknown.

   | Keyword pattern | EDTF |
   |---|---|
   | `Y!M!D!` (1942-11-25) | `1942-11-25` |
   | `Y!M!` (1960-05) | `1960-05` |
   | `Y!M~` | `1960-~05` |
   | `Y!` | `1960` ( This is the same as Y!M?D? )|
   | `Y~` | `1960~` (circa) or `19XX` (decade) |

2. **`import_date` never becomes truth.** The forced full `YYYY-MM-DD` written for EXIF compatibility is a technical workaround; only the EDTF value flows into archive records.
3. **On processing, the source ID is embedded as a keyword:** `SOURCE: S-xxxxxxxxxx`. Third redundant carrier of the source↔file link.
4. **People in photos: bare `P-xxxxxxxxxx` ID keywords + the `face_tags:` map.** Each person record's `face_tags:` (plus `name`/`name_variants`) maps the library's existing face/people-tag strings to the P-id - the resolution layer, one durable line per person, no *name* double-tagging. On top of that, tagging tooling writes a **bare `P-id` keyword** onto the photo for each identified person (e.g. keyword `P-de957bcda1`) - an in-file, unambiguous marker that survives any catalog and settles same-name collisions outright. Always previewed; `fha photoindex tag-person` applies them across a face-tag match or to specific photos.
5. **AI output stays marked as AI** (analysis keywords, marker blocks); human captions are preserved.
6. AI-derived assertions raised into claims enter at `status: suggested`.

## 21. Publication and export `LOCKED`

Generated output that leaves the archive falls into two categories with different binding privacy rules: **public output**, meant for redistribution beyond the family, and **private export**, a family-facing copy that stays within the family but is gathered outside the archive's own access controls.

**Public output - living-person redaction (mandatory):**
- Any person whose `living` flag is `true` or `unknown` is redacted: their name is replaced with "Living [Surname]" or "Living Person", and all claims, photos, and source citations naming them are withheld.
- `unknown` is treated as living. Stubs default to `unknown`.
- Direct-line couple folders whose occupants are all redacted are collapsed to a stub entry; their folder number is retained so the pedigree chain remains intact.

**Public output - restricted material (mandatory):**
- Anything carrying `restricted` - a source, a claim, a person, or a name - is never included in any public output. A restricted source's claim contributions (dates, vitals) may appear only if an unrestricted co-source also establishes the same fact independently; a restricted claim, person, or name is withheld (a restricted name is rendered as the person's unrestricted display name).
- DNA evidence always carries `restricted: true`. No DNA-derived conclusions appear in public output without an additional, independent non-DNA source establishing the same fact.

**Scope (what "public output" covers):**
- `fha site` - the static HTML snapshot.
- Any future public-publication export path (GEDCOM, WikiTree, etc.) unless that path has an explicit `--include-living` / `--include-restricted` opt-in documented in its TOOLING entry.

**Private export (`fha packet`) - its own rules, per TOOLING §8:**
- `fha packet` is a family/private export, not public output, and is not subject to the redaction rules above. A packet may include `living: false` people's full prose and cite other people who are still living, with a README caution rather than redaction.
- The packet *subject* is held to a stricter rule than the cited-other-people case: `living: true` and `living: unknown` subjects are refused before any output is written, with no opt-in.
- Anything `restricted` is excluded by default (restricted sources named by ID only in the README); `--include-restricted` overrides, except the **no-override** types: `restricted: dna` stays excluded until `--include-dna` is also passed, and `restricted: by-request` is never included by any flag. A `restricted: by-request` *person* vanishes from packet content but keeps their folder number so the pedigree chain stays intact (as with the all-redacted couple-folder collapse above).

**Site generation freshness contract:**
- `fha site` reads structured data (claims, vitals, relationships, sources) from `.cache/index.sqlite` - it is as live as the last `fha index` run.
- Biography prose and Stories sections are read from the curated person `.md` file directly.
- The generated `.md` views (timeline, sources-index, draft-queue) are research artifacts for the agent; `fha site` does not read them.
- The site snapshot is frozen at generation time. Regenerating is idempotent; an old snapshot remains a valid frozen view as the archive moves on.

**View maintenance (`fha views clean` / `fha views refresh`):**
- Generated `.md` views carry the `<!-- GENERATED … -->` header. This header is the sole signal for deletion by `fha views clean` - files without it are never touched, even if they match a view filename pattern.
- Views rendered as standalone HTML (`--format html`) are single-file documents under `generated/views/`, carrying the same header as their **first line, before the doctype**; `fha views clean` sweeps them by the same marker-per-file signal (a hand-written file in that folder is never touched). They render the same content as their `.md` twins and remain private, unredacted research artifacts - not public output (TOOLING §7 D11).
- `fha views refresh` is the counterpart: regenerate all content views in one pass after `fha index`. It is the recommended post-index step for bulk regeneration (a fresh copy, a reset after `views clean`); a review session instead refreshes just the touched persons' views (TOOLING §17), so untouched views keep their generation dates.
- Deleting generated views reduces archive size for sharing but does not affect archive correctness; all views are rebuildable from the index.

---

# Part IV - Tooling requirements

This part states **what** every tool must do - the binding requirements. **How** each is built (schemas, algorithms, CLI design, libraries, error handling) is specified in **`TOOLING.md`**, which is part of this spec for governance purposes: tooling design changes are logged decisions.

Invariants for all tools: generated artifacts are disposable caches; tools report by default and modify only on explicit command; every tool is regenerable from the two documents; generated `.md` views written into the tree carry a `GENERATED - do not edit` header.

| Tool | Requirement (the *what*) |
|---|---|
| **Index builder** | Rebuild, from scratch on demand, a queryable SQLite index of all persons, sources, claims, places, files, citations, plus full-text search over transcripts and notes. Never authoritative, never appended. |
| **Linter** | Walk the archive; verify every rule in this spec (IDs, filenames, schemas, references, statuses, dates, inventory/keyword agreement, summary-block drift); report vitals gaps and suggested-claim backlogs; spawn questions for contradictions on request; reconcile each sourced person-doc `relationships:` entry against its accepted `relationship` claim (pair, role, subtype) and check reciprocity, offering to add a missing mirror edge. |
| **ID mint** | Generate spec-conformant IDs with existence checking; batch capable. |
| **Stub minter** | Create person stubs in bulk from claims that reference unresolved people. |
| **Processing assistant** | Given a file or folder: mint S-id, mark identity (documents: rename; photos: keyword only - never rename under Lightroom), scaffold the source record; folder mode triages candidates first. |
| **View generators** | Per-person timelines; per-person and per-couple-folder sources-indexes; refreshed folder bracket lists; **relationship views** - ancestor / descendant / FAN trees for any person - all derived from accepted claims, never stored. |
| **Relationship calculator** | For any two persons, derive both the **blood relationship** (the cousin/removal/great-grandparent name, via lowest common ancestor over genetic edges) and the **shortest social path** (a readable chain of role-named hops over all relationship edges) - at query time, from accepted `relationship` claims, never stored. Returns a structured result; the text form renders the sentence. |
| **GEDCOM exporter** | Derive a standard GEDCOM (relationships + vitals) for a person or the whole tree, at export time, from relationship/vital claims. For exchange with genealogy apps only - never the corpus, never re-imported as truth. |
| **GEDCOM importer** | File a *foreign* GEDCOM (an Ancestry download, another program's export) as ONE source record whose every assertion enters as a `suggested` claim with a line anchor, plus a person stub per individual (provisional vitals, safe `living:` defaults). Plan-then-apply with a one-shot re-run guard and full rollback; the archive's own export is refused (one-way bridge, no round-trip); duplicates are reported, never auto-merged. Writes only forms §8/§9/§14 already define - no schema additions. |
| **Person packet** | Gather everything about a person - profile, claims, sources, files, *and all photos of them* (bare `P-id` keywords + `face_tags:` resolution) - into a zip of copies, clearly labeled as a derived export, honoring `living`/`restricted`. |
| **Photo metadata index** | Scrape embedded metadata of the entire photo library into a fast, disposable search catalog (so finding photos never requires opening Lightroom); incremental rescan; powers the packet's photo gathering. **Variation-aware:** versions of one physical photo (fronts/backs/copies/negatives) are grouped as one logical photo, returned once; per-variant date tags are resolved to one best-confidence group date, and cross-variant date disagreements are surfaced as a report. |
| **Place geocoder** | Backfill `coords` and `alt_names` in `places.yaml` from an offline gazetteer, with human confirmation. |
| **Interview converter** | Migrate the prior transcript-mining output (T###/R###/Q### records) into conformant sources, `suggested` claims with anchors, stories, and questions. |
| **Static site generator** | Render the archive as a **self-contained static HTML snapshot** - its own web-optimized asset derivatives, only publication-eligible material (living/unknown redacted, restricted/DNA excluded), interactive trees via a vendored rendering library fed a neutral JSON contract - a navigable family tree (a descendant explorer from the line's apex on the home page; each person's page carries a server-rendered immediate-family chart - children, subject + spouse(s), parents, grandparents) built from accepted `relationship` claims, its social and adoptive edges drawn distinctly from the genetic line (§12.2). No server, no accounts, no dependency on the archive once generated; works from a USB stick; embeds in packets. Visual design is built live, not specified here; the JSON data contract is. |
| **WikiTree exporter** | Render a curated profile to WikiTree markup; `[[S-…]]` links → `<ref>` citations. |
| **Doctor** | One health command: root + `fha.yaml` + mapped roots reachable; exiftool/Python present; index & photoindex freshness; lint summary; inbox aging; restricted/living/unknown counts; agent-instruction drift (stale command or skill names in AGENTS/skills). |
| **Formatter** | Conservative normalization as a lint feature (`--format-check/--format-write`): key order, ID casing, blank lines, final newline - never rewrites prose. |
| **Web capture** | Turn an open record page into an inbox source stub: scrape citation info from the HTML, accept a dropped/fetched asset (or store the page itself as an HTML asset when the page *is* the record), write a research-log entry, and stage for the processing pipeline. Site recipes for common sources; generic scrape as default. Sits on the open page - no credentialed scraping. A sibling mode, `--path`, registers a file that must never move (no page, no asset copy - a pointer stub only). *Engine `fha capture` (paste fallback + the `--ingest` sweep + `--path`), the MV3 browser extension, and the native-messaging host are built; Claude-in-Chrome rides the same staged-bundle contract; the bookmarklet was evaluated and not pursued. Full design: TOOLING_INGESTION.md.* |
| **Deterministic write-backs** | Human-directed, surgical edits to a record's own fields - the review/minting gesture behind every "confirm" click: move a claim's `status:`/correct its fields, or mint a brand-new claim onto a source (`fha claim`); set a person's `living:` flag, mint a new person stub, jot an unsourced relationship belief, write a provisional vitals estimate, or edit/append a curated profile's prose section (`fha person`); append a note to a source's own `## Notes` (`fha source`). Every edit is bounded to the field/section named, previewed before writing, and never moves a claim to `accepted` except by direct human instruction (§6). *Built: `fha claim`/`fha claim new`, `fha person set-living`/`new`/`relate`/`estimate`/`edit`/`note`, `fha source note`. Full design: TOOLING.md §3b/§3c/§3d.* |
| **Research workbench (front door)** | A human-started, local, browser-based front door onto the same headless tool engines every terminal command drives (§5) - not a second capability, a second way to reach the first one. Every action previews before it writes and is gated by the same human decision §6 requires; nothing it does is undocumented (every action names its equivalent command). Not a daemon, no accounts, no network beyond the machine it runs on; disposable by construction (§5). *Built: `fha serve`. Full design: TOOLING_INTERFACE.md §1b.* |
| **Citation assistant** *(backlog)* | Suggest missing `[[S-…]]` citations by matching uncited prose against accepted claims. |
