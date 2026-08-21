---
name: setup-interview
description: >
  Run once, right after `fha install`, when the human wants to do the setup interview - "let's do the
  setup interview", "I just installed this, where do I start?", "who are you and how do you want to be
  numbered?" - or whenever a session notices `root_person` is still unset in a freshly installed
  archive's `fha.yaml`. Asks who he is and how he wants the tree numbered (`root_generation: self |
  children`), then his immediate family - parents, spouse(s), children - as firsthand testimony from the
  one informant the archive is guaranteed to have. Mints his own person record directly (name, `sex:`,
  `living:`), writes one `source_class: authored` interview source, and drafts `relationship`/`marriage`
  claims from it naming everyone with explicit `roles:`, all `status: suggested`; hands off to
  `review-claims` so his own review pass is still the gate - the interview proposes, it never
  self-accepts.
---

# setup-interview

A brand-new archive knows nothing about the one family it is guaranteed to have authoritative
information on: the researcher's own. This skill asks him, once, at setup - who he is, how he wants
the tree numbered, and who his immediate family is - and writes what he says the way the archive writes
everything else: a real source with a real citation, and ordinary `suggested` claims that live or die
by the same review gate as any other evidence (issue #74). It orchestrates existing `fha` verbs only;
nothing here is a new tool. See [`../_STANDARD.md`](../_STANDARD.md).

## When this runs

Right after `fha install`, once - `fha install`'s own "Next steps" points here. Triggers: "let's do the
setup interview", "I just set this up, where do I start?", or a session noticing `root_person:` is still
unset (commented out, as the template ships it) in a freshly installed archive's `fha.yaml`.

**Normally once per archive.** If `root_person` is already set when this starts, the interview (or an
equivalent by-hand setup) has already happened - say so plainly, name who it currently points at
(`fha find <the current root_person P-id>`), and ask whether he wants to add something that was missed
(another child he forgot to mention, say) or is here for something else. Never silently re-run the whole
flow or mint a second record for someone already on file. Adding one missed person later is a light
re-entry into step 3 alone, not a repeat of steps 1-2.

## The contract for this skill

- **Every `relationship`/`marriage` claim this skill drafts is `status: suggested`.** Even though the
  informant is uniquely authoritative - he cannot be wrong about who his own parents, spouse, or
  children are - the human's own review pass is still the only path to `accepted` (_STANDARD.md §3).
  The interview proposes; it never self-accepts.
- **The person records it mints (himself and his named family) are not claims** and carry no `status:`
  field - minting a stub, setting `sex:`, and setting `living:` are the same direct, confirmed-in-
  conversation writes `fha person new` / `set-sex` / `set-living` make everywhere else in this archive.
  They are gated by asking him plainly before writing, not by `fha claim`.
- **Record the AI pass** in the interview source's `## AI Passes` block before handing off (_STANDARD.md
  §3).
- **Respect privacy** (_STANDARD.md §3): his immediate family is almost always living people, and
  `living:` defaults to `unknown` on a fresh stub - this skill sets it explicitly (true/false/unknown)
  for everyone it touches, because his own household is exactly the case he can state with certainty.
- **`root_person`/`root_generation` are written straight into `fha.yaml`**, which is documented as a
  plain, hand-editable file (SPEC §12.4) - unlike `places.yaml` or the index, nothing here needs a
  dedicated verb to edit it, only a shown preview before the write.

## Flow

### 1. Who are you?

Ask his full name, and in the same breath: *"And for the tree's numbering - are you male, female,
intersex, or would you rather I leave that blank?"* (`sex:` is what the Ahnentafel derivation reads to
place a parent in the father/mother slot - #72's own comment thread found it missing on the owner
himself in a real archive, silently degrading the whole tree's numbering). A rough birth year is welcome
but optional.

- **Resolve first, never assume a blank slate:** `fha find "<his name>"` - a session may be resuming, or
  he may already have a stub from other work. A clear match → confirm it's him and reuse that `P-id`
  for every step below. Several candidates → show them and let him pick. No match → mint on his
  confirmation:
  ```
  fha person new "<Full Name>" --sex <M|F|intersex|unknown> --birth "<rough year, if given>" --dry-run
  fha person new "<Full Name>" --sex <M|F|intersex|unknown> --birth "<rough year, if given>"
  ```
  (`--surname` overrides the filename's surname split when the automatic split would get it wrong -
  Spanish double surnames, particles, surname-first conventions.)
- He is, definitionally, in the room: set `living: true` without asking a strange "are you alive"
  question.
  ```
  fha person set-living <P-id> true
  ```

### 2. How do you want to be numbered?

Ask in plain words - this is the owner's own framing, verbatim: *"Should you be position #1 in your
family tree, or should that spot belong to your children, together, one generation in from you?"* If he
has no children on record (yet, or ever), `self` (the default) is the simple right answer - he anchors
the tree exactly as position #1. If he *does* have children in the archive, `children` is usually what
he wants: without it, a `root_person` with a child on record numbers every ancestor one generation high,
invisibly (SPEC §12.2, W127) - the old workaround was pointing `root_person` at one of his own children
instead of himself, which broke the moment there was more than one child (one became #1 arbitrarily, the
rest went unnumbered) and had no answer at all for a childless researcher. `root_generation: children`
anchors the archive at *him* while still numbering *his own* parents, grandparents and so on correctly -
and it works even before any child is on record. Either way is a legitimate, permanent choice, and
`root_generation` is a plain `fha.yaml` line he can change later if he's not sure now.

