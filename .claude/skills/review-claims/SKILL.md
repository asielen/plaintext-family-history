---
name: review-claims
description: >
  Run when the human says "review the census claims" / "review this source" / "let's go through what you
  drafted", or right after `process-source` or `mine-transcript` hands off. Walks one source's `suggested`
  claims — guided one-by-one, in numbered batches, or self-serve — shows each in plain language with its
  evidence context and its source file link, captures the human's accept/dispute/edit decision, and writes
  it with `fha claim` (grouped same-status decisions as one batch write). Closes with a reindex, an
  `fha xref` pass, a timeline/sources-index/draft-queue refresh for the people touched, and `fha lint`.
  This is the human gate: it never accepts a claim on the human's behalf.
---

# review-claims

Stage C of the pipeline — the **only** path a drafted claim takes to `accepted`. The deterministic half
already exists: `fha claim` moves a claim's status and stamps `reviewed:` — one claim at a time, or
several C-ids in one status-only batch. The judgment this skill adds is *presentation and capture* —
showing each suggested claim with its evidence so the human can decide quickly, and turning his stated
decision into the right tool call. Every skill that ends in review (`process-source`, `mine-transcript`)
hands off here, so this gate is the reused interaction. See [`../_STANDARD.md`](../_STANDARD.md).

## When this runs

"Review the census claims", "review this source", "let's go through the Hartley notes", or automatically
as the last stage of `process-source` / `mine-transcript`. Always scoped to **one source at a time** — a
source's claims are reviewed together because they share evidence. The one exception to
whole-source scope: a GEDCOM-import source (`fha gedcom import`) can hold thousands of claims and is
reviewed **person-by-person or family-by-family** ("review the Hartleys"), never front-to-back — filter
its claims to the people asked about and leave the rest for later sessions.

## The contract for this skill (state it before you start)

- **The human is the only gate to `accepted`.** The skill presents; the human decides. **Never** set
  `status: accepted` without an explicit decision the human stated *this session* — no accepting on his
  behalf, no inferring a yes from silence, no batch-accepting "the obvious ones." Grouping is
  **presentation and capture, never judgment**: a batched reply ("accept 1–4") states one decision per
  numbered claim, and a claim his reply doesn't cover stays `suggested`.
- **`accepted` is written only through `fha claim <C-id> [<C-id> …] --status accepted`,** which stamps
  `reviewed:` (lint E006 fails on an accepted claim with no `reviewed:` date). Directing that tool *is*
  his accept — one C-id or several, the decision behind each is his.
- **Translate, don't quiz** (_STANDARD.md §4): show each claim as a sentence with its evidence, not as a
  YAML blob; never say "C-id" or "EDTF" at him without a plain gloss.

## Flow

