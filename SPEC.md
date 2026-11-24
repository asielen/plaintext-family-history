# SPEC.md: Plaintext Family History

*The specification for a durable, file-first family-history archive.*

**Who this is for:** people defining or auditing the archive rules (spec authors, implementers, and anyone checking whether a tool or record conforms). To use the archive, start with `GETTING_STARTED.md` in the archive's `docs/` folder.

**Version 2.38, 2026-11-24**

This document is the source of truth for the archive: its philosophy, its data model, and its physical format. Its companion, **`TOOLING.md`**, is the design document for every supporting script, deep enough that all tooling can be rebuilt from scratch, in any language, from the two documents together. When a tool and the spec disagree, the spec wins. When the spec and the files disagree, fix one, and record the decision in the commit that fixes it.

This spec describes a **target system**. Normative language (*must*, *should*, *is to be*) defines what the archive and its tools are required to do, whether or not they exist yet. The T.E. Hartley pilot is the first conforming material.

**How to read this document.** Part I is the philosophy: why the system is shaped this way. Part II is the data model: what exists. Part III is the physical format: how it lives on disk. Part IV states the requirements every tool must meet (the *what*; `TOOLING.md` is the *how*). Appendix A defines the optional extensions: fields that are valid and fully specified but that most archives never use. A record that uses none of them is complete.

**Governance.** Normative changes to this document are proposed to the archive's owner and approved before they are merged; each one bumps the version above. The document states the current rules in the present tense, with a design reason beside a rule where the reason explains it. The history of a change lives in git and the issue tracker, never here.

---

# Part I: Philosophy

## 1. What this is

For most of the history of genealogy, research lived in filing cabinets: the document in the drawer, the note stuck to the document, the binder of typed family sheets, the shoebox of labeled photographs. **Anyone could open the drawer**, with no login, subscription or schema migration, and a descendant could still pull the folder and read it a century later.

This project keeps that property. It grounds research in a plain file system and stacks three layers *on top of it*, never in place of it: a 20th-century layer (durable digital formats, embedded metadata), a 21st-century layer (search, structured claims, generated indexes) and an AI layer (assisted extraction, research feeds). Strip away every layer above the files and the archive still works.

The archive comes first. It *may* use genealogy software, but genealogy software never holds the archive. The deliverable is **this written spec** and **a kit of small, replaceable scripts**. There is no app. The format and the process are what lasts; every tool is a borrowed engine or regenerable glue.

The standard is *good-enough* research practice, kept maintainable and searchable for future generations: research that someone can pick up in fifty years, on whatever computers look like then, and continue.

## 2. Guiding principles

1. **The archive is the source of truth; tools are replaceable.**
2. **Durable, plain formats.** First tier: `.txt`, `.md`, `.csv`, `.jsonl`, `.yaml`, `.jpg`, `.tiff`, embedded IPTC/XMP. Second tier (acceptable): static HTML/CSS/JS, PDF. Everything else (audio, video, raw DNA, tool exports) only when necessary.
3. **Every important fact is traceable to a source.**
4. **Folder location is for human browsing; metadata carries meaning.** Folder trees are projections: regenerable, rearrangeable, never the only carrier of a relationship.
5. **Stay light.** Long-term durability beats short-term interface convenience. The right answer is usually simple and boring at the archive layer, with optional sophistication above it.

## 3. The four layers

The system is built around four layers, starting from the source material and working up to what gets shared with the world. Each layer may only depend on the layers below it, and nothing above the first layer is ever allowed to become the truth. This document defines layer 1 precisely and constrains how the other three are permitted to behave.

1. **Durable archive layer:** files, folders, embedded metadata, markdown records, YAML blocks, stable IDs. *(Parts II-III.)*
2. **Working interface layer:** optional editors and apps over the files, such as Obsidian or the like for notes, photo management software (like Lightroom), an AI session for research, a genealogy app fed by export. **The working or ingesting interface is never the truth.** Tooling that supports research or ingestion exists only for those purposes and is never load-bearing: it owns no data, and the archive must not notice if it disappears.
3. **Generated intelligence layer:** the index, full-text search, generated views (timelines, sources-indexes, family trees), AI-built caches. Fully rebuildable from layer 1, on demand, every time.
4. **Publication/export layer:** the static HTML explorer, person packets, profile exports, share packages. Derived outputs, never stored objects.

## 4. From file to fact: the processing path

All documents, photos, recordings, and scans start as just that: **a file in a file system**. Most of them stay that way forever, identified by their filename and embedded metadata, browsable in their folders, and that is a complete, legitimate, permanent state, not a backlog.

A file earns more structure only when it earns attention:

- When a file turns out to carry evidence (names on the back of a photo, a household in a census page), it should be **processed** into a Source: it is assigned an ID, and a source record is created describing what it is, where it came from, and how to cite it (§14).
- The next level connects the dots between the file and the research. This is done through **Claims**: a claim is a single fact supported by the source, such as a birth, a residence, a marriage, a relationship. Claims should be created *inside the source record*, parallel to the evidence they rest on (§8).
- Once a fact exists as a claim, it can be **cited in research**: a person's profile states the fact in prose and cites the source, so any reader can walk from the sentence to the evidence (§18).

The same born-minimal pattern governs everything else. A **person** mentioned in a census enters as a stub (an ID and a name) and graduates to curated only when actively researched; expect dozens of curated profiles among ~2,000 stubs. An **assertion** in an interview transcript stays as searchable text in the preserved transcript until it is substantive enough to be processed into a claim. In observed collections, roughly 1 photo in 50 carries evidence for a source record. Hand-labor scales with *curiosity*, not with the size of the family; that is what makes a 2,000-person, 10,000-photo archive maintainable by one person.

## 5. Tooling philosophy

Own the format and the glue; borrow the hard engines (exiftool, OCR, transcription, SQLite). Specifics:

1. **Nothing generated is load-bearing.** Indexes, search, caches, generated views, embeddings: all rebuildable from the archive. Delete any of them and the next run restores them.
2. **The spec is the source of truth for tooling.** Every script is an executable copy of rules written in these documents; if a script cannot be regenerated from the spec, the spec is incomplete. If Python fades or every script dies, the archive remains fully usable **by hand**: a person names a file, fills in the template that comes with the archive (`archive-template/`), and links to it by name (`[[grandpas-letter]]`), with no software at all. The machine IDs are a convenience the tools add and maintain; a human never has to create one.
3. **Python is preferred** for scripts, minimal dependencies, small and single-purpose. No script may be *required*. The preferred language may change in the future.
4. **Tools report by default** and modify only when explicitly asked.
5. **The tools are a headless core.** Each tool is an engine that computes and returns a structured result; rendering that result for a human is a separate, replaceable layer. Whatever drives the tools (a terminal command, an AI agent shelling out, a batch pipeline, a click in a UI) calls the same engine and reads the same result, so a deterministic write-back behaves the same whichever interface runs it (§6).

**The archive test.** Any tool that touches the archive must pass:

1. **Original content untouched.** Tools never alter pixels, audio, scan content, or document text. Three sanctioned, controlled operations exist: processing renames a *documents-root* file (filename only, never content or location; photos are never renamed while they live in the photos root), `fha process refile` moves a misfiled original across the two asset roots (§12.1's narrow correction, with the rename handled at the crossing, §13), and tools may write the spec'd non-destructive embedded metadata (§20: `SOURCE:` source keywords, bare `p-id` person keywords, date-confidence keywords, captions, AI markers), which are preservation operations, not content edits.
2. **Rebuildable.** Anything the tool generates can be rebuilt from the archive.
3. **Links to stable IDs:** or at minimum does not block adding them later.
4. **Plain export.** No knowledge trapped in an opaque store.
5. **Privacy-aware.** Can respect the `living`/`restricted` flags (§19).
6. **Disposable.** If the tool dies, the archive survives without it.

A tool failing 1, 2, or 6 cannot own any part of the durable layer.

Tool Evaluation Rule of Thumb: *can data get in and out with a small script, or does the tool want to be the center?*

## 6. Working with AI

AI can do the most for this archive and the most damage to it, so it gets its own rules.

**The AI contract.** Any AI (any model, any harness, any vendor) that touches the archive must honor:

1. **AI suggestions are not facts.** Every AI-derived assertion enters the claim lifecycle at `status: suggested` and reaches `accepted` only through human review. AI-written text is marked as AI wherever it is stored (keywords, marker blocks, §16c); human-written content is never overwritten. The review gate is the human's *decision*, not any one editing method: a tool may perform the actual status change deterministically, but it does so only because a human directed it. Whatever issues that confirmation (a typed instruction, a click) is the gate; the tool never moves a claim to `accepted` on its own.
2. **AI sessions are an interface, never memory.** No conversation, chat history, or agent state is ever the store of record. Anything worth keeping is written into the archive in the formats of this spec, where it is reviewable, diffable, and durable.
3. **Extraction is indexing, not preservation.** Originals (transcripts especially) are preserved verbatim in the archive, so AI mining can be selective and can be *re-run* anytime as models improve. Each extraction pass over a source is recorded in that source's frontmatter `ai_passes:` list (who, when, what, §14).
4. AI tooling passes the archive test like everything else.

The day-to-day research workbench is an **agentic CLI harness opened on the archive root**: an AI that reads and edits the files directly, runs the `fha` tools as its hands, and sits beside a plain text editor so human and AI work the same files in the same place. **Claude Code is the current operating choice; it is not a required one.** Vendor lock-in is prevented structurally: the harness configuration lives in open formats at the archive root (`AGENTS.md` as the canonical agent instructions, with `CLAUDE.md` deferring to it; skills in the portable SKILL.md standard), no harness-only state is ever load-bearing, and any agent harness honoring the contract above is an acceptable drop-in. Harness configuration and the workflow skills are specified in `TOOLING_INTERFACE.md` (summarized in `TOOLING.md` §16). Other modes keep their own surfaces: chat projects for thought-partnership, headless pipelines for batch work, Obsidian/Lightroom/the generated site for human browsing.

---

# Part II: The Data Model

## 7. Record types and what each ID is for

| Concept | What it is | Identity |
|---|---|---|
| **Person** | A human, living or dead. | `p-` id |
| **Source** | An evidence-bearing item: a record, document, photo-as-evidence, interview, letter, clipping. | `s-` id |
| **Claim** | A single sourced assertion: a date, place, relationship, event, attribute. The heart of traceability. | `c-` id |
| **Place** | A location referenced by claims. | `l-` id |
| **Working answer** | A question the researcher has answered with a guess but not yet proved, never a fact. It is the same entry as the question, with the guess written in (§17). | `h-` id, given when the guess is written in |
| **Asset** | An actual file on disk (photo, scan, recording, transcript). | **Shares its source's `s-` id** once processed, carried in the filename (documents) or embedded keyword (photos); *no ID before processing* |

**Assets share their source's identity.** All files of a processed source (fronts, backs, copy B, negatives, pages, transcripts) are *copies and facets of one piece of evidence*, so they all carry the source's ID: in their filenames for documents-root files (§13), in embedded keywords for photos-root files (§20, never renamed). Searching a cited ID in filenames+keywords surfaces the record *and* the files together. Unprocessed files (most photos) are **not** auto-assigned IDs: renaming ten thousand files would buy nothing, since filename plus embedded metadata already identifies them completely. IDs are assigned at processing, on need: the processing path (§4) applied to identity itself.

**What each ID is for:** the usage map that justifies each one's existence:

- **`s-` ids** are the *citation and retrieval unit*: prose cites them (§18), filenames carry them, embedded keywords repeat them. A reader of the archive needs only s-ids.
- **`c-` ids** are the *assertion unit*, used almost entirely by tooling and active research: claim-to-claim links (`corroborates`, `contradicts`, supersession), review tracking and backlogs, the linter's cross-checks (summary block ↔ accepted claims), generated timelines, and the rare precision citation `[[c-…]]` for one disputed assertion. Readers never need them; the system does.
- **`p-` ids** make people unambiguously linkable across name changes, spelling variants, and duplicate names, in claims' `persons:` lists, in profile cross-links and in photo keywords (§20). That is why a human-typed name resolves to a person only when it is unambiguous, and a shared name (two `John Smith`s) is pinned to its ID.
- **`l-` ids** keep places stable across renamings and spelling variants in claims.
- **`h-` ids** give working answers a stable handle: `fha find h-…` locates one, the report counts the open ones per person, and a claim that settles one may point back to it (Appendix A.4). **An h-id never converts to a c-id**: IDs are immutable and typed for life. Settling a working answer mints a *new* claim from the source that proves it; the entry records the source that settled it (§17), and both persist.

Relationships and events are not separate object types: they are **claim types** (§8.2), sourced like any other fact. Organizations are claim values, not a dedicated record type: an organization's name lives in a claim's `value` (grouping by organization is Appendix A.5), never in an `Org-` record. A person's **membership** in one (a tribe, a military unit, a lodge, an employer, a church) is a structured `relationship` claim (`subtype: member-of` or `employer`, §8.2), sourced like any fact or held as a working answer until it is, so the belonging edge is queryable while the organization stays a value.

## 8. Claims

A claim is a single sourced assertion. Claims live in a fenced YAML list under the `## Claims` heading of their source's record: one Markdown file per source, frontmatter (the source's identity) on top, then the claims it supports (the full file layout is §14, Part III; this section defines the claims themselves). A claim belongs to the *source*, never to an individual file copy: the back of copy B supports the same source's claims as the front of copy A.

### 8.1 Three orthogonal axes

**type:** *what* is asserted. Controlled vocabulary (§8.2).

**status:** *how reviewed*. The fact-safety lifecycle:

```
suggested → accepted | disputed | rejected | superseded
```

Nothing reaches `accepted` without human review. AI-generated claims always enter at `suggested`. `superseded` claims are kept, pointing forward via `notes`. A reviewer who has looked at a claim and cannot yet settle it leaves it `suggested` and writes the `reviewed:` date. So a suggested claim with a `reviewed:` date has been looked at and is still open; one with no date has not been looked at yet. (A source that says something only weakly is the other axis: `accepted` + low confidence, §8.5. A guess no source states is not a claim at all; it is a working answer, §17.)

**significance:** *how much it defines a complete record*. Set by the claim's type (§8.2), never per claim.

### 8.2 Type vocabulary and significance table

This table is the spec's own and editable; editing it is a spec change (see Governance), never a per-claim choice.

| Significance | Types | Role |
|---|---|---|
| **vital** | `birth`, `death`, `marriage` (+ `baptism`, `burial` where they stand in for vital records) | Defines completeness: a person's record is complete when each applicable vital type has ≥1 `accepted` claim. *Applicability:* `death` is inapplicable while `living: true|unknown`; `marriage` is satisfied by a `no_known_marriages` flag or a `negated: true` marriage claim (Appendix A.2); a confirmed absence counts complete, not missing. |
| **substantive** | `residence`, `census`, `occupation`, `education`, `military`, `immigration`, `divorce`, `name`, `relationship` | Enriches the record; recurring; not required for completeness. `relationship` carries a **`subtype` naming the nature of the bond**, drawn from a standardized vocabulary. *Kin-nature:* `biological` (the default for an unqualified parent or child edge), `adoptive`, `step`, `foster`, `guardian`, `surrogate-gestational`, `surrogate-genetic`, `donor-sperm`, `donor-egg`, `social`, and for siblings `half` (one parent shared; `half-sibling` is read as the same word). *Non-kin and power ties:* `enslaver`, `enslaved-by`, `employer`, `employee`, `member-of`, and the social ties `friend`, `associate`, `neighbor`. The latter, when sourced (a hunting-party clipping, a unit roster), is how the FAN network is built; unsourced ties live as working answers (§17). Two parents of differing nature (a biological and an adoptive father) are two co-valid edges, never a contradiction. The list is mostly closed: an unlisted nature lands in `subtype` free text, so no relationship stalls for lack of a word. A word that is not a nature does not belong there: a role (`mother`, `child`, `witness`) goes in `roles:`, a record's wording (`survived-by`, `nickname`) in the claim's `notes:`, and a degree of kinship (`cousin`, `uncle-nephew`, `grandparent-grandchild`) in a descriptive role such as `grandchild:`. Tooling reports such a subtype, since every view reads it as the nature of the tie. |
| **incidental** | `event`, `note` | Preserved, never scored: anecdotes, one-off moments. `subtype` free text carries detail. |

The vocabulary is mostly closed: new normalized types are added here deliberately; everything else lands in `event`/`note` + `subtype`, so no fact ever stalls for lack of a category.

### 8.3 Multi-subject claims and roles

A claim may name several people. **`persons:` lists who the claim is about; the optional `roles:` map says what each of them is.** `roles:` is **required on `relationship` claims**, and the order of `persons:` never carries meaning on any claim: positional convention is too fragile for exporters and tree regeneration.

**An event claim says whose event it is.** A `birth`, `baptism`, `death`, `burial`, `marriage` or `divorce` claim that names two or more people gives **every** person on it a role, and the event's own people carry its subject role: `child` (birth, baptism), `deceased` (death, burial), `spouse` (marriage, divorce). The others get the part they played: `parent`, `spouse`, `informant`, `witness`, or a descriptive word.

Why: records of these events name the people around them as a matter of course, and a claim that names them all without saying who is who is how a son's death is read as his father's, a bride's father as her husband, or a mother's obituary filed under her son.

The readers below keep their fallbacks so older records still read. The linter reports any gap (an error on an accepted claim, a warning on a suggested one). `fha claim` will not accept such a claim, nor a death or burial whose deceased is marked living until the human has checked that the record really is theirs and says so; then the one accept also marks them no longer living.