One thing this setting does **not** do, so as not to overpromise: it numbers `root_person`'s *own*
ancestor line only. A spouse's parents are never assigned an Ahnentafel position by this tool, under
either setting - they still get real person records and a sourced parent-child claim from this
interview (so they're findable and correctly linked), just not a folder number.

- "Just me" / "position 1" → `root_generation: self` - the default; **do not write the key**, matching
  `fha.yaml`'s own convention of leaving defaults unset.
- "My kids" / "one generation in" → write `root_generation: children`.
- Either way, `root_person: <his P-id>` gets written.

Show him the exact two lines before writing them (this is a plain text-file edit, previewed the same
way any other write here is), then add them to the **Active configuration** section of `fha.yaml`,
beside `roots:`:
```yaml
root_person: P-xxxxxxxxxx   # <His Name> - set from the setup interview, <date>
root_generation: children   # only if he chose that
```

### 3. Immediate family

Ask, in plain sentences, forgiving of "I don't know" or "skip that one" at any point (_STANDARD.md §5):
his **parents**, his **spouse(s)** (past or present - none is a fine answer), and his **children** (none
is a fine answer). For each person: name, sex (for the same numbering reason as step 1 - ask, don't
assume), a rough birth year if he has one, and whether they're still living. If how he describes a
parent isn't a straightforward biological tie ("she raised me but isn't my birth mother", "my dad
adopted me at three") - translate that into the right `subtype:` (`adoptive`, `step`, `foster`,
`social`, …, SPEC §8.2) rather than defaulting silently to `biological`.

1. **Resolve every name first**, exactly as in step 1 - `fha find "<name>"` before minting anything;
   several candidates go to him as a pick, same as an ambiguous name anywhere else in this archive.