1. **Locate the source, list its suggested claims, and hand him the evidence itself.**
   ```
   fha find <S-id>        # record path, asset files, claim counts by status
   ```
   (If the human named the source in words — "the 1880 census" — resolve it with `fha find <text>` first,
   then confirm you've got the right one.) Then **relay the paths** so he can look at the actual evidence
   while you talk: the source record's path, and each asset file's on-disk path, as clickable paths in
   plain words — "the census scan is at `documents/census/1880-census-hartley.jpg` — open it alongside
   and we'll go through what it says." He should never have to review a claim about a document he can't
   see. Open the source `.md` and read its `## Claims` block; the ones with `status: suggested` are the
   backlog.

2. **Offer the three review styles, let him choose** (AGENTS.md §"Review claims with the human"):
   - **Guided, one-by-one** — you walk each claim in turn (the default; best for a handful, or when he
     wants your read on the evidence).
   - **Batched** — you present the claims in **numbered groups of about five, grouped by person**, each
     claim as a one-line sentence plus its anchor, a confidence flag when the evidence is shaky, and its
     source link (step 3). He replies with one grouped decision — "accept 1–4, edit 5: the date was June
     1923" — and because every number is one claim, each claim still receives an **individually stated
     human decision**: the gate holds, the typing shrinks. **Offer Batched proactively** whenever a
     source has more than about six suggested claims; guided pacing on a forty-claim census is a chore,
     not care.
   - **Self-serve skim** — you open the source file and let him skim the whole `## Claims` block himself,
     then tell you the decisions. Offer this for a long backlog or when he'd rather drive.

3. **Every claim shown, in ANY style, carries its evidence link — never blind.** Present, in plain
   language:
   - the claim as a **sentence** ("Thomas Hartley, occupation bookkeeper, Plains Junction Railroad,
     about 1880"),
   - its **evidence context**: the `anchor:` (the exact spot in the source — a page, a line, a
     timestamp) and, where the source is a transcript or note, the quoted span it was drawn from,
   - its **source link**: the anchored source file path — the asset file the claim's anchor points into
     when you can identify it, else the record `.md` (`fha find <C-id>` prints `source: <path>:<line>`)
     — followed by the `[[S-id]]` token in parentheses. Path first, token second: the path is what he
     clicks, the token is what the archive greps. Example: "`documents/census/1880-census-hartley.jpg`,
     page 2, line 31 (`[[S-fa1234567b]]`)".
   - the **source, date, and place** as written,
   - the **Mills fields** in plain terms when they matter ("this is *secondary* evidence — inferred from
     the age column, not a birth record"), so a shaky inference reads as shaky.

   Then ask for his decision: **accept / dispute / edit / reject / park** — plus any claim he wants to
   *add* by hand that the draft missed.

4. **Write each decision with `fha claim` (preview, then apply).**
   - **A grouped same-status decision** ("accept 1–4", "reject all three of those") is written with the
     batch form — one preview for the whole group, then one apply:
     ```
     fha claim C-a C-b C-c --status accepted --dry-run
     fha claim C-a C-b C-c --status accepted
     ```
     (stamps `reviewed:` on each; any of the five statuses batches the same way — batch-reject and
     batch-needs-review are as legitimate as batch-accept). The tool validates every id before writing
     anything, so a mistyped id refuses the whole batch cleanly.
   - **A single accept:**
     ```
     fha claim <C-id> --status accepted --dry-run
     fha claim <C-id> --status accepted
     ```
   - **Edit then accept** — correct a value or date in his words, translating to stored form yourself
     ("he says it was really June 1923" → `--date 1923-06`). A field edit is **always an individual
     `fha claim <C-id> …` call** — the tool refuses field flags on a batch by design, because a
     correction states a new fact about one particular claim. **Preview first** — an edited value/date
     must never land stamped `reviewed:` before the human has seen exactly what will be written:
     ```
     fha claim <C-id> --status accepted --value "…" --date 1923-06 --dry-run
     fha claim <C-id> --status accepted --value "…" --date 1923-06
     ```
   - **Dispute** (keep it, mark it contested): `--status disputed` — same `--dry-run`, then apply.
   - **Reject** (wrong, but preserve the trail — never delete): `--status rejected` — preview, then apply.
   - **Park** (not sure yet): `--status needs-review` leaves it for later without accepting (preview
     first too). When he parks a claim, **offer — never auto-write — to record what would settle it**:
     "want me to jot down what would settle this, so it resurfaces when that record shows up?" Only on
     his explicit yes, write one of:
     - an **open question**, hand-written per SPEC §17's shape — an `## Q:` heading naming the question;
       `- origin: human`; `- status: open`; `- refs: [C-id]` (single brackets, comma-separated for
       several - e.g. `- refs: [C-abc1234def, C-...]`; the `[[C-…]]` double-bracket form is for
       PROSE links only, and in this structured field the report's question parser stops at the
       first `]` and normalizes it to an invalid ref); a dated context line noting what was
       parked and why — into the person's `_research` file when the question is person-specific, else
       `notes/questions.md`. If a same-question block already exists, **append a context line to it**
       rather than writing a twin;
     - or, when what he voiced is a testable belief rather than a question, a **hypothesis** with a
       `verify:` line ("what evidence would settle it") in the person's research file.
     No yes, no write — a parked claim with no note is a fine outcome too.
   - **A manual addition** he dictates is drafted into the source's `## Claims` as a new `status: suggested`
     claim — write the **full claim shape** `process-source` uses, not just an id: a fresh `id:`
     (`fha id mint C`), `type:`, `persons:`, `value:`, `confidence:`, the Mills `information:`/`evidence:`
     fields, and an `anchor:` to where in the source it comes from. `confidence:` in particular is required
     on every claim, so an id-only draft would fail lint. Then review it like the rest — it does **not** go
     straight to `accepted`.

5. **Close out the batch.**
   If a **`death` claim was accepted** this session for a person whose `living:` is still `true` or
   `unknown`, offer the flag flip before anything else — "mark them as no longer living, so exports can
   include them? → `fha person set-living <P-id> false`" — and run it **only on his explicit yes**. The
   flag is a privacy judgment and nothing ever flips it automatically; the tool's own output states the
   export consequence.
   ```
   fha index                      # full rebuild — if this pass minted new people/places (a
                                  # process-source / mine-transcript hand-off usually does), `--source`
                                  # reindexes only the source's claims, NOT new person/place records or
                                  # their aliases (index.py upsert_source), so xref / find --related would
                                  # run on stale person data. Reserve `fha index --source <S-id>` for a
                                  # status-only pass that created no people or places.
   fha xref                       # surface new corroboration / contradiction across sources
   ```
   If `fha xref` proposes a link, present it plainly ("this now agrees with the 1871 marriage notice —
   want to record that they corroborate?") and act on his pick:
   ```
   fha confirm xref <C-a> <C-b> --as corroborates --dry-run   # preview first (writes both sources)
   fha confirm xref <C-a> <C-b> --as corroborates             # or: --as contradicts
   ```
   A `--as contradicts` confirm automatically spawns the open question that keeps lint **E009** satisfied
   ("a `contradicts:` link with no open question") — you don't hand-write that question.
   If you confirmed **any** xref link, **reindex again** before the view refresh below — `fha confirm xref`
   writes the `corroborates:`/`contradicts:` links into both sources but does not reindex, so `claim_links`
   (read by `fha find --related`, xref dedup, and the report's corroboration/discovery sections) would
   otherwise stay stale for the rest of the session:
   ```
   fha index
   ```

6. **Refresh the touched people's views — quietly, without asking.** The session just changed exactly
   what the generated views show: an accepted claim leaves the timeline's "unreviewed" tail and joins the
   draft-queue's writing backlog. For every **curated** person named in a claim decided this session
   (stubs carry no companion views — SPEC §16 — skip them):
   ```
   fha views timeline <P-id>
   fha views sources-index <P-id>   # the source list gains the just-reviewed source's evidence
   fha views draft-queue <P-id>
   ```
   Refresh only the people touched — never `fha views refresh` here: it regenerates *every* curated
   person's views and churns their dated GENERATED headers into git noise. (A successful view write
   exits `0` and prints a "run `fha index` when convenient" nudge — advice, not a warning.) If a
   `relationship` claim was accepted, also run `fha views brackets` (report mode) and relay anything it
   flags in plain words; applying `--fix` renames folders and moves person files, so that stays the
   human's explicit call.

7. **Finish with the done-gate and report it plainly.**
   ```
   fha lint
   ```
   Translate the result: "All good — three facts accepted, the census now agrees with the marriage
   notice, nothing left flagged." If lint flags something, name the fix in plain words (_STANDARD.md §4),
   don't paste the code.

   Then, with the index fresh from step 5, check for **two nudges — promotion and places — each capped at
   one per session, same explicit-yes gate, never repeated once declined this session.**

   **Promotion**, for **direct-line** people only (the promote verb serves the direct line; curating
   anyone else is an open design decision): is any person decided-on this session flagged by the
   `fha views brackets` run above as a **direct-line stub**, or a direct-line stub now holding accepted
   claims at or over the promotion threshold — `fha.yaml`'s `promotion:` → `claims_threshold`, default 5
   when the key is absent (read the file directly)? If so, say one plain nudge: "Frank S. Woodbury now has
   9 accepted claims and no curated profile — want me to promote him and draft a bio?" **Only on his
   explicit yes**, run `fha person promote <P-id>` and hand off to `write-biography`. A NON-direct person
   who crosses the threshold gets an FYI, never the offer: "Frank keeps turning up — 5 accepted claims
   now — but he sits off the direct line, so a real profile isn't wired up for people off it yet — that's
   a known gap (issue #80), not a permanent no." No yes, no write, no nagging — one nudge per person per
   session, then let it rest.

   **Places.** A review pass that just accepted a batch of claims is precisely the moment a place-text
   cluster crosses its threshold — check for it here rather than leaving it for a session that never comes
   back to it (issue #81):
   ```
   fha places candidates   # ranked unlinked place-text clusters (default threshold 3), sorted largest first
   ```
   That threshold (3) is `fha places candidates`' own bar for "worth surfacing at all" — the same one
   `place-research` and report §6b already use. This nudge asks a stricter question — "worth interrupting
   the close-out for" — so only act when the single **largest** returned cluster is at **10 or more
   claims** (`process-source`'s same offer bar, §"Resolve places…"); a smaller cluster is a real candidate
   but not this nudge's business, and stays for `place-research`/a later session instead. At or over 10,
   name that one cluster only — not the whole list, same one-thing-at-a-time restraint as the promotion
   nudge: "'San Diego, California' now appears in 22 claims and isn't a registered place yet — want me to
   register it?" **Only on his explicit yes**, register it the way `place-research` does (never hand-write
   `places.yaml`):
   ```
   fha confirm place <C-id> <C-id> … --name "…" --hierarchy "…" --dry-run
   fha confirm place <C-id> <C-id> … --name "…" --hierarchy "…"
   ```
   (the cluster's own `claim_ids` list, printed by `fha places candidates`, is the id list to pass). No
   yes, no write — one nudge per place per session, then let it rest, same as promotion.

## Guardrails

- **Never** `--status accepted` without an explicit human decision recorded in the session. If he didn't
  say yes to *this* claim, it stays `suggested`. A batched reply counts exactly as far as its numbers
  reach — "accept 1–4" decides claims 1 through 4 and nothing else.
- Every accepted claim carries `reviewed:` — that's `fha claim`'s job; never hand-edit a status in the
  file.
- Every claim presented — in any style — carries its source link (the anchored file path, then the
  `[[S-id]]` token). A claim shown without its evidence path is a claim shown blind.
- **Rejected ≠ deleted** — prefer `--status rejected`/`superseded` and keep the claim; the research trail
  matters (AGENTS.md §"Don'ts").
- A contradiction always ends with an open question (E009-clean) — let `fha confirm xref … --as
  contradicts` spawn it.
- The park offer, the promotion nudge, and the place nudge are **offers**: they write (`## Q:` block,
  hypothesis) or run (`fha person promote`, `fha confirm place`) only on the human's explicit yes, never
  on silence or a hedge.
- The place nudge names at most **one** cluster (the largest) and fires at most once per session, same
  cap as promotion — never every returned cluster, never repeated once declined.
- Record no separate `## AI Passes` entry here *unless* you drafted a new claim in this session (a manual
  addition you formatted) — plain acceptance of existing drafts is the human's pass, not the AI's.
- Any record ID you write into prose (a note, a spawned question, a story) is `[[ ]]`-wrapped,
  `[[ID|Name]]` preferred; bare IDs belong only inside claims-block YAML fields and tool arguments
  (_STANDARD.md §11).

## Done when

- Walking a source's suggested claims in a session on `example-archive` produces **one stated human
  decision per claim** and the matching writes: **one `fha claim` write per individual decision, one
  batch `fha claim C-a C-b … --status X` write per grouped same-status decision** (field edits always
  one call per claim), a reindex (full `fha index` when the pass minted new people, else `--source`), an
  `fha xref` pass, a `fha views timeline` + `sources-index` + `draft-queue` refresh for each curated
  person touched, and a final `fha lint`.
- **Every claim presented carried its source link** — the anchored file path first, the `[[S-id]]` token
  in parentheses — in whichever review style the session used.
- **No** claim reaches `accepted` without an explicit human decision in the transcript; every accepted
  claim carries a `reviewed:` date (post-run `fha lint` shows no **E006**).
- A contradiction surfaced by xref ends in `fha confirm xref … --as contradicts`, leaving the archive
  **E009**-clean.
- The park offer (an SPEC §17 `## Q:` block or a `verify:` hypothesis), the promotion nudge
  (`fha person promote` + `write-biography` hand-off), and the place nudge (`fha confirm place`, when the
  session's `fha places candidates` top cluster is at or over 10 claims) fired **only on explicit yeses**
  — no yes, no write, no run.
- `fha lint --root example-archive` still exits 1 with only the documented baseline warnings
  (`_STANDARD.md` §9).