**Every other claim naming two or more people gives everyone a role too**: the person whose fact it is as `subject:` (a residence, an occupation, a military record, or `head` / `household_member` on a census), the others by their part (`spouse`, `child`, `parent`, `employer`, `witness`, a descriptive word). When nobody is primary (a party they all attended, a household that moved together), `subject:` lists everyone, and that is the human's answer, not a default. A father's occupation that names his son, unroled, shows on the son's timeline as the son's job. The linter warns on the gap; it does not block an accept, since these claims make no one born, dead or married.

**Role words.** The `roles:` keys this section gives a meaning to form one closed list: the derivation roles `child`, `parent`, `spouse`, `head`, `household_member`, `enslaved`, `enslaver`, `employer`, `employee`, `member`, `deceased`, with the social-tie parties `friend`, `associate`, `neighbor` (the §8.2 subtypes of the same name, which name their own parties); and the people a record names who are not parties to the event, `informant`, `witness`, `midwife`, `physician`. A word outside the list is a **descriptive role**: valid and never rejected, it places the person on the claim and derives nothing. Spell the common ones one way: `subject` (whose fact the claim is, on a claim that is not an event), `sibling`, `grandparent`, `grandchild`, `godparent`, `grantor`, `grantee`, `officiant`, `named_for` (the person a child was named after, on a birth, baptism or `name` claim). A word that is plainly a derivation role misspelt or in the plural (`parents:`, `parnet:`) is reported rather than read as a description, since reading it that way would keep that person out of the tree with nothing said.

**Synonyms.** Six kin words are read exactly as the role they name: `mother` and `father` as `parent`, `son` and `daughter` as `child`, `husband` and `wife` as `spouse`.

Tools build edges only from the derivation roles and their synonyms. Neither list is configured per archive: a meaning kept in one archive's settings would be lost the moment a record left that archive. A new derivation role or synonym joins the list here, deliberately, the way a new claim type joins §8.2.

The directional pairs (`enslaved`/`enslaver`, `employer`/`employee`) and `member` exist so an exporter never confuses victim and perpetrator, or guesses a direction the source made explicit.

**Repeated roles and several roles.**

- A tie needs both its sides: a `parent` needs a `child` on the same claim, and a `sibling` tie names both people under `sibling:` (`sibling: [p-a, p-b]`, with `subtype: half` for half-siblings). A couple is read by "Who married" below, so two people named with one of them under `spouse:` are already the couple. A claim from which no tie at all can be read, because it names one side only, records nothing, and tooling reports it.
- A claim records every tie its roles name. "Edith, daughter of Clara, married George" (`parent: [Clara]`, `child: [Edith]`, `spouse: [Edith, George]`) gives the tree both the parent tie and the marriage, and Edith's `relationships:` list (§9) owes an entry for each.
- A `relationship` claim may repeat a role with distinct subtypes: a child can carry several `parent` edges (`subtype: biological`, `adoptive`, `surrogate-gestational`, `donor-egg`), each a separate, equally valid bond rather than a conflict.
- A person may carry several roles on one claim, and every one of them counts: the mother who was also the informant on her child's birth certificate is listed under `informant:` and under `parent:`, and she is a parent of the child whichever line is written first. Order in the `roles:` map never decides a role.
- The one contradiction a map can hold is a person listed as both the `child` and a `parent` of the same record. Nobody is their own parent and the record is about the child, so that person counts as the `child` only, and tooling reports the claim.

**Who married (`marriage`, `divorce`).** The couple is:

1. the people under `roles: spouse:`, when it names two or more (three or more is the serial case, one claim recording successive marriages, and every pairing derives); otherwise
2. the two people named, when exactly two distinct people are named and neither carries only roles other than `spouse` (a spouse who was also the informant is still a spouse); otherwise
3. nobody. Tooling reports a claim that resolves two or more people and yields no couple, rather than guessing.

Why: a marriage certificate ordinarily names the couple *and* both sets of parents, and listing all six in `persons:` is correct, since `persons:` is who the claim is about. Pairing everyone off would make a man his own father-in-law's husband, and on a `divorce` claim it would end other people's real marriages. Case 2 keeps the ordinary hand-written claim working without ceremony. What its role test excludes is the claim that answered the question and said no: `roles: {spouse: [p-a], parent: [p-b]}` calls p-b a parent, so pairing them would contradict the claim in its own words.

**Who was born to whom (`birth`).** A `roles:` map naming a `child` and a `parent` on a `birth` claim records parentage exactly as a `relationship` claim does: one fact, two ways of writing it, one edge in the tree. The map is the only thing that says so. A `birth` claim with no usable map yields **no** parent edge, and there is no two-person fallback as there is for marriage.

Why: `persons: [child, father, mother]` is a habit, not a contract; `persons: [p-a, p-b]` does not say which of them was born; and the second name on a birth register is as often an informant, a midwife, or the attending physician as a parent. Marriage is symmetric, so two people and no map admits exactly one answer; parentage is directed, and getting it backwards makes a newborn her own mother's parent. A false parent edge is read back as fact by every view, export and relationship calculation, and drags a whole ancestral line with it.

The silence is never left unsaid: tooling reports every accepted birth claim that names two or more people, yields no parentage, and either names nobody as the `child` or leaves someone unplaced (a person with no role at all). A claim whose map places everyone has said all its record says and is not reported: the child and the informant who reported the birth, or two people's own births copied from their son's certificate, which names no parents of theirs. A warning that nothing could clear short of inventing a role would teach the owner to ignore the real cases.

**Who was baptised (`baptism`).** The subject is the person under `roles: child:`, as on a birth claim, or the one person left unroled when everyone else carries a role. A baptism records no parentage: a parent named on it is placed on the claim and derives no edge.

**Who died (`death`, `burial`).** `roles: deceased:` names the subject, and may name several people who died in one event (a shipwreck, a house fire): `roles: {deceased: [p-a, p-b]}`. Without it, the person the claim leaves unroled is the subject (or the people left unroled, when everyone else present carries some other role). A death or burial record routinely names more than the deceased (the surviving spouse, the informant, the children who reported it), so this is the ordinary shape. A claim naming two or more people with **no role on anyone at all** yields no subject rather than a guess, and tooling reports it; `roles: deceased:` is how an owner answers that silence when the death was, in fact, shared.

**What ends a marriage.** An accepted, non-negated `death` claim ends the deceased's own spouse edges on the death date, and never the marriage of anyone else the record names (the widow's son who reported the death keeps his). A `divorce` ends the edge between the couple the "Who married" rule finds on it, and no other.

### 8.4 Claim field reference

```yaml
- value: bookkeeper, Plains Junction Railroad   # required, FIRST - the human-skimmable
                                 #   summary of the assertion; a claims block is read by value
  id: c-90ad2e11b7              # required; §10
  type: occupation               # required; §8.2 vocabulary
  persons:                       # required; one or more people (§8.7 for the written form)
    - "[[p-de957bcda1|Thomas Edward Hartley]]"
  date: 1869/1874                # EDTF (§11); omit only if truly undatable
  place: l-baba9801fa            # optional; l-id
  status: accepted               # required; §8.1 lifecycle
  confidence: high               # required; high | medium | low (§8.5). Tooling defaults by
                                 #   source_type (vital-record → high) and asks only when unclear
  reviewed: 2026-06-10           # date of the human decision. Required on accepted, disputed,
                                 #   rejected and superseded; optional on suggested (a dated
                                 #   suggested claim means "looked at, still open", §8.1)
  reviewed_by: human             # optional; who made that decision - `human`, which a tool
                                 #   recording the decision writes by default, or a researcher's name when the archive
                                 #   has several. Never guessed: an older claim without it simply
                                 #   does not say
  notes: >                       # optional but EXPECTED - the context/detail behind the claim,
                                 #   2-3 sentences typically, a "novel" if the claim is dense;
                                 #   tooling warns when a non-vital/low-confidence claim
                                 #   lacks it. Provenance remarks, supersession pointers go here too.
    Listed as book-keeper for the Plains Junction RR in the 1874 directory; the 1869
    Champion item places him there from the railroad's early days.
  # ---- other optional fields, present only when used ----
  subtype: biological            # the NATURE of a relationship (kin: biological [default] | adoptive |
                                 #   step | foster | guardian | surrogate-gestational | surrogate-genetic |
                                 #   donor-sperm | donor-egg | social; sibling: half; non-kin: enslaver | enslaved-by |
                                 #   employer | employee | member-of | friend | associate | neighbor);
                                 #   also free-text refinement on event/note. Unlisted natures: free text.
  roles:                         # explicit semantics for multi-subject claims;
    child: p-de957bcda1          #   REQUIRED for type: relationship; repeated roles with distinct
    parent: [p-aaaaaaaaaa]       #   subtypes are valid (multiple parents of differing nature).
                                 #   Role words: §8.3. On a type: birth claim, child:/parent:
                                 #   records parentage exactly as a relationship claim does, and
                                 #   is the ONLY thing that does - persons: order never stands in
                                 #   for it (§8.3).
  restricted: true               # optional; keeps this single claim out of public output and out of
                                 #   packets unless --include-restricted (§19). A free-text type
                                 #   (restricted: by-request) marks a no-override exclusion.
  place_text: "Fairview City, Breton Co., Kansas"   # the place AS WRITTEN in the source;
                                 #   `place:` is the normalized l-id interpretation
  anchor: "00:14:32"             # position inside the source: timestamp, page, or line
  corroborates: [c-xxxxxxxxxx]   # this claim independently supports those
  contradicts: [c-xxxxxxxxxx]    # conflict - tooling spawns an open question
  # ---- optional extensions (Appendix A) ----
  # negated (A.2) · information, evidence (A.3) · hypothesis (A.4) · value_org (A.5)
```

**Evidence a claim rests on is a source.** A photo or scan that a claim rests on, or that a record refers to as evidence, must have a source ID. When the evidence is in a photo (an inscription, a caption on the mount, a sign or a date visible in the image), the claim lives on that photo's own source record (processed `--type photo`, a front and its back as one source), with a transcript of what is written on it attached. A claim on another source that only *reports* the photo (an interview where someone reads the inscription aloud) stays where it is as a secondary reading, linked to the photo's claim with `corroborates:`. Prose that points at a photo or scan as evidence cites `[[s-…]]`, never a bare photos-root or documents-root path: a path breaks silently when a library is reorganised, and only a source can hold the evidence's citation, analysis fields and transcript. Tooling warns on a path no source owns.

### 8.5 Confidence, distinct from status

Status is *review state*; confidence is *evidence quality*, **required on every claim**. A hearsay claim can be `accepted` (as what was said) while remaining `low` confidence (as what happened). Rubric: `high` = first-person/primary with specific date and place, ideally corroborated · `medium` = single source with moderate specificity · `low` = hearsay, vague time/place, or unresolved speaker. **Tooling defaults confidence from `source_type`** (vital-record → high, census/newspaper → medium, interview hearsay → low) and only asks the human when the source class is ambiguous; the human can always override. Tentative identification ("a John Smith who may be ours") is expressed as low confidence + a working answer (§17), never a separate mechanism.

The full evidence-analysis vocabulary of the field (Mills, *Evidence Explained*) is an optional extension: Appendix A.3.

### 8.6 Confirmed absences

Negative facts ("we researched and it did not happen") are an optional extension: `negated: true` claims and the person flags `no_known_marriages` / `no_known_children`, defined in Appendix A.2. Every derivation in this document reads accepted, **non-negated** claims.

### 8.7 Claims are a background layer

A human reading the archive never needs claims: prose cites *sources*, and the reader's path is profile → source record → file. Claims exist to power tooling: timelines, completeness checks, sources-indexes, exports, review workflows, contradiction detection. The source file is each claim's durable, human-readable home; all *querying* happens against the generated index, rebuilt from disk on demand. Tooling must abstract claims away from readers while depending on them completely; the full design implications live in `TOOLING.md`. The cost of files-as-truth (edit, then reindex) is accepted.

Claims live in a fenced ` ```yaml ` block. The parser does not need the fence; it helps markdown tooling render the claims as a block instead of as loose text, and keeps a value's stray `#` or `*` literal. (Frontmatter needs no fence only because it is treated as a special entity by markdown editors such as Obsidian, which is also why frontmatter links can be clickable and a fenced block's cannot, and why the cross-linking lives in the frontmatter `people:` / `places:` fields (§14), not the claims block.) Inside the claims block, **`persons:` is written one person per line as a quoted, pinned link with the name beside it** (`- "[[p-…|Margaret Cole]]"`), so a reader with no tools sees who the claim is about. The ID is what is read; the name is for the human and may go stale after a rename without breaking anything. `roles:` and `place:` stay **bare IDs** (`roles: {child: p-…}`, `place: l-…`): they repeat people already named in `persons:` a few lines above, and `place_text` already carries the place's name. Readers are forgiving in `persons:` and `roles:` alike: a bare ID, `[[ID]]`, `[[ID|Name]]`, or a name that resolves unambiguously (§18) all name the same person. Claims written by hand without the `yaml` fence are still read under `## Claims`, and the linter adds the fence later.

## 9. Persons

Two tiers (§4): **stub** (script-mintable in bulk, a permanent legitimate state) and **curated** (actively researched, the full file set of §16). **Rule:** every `p-id` referenced anywhere must resolve to at least a stub.

Stub and curated are the same file shape (§16): the same frontmatter, the same scaffolded body sections, present from the moment a record is minted. Only the `tier:` flag, the couple-folder placement, and how much of the body a human has actually written distinguish one from the other; promoting a stub to curated never creates a section that was not already there, only moves the file, flips the flag, and fills in whatever the stub had left empty, including any scaffolded section a stub minted under an older shape lacks.

**Merging and separating identities.** When two person records prove to be one human: choose a survivor; the other record gains `status: merged`, `merged_into: p-survivor`, `merge_reason:`, `merged_date:`, and its file **persists forever, renamed with a `MERGED-INTO-p-survivor__` prefix** (e.g. `MERGED-INTO-p-de957bcda1__hartley__thomas_p-old.md`) so the tombstone is obvious on disk; IDs never die and every old reference still resolves through the pointer. Name variants and external IDs fold into the survivor. Tools resolve references *through* `merged_into`: a claim whose `persons:` or `roles:` still names the merged record counts for the survivor in every derivation, view and export. The linter warns on such a claim, and lists remaining direct references, for gradual cleanup: a warning, not an error, because nothing is lost while the pointer resolves. When one record proves to be two people (conflation): mint a new p-id and reassign each claim's `persons:`/`roles:` entries deliberately (a guided human task, since dividing an identity is research judgment), with both records noting the split and date. **Merged sources.** When two source records prove to be one piece of evidence (a duplicate capture, a better scan processed separately), the survivor keeps the other's s-id in its `aliases:`, the other's files are attached to the survivor, and the other record is removed. Its claims move to the survivor with their ids unchanged, its notes and links are added to the survivor's, and the survivor's `merged_from:` (§14) records what moved, so the merge can be undone by hand. Two records whose accepted claims are about different people are not one piece of evidence, and are not merged. A citation or filename carrying the old s-id still resolves to the survivor through that alias, and the linter lists it for gradual cleanup. The asymmetry with persons is deliberate: a merged source's files all move to the survivor, so nothing is left for its record to describe, while a merged person's tombstone still carries the merge reason.

```yaml
id: p-de957bcda1                 # required
aliases:                         # link-resolution handles, tool-maintained: the ID first, then
  - p-de957bcda1                 #   the name and the public other names (so [[Thomas Edward
  - Thomas Edward Hartley        #   Hartley]] resolves in the tools, and Obsidian suggests this
  - T. E. Hartley                #   record as you type [[Thomas or [[Ned), plus any human stem
  - Ned                          #   you like. Distinct in purpose from name_variants (display):
                                 #   see §18.
name: Thomas Edward Hartley     # required; preferred display name (convention: the name at birth)
surname: Hartley                 # optional; the sort name, only when chosen by hand (§13)
name_variants: [T. E. Hartley]    # optional; display variants
# name_at_birth: Margaret A. Cole   # optional; birth / maiden name, when it differs from name
# married_name: Margaret Hartley     # optional; a married or later name (his wife's record shows both)
also_known_as: [Ned]             # optional; nicknames, other spellings
                                 #   name_variants, name_at_birth, married_name and also_known_as
                                 #   all resolve [[name]] links to this person; their public
                                 #   values are mirrored into aliases
face_tags: ["Thomas Edward Hartley"]   # optional: EXACT face/people-tag strings meaning
                                 # this person in the photo library (§20) - the durable
                                 # name→p-id resolution; one line here vs retagging photos
profile_photo: hartley-portrait-1895.jpg   # optional; the person's main portrait - shown on their
                                 # profile page and as a small square in the family tree. A filename,
                                 # path, or the photo's s-id, resolved leniently through the photo
                                 # index (§20) so it survives Lightroom moves. Same export privacy
                                 # gate as the photo strip: a portrait that co-depicts a living person,
                                 # or whose source is withheld, is dropped from public output (§19).
sex: M                           # optional; birth-assigned where a record states it; M | F | intersex | unknown
                                 #   (write `unknown` to say it is not known)
living: false                    # required; true | false | unknown - drives export redaction (§19)
birth: 1840~                     # optional PROVISIONAL estimate (unsourced); EDTF; superseded by a
birth_place: Easton, New York    #   birth/death claim. Linter tracks these toward a source.
death: 1941                      #   birth_place / death_place: optional, as written, unsourced,
death_place: Riverton, California   # going with the estimate beside them
tags: [brick-wall]               # optional; the owner's own labels for finding and grouping people
                                 #   (research status, a project) - never facts; shown on the site
restricted: false                # optional; keeps this person out of public output, and out of packets
                                 #   unless --include-restricted. A type (restricted: by-request) is
                                 #   a no-override exclusion honored by every export path (§19).
relationships:                   # optional; the working surface where relationship claims are
  - to: "[[p-075114a0f8|Caleb Comstock Hartley]]"   # forgiving target: name, [[name]], or [[p-id]]
    type: parent                 #   the ROLE; subtype names the NATURE (§8.2)
    subtype: biological          #   omit for biological (the default)
    claims: ["[[c-90ad2e11b7]]", "[[c-4b1e07d2a9]]"]   # every claim that backs this tie (preferred)
                                 #   or source: [[s-…]]; optional to write, required for the entry to
                                 #   count complete. No link = a belief. One entry per person and role.
  - to: "[[Walter Doe]]"         # an unsourced belief: jotted before evidence is filed
    type: parent                 #   linter lists it as needing a source, never rejects it
    status: hypothesis           #   a working answer built on: drawn as a guess, never exported (§17)
    hypothesis: h-4f20aa91c3     #   optional; the working answer it rests on - the question entry
                                 #   saying why it is believed and what would settle it (§17a)
created: 2026-06-10
tier: curated                    # stub | curated
# ---- optional extensions (Appendix A) ----
# no_known_marriages, no_known_children (A.2) · gender, external_ids (A.7)
```