2. **Summarize the whole list back once** ("So: your parents are Richard and Linda Sample, your wife is
   Jane, and you have two kids, Emily and Michael - all still living except your dad, who passed away
   in 2015. Sound right?") and mint on that one confirmation - not a separate yes/no per person for a
   short list:
   ```
   fha person new "<Name>" [--surname <override>] --sex <M|F|intersex|unknown> --birth "<rough>" --dry-run
   fha person new "<Name>" [--surname <override>] --sex <M|F|intersex|unknown> --birth "<rough>"
   fha person set-living <P-id> true|false|unknown
   ```
3. **Write up what he told you** as a plain dated note - this is the evidence file, not a hint wrapper,
   so it is a bare `.md`, not a `*.notes.md` sidecar. Organize it under short headings (Who I am / My
   parents / My spouse / My children) so the claim-drafting step below can cite a section by name:
   ```
   inbox/setup-interview-<slug of his name>.md
   ```
4. **Process it like any other source**, then correct the frontmatter `fha process` cannot know on its
   own (it defaults every source to `source_class: original`, which is wrong here):
   ```
   fha process inbox/setup-interview-<slug>.md --type interview --title "Setup interview - <His Name>" --date <today> --dry-run
   fha process inbox/setup-interview-<slug>.md --type interview --title "Setup interview - <His Name>" --date <today>
   ```
   Then hand-edit the scaffolded record's frontmatter:
   - `source_class: authored` (he is the informant *and* the record is written up from his own
     account, not a captured original - SPEC §8.5).
   - `repository:` naming him as the informant (e.g. `"<His Name> (the researcher, self-reported at
     archive setup)"`).
   - `citation:` - a real sentence, not the bare title (who, what, when, how it was gathered).
   - `people:` - every person named this session, `[[P-id|Name]]` per SPEC §14's link-valued form.
5. **Draft the claims directly into the source's `## Claims` block** - mint one batch of ids, then write
   the full shape by hand, exactly as `process-source` drafts any claim (there is no `fha claim new
   --type relationship`; it refuses that type outright, since a `roles:` map is too structured for a
   single-claim CLI mint - this is ordinary Stage-B judgment, not a missing tool):
   ```
   fha id mint C -n <count>
   ```
   - **Parentage → `type: relationship`, never `type: birth`** (#71: a birth claim's second person is as
     often an informant or a doctor as a parent - only an explicit `roles:` map says who is what,
     unambiguously, which is the whole point of asking him directly). One claim per child (including
     himself, as the child of his own parents), naming every parent he gave together:
     ```yaml
     - value: <Child>'s parents are <Parent A> and <Parent B>
       id: C-xxxxxxxxxx
       type: relationship
       subtype: biological   # or adoptive/step/foster/social, per what he actually described
       persons: [P-child, P-parentA, P-parentB]
       roles:
         child: P-child
         parent: [P-parentA, P-parentB]
       status: suggested
       confidence: high       # medium/low if he hedged - translate the hedge, don't default it away
       information: primary
       evidence: direct
       anchor: "'My parents' section"   # or whichever heading it came from
       notes: >
         Self-reported by <His Name> during the setup interview.
     ```
   - **A spousal bond → `type: marriage`** (SPEC's vital-significance type for a couple, scoped by
     `roles: spouse:` - not `type: relationship`), one per spouse he named:
     ```yaml
     - value: <Him> and <Spouse> are married
       id: C-xxxxxxxxxx
       type: marriage
       persons: [P-him, P-spouse]
       roles:
         spouse: [P-him, P-spouse]
       status: suggested
       confidence: high
       information: primary
       evidence: direct
       anchor: "'My spouse' section"
       notes: >
         Self-reported by <His Name> during the setup interview.
     ```
   - `information: primary` / `evidence: direct` are not the generic interview default (TOOLING.md
     defaults interview claims to *low* confidence, because most interview content is hearsay about
     other people) - they are the deliberate, reasoned call for *this* narrow case: a person's own
     firsthand statement about who his own parents, spouse and children are is about as strong as
     non-documentary evidence gets. Keep `confidence: high` unless he actually hedges ("I think", "as
     far as I know") - then translate the hedge to `medium`/`low`, the same rule review-claims applies
     to any other claim.
   - Anything he says that doesn't map to a claim - how he met his spouse, a story about a parent -
     goes to `## Stories` or `## Notes`, same routing rule as `process-source` step 7.
6. **Record the AI pass:**
   ```yaml
   ## AI Passes
   - {date: <today>, model: {your-model-id}, harness: {your-harness},
      task: "draft setup-interview relationship claims from the researcher's own account",
      outputs: [C-…, C-…], human_reviewed: false}
   ```

### 4. Hand off to the gate

Say plainly what happens next - *"That's everything: I've drafted N facts from what you told me, but
they're still proposals until you say yes. Let's go through them."* - then hand off to
[`review-claims`](../review-claims/SKILL.md) for this source, exactly as `process-source` does at its
own Stage C. Don't duplicate its close-out here: the reindex, `fha xref`, the view refresh, and `fha
lint` all belong to that hand-off, not to this skill.

## Guardrails

- Parentage is written **only** as a `relationship` claim with an explicit `roles: {child:, parent:
  […]}` map - never a `birth` claim (#71).
- A spousal bond is written as a `type: marriage` claim with `roles: {spouse: […]}` - the SPEC-correct
  type for a couple, not `type: relationship`.
- Every relationship/marriage claim drafted here is `status: suggested`; this skill **never** writes
  `status: accepted` - not even on facts the informant cannot be wrong about. That judgment belongs to
  `review-claims`, on his explicit word, same as every other claim in this archive.
- Every named person is resolved against the index first (`fha find`) before minting - never a silent
  duplicate of an existing stub or curated record.
- `living:` is set explicitly for every person this skill touches, not left at the `unknown` default -
  his own household is exactly the case he can state with certainty.
- `root_person`/`root_generation` are hand-edited into `fha.yaml` (a plain, documented-editable file) -
  never into `places.yaml` or `.cache/`, which stay off-limits to hand-editing.
- `root_generation: children` is written only when he actually chooses it; the unset default (`self`)
  is left unwritten, matching the template's own convention.
- Any record ID written into prose (the source's `## Notes`, the hand-off sentence) is `[[ ]]`-wrapped,
  `[[ID|Name]]` preferred; bare IDs only inside the claims-block YAML, frontmatter lists, and tool
  arguments (_STANDARD.md §11).
- Runs once per archive in the ordinary case; if `root_person` is already set, this skill confirms with
  him before minting or writing anything else, rather than silently repeating itself.

## Done when

- Running the interview in a session against a freshly `fha install`-ed archive (or an equivalent
  scratch archive built from `archive-template/`) produces: the researcher's own person record with
  `sex:` and `living: true` set; `root_person` (and, when he chose it, `root_generation: children`)
  written into `fha.yaml`; a person record per named family member with `sex:`/`living:` set; one
  `source_type: interview`, `source_class: authored` source whose `people:` links everyone named; a
  `relationship` claim per parent-child edge (never a `birth` claim) and a `marriage` claim per spouse,
  each `status: suggested` with `information: primary`, `evidence: direct`, and an `anchor:`; the AI
  pass recorded in `## AI Passes`; and a hand-off into `review-claims`.
- Nothing reaches `accepted` without the human's own pass through `review-claims`.
- A loosely-answered question ("I don't really know my dad's exact birth year", "skip my mom's side for
  now") degrades gracefully - a skipped fact is simply omitted, never a stall or a refusal.
- Run against a fresh scratch archive, `fha lint` afterward shows only the expected, self-explanatory
  backlog - `W102` for the newly-suggested claims, `W119` for any now-visible direct-line ancestors still
  filed as stubs, and (only if `root_generation` was left at `self` while a child was named) the `W127`
  anchor note - nothing unexpected, and no error.
- `fha lint --root example-archive` is unaffected (`_STANDARD.md` §9 baseline) - this skill is exercised
  against a separate scratch archive, never against `example-archive/` itself.