The person record is identity, flags, and prose; sourced facts live with evidence, as claims. Tools write the frontmatter keys in the order shown above (`id`, `aliases`, `name`, `surname`, …, `created`, `tier`); a reader must not depend on order.

**Provisional vital estimates.** The record may carry optional **provisional** `birth:` / `death:` estimates: the unsourced date the researcher knows before finding the record, which most genealogists expect to record as a core data point. They are assertions of current knowledge, not sourced facts, and recording what is known before it can be proved is a legitimate starting state, not an error. Like any fact they want a source: add a `birth`/`death` claim tagged to the person, and that sourced claim supersedes the frontmatter estimate. When run, the linter matches the provisional `birth:`/`death:` values against sourced birth/death claims to keep them aligned, and lists any still needing a source.

A person's `name` and each public value of their other-name fields (`name_variants`, `name_at_birth`, `married_name`, `also_known_as`) are mirrored into `aliases:`, after the ID, so a `[[Thomas Edward Hartley]]` link resolves to that person in the tools without the unique ID, and an editor that reads `aliases:` (Obsidian) suggests the record when the name, a nickname, a birth name or a married name is typed after `[[`. Every tool that writes a name keeps the mirror in step; the linter reports a record whose mirror is behind (TOOLING §3). People can share a name, though: if two `John Smith`s exist, the linter flags an **alias clash**, and the clashing name is pinned to its ID (`[[p-…|John Smith]]`). The link keeps both the stable unique ID and an easy-to-read name. An entry in any of those fields may be written as a `{value:, restricted: true}` mapping rather than a bare string when the name should stay private: a prior name someone no longer uses, for instance. It still resolves internally (so existing links keep working) and is redacted on export (§18, §19). It is never mirrored into `aliases:`, where it would sit as plain text with nothing marking it private, and a public entry with the same text is not mirrored either; the tools take a restricted name out of `aliases:` when they find it there. It is the general `restricted` marker (§19) applied to a name, not a separate mechanism.

The optional `relationships:` block is the human-writable surface where relationship claims are applied to the lives they concern. Each entry names the other person the forgiving way (a name, a `[[name]]` tag, or a pinned `[[p-id|Name]]`, settled by the tools), a `type` (the role: parent, child, spouse, sibling, …), and a `subtype` (the nature of the bond, §8.2). A **sourced** entry links its backing with `claims: ["[[c-…]]", …]` (every claim that backs the tie) or `source: [[s-…]]`; the link is optional to write, required for the entry to count complete, and the linter reconciles each linked claim against the tie it is the durable home of. **One entry per person and role:** a second record proving the same tie adds its claim to that entry's `claims:` list, never a second entry, so the block lists each person once however much evidence stands behind them. An **unsourced** entry carries `status: hypothesis`: a guess the researcher is building on before it can be cited (step 3 of the research ladder, §17a). It is listed as needing a source, never an error; the tree and the site draw it as a guess, and exports leave it out. It may name the working answer it rests on with `hypothesis: h-…`, so the reason for the guess and what would settle it are one link away from the tie they concern; a link to an `h-` id no question entry defines (the guess was ruled out, or the id is mistyped) is reported. This mirrors the provisional `birth:`/`death:` pattern above: most of what is known about a person begins as an unsourced jotting and graduates when the source is found and mints the claim. **Graduating is the same entry, not a second one:** once the tie is sourced, the guess and the sourced entry are one tie (one entry per person and role), so the claims join that entry, `status: hypothesis` goes, and a `hypothesis:` link stays as the record of which working answer the evidence settled, as it does on a claim (Appendix A.4). Tools never make that change themselves, since the guess is the owner's own lines and often carries a note on why it was believed: a guess left beside an entry citing an accepted claim for the same person, role and nature is reported, for the owner to fold in.

Two parents of differing nature sit side by side as equally valid edges (a biological and an adoptive father, a gestational and a genetic and an intended mother), never a contradiction. The claim stays canonical: delete the block and a reindex rebuilds every sourced edge from claims, while the unsourced guesses stay here. The linter records the reciprocal edge on the other person automatically (a `child` edge here implies a `parent` edge there, pointing at the same claim) and offers to add a missing mirror rather than asking the human to write both ends by hand. Accepting a kin claim applies it: the tools add each entry it owes (or add the claim to the entry already there for that person and role) on each person it connects whose record keeps the block, and never start a block on a record without one, since a block, once there, is expected to list every tie. An entry citing the claim's source with `source:` already applies it. Disputing, rejecting or superseding the claim, or moving it from accepted back to suggested, takes it out of every `claims:` list and never removes an entry: one left citing no evidence is named for the owner to keep or delete. An empty `relationships: []` is still a list the person keeps.

## 10. Identifiers

The system does not expect a human to create an ID. A person references records by **filename and name**; the **ID is the machine identity**, assigned by tools, not by people. A hand-written record may carry no ID until a tool first touches it, a valid pre-machine state (§4); on contact a tool mints the ID, records the filename/name as an alias so existing name-links keep resolving, and maintains it thereafter.

The ID is the more durable identity. A filename is mutable and can collide; an ID is immutable and unique (format below), so machine references hold across renames, spelling changes, and duplicate names. Name-based references work while names stay unique; IDs are stronger, which is why tools assign one to every record.

```
{type}-{10 random Crockford Base32 characters}   e.g.  p-3kq9v8x2m1, s-7n4hp0wztb
```

- **Written all lowercase, matched without regard to case.** The canonical form is lowercase, prefix included (`p-`, `s-`, `c-`, `l-`, `h-`), and every tool writes it that way. Every reader matches IDs case-insensitively, so a human who types `p-3KQ9V8X2M1` is never wrong, and an asset filename or embedded keyword written with an uppercase prefix by an older tool needs no repair.
- The type prefix (`p`/`s`/`c`/`l`/`h`) is the **only** meaning an ID carries, safe because a record never changes type. Nothing else is ever encoded: no dates, names, or sequence. Anything correctable (in genealogy: everything) lives in metadata.
- **Alphabet:** Crockford Base32, `0123456789abcdefghjkmnpqrstvwxyz` (the letters `i l o u` are deliberately omitted to avoid confusion with `1 0` and accidental words). Because matching ignores case, an ID can never collide with itself across a case-insensitive filesystem (macOS, Windows).
- 10 Base32 chars ≈ 32¹⁰ ≈ 1.1 × 10¹⁵ values (~50 bits): collision probability at family scale is vanishingly small, and the linter checks for duplicate IDs anyway, so a rare collision is caught.
- **IDs are immutable:** never changed, never reused, including for deleted records.
- **No registry.** IDs are random, so no counter or ID database exists. **The archive itself is the registry**: every record carries its ID, so the set of used IDs is always derivable by walking the tree. Minting tools generate a candidate and check existence (against the tree, or the rebuildable index as a cache). Two machines mint independently. Sequential IDs are rejected precisely because they would require the registry this design avoids.
- IDs are assigned **on need** (at processing), never in bulk to unprocessed files (§7).
- **Minting is the machine's job.** `fha id mint` (or any tool, on first contact with an id-less record) generates a candidate and checks it appears nowhere in the tree. A human may mint one by hand (ten characters from the alphabet above, checked absent), but nothing requires it.

## 11. Dates: EDTF everywhere

All dates in archive records use **EDTF (ISO 8601-2)**:

| Need | EDTF |
|---|---|
| Known year | `1850` |
| Circa | `1850~` |
| Decade | `185X` |
| Year + month | `1850-05` |
| Month approximate | `1850-~05` or `1850-05~` (tilde before or after month; both valid EDTF Level 1) |
| Day approximate | `1850-05-~20`; `1850-05-20~` makes the whole date approximate |
| Uncertain | `1850?` |
| Before | `[..1920]` |
| After (on or after) | `[1920..]` |
| Interval | `1871-02/1871-03` |

Each meaning has one spelling, in one bracket convention. `[1920..]` is the mirror of `[..1920]`: a burial record that gives only the death date ("buried on or after 30 Sep 1880") is `[1880-09-30..]`, which neither drops the date nor invents an upper bound. The EDTF Level 1 open interval `1920/..` is deliberately **not** accepted as a synonym, to keep the archive's date dialect small.

The one exception is embedded photo metadata, which cannot hold partial dates; §20 defines the bidirectional mapping to the keyword-pattern system used there.

**Calendar quirks.** `date:` is always a date in today's (Gregorian) calendar, the calendar EDTF is defined in. `value` keeps the record's own words: a double date like "11 Feb 1731/32", an Old Style or Orthodox register date, a regnal year, a feast day or a Hebrew date. When the record used another calendar, convert it: an Old Style day becomes the same day in today's calendar (10 to 13 days later since 1582, by century: "11 Feb 1731/32" is 1732-02-22), a double-dated year is read as the later year, and a feast or Hebrew date becomes its day. Before 1752 England, Ireland and the British colonies began the year on 25 March (Scotland from 1600 began it on 1 January), so a date or a feast from 1 January to 24 March written with one year usually belongs to the next year by today's count: when the record counts years that way (an English parish register usually does), convert it as the double year it stands for, or give `fha date feast` the later year (`notes` says the record wrote one year), and when that is unclear, keep the year as written and say so in `notes`. `notes` says which calendar the record used and that `date:` was converted. An Old Style month alone is written as the record gives it (with the same 25 March rule for January to March), since converting it would only move its edges by days; a year alone is written as the record gives it too, though where the year began on 25 March it reached into the next one. A Hebrew month or year alone covers parts of two of today's months or years, so `date:` is the interval from its first day to its last, each converted on its own ("1 Tishrei 5650" and "29 Elul 5650" give 1889-09-26/1890-09-14). When a conversion has two answers (a Western feast between 1582 and 1752, plain Adar in a Hebrew leap year), the record's place or context picks one, and when nothing does, the human is asked. Every tool compares `date:` values across people (ages, order, the family calendar, the plausibility checks), so they must all be in one calendar. `fha date convert` and `fha date feast` do the conversion (TOOLING §4b); never convert a date in your head.

---

# Part III: Physical Format

## 12. The on-disk tree

```
family_archive/              ← the root (default name; rename freely - nothing parses it)
  START_HERE.md              ← the first page for anyone who opens the folder, written for a
                                 reader with no tools: who the family is, how to read a
                                 record. The owner's file once written (no README.md here)
  fha.yaml                   ← config + root mapping (§12.4)
  AGENTS.md  CLAUDE.md       ← agent operating instructions (harnesses look for them here)
  fha  fha.cmd  serve.cmd    ← the launchers
  docs/                      ← every other document: SPEC.md and TOOLING.md (the archive
                                 carries its own spec), GETTING_STARTED.md (the owner's
                                 first day), CHEATSHEET.md, and the rest of the manual
  ── plain-text core (git-versioned) ──────────────────────────
  sources/{type}/            ← RECORDS: one .md per source (census/, newspaper/, photos/, …)
  people/
    NNN <Couple folders>/    ← direct line, Ahnentafel-numbered (§12.2)
    connections/             ← everyone else, anchor-numbered (§12.3)
    stubs/                   ← holding pen for people not yet placed
  places/places.yaml         ← single-file place registry (§15)
  places/{slug}_{l-id}.md    ←   generated place notes, Obsidian link mode only (§15)
  notes/                     ← general research workspace (§17)
  notes/obsidian/*.base      ←   ready-made Obsidian tables (Bases), Obsidian link mode only; written once, then the owner's
  ── assets (mappable elsewhere via fha.yaml; not git-pushed if local) ──
  photos/{year}/             ← ASSET TREE: all photos, by year (§12.4 - often external)
  documents/{type}/          ← ASSET TREE: scans, clippings, recordings, transcripts
  inbox/                     ← STAGING: new scans/downloads before filing (§12.1; mappable)
    processed/               ←   originals of items already filed, kept for a look before deleting
  ── machinery and disposable output (never the truth) ──
  .fha/                      ← the vendored tools and site design; refreshed by `fha update-tools`
  .cache/                    ← tool caches (index.sqlite, photos.sqlite), rebuilt from the records;
                                 leave it to the tools
  generated/                 ← built output (the site, standalone views, galleries); regenerable
```

**Source record folders.** A source record's folder is named for its `source_type` value (`sources/newspaper/`, `sources/vital-record/`, `sources/census/`), with two exceptions: `photo` records live in `sources/photos/` and `proof-argument` records in `sources/proofs/`. Tools file new records this way. A record in any other folder is still valid (folders are a projection (§2)) and is read the same.

**Files in `notes/` that tools read.** Besides the research workspace of §17:

- `notes/home.md`: the site homepage's introduction, plain markdown written by the human (or drafted by an AI and accepted); `fha site` shows it when present.
- `notes/discoveries.md`: the log of research wins, appended by `fha confirm discovery`.
- `notes/cooccur_dismissed.json`: pairs of people the human has said are *not* connected, and pairs he has said are *not the same person*, written by `fha confirm dismiss` and taken back by `fha confirm undismiss`, so the report stops suggesting them.
- `notes/gedcom-imports/{slug}_{s-id}.csv`: one per `fha gedcom import`, saying which person record each person in the imported file became, so a newer download of the same tree finds the same people (TOOLING §13a2).
- `notes/research-log.md`: the research log of every search, by hand or by `fha capture` (§17).
- `notes/questions.md`: open questions and working answers (§17).
- `notes/obsidian/People.base`, `Sources.base`, `Places.base`: ready-made Obsidian tables (Bases) of the people, sources and places, written by `fha views places` and `fha views refresh` only with `links: obsidian` (§18), and only when `notes/obsidian/` does not yet exist. They own no data (§3, the working interface is never the truth): each reads what the records already say, so deleting one loses nothing. Obsidian saves the owner's column and sort changes into them, so after the first write no tool rewrites or removes them. They sit outside `people/`, `sources/` and `places/`, so no record tree holds a file that is not a record.

Each is optional; a missing file means "nothing recorded yet".

The **plain-text core** (records, people, places, notes, the docs) is small and designed to be **git-versioned**; that is the change log. **Assets** (photos, documents, inbox) are large and binary; they may live inside the root (then git-ignored) or, more often, on a separate drive mapped via `fha.yaml` (§12.4). The root's default name is `family_archive`; rename it freely; no tool parses the root's name.

### 12.1 Assets and records are fully separated

**No asset ever lives inside a record folder.** All original files live in the asset trees; source records reference them in place by path. Subdividing asset trees (by type, then decade) is free; folders are projection.

**Staging and filing.** New material (scanner output, downloads) lands in `inbox/`. **Filing** (taking a file from `inbox/` into the right asset tree) is the one sanctioned *move* of a file, performed by human or agent at intake. When `fha process` files an item it copies the file, byte for byte, into its asset tree and then moves the inbox original to `inbox/processed/`, under the same relative path it had in the inbox (less a leading triage folder named in `fha.yaml`'s `inbox_triage_dirs:`, §12.4). Nothing is deleted: removing the parked originals is the human's decision, made after a look. The "originals never move" rule applies from the moment a file is filed. One narrow correction is likewise sanctioned: **`fha process refile`** moves an already-filed original **across** the two asset roots when the original filing decision was wrong (a scan processed as a document that belongs in the photo library, or the reverse), updating the record's `files:` inventory and `## Notes` in the same transaction. It is a correction of filing, not a reorganization tool; moves *within* a root remain free (folders are projection) and are healed by `fha reconcile`.

**Source stubs (the half-formed middle state).** Between "raw file in the inbox" and "fully processed Source" sits a deliberate intermediate: a **source stub**, an asset (or no asset at all) paired with rough, unprocessed notes capturing *why this matters and what it is*. A stub has two equally first-class origins: **created by hand** (the human drops a scan in the inbox and writes a plain notes file beside it ("Grandma's photo, that's her brother on the left, probably 1925, from Aunt Mary's album"), or jots a note with no asset yet at all) or **pre-filled by capture** (the browser companion; TOOLING.md §13b, designed in full in TOOLING_INGESTION.md). The format is identical; the only difference is who typed the notes.

A stub is a plain Markdown notes file:

- **Lone sidecar** for a single asset: `photo.jpg` + `photo.notes.md` beside it (paired by basename).
- **Bundle folder** the moment there's **more than one file** (or none): `inbox/hartley-interview-2024/` holding `interview.mp3`, `interview-transcript.md`, and `notes.md`, or `inbox/2026-06-12-ancestry-census/`, or `inbox/grandmas-album-scan/`. A multi-version item (a recording plus its transcript, a document plus a translation, a photo plus its back) is **always a bundle folder in the inbox**, never a naming convention, because a stub has no s-id yet and the folder is the only thing grouping the files. The single `notes.md` is the stub for the whole bundle.

The notes file is **freeform-first**: the body is whatever the human wants to say, and a *light, optional* YAML frontmatter holds any structured hints that happen to exist (a captured recipe's citation fields, a parsed person, a source-type guess, and (for a bundle) optional per-file role hints like `recording` / `transcript`). A notes file written by hand may skip the frontmatter entirely and hold only prose; capture fills more in. It carries **no s-id**: a stub is *pre-source*, exactly as an untagged photo is pre-source; processing is what mints the ID and promotes the stub into a real source record (§14). Either way, **processing reads the stub as its starting point** rather than working from a blank page: the human's notes and any person/vital hints become `suggested` claims and scaffolding for review, never accepted facts.

**At processing the bundle folder dissolves:** one s-id is minted for the source; each file is filed into its asset tree carrying that shared s-id via the `[-role]` filename grammar (documents root, §13) or its `SOURCE:` keyword (photos root); the files' roles populate the source record's `files:` inventory; and the stub's notes flow into the record's `## Notes`. **Grouping moves from the folder to the shared ID** (§14): the folder was pre-ID scaffolding, not durable structure, and it goes away.

**Exception: an interview session keeps its folder.** The recordings and transcripts of one interview sitting are one source and stay together in one folder in the documents root, `documents/interviews/{interviewee}-{yyyy-mm-dd}/`, each file still carrying the shared s-id in its name. A session can hold many recordings, and its folder is how a listener finds the parts of one conversation in order. An unworked stub is a legitimate resting state, like any inbox item: "captured or jotted, not yet processed."

**Processing** = creating a source record for a file, the operation `fha process`. Identity marking depends on the root:
- **Documents root:** the original is renamed to append `_{s-id}`, the one sanctioned *rename* of a filed original: *its filename only, never its location, and never its picture, sound or text*. Prior name preserved as `original_filename` (provenance). A file whose format holds embedded metadata (TIFF, JPEG, PNG, PDF) also carries the `SOURCE:` keyword (§20 rule 3): a metadata write of the kind §5 rule 1 sanctions, not a content edit, and the third carrier, which survives a sync tool, an export or a rename that changes the filename.
- **Photos root: files are NEVER renamed while they live there**, because renames break the Lightroom catalog (links, edits, collections); the lone exception is the rename at the crossing when `fha process refile` carries a file OUT of the photos root (§12.1). Identity carriers for photos are the embedded `SOURCE: s-xxxx` keyword (written via exiftool during processing, into every keyword field a photo manager reads, §20 rule 3, so the photo library can read it in) and the source record's `files:` inventory: two carriers instead of three; keyword search (photo index, Lightroom itself) replaces filename search for photos.

Reorganizing or rescanning assets must never orphan a source or claim; the record, not the path, is the identity.

### 12.2 People: Ahnentafel couple folders

**Folders.**

- One folder per **direct-line ancestral couple**, numbered with the even (male-partner) Ahnentafel number; the wife's number is the folder's plus one (a child at position n has father 2n and mother 2n+1, so folder 040 holds positions 40 and 41). Root: **#1 = the children, collectively**, valid because full siblings share one ancestor tree. (#2 their father, #4 his father, … T.E. Hartley = 040.)
- Folder names are free-form human convenience (spaces, `+`, bracketed child lists): `040 Thomas Hartley + Margaret Cole [Ethel + Frances + Calvin + Edward]`. **Folder names carry no machine meaning**; scripts never parse them (files inside are self-identifying). Bracket lists (and the `+ second spouse` half of the name) may drift until a tool refreshes them from relationship claims (a tool-created folder starts with one partner's name; the refresh adds the other once both are curated there).
- A couple folder contains both partners' person files and the stub/person files of their **non-direct children**: that is where a human looks for an ancestor's siblings.
- Direct-line children get their own numbered folder, never a subfolder.
- **A direct ancestor's non-ancestral marriages** get suffix folders sorting beside the ancestral one: `040b Thomas Hartley + (second spouse) [children]`. Occupants beyond the ancestor are connections-tier people; half-siblings of the line live here.
- Ahnentafel's even/odd convention is a sorting convenience, not an assumption; use one partner's even number consistently; nothing in the model requires opposite-sex couples.
- A person who occupies **multiple Ahnentafel positions** (cousin marriage, endogamy, pedigree collapse) **homes to the lowest position number**; their files live in that one couple folder, and every higher position carries a cross-link entry in its folder's bracket list (`Thomas Hartley (also no. 128 - see 040)`). The lowest-number rule is deterministic, so placement and bracket refreshes are stable across runs. The folder is a projection, so the cross-link lives in the regenerated bracket text, not in an extra file.

**Numbering.** The whole tree is a projection, regenerable from the claims that record parentage; Ahnentafel numbers are derivable, never stored in records.

- **The anchor.** `fha.yaml`'s `root_person` (§12.4) names **the person this archive is centred on**, not necessarily #1 itself. `root_generation` says which position they occupy:
  - **`self`** (the default; omitting the key means this) puts `root_person` at #1.
  - **`children`** puts `root_person` at #2, leaving #1 to `root_person`'s own children, collectively. This is the way for a researcher who is the anchor but is not the youngest person on the direct line to be numbered without naming a child as `root_person`, and the only way for a childless researcher to be numbered at all.
  - Any other value is rejected by the same tools that check the anchor (`fha lint`, `fha views brackets`), never silently read as `self`.
- **Anchor `self` at the youngest generation.** Under `root_generation: self`, any full sibling works as `root_person`, since siblings share one ancestor tree. A `root_person` who has a child in the archive is one rung up from #1, and every ancestral couple below then derives one generation high. The folders cannot show that mistake, because placement checks only ask whether a folder's number matches the number this same walk produced, so an archive numbered wrong throughout matches itself. The tools therefore check the anchor itself and report a `root_person` with a child on record while `root_generation` is `self` (`fha lint` and `fha views brackets`). It is a **warning, never an error**: a researcher anchoring at themselves with `root_generation` left at `self` is making a legitimate choice, and so is re-anchoring downward later. The warning is silent under `root_generation: children`, which says outright that a child on record is the intended shape.
- **Re-anchoring.** Either edit moves every number: pointing `root_person` at a descendant re-anchors the whole tree down a generation (the researcher's children become #1, researcher and spouse #2/#3, and every ancestral couple's number doubles), and setting `root_generation: children` shifts the same generation without renaming `root_person`. After either edit, one previewed pass realigns the already-promoted folders: `fha index`, then `fha views brackets --realign` (TOOLING §7). The reindex comes first because `fha.yaml` is part of what decides whether the index is stale, and the realign refuses to rename folders from stale data.
- **Which edges number.** Tools compute every ancestral couple's Ahnentafel number by following only the **genetic** parent edges among accepted, non-negated claims (`subtype: biological`, which is the default, and `surrogate-genetic`, `donor-sperm`, `donor-egg`). A parent edge comes from a `relationship` claim or from the `roles:` map of a `birth` claim, which are the same fact written two ways and reach the pedigree as one (§8.3). Social and legal parent edges (`adoptive`, `step`, `foster`, `guardian`, `surrogate-gestational`, `social`) are shown in the folder's bracket list and the relationship views, clearly marked, but are not numbered into the pedigree.
- Tools also verify and correct folder placement (see `fha views brackets`, `TOOLING.md §7`).

### 12.3 Connections (everyone beyond)

Ancestor siblings' lines, in-laws, and non-family (friends, associates, neighbors: the genealogical "FAN club", since researching the people *around* a family often gets past a brick wall) live flat in `people/connections/`, as ordinary **§13 person files**:

```
cole__susan_p-s5a8g87at3.md          Margaret's sister - anchors through couple 040
layng__charley_p-ajnng40q36.md       Thomas's lodge brother - anchors through couple 040
```

The **anchor** is the nearest direct-line couple: every non-direct person anchors to the *family member they connect through*. A friend of Thomas Hartley anchors through Thomas's couple; Margaret's sister's husband anchors through Margaret's. The anchor is **derived, never encoded in the filename**: it follows from the person's relationship claims, and tooling answers "everyone around couple 040" as a query (`fha find --related`), not a filename prefix. A file may carry the anchor as a plain comment for the browsing human, the way the example archive's connections do. One filename grammar everywhere means connections sort by surname beside every other person file, survive the same renames, and need no second dialect for tools or search to learn.

### 12.4 Asset roots (the records/assets split)

The plain-text core and the asset libraries are **physically separable by design**. The photo library especially predates the archive, is managed by an external photo library tool (such as Lightroom), and warrants its own backup/sync policy; documents and the intake staging area may likewise live elsewhere. Roots are configured in `fha.yaml`, never hard-coded:

```yaml
# fha.yaml - plain, hand-editable archive configuration
root_person: p-xxxxxxxxxx    # the person this archive is centred on. Enables folder-number
                              # verification and person placement via `fha views brackets`.
                              # Omit to disable Ahnentafel tooling.
root_generation: self         # self (the default, also when omitted) = root_person is #1,
                              # father #2, mother #3, … - any full sibling works here, since
                              # siblings share one tree.
                              # children = root_person is #2; #1 is root_person's own children,
                              # collectively (§12.2) - anchor at root_person's own generation
                              # without naming a child as root_person.
roots:
  photos: C:/Photos          # absolute path (external library), or "photos" to keep it internal
  documents: documents       # relative → under the archive root
  inbox: C:/Photos/_inbox    # staging may sit inside the photo library's own workflow
backup:                      # optional: where `fha backup` writes its dated zips (absolute, or
  path: D:/ArchiveBackups    # relative to the archive root - same tolerance as roots: values).
                              # Default: a "{root-folder-name}-backups" folder beside the archive.
                              # Must resolve outside the archive and its asset roots (TOOLING §13e).
photos_ignore:               # optional: fnmatch patterns, relative to the photos root, that the
  - Flickr Export            # photo catalog (`fha photoindex`, TOOLING §9) skips entirely - for
  - '*.tif'                  # material inside the library that is not the archive's subject.
```

The other keys, all optional:

```yaml
inbox_triage_dirs: [review, already-existing]   # sorting folders inside the inbox; left out of
                              # the path an original is parked under in inbox/processed/. Names only.
intake_defaults:              # standing rules: what an item filed from an inbox folder is. First
  - match: "Postcards/**"     #   match wins; an explicit --type or a notes source_type: overrides.
    source_type: letter       #   Patterns as photos_ignore:, relative to the inbox. Never "photo".
originals_backup: D:/PhotoOriginals   # one untouched copy of each photo or scanned record is kept
                              # here BEFORE fha first writes metadata into it. Must be outside the
                              # archive and its asset roots; if the copy cannot be made, the write
                              # is refused.
capture_staging: C:/Users/me/Downloads/fha-inbox   # where the browser add-on drops captures for
                              # `fha capture --ingest`; not an archive root.
browser_extensions: extensions   # folder where `fha update-tools` keeps the optional Seen It add-on
                              # current; `none` = the owner declined it.
biography:
  style: chronicle            # chronicle (fact-forward, the default) | narrative (story-driven)
links: id                     # id (the default) | obsidian: how tools write a link (§18)
promotion:
  claims_threshold: 5         # accepted facts on a stub before tools suggest curating it (default 5)
views:
  completeness_line: false    # true adds the Records line to each profile's summary lines (§16)
site:                         # `fha site` presentation
  archive_name: The Hartley Family Archive   # site title (default "Family History Archive")
  hero: hartley-portrait-1895.jpg   # homepage banner: a photo reference, or a mapping
                              #   {image:, title:, tagline:}; a photo that cannot be published is
                              #   replaced by the default banner
  home_person: p-xxxxxxxxxx   # whose pedigree the homepage shows (default: root_person)
  home_pedigree_generations: 5   # generations of that pedigree (default 5, at most 8)
  picture_workers: auto       # web pictures made at once while building (auto, or a whole number)
  completeness_rings: false   # a completeness ring on each person page (TOOLING §12; default off)
  calendar: false             # "This month in the family" on the homepage (TOOLING §12; default off)
  search: true                # a search box on every page (TOOLING §12; default on)
```

**How a path resolves.** Every record path keeps the alias form (`photos/1880/…`); tools resolve the first segment through the mapping (absolute → used as-is, relative → joined to the archive root, missing → an internal folder of that name). Moving a library is a one-line edit and **no record changes**. The spec's internal structure (`photos/{year}`, `documents/{type}`) describes the tree *under each root*, wherever it lives.

- **Exclude with `photos_ignore:`, not by narrowing a root.** Alias paths are relative to the root, so narrowing `roots: photos` to a subfolder makes every `files:` entry already filed under that alias stop resolving. `photos_ignore:` excludes without moving the root. `fha lint`, `fha doctor` and `fha index` warn when a `roots:` change has orphaned filed entries (TOOLING §3).
- **An external root has no internal placeholder.** An alias whose `roots:` value resolves outside the archive folder gets no internal `documents:`/`photos:`/`inbox:` folder, since it would never hold anything: `fha install` does not create one, and `fha update-tools` removes an installed one once the root goes external, but only while it is still empty (nothing inside but the scaffolding's own placeholder files, TOOLING §13c). A `roots:` value that renames the internal folder but keeps it inside the archive (`documents: archive-docs`) still names a real internal folder.

The design fact this establishes: **the archive is a records core plus mapped asset libraries.** The git-versioned core travels as plain text; the assets are referenced wherever they live. Exports (packets, site) copy resolved files so *outputs* stay self-contained, and the backup policy must cover both the core and the mapped roots. A human learns where assets live by reading `fha.yaml`.

**Working copies (the `WORKING_COPY` marker).** The records core may be synced to a second machine, typically a git working copy carrying `sources/`, `people/`, `places.yaml`, `notes/`, and `fha.yaml` but **not** the binary assets (they are git-ignored or live on a drive that does not travel). Such a copy is flagged by a file named **`WORKING_COPY`** at the archive root, declaring the asset roots **populated elsewhere, not missing**.

- **A file, not an `fha.yaml` key.** Working-copy status is a fact about *this copy on this machine*, not about the archive, so it must never travel: the marker is **git-ignored** (it can never be committed and synced back to flip the main archive into working-copy mode) and **machine-local**.
- **Human-managed and visible.** The human opts this copy in, and opts out by removing it, so it is a visible file at the root (found by opening the folder in any file browser), carrying a plain-language explanation inside. Only its *existence* matters to the tools, not its content.
- **`fha working-copy on` / `fha working-copy off`** create or remove the marker (and keep it git-ignored), so nobody has to remember the filename or contents. Turning the mode *on* only ever withholds, so it needs no confirmation. Turning it *off* **must confirm first**: if the originals are not in fact on this machine, switching off would make the tools treat them as missing and could let a photo re-index prune the catalogue.
- The scaffolding commands (`fha install`/`fha update-tools`) never touch the marker: it is a statement about this machine's copy, not about the tools.

In this mode tools *must* treat absent assets as present-but-unavailable rather than lost:
- asset-on-disk checks are suppressed: a missing original is not flagged as an error;
- asset caches are never pruned for files that are simply not on this machine (a working copy must not be able to empty the photo catalogue);
- operations that need the originals (processing a new asset, embedding photo keywords, building an export packet) refuse with a plain pointer to the main archive rather than acting on absence.

The plain-text surface stays fully usable: rebuilding the query index from records, search, views, reports, narrative drafting against existing accepted claims, and adding new material to `inbox/` to carry back all work normally. Every command announces the mode, so behaviour stays obvious and nothing is silently rebuilt over absence. The round trip is safe by construction: `.cache/` and the `WORKING_COPY` marker are never synced, so a working copy's caches and mode flag cannot reach the main archive, and the return path is the ordinary `inbox/` workflow. Because a working copy never needs to edit `fha.yaml` (it ignores the asset roots), `fha.yaml` may itself keep syncing without conflict. (Tooling design: `TOOLING.md` §13d.)

## 13. Filenames

Every record file is **self-identifying**: its ID is in its filename, so files survive separation from their folders, and searching an ID finds everything carrying it.

The ID-in-filename is the **formalized** form, maintained by tools. A record a person creates by hand may be named however they like (`john-smith.md`, `grandpas-letter.md`) with no ID suffix, a valid pre-machine state (§10): the human identifies it by name. On first tool contact the file is renamed to the grammar below, with the original name preserved both as the mutable `slug` and as an alias, so existing `[[name]]` links keep resolving. From then on the filename carries the ID and the self-identifying invariant holds.

The deliberate style difference (person files underscored, source files hyphenated) instantly distinguishes record kinds in search results.

### Source records

`{slug}_{s-id}.md`: slug lowercase hyphenated, mutable; ID immutable.

### Source files (documents root)

`{slug}[-{copy}][-{role}]_{s-id}.{ext}`: the *source's* ID, shared by all versions. Rarely more than ~3 versions; skimmable by design. (The photo pipeline propagates text between versions with "text from alternate version" tags, so any copy reveals the others.)

**Photos-root files are never renamed by the tools** (§12.1). Another system (eg Lightroom, a cleanup pass) may rename or move them, so the filename is **not** a reliable identifier for photos. The durable identity is the embedded `SOURCE:` keyword; the record inventory stores the last-known path as a hint, reconciled by `fha reconcile` (which runs `fha photoindex reconcile` for the photo catalog, TOOLING §9) when files move.

- A photo gone from its recorded path is re-tied, on its source record and in the photo catalog alike, only to the one photo under the photos root with its exact filename, and only when that photo carries the source's `SOURCE:` keyword and no other source's. A same-named photo carrying only another source's keyword is not a candidate.
- Anything else (a keyword match under another name, such as an edited copy, a back scan or an export; several candidates; keywords not yet read) is reported for the owner to decide, and the record is left as it is.
- The photo catalog alone may still re-tie its own entry when exactly one new file under the photos root holds the vanished photo's picture data byte for byte (its content fingerprint) and no other catalogued photo holds that picture. For a source's photo it does so only under `--with-exif` and only to a file carrying the same source.

**Refile exception (§12.1).** When `fha process refile` carries a file across the roots, the rename happens **at the crossing**, before the file becomes a file of its destination root. A document entering the photos root sheds its `_{s-id}` name (the recorded `original_filename` restored when known), the last legal rename it will ever get; a photo entering the documents root is renamed **into** this grammar at the same moment. Neither case renames a file within its root.

**Roles.** The role list is open: no tool rejects a role it does not know, and a role is filed as the `-{role}` suffix like any other. The roles with a defined meaning:

| Role | Meaning |
|---|---|
| `front`, `back` | the two faces of a photo, card or page |
| `page-N` | the Nth page of a multi-page document |
| `clipping` | a cut newspaper item |
| `recording`, `transcript`, `translation` | an audio/video recording, a text transcript of it or of a document, an English rendering of a non-English original (`transcription` is an older spelling of `transcript` and means the same) |
| `extracted-text` | the text layer a tool dumped out of a PDF, page-marked (`fha source extract`) |
| `webpage` | the saved copy of a web page itself, a copy of what the site showed, never the original record |
| `record` | the record's own image or scan, fetched from a site |
| `site-data` | a site's index or data for the record, kept as `.json`, the site's reading, never a transcript |
| `page` | the whole newspaper page a clipping was cut from, one page, distinct from `page-N` |
| `annotated` | that page with the item boxed, a capture-time derivative, never the newspaper |
| `{kind}-N` | a grave memorial's photos, numbered per kind: `grave-1`, `person-1`, `family-1`, `document-1`, `flowers-1`, `cemetery-1`, `other-1`, `photo-1` |

**A text role makes a source searchable.** A source is searchable by its words only when one of its files carries a text role (`transcript`, or `transcription`, or `extracted-text`) or is itself a text file (`.md`, `.txt`, `.ged`; a family tree exported as a GEDCOM is the evidence itself). A source whose evidence is only images, scans or recordings is invisible to text search until such a companion is attached. Text search also reads two other kinds of file:

- A `site-data` file is read by text search but never makes a source searchable by its words: it is the site's reading of the record.
- A saved `webpage` copy (`.html`, `.htm`, `.xhtml`) is read by text search too. Beside a record image it does not stand in for a transcript of that image. A source whose files are only saved page copies (with or without site data) is searchable by its words, because the page is all the evidence there is and the search already reads it.

**Copies and derivatives.**

- Copies: `b`, `c`, `negative`….
- `-crop` stacks on any other suffix (`front-crop`, `back-crop`, `negative-crop`), marking supplementary detail images, never independent sources.
- `-negative` is mutually exclusive with `-front`, `-back`, and `-pageN`: it is the physical film or glass-plate source material for the root image.
- **Things kept with the photo** and scanned on their own group with it, so whoever looks at the portrait also sees the studio folder whose imprint says where it was taken. Only these words do it, as the name's last word in any capitals: `-cover` (a studio folder's or album's cover), `-folder` (a studio folder, inside or out), `-mount` (the card mount, scanned apart from the print), `-envelope` (the envelope or sleeve it was kept in), `-sleeve` (a negative or print sleeve), `-note` or `-note-N` (a note clipped or stuck to it). Each may be followed by `-back` or `-front` and by `-crop` (`-envelope-back` is the envelope's back, never the photo's). They sort after the photo's own back and are never its main image. Any other trailing word is part of the photo's name. Writing on the photo itself is still `-back`; a loose letter that came with it is its own item.
- Suffix parsing priority: `-crop` stripped first; then a kept-with word, with any `-back` or `-front` after it; else the part-kind (`-pageN`, `-negative`, `-back`, `-front`, `-bw`); then a trailing copy letter. The remaining stem is the base id (see `TOOLING.md` §6 for the full algorithm).

### Person files

`{primary_sort_name}__{given_names}[_{kind}]_{p-id}.md`: **double underscore** after the sort name (families sort together), underscores within given names.

- **The primary sort name** is the birth surname when there is one (keeping women findable under the name in their early records; matching WikiTree practice).
- **No surname.** Where a person has no surname (a mononym, an enslaved ancestor recorded by given name, a patronymic, a foundling), the slot is empty and the filename **leads with the double underscore** (`__caesar_p-….md`, `__jon_thorsson_p-….md`), a distinct no-surname sort group.
- **Surname not known.** "Not known" and "none" stay different: a name of one word says nothing about a surname, so tools file it under `unknown` (`unknown__caesar_p-….md`), and write the empty slot only when told the person has no surname (`surname: ""`).
- **Other naming systems.** Two-surname systems put the paternal (or chosen sort) surname in the slot and the full `Apellido1 Apellido2` form in `name`; surname-first cultures sort by the surname slot and display in cultural order via `name`. The full cultural name always lives in `name` / `name_variants` (§9); the filename is only a sort handle.
- **Letters and digits only.** Accented letters are written as their base letters (`wisniewska__halina_p-….md` for Halina Wiśniewska) while `name` keeps the full spelling.
- **`kind`** ∈ `research` | `timeline` | `draft-queue`. The two **generated** kinds (`timeline`, `draft-queue`) carry a `view_` marker immediately before the kind word: `{primary_sort_name}__{given_names}_view_{kind}_{p-id}.md`, e.g. `hartley__thomas_edward_view_timeline_p-de957bcda1.md`, because `view` sorts after both the bare profile filename and `research`, so a folder listing reads profile, then `research`, then the generated companions together, below both. Without it the kind word alone would decide the order and put the disposable `draft-queue` cache ahead of the person's own record. `research` carries no marker (it is hand-authored, not generated) and is the sole OPTIONAL member of this grammar (§16): most people never have one on disk at all.

**Reading a surname.** A tool that needs a person's surname reads it from the record, never from the filename: the `surname:` line when present, else the last word of `name` once a generational or regnal suffix is set aside. When a person's sort name was chosen by hand rather than read from `name` (a surname the automatic split would get wrong, such as a capitalised `XI` after a given name), the record may carry that optional `surname:` line naming it; `surname: ""` (empty) records that the person has no surname, matching a filename that leads with the double underscore. Tools write it when told the surname explicitly (or told there is none) and read it where the filename alone cannot say whether the slot was chosen or guessed. Deleting the line returns the record to the automatic reading. Because the record, not the filename, is read, a file named under an older rule needs no repair to be read right: "Jinping XI" filed under `xi__` on purpose and "Henry VII" filed under `vii__` by mistake look identical on disk, and only the record can tell them apart.

## 14. The source record

**One source = one file**: `sources/{type}/{slug}_{s-id}.md`. Frontmatter carries metadata and the file inventory; `## Claims` carries all of this source's claims; `## Stories` (interviews especially) carries mined narrative chunks; `## Notes` carries prose. **Never one file per claim**: a rich interview yielding 50-100 claims in one block is expected; it is queried through the index and reviewed in filtered passes, never read linearly.

```markdown
---
id: s-b237895f31
aliases:                       # link-resolution handles for the tools: the ID first, then any
  - s-b237895f31               #   human stem you used before processing (e.g. the inbox filename)
title: Campaign card for T. E. Hartley, Clerk of the District Court, 1880
source_type: photo            # census | vital-record | photo | interview | letter | newspaper | …
source_date: 1880-11~         # EDTF; the date OF the source itself
source_class: original        # original | derivative | authored; tools write it on every record
                              #   (original unless known otherwise; proofs: authored). Appendix A.3
subtype: campaign card        # optional free text refining source_type, chiefly when it is `other`
original_language: de         # optional; language of the evidence itself, only when not English (BCP-47/ISO-639)
repository: family collection  # where the evidence came from / lives
citation: >
  Campaign card for T. E. Hartley, candidate for Clerk of the District
  Court, Fairview, Kansas, circa November 1880.
people:                        # who this source involves/depicts - the human cross-link surface,
  - "[[p-…|Thomas Hartley]]"    #   link-valued so the tools read it as a link; hand-editable by
  - "[[p-…|Margaret Cole]]"     #   name ([[Margaret Cole]]), settled to [[p-…|Name]] by the tools
places:                        # optional; where the source is set - same link form; may be
  - "[[l-…|Fairview]]"          #   hand-added or synced from the claims' place: fields
restricted: true              # only when applicable (§19); DNA always
provenance: "Robert Hartley's collection, acquired 2025"   # optional: where the original came from
external_links:               # optional: where the evidence is online
  - url: https://www.wikitree.com/photo.php/f/f6/Hartley-6084-1.jpg
    accessed: 2026-06-10      #   the day it was seen there (a bare URL string is also read)
files:                        # inventory: roles + provenance
  - file: photos/1880/Hartley-6084-1.jpg          # PHOTOS ROOT: never renamed
    role: front                                   #   identity = SOURCE: keyword + this inventory
    is_primary: true          # the source's main file; exactly one entry carries it
    date: 1880-11~             # optional EDTF; THIS file's own date, for a source whose files
                               #   weren't all created the same day
  - file: photos/1880/Hartley-6084-1-back.jpg
    role: back
    copy: b                   # optional; which print or copy this file is, when there are several
    note: "pencil on the back, in Margaret's hand"   # optional; a remark about THIS file
  - file: documents/interviews/…-transcript_s-….md  # DOCUMENTS ROOT: renamed at processing
    role: transcript
    original_filename: hartley-interview.txt   # the name it had before processing renamed it
    derived: true             # hand-corrected derivative; an original in its own right
  - file: documents/interviews/…-translation_s-….md
    role: translation         # an English rendering of a non-English original
    derived: true             # a derivative, an original in its own right
ai_passes:                    # present once an AI has extracted from this source (AI contract rule 3)
  - {date: 2026-06-11, by: claude-opus-4, task: claims}
merged_from:                  # present once another source was merged into this one (§9)
  - source: s-7q2x9c4m1k      #   the merged source's id (also kept in aliases:)
    title: Campaign card, second scan
    date: 2026-11-20          #   the day of the merge
    files:                    #   each file that moved: its name before and after
      - from: photos/1880/Hartley-6084-1-large.jpg
        to: photos/1880/Hartley-6084-1-large.jpg   # photos keep their names
    claims: [c-…]             #   the claims that moved, ids unchanged
created: 2026-06-10
# ---- optional extensions (Appendix A) ----
# rights, physical_location (A.6) ·
# tested_person, provider, test_type on DNA sources (A.8)
---

> Purpose block (§16a): a short visible blockquote, not an HTML comment,
> stating what this file is and who writes it. Every tool-scaffolded document
> opens with one.

## Claims
(fenced YAML block - §8.4 schema)

## Other headings
(optional; a human may add any `##` section of their own - tools read the
named sections above and leave the rest alone)

## Stories
(narrative chunks mined from the source, each with topics + [[p-…]] refs - feedstock
for profile Stories sections)

## Notes
(free prose: context, verification TODOs)
```

Tools write the frontmatter keys in the order shown above (`id`, `aliases`, `title`, `source_type`, `source_date`, `source_class`, `repository`, `citation`, `people`, …, `files`, `ai_passes`, `merged_from`, `created`); a reader must not depend on order.

**AI passes.** `ai_passes:` records each time an AI read this source to extract from it (AI contract rule 3), so a later reader knows what was mined, by what, and when, and can re-run it with a better model. One entry per pass: `date`, `by` (the model, and the harness when it matters), `task` (`claims`, `transcript`, `stories`, or a short phrase), and an optional `note`. A tool or skill appends the entry when the pass ends; a human never has to. Who *accepted* a claim is recorded on the claim (`reviewed`, `reviewed_by`, §8.4), never on the pass.

**Merged sources.** `merged_from:` records each source merged into this one (§9), one entry per merge: `source` (the merged id, which also stays in `aliases:`), its `title`, the `date` of the merge, `files` (each file that moved, as `from` and `to`, its alias-form path before and after; a photo's two are the same, since photos are never renamed) and `claims` (the ids that moved, unchanged). The tool that merges writes it; it is what an undo by hand reads.

The `files:` inventory documents roles and provenance for humans. Each entry's keys are written `file`, `role`, `copy`, `is_primary`, `original_filename`, then any others. **The main file.** Exactly one entry carries `is_primary: true`: the file a reader should open first (a photo's front, a letter's first page, a single-file source's one file). It is the only way a record says which file is main; `role:` says what a file *is* (`front`, `page-2`, `transcript`) and is omitted when there is nothing to say. `fha source set-primary` moves the mark. Each file may carry an optional `status:`, omitted means present; **`missing-fixture`** marks a deliberately absent placeholder, allowed **only** under `example-archive/` and `tests/fixtures/` (warning-level there); a `missing` file in a real archive is an error. For documents-root files the link has three carriers (filename, inventory, and the embedded `SOURCE:` keyword where the format holds metadata: TIFF, JPEG, PNG, PDF); for photos-root files, two (inventory + keyword, since photos-root filenames never change). Tooling verifies the carriers agree.

**Languages.** A source whose evidence is not in English carries `original_language:` (BCP-47 / ISO-639: `de`, `la`). It tells a reader the original needs reading in that language, says which language a `transcript` is written in (a transcript is always in the original language), and marks the source as wanting a `translation`, an English rendering filed as a derivative beside the original, the same way a corrected transcript is. A file's role says its language, so `files:` entries carry none.

**Per-file dates.** Each `files:` entry may carry an optional `date:` (EDTF), that file's own date, distinct from the source's own `source_date:`, for a source whose files are facets of one piece of evidence (§7) but were not all created on the same day: a multi-page letter written across several sittings, a ledger whose entries span months. It is optional and human-paced, never required, even when a source has more than one dated file.

- It does not license bundling separately published items (several newspaper clippings about one event) under a shared s-id because they share a date range; each remains its own source, per §7.
- Omitted, a file is undated at the per-file level and the page falls back to `source_date:` alone.
- When a source's files carry different dates and `source_date:` is a single point, giving `source_date:` an EDTF interval (`1916-02/1916-06`) is the recommended zero-code fallback (CHEATSHEET.md) rather than leaving the page with no date signal at all.

The frontmatter `people:` / `places:` are the human cross-link surface: link-valued so someone working only in an editor can cross-link by name, hand-editable, normalized to the stable `[[p-…|Name]]` / `[[l-…|Name]]` form by the tools. The claims block (§8) is structured data in a fenced block, and its links are not clickable there; its written forms are §8.7's (`persons:` as pinned links with the name beside them, `roles:` and `place:` as bare IDs). `places:` is optional and may be hand-added or linter-synced from the claims' `place:` fields.

**Source type vocabulary** (controlled, expandable by spec change; same pattern as claim types): `census` · `vital-record` · `newspaper` · `photo` · `interview` · `letter` · `military-record` · `land-record` · `probate` · `directory` · `dna` · `book` · `website` · `artifact` · `proof-argument` · `ephemera` · `other` (+ free-text `subtype` when nothing fits). A passenger list, naturalization or other immigration record that is not itself a vital record is `other` with `subtype: immigration`.

**Proof-argument sources.** A conclusion resting on indirect or negative evidence is written as an **authored source**: `sources/proofs/{slug}_{s-id}.md`, `source_type: proof-argument`, `source_class: authored` (Appendix A.3). The body *is* the argument, citing the contributing claims and sources with normal `[[c-…]]`/`[[s-…]]` links (the linter verifies them); the concluded claim(s) live in the proof's own `## Claims` block (the proof is their source), typically with `evidence: indirect`. Biographies then cite the proof like any source.

**Ephemera sources.** `source_type: ephemera` is contextual or scene-setting material (period news, local color, general history), read, evaluated, and kept on purpose for texture or a future biography, that **links to no archive person**.

- `people:` stays **strictly empty** (not a research gap waiting to be filled, and not merely small), and `## Claims` is expected to stay empty too: the source exists for its `## Notes`/`## Stories` context value, not fact-claiming.
- The boundary is whether the source links to an archive person at all, not whether a name appears in its prose. A period mayor or a neighbor mentioned in passing, with no record of their own and none worth minting on this mention alone, does not disqualify it.
- The moment a source names someone who already has an archive record, or clearly warrants one on the mention's own merits, it no longer qualifies: it is re-typed to whatever fits (`newspaper`, `book`, …) and picks up that person's link like any other source. A source may be re-typed into or out of `ephemera`.
- A keepsake or scenic photo that is not proof of anything but is still linked to who it is about is an ordinary source with an optional, empty `## Claims` block, not `ephemera`: it belongs to the person it names and appears on their page as usual.

*Where ephemera shows up.* A person's profile source list, packet, timeline, and draft queue all select a source the way `fha views sources-index` does (§16): by the frontmatter `people:` link **or** by any claim's own `persons:`. So an ephemera source with empty `people:` and empty claims (the ordinary case) is unreachable from every person-scoped surface, while one that still carries a claim naming a person (an occasional contextual claim) reaches that person's surfaces. Tooling checks only the `people:` half of that pair. Either way it is not lost: `fha site` gives it its own source page and lists it, alongside every other source, on the homepage's general Sources index (grouped by decade), and its `## Notes` prose is full-text searched.

**DNA sources.** `source_type: dna`, **always** `restricted: true`; raw files live in `documents/dna/`. The fields that describe the test are an extension (Appendix A.8). Export rule: DNA is excluded from every packet, site, and export by default, and `--include-restricted` does **not** include it; DNA requires its own explicit `--include-dna`.

**Draft-prose markers.** `(TODO: import source)` is the recognized marker for useful factual prose awaiting its source; the linter lists every one. A marked sentence is uncited, so it already reads as story or context (§18) and needs no export rule of its own.

## 15. Places

A single `places/places.yaml` holds all places; they are tiny and number in the hundreds. Move to per-place records only if places start accumulating prose. The place notes Obsidian link mode generates (below) are views of this file, not records.

**One record per physical location.** Names and jurisdictions change over time; the physical location does not. `coords` anchor a place's identity (one l-id per physical place, forever), and a dated `history:` (Appendix A.9) carries what it was called and governed by over time. Claims always reference the single l-id (recording the source's wording in the claim's own `place_text`); the claim's date lets tools render the period-correct jurisdiction.

```yaml
- id: l-baba9801fa
  name: Fairview                     # modern/common name
  coords: [39.5631, -95.1216]        # lat, lon - the identity anchor; tooling backfills
  hierarchy: Fairview, Breton County, Kansas, USA   # modern hierarchy
  alt_names: [Fairview City]
  notes: Laid out in 1855 on the Kansas River; county seat from 1857.   # optional
  # ---- optional extensions (Appendix A.9): history, within ----
```

`notes:` is optional and written only when there is something to say about the place: a brief history, with loose citations (Wikipedia) acceptable, since places are reference data, not genealogical conclusions. A new place carries none.

**Place notes (Obsidian link mode).** With `links: obsidian` (§18), `fha views places` (and `fha views refresh`) writes one generated note per registered place: `places/{slug}_{l-id}.md`, the slug being the place's `name` folded to lowercase letters, digits and hyphens. The note exists so a place link has a file Obsidian can follow and each place is one node in its graph. It opens with frontmatter whose `aliases:` hold the place's ID, `name` and `alt_names`, and, for a place with coords, `location: [lat, lon]`, the property Obsidian's map views read; then the `<!-- GENERATED … -->` header and the purpose block (§16a), then what the registry says: the hierarchy, the place it sits `within`, the coords, the other names, the dated `history:` and the `notes:`. Every registered place gets one. It lists no people or sources: Obsidian's backlinks show what mentions the place. `places.yaml` stays the only truth: the note is rebuilt from it and never read back, a change is made in `places.yaml`, and a place renamed there gets a note under its new name while the old note is removed and links naming the old file are pointed at the new one. A file in `places/` without the header is the human's: it is never overwritten or removed, and never a link target, even when its name carries a place's ID. `fha views clean` removes the notes, and in `id` mode none are written. The note adds nothing an export can carry: `fha site` builds its place pages from the registry, and no export copies `places/`.

Most addresses never become places at all: they live as `place_text` on claims. A micro-place (house, address, cemetery, church, building) earns an l-record by the processing path like everything else: when it recurs and matters (the family home across decades of claims; the cemetery holding six relatives). **Recurrence is detected, not remembered:** the report surfaces unlinked `place_text` values that cluster past a threshold (and photo-GPS clusters near no known place) as place candidates; confirmed elevation mints the l-id and guides per-claim backfill of `place:`; `place_text` itself is never altered.

**Write-time resolution.** A claim's `place_text` is also checked against the registry at the moment it is written (`fha claim new`/`fha claim <c-id>`), and `place:` is attached automatically when the text fits exactly one registered place. It fits a place when it equals the place's name or one of its `alt_names` (case, spacing and punctuation aside), or when it agrees with the place's hierarchy: its first comma part names the place (the first part of its name or of an alt name) and every other part names a division above it in the place's `hierarchy:` (never the place's own name). Parts compare with case, accents and punctuation folded. A later part written plainly may name any level, and the text need not name every level, so "Chicago, Cook, Illinois" fits Chicago whose hierarchy is "Chicago, Cook County, Illinois, USA", with or without the country, while "Chicago, Ohio" does not. A later part that says it is a county ("County", "Co.", "Parish", "parafia", "gmina", "powiat") names the county level: an explicitly written county, or, when no level is written as a county, a level above the place other than the hierarchy's last two (which may be a state and a country, however the country is spelled). So "Chicago, Cook County, Illinois" fits a Chicago registered as "Chicago, Cook, Illinois, USA" but not one registered as "Chicago, Cook, Illinois", which goes to the human, while "Jackson, Jackson County" does not fit the Jackson in Hinds County, "Jackson, Mississippi County" does not fit Jackson, Mississippi, USA, and "Brooklyn, New York County" does not fit Brooklyn in Kings, New York. In the first part every word counts ("Co." reads as "County"), so "Breton County, Kansas" never fits a town called Breton, nor "Breton, Kansas" the county. A text that fits two or more places is left for the human, and nothing else (population, nearness) breaks the tie. A near match (word order, an abbreviation) is surfaced as a note only, never auto-attached: a wrong `place_id` is worse than an unlinked `place_text` the recurrence-detection pass above will still catch. The same rule links claims written before their place was registered (`fha places link`), and `place_text` is never altered.

## 16. The curated person files

Per the filename grammars of §13, a person, stub or curated alike (§9), has, in their folder:

| File (`{surname}__{given}…`) | Nature |
|---|---|
| `…_p-xxxx.md` | **Profile:** the "hand this to grandma" document. One shape at either tier: a stub and a curated profile scaffold the same sections (§9); only the folder and the `tier:` flag differ, and how much of the body a human has filled in. |
| `…_research_p-xxxx.md` | **OPTIONAL working file:** this person's open questions and working answers (§17) and a `## Research Notes` of its own. It exists only once something is written in it (§16b); most people never have one. |
| `…_view_timeline_p-xxxx.md` | **Generated** from claims, EDTF-sorted. Never hand-edited. |
| `…_view_draft-queue_p-xxxx.md` | **Generated** uncited-claim backlog; consumed by write-biography. Never hand-edited. |

**Profile structure:** frontmatter (§9), then:

```markdown
# Thomas Edward Hartley (1840-1941)

> **This person's record - yours to write.** The main page for this person:
> summary, biography, relationships. The summary lines under this note and
> the `## Sources` list are generated; everything else here is yours.

<!-- GENERATED-BEGIN summary by fha views summary on 2026-11-17 -->

*(Written by `fha views summary` from accepted claims. To write these lines by hand, delete the two marker comments around them.)*

**Born:** 3 March 1840 - Easton, Carrow Co., New York [[s-xxxx]]
**Died:** 19 January 1941 - Riverton, California [[s-xxxx]]
**Married:** [[p-cd795c61e0|Margaret A. Cole]] - February 1871, Fairview, Kansas [[s-ea61339378]]
**Parents:** [[p-075114a0f8|Caleb Comstock Hartley]] [[s-xxxx]] · [[p-d00c678c1a|Chastina Augusta Reed]] [[s-xxxx]]
**Children:** [[p-c4b26bb4bc|Ethel]] [[s-xxxx]] · [[p-83e768cacb|Frances]] [[s-xxxx]] · [[p-fa7541e871|Calvin]] [[s-xxxx]]

<!-- GENERATED-END summary -->

## Sources
(generated region, §21b - every source touching this person, from cited claims)

## Biography
(chaptered by era/place)

## Stories
(the incidental long tail, each linking its source)

## Research Notes
(hunches, leads, brick walls - see §16b; a question goes in the research file or `notes/questions.md`, §17b)

## Friends & Family
(non-relative connections and context - the FAN club)
```

**Citation density:** in the summary block, **one citation per line** is sufficient; it is a curated overview. In the body sections, factual statements should carry **all relevant citations** (every source that supports the fact), since the body is where corroboration is shown. (Tooling may suggest missing citations by matching prose against claims; see `TOOLING.md`.)

**The summary lines** say who the person was in a few plain lines a reader with no tools sees first: `Born`, `Died`, `Married`, `Parents`, `Children`, each fact citing its source, people as `[[p-…|Name]]` links (zero-hop, since person filenames carry IDs, so searching the link's ID finds the person). Tools write them from **accepted** claims only, as a generated region (§21b) right after the H1 and its purpose block: `fha views summary` (and `fha views refresh`) rewrites the region in place, so it is plain text on disk, readable with no tools, and stale only until the next refresh. A line with nothing accepted behind it is left out; a birth or death known only from a baptism or burial says so (`baptized …`, `buried …`). `Married` lists each wedding in date order, a divorce right after the marriage it ended: a couple who married, divorced and married again shows both weddings. Records of one couple's marriage are one wedding unless a divorce falls between them. With `views: completeness_line: true` in `fha.yaml` (§12.4), tools add one last line, `Records`, the person's completeness (TOOLING §7a): it is generated like the rest of the region and, like the region, never exported. **Hand curation stays possible:** delete the two marker comments and the lines become the human's: tools never rewrite summary lines that carry no markers, and the linter cross-checks such hand-written lines against accepted claims and flags drift.

### 16a. The purpose block

Every document a tool writes or scaffolds (a profile, a source record, a generated view, the optional research file) opens its body with a short, **visible** purpose blockquote: real markdown, never an HTML comment, stating what the file is, who writes it, and where to work instead if it isn't the reader. It answers the question a `<!-- GENERATED … -->` header cannot, because an HTML comment is invisible in Obsidian preview, the generated site, and GitHub, exactly where the warning is most needed. The block is scaffolding for the working archive, not content for the family: `fha site` and `fha packet` strip it from anything they render or copy (§21b).

### 16b. `## Research Notes` and the optional research file

Every profile carries its own `## Research Notes` section (above): free prose for hunches, leads and brick walls, the lightweight default home for working notes on this person.

A research file holds what outgrows that: `## Research Notes` (the overflow, not a duplicate of the profile's section) and `## Open Questions` (this person's questions and working answers, in the §17b entry form). **It exists only once something is written in it.** No tool scaffolds an empty one: `fha person new`, `fha stubs` and `fha person promote` never create it, and a tool that writes this person's first question or overflow note creates the file at that moment. A research file holding nothing but its scaffold is clutter and may be deleted. A person with both keeps the profile's `## Research Notes` as the short version; tooling never reconciles the two.

The `## Sources` section is never hand-maintained; it is generated from cited claims (§21b).

### 16c. Marked prose: AI drafts and private notes

Three HTML-comment markers change what happens to prose when it is published. They are plain comments, invisible in a rendered page and readable in any text editor.

- **`<!-- AI-DRAFT … -->`** marks prose an AI drafted that no human has accepted. The marker sits at the **end** of the span it covers; the span begins at the previous AI marker of either kind or the previous `#`/`##` heading, whichever is nearer. The text inside the marker is free provenance (model, date, task). Publication drops the whole span, marker included. In a transcript companion, one `AI-DRAFT` marker anywhere says the text is an unreviewed machine reading of the images.
- **`<!-- AI-ACCEPTED … -->`** is the same marker after a human accepted the span (`fha confirm draft` rewrites it in place, appending `(accepted DATE)`, so the provenance is kept). Publication keeps the prose and drops the marker. In a transcript with no `AI-DRAFT` marker left, it says a human checked the text against the images; a transcript with neither marker makes no claim either way.
- **`<!-- private -->` … `<!-- /private -->`** fences prose that stays in the working archive. Every shared copy drops the fenced text: a standalone `fha site` build, `fha wikitree`, and `fha packet` (the profile, a research file copied with `--include-research`, and source records alike). The owner's own working copies keep it: the linked site preview, `fha serve`, the standalone HTML views, and backups. Fences nest, so a fence wrapped around a section that already holds one ends only at its own closer. A person named only inside a fence is not named in what ships.

All three fail closed, and drafts and fences are judged together on the text as written, so neither can hide the other's boundary. A damaged AI marker (an unterminated `<!-- AI-DRAFT`, a stray marker word) anywhere in a person's record withholds all of that person's published prose rather than risk publishing a draft: the site drops Biography, Stories and Research Notes, and `fha packet` and `fha wikitree` refuse the profile. An unterminated `<!-- private -->` hides everything after it.

## 17. Notes and the research ladder

`notes/` is the **general** workspace: research strategy, todos, surname studies, multi-person narratives.

- `notes/research/`: working notes spanning people or topics.
- `notes/narratives/`: formal multi-person write-ups; every factual claim cites a source or is explicitly marked context/speculation; exportable.
- `notes/questions.md`: the questions and working answers that span people or concern no one person (§17b). A question about one person may instead live under `## Open Questions` in that person's research file (§16b); the format is the same and tools read both.
- `notes/research-log.md`: the research log (§17c).

Notes connect to the core elements through ID links in their text, and linked frontmatter `persons:` / `sources:` / `places:` lists. **A script reading only the IDs those links resolve to must be able to reconstruct every connection**: the bare ID is the load-bearing part; the double brackets are durable plain-text delimiters, and the editor's *resolution and graphing* of them is an editor convenience, never load-bearing.

### 17a. The research ladder

Everything not yet proved climbs one ladder, written in one place: the question entry (§17b).

1. **Question:** something not known. "Who were Margaret Cole's parents?"
2. **Working answer:** the same entry, with a guess, the reason for it, and what would settle it. A guess is not a separate object: answering a question without proof is adding those lines, and the entry gains an `h-` id so it can be referred to.
3. **Build on it:** research may continue past a guess. A guessed relationship may be entered on the person's `relationships:` with `status: hypothesis` and `hypothesis: h-…` naming this entry (§9), so the tree can be followed past it and anyone reading the tie can find why it is believed; the tree and the site draw it as a guess, and exports leave it out.
4. **Settle:** one of three ways:
   - a source says it: process the source, and its claim carries the fact like any other;
   - the pieces add up: write the reasoning as a proof-argument source (§14), a short write-up of why the evidence, taken together, proves it, whose claim cites the evidence;
   - it is wrong: the guess is ruled out. Its lines leave the entry and one dated `context:` line records it (`ruled out h-…: Daniel Cole - his daughter Margaret died in 1852`), so the same guess is not made again. The question stays open for the next guess, which gets a new `h-` id.

**Weak source or working answer?** If anyone or anything other than the researcher says it (a family story, an index entry, another researcher's tree), it is a source, processed with a low-confidence claim (§8.5). If only the researcher says it, it is a working answer. A guess is never a claim, since a claim requires a source.

### 17b. The question entry

An entry is an `## Q:` heading, then one markdown list line per field. The `- ` at the start of each field line is part of the format: tools find the fields by it.

```markdown
## Q: Who were Margaret Cole's parents?
- origin: human
- status: open
- refs: [p-cd795c61e0]
- id: h-4f20aa91c3
- guess: Daniel Cole and Ruth Ames of Easton, New York
- basis: the only Cole household in Easton in 1850 with a Margaret of the right age
- verify: Margaret's death certificate, or Daniel Cole's will
- context:
  - 2026-06-12 (human): 1850 census checked; Margaret, 9, in Daniel's household
```

- `origin:`: `human` | `tool` | `agent`; machine questions are marked at birth.
- `status:`: `open` · `answered [[s-…]]` · `resolved` · `closed (not pursuing)`. `answered` and `resolved` mean the same, and either may carry the link that settled it. Closing without an answer is a legitimate, recordable research outcome. The status is the question's; a guess that proved wrong is recorded in `context:` (§17a), not here.
- `refs:`: a bracketed list of bare IDs, `[p-…, c-…]`, naming who and what the question is about.
- `id:`, `guess:`, `basis:`, `verify:`: present once the question has a working answer (step 2 of the ladder). The `h-` id is added with the guess and never changes or is reused; the people in `refs:` are the people the working answer is about.
- `context:`: dated, origin-attributed findings indented beneath it and appended over time.
- `share:`: optional; `family` marks a question the human is willing to show relatives (the shared site's questions page, `TOOLING.md` §12). Only its heading and `refs:` are shared; the working answer and `context:` never leave the archive. Absent means the question stays in the working archive. Like `status:`, it is the human's to set.

Tooling may propose answers and closures and append context; status changes require human confirmation.

**What belongs here.** A question is written down when a person asks it, or when two claims conflict (a `contradicts:` pair, §8.4). A gap a tool can compute (no birth date, no parents, no source for a provisional date) is never written as a question: `fha report` shows it fresh every time, and a written copy would only go stale.

### 17c. The research log

`notes/research-log.md` records searches performed (including empty ones) so no collection is fruitlessly re-searched, and so "reasonably exhaustive" is demonstrable. It is the one search log: hand searches, executed `research-next` plans and every `fha capture` append to it.

```markdown
- date: 2026-06-12
  question: "[[h-4f20aa91c3]] parents of [[p-cd795c61e0|Margaret Cole]]"
  repository: Ancestry
  collection: "Kansas State Census, 1875"
  years: 1875
  terms: "Hartley, Breton Co."
  result: nil

- date: 2026-07-09
  question: "death of [[p-de957bcda1|Thomas Edward Hartley]]"
  repository: Breton County clerk
  collection: "Death register"
  years: 1911/1925
  terms: "Hartley"
  result: unavailable (the clerk's office holds no registers before 1930; they went to the state archive)
```

One entry per search: a markdown list item whose other fields sit on two-space-indented lines below it. `date` (required); `question` (the objective, free text, which may link a question, a working answer or a person; a person ID there makes the search count for that person); `repository`; `collection`; `years` (optional: the years of records the search covered, in EDTF, such as `1875` or `1911/1925`; with no `years`, the search covered whatever the collection holds); `terms`; `result`, one of:

- `nil`: searched, and nothing was found in what the entry says was searched;
- `found [[s-…]]`: found, and processed as that source;
- `partial (note)`: something found, the note says what;
- `staged inbox/…`: captured into the inbox, not yet processed;
- `requested (note)`: asked for by letter, email or form, and no answer yet;
- `unavailable (note)`: could not be searched at all (the office was closed, the records are lost or closed to the public, the site was down); the note says why.

`fha capture` appends exactly this shape, one entry per capture, with `result: staged` and the inbox path.

**A nil says what one search covered.** It is not on its own evidence that the record was never made: it supports a negative finding (Appendix A.2) only together with what shows the record would have been made, survives for those years and that place, and could have been found by that search, and that reasoning is written in a proof-argument source (§14). An `unavailable` or `requested` entry is no evidence either way: the search is still to be done.

Entries are **dated** because collections grow: a nil from 2024 is worth re-running in 2027. `research-next` and the report **check the log first**: "already searched (date)" is surfaced before any lead is proposed. An `unavailable` or `requested` entry is shown as still to do, never as already searched.

**Search history is optional.** The log is useful when present and never needed to understand or operate the archive. Being a durable record, it lives under `notes/`, never in `.cache/`.

## 18. Citations and linking

Double-bracket ID links, greppable and tool-verifiable:

- **`[[s-xxxx]]` is the standard citation** on factual statements in any narrative body. It matches natural research practice (footnotes cite evidence) and is zero-hop: searching the ID surfaces the source record *and* its files together (the bare `s-xxxx` is a substring of the link). The double brackets are durable delimiters: plain text in any editor, and a link in every tool and export that reads the archive. Obsidian resolves a link by file name only, never through `aliases:`, so it shows an ID link as unresolved; searching the ID there finds the record, whose file name ends in it.
- The link always carries the **immutable ID**, so a citation survives a record rename. The target is the bare ID or the record's **file stem**, which always ends in its ID (§13): `[[hartley__thomas_edward_p-de957bcda1]]` is the same link as `[[p-de957bcda1]]`. A stem resolves by the ID at its end and nothing else, so a stem whose leading part no longer matches the file's name (the record was renamed since) still resolves. An optional display follows a pipe: `[[p-xxxx|Margaret Cole]]` reads as the name and links by ID.
- `[[c-xxxx]]` is permitted when claim-level precision matters (one disputed assertion and its status), the exception; it resolves to the claim's source record (§8.7). A claim has no file of its own, so its stem form names the source and puts the claim ID as the display: `[[source_stem|c-xxxx]]` or `[[source_stem#Claims|c-xxxx]]`. A link to a source whose display is exactly a claim ID is a link to that claim.
- `[[p-xxxx]]` cross-links people; `[[l-xxxx]]` a place. Both zero-hop via the record's ID/alias.
- **Link mode.** `fha.yaml`'s `links:` says how tools *write* a link; every reader accepts both forms whatever it says. `id`, the default, writes the ID as the target: `[[p-xxxx|Margaret Cole]]`, `[[s-xxxx]]`, `[[c-xxxx]]`. `obsidian` writes the record's file stem, so Obsidian, which follows a link by file name only, can follow it: a person `[[cole__margaret_a_p-xxxx|Margaret Cole]]`, a source `[[letter_s-xxxx|s-xxxx]]`, a claim `[[letter_s-xxxx#Claims|c-xxxx]]` (its source's stem, opened at the Claims section), a place `[[fairview_l-xxxx|Fairview]]` (its place note's stem, §15). Display text already written is kept; a link with none gets the person's or place's `name` or the source's ID. Inside a Markdown table the display follows `\|` (`[[cole__margaret_a_p-xxxx\|Margaret Cole]]`), as Obsidian writes it there, and every reader takes `\|` as the separator in either mode. A link to a merged person or source is written to the record it was merged into. A place link takes the stem form once its place note exists and keeps the ID form until then. Two kinds of link keep the ID form in both modes: a link inside a fenced block such as the claims YAML, which Obsidian does not follow; and a claim link with display text of its own, which the stem form cannot carry. With `obsidian` set, `fha lint` warns about a person, source or claim link, or a place link whose note exists, with an ID target or a stem that no longer matches its file, and `fha normalize-links --obsidian` rewrites both; the same command converts an archive when the mode is first turned on. A file name changes when its record is renamed, so in `obsidian` mode every tool that renames or removes a record file (a person rename, the tombstone a person merge leaves, a source merge, `fha lint --fix-ids`) points the person, source and claim links to that record at its current file in the same run: a merged record's links at the record it was merged into, a moved claim's at its new source, and a plain `[[old name]]` link that Obsidian followed by a file name `--fix-ids` replaces at the new file, its words kept as the display. The tool says how many links in how many files it changed, `--dry-run` shows the same count, and a failure or an interruption puts those files back with the rest of the run; a file that cannot be put back is named, never reported as undone. `--fix-ids` is the one exception: it keeps the IDs and file names it gave even when the links cannot be updated, since every tool still finds the old name in `aliases:`, puts the link files back, and names `fha normalize-links --obsidian --write` to finish. It also leaves a `[[old name]]` link as written, and says so, when another file in the archive carries the same name, since the link may mean that file. Moving a record to another folder keeps its file name and changes no link. In `id` mode a rename changes no link, since an ID never changes. A value other than `id` or `obsidian` reads as `id`, and lint names it. A link whose display is exactly its own ID reads as the same link with no display. The mode changes only how a link is written, never what any export withholds (§19): an export that copies a record's text reads its file-name links back to the ID form, since a file name carries the record's name.
- **Forgiving by default.** A hand-typed single-bracket `[s-xxxx]`, a bare `s-xxxx`, a human alias (`[[grandmas-album]]`), or an Obsidian fragment (`[[s-xxxx#Notes]]`) all still resolve, and nothing rejects them. On request, `fha normalize-links` rewrites a single-bracket `[s-xxxx]` to `[[s-xxxx]]` and a resolved name or hand-made stem link to `[[id|name]]`, keeping the human's text as the display; a file-stem link already carries its ID and is left as written. It leaves a fully bare ID in prose alone, since it cannot tell a citation from a mention, and never guesses an ambiguous name.
- **Uncited prose is by definition story/context, never fact.**
- Exporters swap links for refs; the WikiTree exporter renders `[[s-…]]` as `<ref>` blocks from the source's `citation` field, and redacts the display name of any `living`/`unknown` person (§19).

**Resolution and the alias surface.** A `[[ ]]` link's target may be an ID, a record's file stem, or a name/stem; a file stem resolves by the ID it ends in, and anything else resolves to a canonical ID through the record's `aliases:`. A `#Heading` or `#^block` suffix on the target (`[[s-xxxx#Claims]]`) is ignored for resolution. Three related fields stay distinct in purpose so they never conflate: **`aliases:`** are link-resolution handles, namely the canonical ID, any human stems, and (for persons *and* places) the `name` plus its public other names mirrored in (a person's `name_variants`, `name_at_birth`, `married_name` and `also_known_as`, a place's `alt_names`), so a name-link resolves; **`name_variants:` / `alt_names:`** are the display/genealogical name variants a human curates, the *source* the tool mirrors into `aliases:`; **`face_tags:`** are exact photo people-tag strings (§20). The human edits `name` / `name_variants` / `alt_names`; the tool keeps `aliases:` in sync. Because mirrored names can collide (two `Springfield`s, two `John Smith`s), the alias-clash check (`TOOLING.md` §3) governs all of them, and a clashing name resolves only when pinned to its ID (§7). In Obsidian, `aliases:` only helps find a note while typing `[[`: picking a suggestion writes the file name as the link's target and the alias as its display, and the tools follow that link by the ID the file name ends in. A name marked `restricted` (an entry in a person's other-name fields carrying `restricted: true`) still resolves through the alias surface internally, since the tools read it from the field it sits in and never mirror it into `aliases:` (§9), so links and search keep working, but its display is redacted in any external export: the same internal-resolves / external-redacts rule that governs a `living` person's name (§19).

## 19. Privacy

Two privacy flags; no tier system. Flags appear only where they apply.

- `living: true | false | unknown` (Person): drives redaction in any external export, packet, or publication. **`unknown` is treated as living** for all external-facing output; stubs default to `unknown` (uncertainty is safe by default).
- `restricted`: a marker placed on whatever should stay out of public output: a **source**, a single **claim**, a whole **person**, or a **name** (an entry in any of a person's name fields, `name_variants`, `name_at_birth`, `married_name` or `also_known_as`, written as `{value:, restricted: true}`, how a private prior name is recorded). Plain `restricted: true` is excluded from public output and from packets unless `--include-restricted` is passed. It may carry a free-text **type** instead (`restricted: dna`, `restricted: by-request`, …) when types must be told apart; two are **no-override**: `dna` (always restricted; requires its own `--include-dna`) and `by-request` (a person who asked to be left out, honored by every export path with no opt-in). The marker keeps material in the archive and out of what leaves it; nothing is deleted, and a restricted name still resolves internally (§18).

## 20. Embedded metadata

An AI photo-categorization pipeline (a separate tool, not part of this kit) is a curation-layer adapter writing IPTC/XMP: keywords, verbatim transcriptions, AI captions, date-confidence tags. Embedded metadata is part of the durable layer; it travels with the bytes. Integration rules:

1. **Date mapping is bidirectional.** Photo metadata cannot hold partial dates, so a photo's date is two parts: the **digits** live in EXIF `DateTimeOriginal` (a forced full `YYYY-MM-DD`, rule 2), and a keyword **`DATE: {pattern}`** says how much of them to believe. The pattern is letters only: `Y`, then optionally `M`, then optionally `D`, each followed by `!` confident, `~` best guess, or `?`/nothing unknown; trailing time letters (`H`, `M`, `S`) are allowed and ignored. Building the date stops at the first component that is unknown, so a pattern with no confident or best-guess component gives no date; a best-guess year followed by a confident month (`Y~M!`) also gives none, since EDTF cannot say it. Only a photo carrying a `DATE:` keyword has an archive date: its presence is what says a human reviewed it, since EXIF alone is often the scan's own date. A `DATE:` keyword holding digits (`DATE: 1880`) is not this grammar and gives no date; tools report how many photos carry one.

   | Keyword pattern | EDTF |
   |---|---|
   | `Y!M!D!` (1942-11-25) | `1942-11-25` |
   | `Y!M!` (1960-05) | `1960-05` |
   | `Y!M~` | `1960-~05` |
   | `Y!` | `1960` ( This is the same as Y!M?D? )|
   | `Y~` | `1960~` (circa) |

   A keyword pattern cannot say "the 1960s"; a decade belongs in a claim's `date:` as `196X` (§11). (`19XX` is a century in EDTF, not a decade.)

2. **`import_date` never becomes truth.** The forced full `YYYY-MM-DD` written for EXIF compatibility is a technical workaround; only the EDTF value flows into archive records.
3. **On processing, the source ID is embedded as a keyword:** `SOURCE: s-xxxxxxxxxx`. Third redundant carrier of the source↔file link: in a photo, and in a documents-root file whose format holds metadata. **Every keyword the tools add or remove goes into all three keyword fields photo managers read** (IPTC `Keywords`, XMP `dc:subject` and Lightroom's `lr:hierarchicalSubject`) in one write, and keywords are read as the union of the three; IPTC is left out only where a format has no standard IPTC block (PNG, GIF, HEIC, PDF). Lightroom reads and saves the XMP lists, so a keyword written to IPTC alone would be invisible there and could be erased by its next metadata save.
4. **People in photos: bare `p-xxxxxxxxxx` ID keywords + the `face_tags:` map.** Each person record's `face_tags:` (plus `name`/`name_variants`) maps the library's existing face/people-tag strings to the p-id, the resolution layer, one durable line per person, no *name* double-tagging. On top of that, tagging tooling writes a **bare `p-id` keyword** onto the photo (in the rule-3 fields) for each identified person (e.g. keyword `p-de957bcda1`): an in-file, unambiguous marker that survives any catalog and settles same-name collisions outright. Always previewed; `fha photoindex tag-person` applies them across a face-tag match or to specific photos.
5. **AI output stays marked as AI** (analysis keywords, marker blocks); human captions are preserved.
6. AI-derived assertions raised into claims enter at `status: suggested`.
7. **A wrongly-embedded keyword is cleared or corrected only through `fha source clear-keyword`, never by hand**: the same durable-layer discipline as every other write in this section; the stray value comes out of every keyword field and a correction goes into all of them (rule 3). Scope is documents-root only (photos-root person-keyword correction is a separate, undesigned decision); `fha lint --with-exif` is what finds the candidates.

## 21. Publication and export

Generated output that leaves the archive falls into two categories with different binding privacy rules: **public output**, meant for redistribution beyond the family, and **private export**, a family-facing copy that stays within the family but is gathered outside the archive's own access controls.

**Public output, living-person redaction (mandatory):**
- Any person whose `living` flag is `true` or `unknown` is redacted: their name is replaced with "Living Person" (never with a surname, which says who they are related to), and the photos naming them are withheld.
- A claim naming a living person is withheld when it is that person's own event, since its date and place are their birthday or wedding: the `child` on a birth or baptism, the `deceased` on a death or burial, a `spouse` on a marriage or divorce (§8.3), and on such a claim anyone named with no role. It is also withheld when it is a `relationship` claim, or when everyone it is about is living: the people under `subject`, `head` or `household_member`, or everyone it names when it names none of those.
- Any other claim naming a living person is kept, since it may be central to a deceased person's story: a baptism with a living godparent, a census of a household with a living child. Its kind, date and place are shown with the deceased people it is about. The living people are not named, linked, listed or labelled on it, and its own wording, which usually names everyone on the record, is not shown. It adds no one to a chart, the family tree, the calendar, or any list of the family.
- The source of a kept claim keeps its page and its citation. Its page may show the record's own words and images, which can print a living person's name, but never lists, links or labels that person. A source naming a living person and carrying no kept claim stays out, and its citation reads as withheld.
- `unknown` is treated as living. Stubs default to `unknown`.
- In a shared site's family trees, a living or unknown adult is drawn as a card reading "Private" with nothing else on it, so the tree keeps its shape; a living person not known to be an adult is left out. The relationship line and family-tree number may be worked out through ties to people the page does not show; those people are never named.

**Public output, restricted material (mandatory):**
- Anything carrying `restricted` (a source, a claim, a person, or a name) is never included in any public output. A restricted source's claim contributions (dates, vitals) may appear only if an unrestricted co-source also establishes the same fact independently; a restricted claim, person, or name is withheld (a restricted name is rendered as the person's unrestricted display name). A claim naming a restricted person in any part is withheld whole, whatever else it names.
- DNA evidence always carries `restricted: true`. No DNA-derived conclusions appear in public output without an additional, independent non-DNA source establishing the same fact.

**Scope (what "public output" covers):**
- `fha site`: the static HTML snapshot.
- Any other public-publication export path (GEDCOM, WikiTree, etc.) unless that path has an explicit `--include-living` / `--include-restricted` opt-in documented in its TOOLING entry.

**Private export (`fha packet`), its own rules, per TOOLING §8:**
- `fha packet` is a family/private export, not public output, and is not subject to the redaction rules above. A packet may include `living: false` people's full prose and cite other people who are still living, with a README caution rather than redaction.
- The packet *subject* is held to a stricter rule than the cited-other-people case: `living: true` and `living: unknown` subjects are refused before any output is written, with no opt-in.
- Anything `restricted` is excluded by default (restricted sources named by ID only in the README); `--include-restricted` overrides, except the **no-override** types: `restricted: dna` stays excluded until `--include-dna` is also passed, and `restricted: by-request` is never included by any flag. A `restricted: by-request` *person* vanishes from packet content.

**Printed reports (`fha views family-sheet` and the other printed reports), their own rules, per TOOLING §7:**
- A printed report is made to hand to relatives, so it follows the public-output rules for evidence: only `accepted` claims; no `restricted` claim, person, name or source; no DNA source; no claim naming a living person. A source withheld only because it names a living person is cited by a shared placeholder, never by its title.
- A person whose `living` flag is `true` or `unknown` is never named: a living parent or spouse appears only as "Living Person", with none of their own facts, and a living child is counted ("and 2 living children"). A living person's own descendants are neither named nor counted. A `restricted` person is left out and not counted.
- A report about a living or unknown subject is refused, except the Ahnentafel report, which shows a living subject as "a living relative" and continues with the deceased ancestors (an Ahnentafel of one's own line is the common use); a `restricted` subject is refused without printing the name.
- Reports are written under `generated/views/` with the GENERATED marker and are swept by `fha views clean`. They carry a banner saying living relatives are counted, not named.

**Site generation freshness contract:**
- `fha site` reads structured data (claims, vitals, relationships, sources) from `.cache/index.sqlite`; it is as live as the last `fha index` run.
- Biography, Stories, and Research Notes prose are read from the curated person `.md` file directly. Friends & Family, by contrast, is *not* read as prose: that page section is independently derived from relationship claims (§9), the same as every other structured-data section; a human's own free text under the profile's `## Friends & Family` heading is for the working archive, not the site.
- The generated `.md` views (timeline, draft-queue) are research artifacts for the agent; `fha site` does not read them, nor does it read a profile's own inline `## Sources` region or its summary region (§21b): the per-person source list and summary on a site page are built independently from the index, like every other structured section.
- The site snapshot is frozen at generation time. Regenerating is idempotent; an old snapshot remains a valid frozen view as the archive moves on.

**View maintenance (`fha views clean` / `fha views refresh`):**
- Generated `.md` views carry the `<!-- GENERATED … -->` header. This header is the sole signal for deletion by `fha views clean`: files without it are never touched, even if they match a view filename pattern.
- Views rendered as standalone HTML (`--format html`) are single-file documents under `generated/views/`, carrying the same header as their **first line, before the doctype**; `fha views clean` sweeps them by the same marker-per-file signal (a hand-written file in that folder is never touched). They render the same content as their `.md` twins and remain private, unredacted research artifacts, not public output (TOOLING §7 D11).
- `fha views refresh` is the counterpart: regenerate all content views in one pass after `fha index`. It is the recommended post-index step for bulk regeneration (a fresh copy, a reset after `views clean`); a review session instead refreshes just the touched persons' views (TOOLING §17), so untouched views keep their generation dates.
- Deleting generated views reduces archive size for sharing but does not affect archive correctness; all views are rebuildable from the index.

### 21b. Purpose blocks and generated regions in publication

Two conventions sit *inside* an otherwise human-owned file rather than replacing it, and neither is publication content:

- **The purpose block** (§16a) is the first thing in the body of every document `fha` writes or scaffolds: a visible blockquote, distinct from the whole-file `GENERATED` header above and not consumed by `fha views clean`. `fha site` never reaches it: every prose section it publishes is read by a specific `## Heading` match that starts capturing strictly after that heading line, and the purpose block always sits before the first `##` heading, right after the H1. `fha packet` reaches the block structurally (a profile or source record is read as one string before it is copied), so it strips the block explicitly before any copy ships.
- **A GENERATED-BEGIN/END region** is a third generated-content convention, distinct from both the whole-file header and the purpose block. A profile has two: its `## Sources` section and its summary lines under the H1 (§16). Paired `<!-- GENERATED-BEGIN name by cmd on date -->` / `<!-- GENERATED-END name -->` markers rewrite one named part of an otherwise human-owned file in place, never the whole file. `fha views clean` never touches it (there is no whole file to delete); the view command that owns it rewrites it in place instead (`fha views sources-index`, `fha views summary`). `fha site` never reads it, per the freshness contract above. `fha packet` drops both entirely from the profile copy it packs, and `fha wikitree` drops the summary, rather than including them verbatim. Each is read from the index with none of the export's own privacy filtering (the summary names every child, living or not; the sources list names sources by archive-relative path), so the copy ships without a substitute rather than with a stale or unfiltered one.

Neither convention changes what "generated" means for §5's disposability rule: both are fully rebuildable, and deleting either loses nothing durable.

---

# Part IV: Tooling requirements

This part states **what** every tool must do: the binding requirements. **How** each is built (schemas, algorithms, CLI design, libraries, error handling) is specified in **`TOOLING.md`**, which is part of this spec for governance purposes: changes to its design follow the same governance as this document.

Invariants for all tools: generated artifacts are disposable caches; tools report by default and modify only on explicit command; every tool is regenerable from the two documents; generated `.md` views written into the tree carry a `GENERATED - do not edit` header (a generated *region* inside an otherwise human-owned file, e.g. a profile's `## Sources`, §21b, carries the equivalent paired marker instead, since there is no whole file to header).

| Tool | Requirement (the *what*) |
|---|---|
| **Index builder** | Rebuild, from scratch on demand, a queryable SQLite index of all persons, sources, claims, places, files, citations, plus full-text search over transcripts and notes. Never authoritative, never appended. |
| **Linter** | Walk the archive; verify every rule in this spec (IDs, filenames, schemas, references, statuses, dates, inventory/keyword agreement, summary-block drift); report vitals gaps and suggested-claim backlogs; spawn questions for contradictions on request; reconcile each sourced person-doc `relationships:` entry against its accepted `relationship` claim (pair, role, subtype) and check reciprocity, offering to add a missing mirror edge. |
| **ID mint** | Generate spec-conformant IDs with existence checking; batch capable. |
| **Stub minter** | Create person stubs in bulk from claims that reference unresolved people. |
| **Processing assistant** | Given a file or folder: mint s-id, mark identity (documents: rename, plus the keyword where the format holds metadata; photos: keyword only, never rename under Lightroom), scaffold the source record; folder mode triages candidates first. |
| **View generators** | Per-person timelines; each person's own inline `## Sources` region (a couple folder has no separate sources index: its two profiles already list their sources); refreshed folder bracket lists; **relationship views** (ancestor / descendant / FAN trees for any person) all derived from accepted claims, never stored; **printed reports** (a family group sheet, and the other printed reports) for relatives, every fact cited, living relatives never named (§21). |
| **Relationship calculator** | For any two persons, derive the **blood relationship** (the cousin/removal/great-grandparent name, via the lowest common ancestors over genetic edges, saying *half* only when the record shows it and saying when an unrecorded parent leaves it open, and naming every separate route when two people are related more than one way), the **tie by marriage** when there is no blood tie (spouse, in-law, step, adoptive and the other recorded kinds of parent, "by marriage"; a step-parent worked out from a marriage is stated only when the dates show the marriage overlapped the child's life, and is hedged when they are missing), and the **shortest social path** (a readable chain of role-named hops over all relationship edges), at query time, from accepted `relationship` claims, never stored. An adoptive, step or other non-genetic parent never makes a blood relationship, and an in-law or step word is never given for two people related by blood. Returns a structured result; the text form renders the sentence. |
| **GEDCOM exporter** | Derive a standard GEDCOM (relationships + vitals) for a person or the whole tree, at export time, from relationship/vital claims. For exchange with genealogy apps only: never the corpus, never re-imported as truth. |
| **GEDCOM importer** | File a *foreign* GEDCOM (an Ancestry download, another program's export) as ONE source record whose every assertion enters as a `suggested` claim with a line anchor, plus a person stub per individual (provisional vitals, safe `living:` defaults). Plan-then-apply with a one-shot re-run guard and full rollback; the archive's own export is refused (one-way bridge, no round-trip); duplicates are reported, never auto-merged. Writes only forms §8/§9/§14 already define; no schema additions. |
| **Person packet** | Gather everything about a person (profile, claims, sources, files, *and all photos of them*, found by bare `p-id` keywords + `face_tags:` resolution) into a zip of copies, clearly labeled as a derived export, honoring `living`/`restricted`. |
| **Photo metadata index** | Scrape embedded metadata of the entire photo library into a fast, disposable search catalog (so finding photos never requires opening Lightroom); incremental rescan; powers the packet's photo gathering. **Variation-aware:** versions of one physical photo (fronts/backs/copies/negatives) are grouped as one logical photo, returned once; per-variant date tags are resolved to one best-confidence group date, and cross-variant date disagreements are surfaced as a report. |
| **Place geocoder** | Backfill `coords`, missing hierarchy levels (county, state, country) and `alt_names` in `places.yaml` from an offline gazetteer, never rewriting what is there, with human confirmation. |
| **Interview converter** | Migrate the prior transcript-mining output (T###/R###/Q### records) into conformant sources, `suggested` claims with anchors, stories, and questions. |
| **Static site generator** | Render the archive as a **self-contained static HTML snapshot**: its own web-optimized asset derivatives, only publication-eligible material (living/unknown redacted, restricted/DNA excluded), built from accepted `relationship` claims, its social and adoptive edges drawn distinctly from the genetic line (§12.2). The home page carries a **marriage-aware ancestor pedigree** (a server-rendered, self-contained SVG, no script required) centered on a configured person and reaching back a configurable number of generations, with that person's own spouse(s)/children/siblings alongside it and a redaction-safe fallback for a living seed; each curated person's page carries the same immediate-family chart engine at its original scope (children, subject + spouse(s), parents, grandparents), plus a per-person opt-in **descendant explorer**, an interactive tree via a vendored rendering library fed a neutral JSON contract. No server, no accounts, no dependency on the archive once generated; works from a USB stick; embeds in packets. One sanctioned exception to the no-external-dependency rule: a place page with coordinates embeds an OpenStreetMap view (plain iframe, no library) that degrades to an empty frame offline while the ordinary map link beside it always works; the snapshot never *depends* on it. Visual design is built live, not specified here; the JSON data contract is. |
| **WikiTree exporter** | Render a curated profile to WikiTree markup; `[[s-…]]` links → `<ref>` citations. |
| **Doctor** | One health command: root + `fha.yaml` + mapped roots reachable; exiftool/Python present; index & photoindex freshness; lint summary; inbox aging; restricted/living/unknown counts; agent-instruction drift (stale command or skill names in AGENTS/skills). |
| **Formatter** | Conservative normalization as a lint feature (`--format-check/--format-write`): key order, ID casing (lowercase, §10), blank lines, record section order, `value` first in each claim, final newline; never rewrites prose. |
| **Web capture** | Turn an open record page into an inbox source stub: scrape citation info from the HTML, accept a dropped/fetched asset (or store the page itself as an HTML asset when the page *is* the record), write a research-log entry, and stage for the processing pipeline. Site recipes for common sources; generic scrape as default. Sits on the open page, no credentialed scraping. A sibling mode, `--path`, registers a file that must never move (no page, no asset copy, a pointer stub only). Full design: TOOLING_INGESTION.md. |
| **Deterministic write-backs** | Human-directed, surgical edits to a record's own fields, the review/minting gesture behind every "confirm" click: move a claim's `status:`/correct its fields, or mint a brand-new claim onto a source (`fha claim`); set a person's `living:` flag, mint a new person stub, jot an unsourced relationship belief, write a provisional vitals estimate, or edit/append a curated profile's prose section, or rewrite one existing note entry named by its exact text (`fha person`); append a note to (or rewrite one note on) a source's own `## Notes`, or merge two records of one piece of evidence into one (§9: a merge moves the files, claims and notes it names and refuses before any write when the two records' accepted claims are about different people) (`fha source`); set a registry place's coordinates/alt-names/dated history or append a dated place research note (`fha places set`/`note`). Every edit is bounded to the field/section named, previewed before writing, and never moves a claim to `accepted` except by direct human instruction (§6). Full design: TOOLING.md §3b/§3c/§3d/§10. |
| **Research workbench** | A human-started, local, browser-based way to run the same headless tool engines every terminal command drives (§5). It adds no capability. Every action previews before it writes and is gated by the same human decision §6 requires; nothing it does is undocumented (every action names its equivalent command). Not a daemon, no accounts, no network beyond the machine it runs on; disposable by construction (§5). Full design: TOOLING_INTERFACE.md §1b. |
| **Citation assistant** *(proposed)* | Suggest missing `[[s-…]]` citations by matching uncited prose against accepted claims. |

---

# Appendix A: Optional extensions

Every field here is valid and fully defined, and most archives never use any of them. (There is no A.1: section numbers here never change, so a reference to one stays right.) A record without them is complete; a reader can skip this appendix until a record carries one. Each extension keeps the meaning it would have in the core.

### A.2 Confirmed absences (negative facts)

Some of the most important genealogical findings are *absences*: a person who never married, had no children, or (for someone still living) has no death record. "We researched and it did not happen" is a real, citable conclusion, represented two ways:

- **Negative-fact claim** (the researched case): a normal claim of the relevant `type` with **`negated: true`** and `evidence: negative`, e.g. `type: marriage, negated: true, value: "no marriage found", confidence: medium` citing the searches that justify it, with what they covered (§17c). It sits in the source's claims list like any claim and is fully sourced, typically by a proof-argument source (§14) assembling the negative searches.
- **Person-level convenience flags** (the common, low-ceremony case): optional booleans on the person record (`no_known_marriages: true`, `no_known_children: true`) for quickly recording a settled judgment without authoring a claim. They are *assertions of current knowledge*, not sourced facts; tooling treats them as "stop flagging this person's missing marriage/children in vitals gaps," and a later contradicting claim supersedes them.

```yaml
  negated: true                  # on a claim: confirmed ABSENCE; pairs with evidence: negative
no_known_marriages: false        # on a person: confirmed-absence convenience flag
no_known_children: false         # on a person: confirmed-absence convenience flag
```

A negated claim asserts an absence, so it **never derives a relationship edge, and never ends one**: a negated `marriage` must not put a couple in the tree, and a negated `relationship` or `birth` claim must not put a parent there, since reading either as a bond would record the opposite of what the research concluded. It is an accepted, sourced finding for every other purpose (it satisfies completeness, it is searchable, it is cited like any claim); it simply is not evidence *for* the thing it denies. Every derivation in the toolchain reads accepted, **non-negated** claims for this reason, and so must every check written against those derivations: a linter that reads a negated claim as an edge asks the human to record the bond the research ruled out.

This keeps completeness honest: a person isn't "missing" a marriage once research has confirmed there wasn't one. **Living persons** (`living: true|unknown`) are likewise never flagged for a missing death: the vitals-completeness check (§8.2) treats death as inapplicable while living.

### A.3 Evidence analysis (Mills)

The full analytical vocabulary of the field (Mills, *Evidence Explained*) is available as optional fields, never required of a human: `source_class` on sources (original | derivative | authored), and `information` (primary | secondary | undetermined, judged per informant per assertion) and `evidence` (direct | indirect | negative, relative to the question) on claims. **AI-assisted research populates these by default**; the linter's informational pass pings accepted claims missing them so cleanup sessions can backfill.

```yaml
source_class: original           # on a source: original | derivative | authored (proofs: authored)
  information: primary           # on a claim: primary | secondary | undetermined
  evidence: direct               # on a claim: direct | indirect | negative
```

### A.4 Claim back-pointer: `hypothesis`

```yaml
  hypothesis: h-xxxxxxxxxx       # the working answer this claim settled (§7, §17a)
```

### A.5 Organizations: `value_org`

```yaml
  value_org: "Cherokee Nation"   # the organization a member-of/employer relationship names,
                                 #   as a string (no record type) - lets views group "everyone in Co. K"
```

The organization stays a value (§7); `value_org` only groups claims that name the same one.

### A.6 Source publication and custody

```yaml
rights:                          # optional publication metadata
  holder: family collection      #   who owns/holds copyright
  publication_ok: false          #   false keeps the source out of the site and every export
physical_location:               # optional: where the PHYSICAL original lives (changes over time)
  holder: Sam Rivera
  as_of: 2025-05
```

- **`rights`:** `publication_ok: false` means the source may not be shared: `fha site` and every export leave it out, exactly as they leave out a `restricted` source, and its claims' contributions follow the same rule (§21). Absent or `true`, the source is shared under the ordinary rules.
- **`physical_location`:** who holds the physical original, and since when. It matters for anything that was scanned: the scan is a copy, and the original may be the evidence a future researcher needs. `fha process` asks for it when it processes a scan and records the answer; leaving it blank is fine.

### A.7 Person: `gender` and `external_ids`

A person may carry `gender:` beside `sex:` (§9): identity, free text with suggested values; omit unless there is something to record. Its one use is pronouns in drafted prose: a biography takes them from `gender` when present, else from `sex`, and leaves them neutral when neither says.

External profile IDs (WikiTree, Ancestry, FamilySearch) are fields on the person: the person's own page on that site. Web capture writes the id it already knows from the page it captured, and on a merge they fold into the survivor (§9).

```yaml
gender: man                      # optional; identity, free text with suggested values
external_ids:                    # optional
  wikitree: Hartley-6084
  ancestry: "382013742308"
```

### A.8 DNA test fields

A DNA source (§14) describes the test with these fields: `tested_person:` (p-id), `provider:` (AncestryDNA, FamilyTreeDNA, …), `test_type:` (`autosomal` | `y-dna` | `mtdna`), optional kit notes. The core rule stands whether or not these are present: a DNA source is always restricted.

### A.9 Places: `history` and `within`

A dated `history:` carries a place's past names and jurisdictions, so a claim's date lets tools render the period-correct one (§15):

```yaml
  history:                           # optional, dated jurisdiction/name changes
    - {period: "1855/1861", hierarchy: "Fairview, Breton Co., Kansas Territory, USA"}
```

**Containment: physical links, political strings.** A micro-place (house, address, cemetery, church, building) may carry one optional `within: l-xxxx` link to the settlement physically containing it, which is stable because physical containment does not change. Settlement→county→state is **never** linked: that is *jurisdiction*, which drifts, and it lives only in the dated `history:` strings. Tooling recurses `within` so "claims in Fairview" includes its houses and cemeteries; coords serve proximity even without links.

```yaml
- id: l-9e2210ab44
  name: Hartley family home, 214 N 5th St
  within: l-baba9801fa               # physically inside Fairview; one hop
  coords: [39.5644, -95.1209]
```
