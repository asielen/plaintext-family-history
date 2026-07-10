---
name: review-claims
description: >
  Run when the human says "review the census claims" / "review this source" / "let's go through what you
  drafted", or right after `process-source` or `mine-transcript` hands off. Walks one source's `suggested`
  claims, shows each in plain language with its evidence context, captures the human's accept/dispute/edit
  decision, and writes it with `fha claim`. Closes with a reindex, an `fha xref` pass, a
  timeline/sources-index/draft-queue refresh for the people touched, and `fha lint`. This is the human gate: it never
  accepts a claim on the human's behalf.
---

# review-claims

Stage C of the pipeline — the **only** path a drafted claim takes to `accepted`. The deterministic half
already exists: `fha claim` moves a claim's status and stamps `reviewed:`. The judgment this skill adds is
*presentation and capture* — showing each suggested claim with its evidence so the human can decide
quickly, and turning his stated decision into the right tool call. Every skill that ends in review
(`process-source`, `mine-transcript`) hands off here, so this gate is the reused interaction. See
[`../_STANDARD.md`](../_STANDARD.md).

## When this runs

"Review the census claims", "review this source", "let's go through the Hartley notes", or automatically
as the last stage of `process-source` / `mine-transcript`. Always scoped to **one source at a time** — a
source's claims are reviewed together because they share evidence.

## The contract for this skill (state it before you start)

- **The human is the only gate to `accepted`.** The skill presents; the human decides. **Never** set
  `status: accepted` without an explicit decision the human stated *this session* — no accepting on his
  behalf, no inferring a yes from silence, no batch-accepting "the obvious ones."
- **`accepted` is written only through `fha claim <C-id> --status accepted`,** which stamps `reviewed:`
  (lint E006 fails on an accepted claim with no `reviewed:` date). Directing that tool *is* his accept.
- **Translate, don't quiz** (_STANDARD.md §4): show each claim as a sentence with its evidence, not as a
  YAML blob; never say "C-id" or "EDTF" at him without a plain gloss.

## Flow

1. **Locate the source and list its suggested claims.**
   ```
   fha find <S-id>        # record path, asset files, claim counts by status
   ```
   (If the human named the source in words — "the 1880 census" — resolve it with `fha find <text>` first,
   then confirm you've got the right one.) Open the source `.md` and read its `## Claims` block; the ones
   with `status: suggested` are the backlog.

2. **Offer both review styles, let him choose** (AGENTS.md §"Review claims with the human"):
   - **Guided, one-by-one** — you walk each claim in turn (the default; best for a handful, or when he
     wants your read on the evidence).
   - **Self-serve skim** — you open the source file and let him skim the whole `## Claims` block himself,
     then tell you the decisions. Offer this for a long backlog or when he'd rather drive.

3. **For each claim, show it grounded — never blind.** Present, in plain language:
   - the claim as a **sentence** ("Thomas Hartley, occupation bookkeeper, Plains Junction Railroad,
     about 1880"),
   - its **evidence context**: the `anchor:` (the exact spot in the source — a page, a line, a
     timestamp) and, where the source is a transcript or note, the quoted span it was drawn from,
   - the **source, date, and place** as written,
   - the **Mills fields** in plain terms when they matter ("this is *secondary* evidence — inferred from
     the age column, not a birth record"), so a shaky inference reads as shaky.

   Then ask for his decision: **accept / dispute / edit / reject** — plus any claim he wants to *add* by
   hand that the draft missed.

4. **Write each decision with `fha claim` (preview, then apply).**
   - **Accept:**
     ```
     fha claim <C-id> --status accepted --dry-run
     fha claim <C-id> --status accepted
     ```
     (stamps `reviewed:` today automatically).
   - **Edit then accept** — correct a value or date in his words, translating to stored form yourself
     ("he says it was really June 1923" → `--date 1923-06`). **Preview first** — an edited value/date must
     never land stamped `reviewed:` before the human has seen exactly what will be written:
     ```
     fha claim <C-id> --status accepted --value "…" --date 1923-06 --dry-run
     fha claim <C-id> --status accepted --value "…" --date 1923-06
     ```
   - **Dispute** (keep it, mark it contested): `--status disputed` — same `--dry-run`, then apply.
   - **Reject** (wrong, but preserve the trail — never delete): `--status rejected` — preview, then apply.
   - **Not sure yet:** `--status needs-review` leaves it for later without accepting (preview first too).
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

## Guardrails

- **Never** `--status accepted` without an explicit human decision recorded in the session. If he didn't
  say yes to *this* claim, it stays `suggested`.
- Every accepted claim carries `reviewed:` — that's `fha claim`'s job; never hand-edit a status in the
  file.
- **Rejected ≠ deleted** — prefer `--status rejected`/`superseded` and keep the claim; the research trail
  matters (AGENTS.md §"Don'ts").
- A contradiction always ends with an open question (E009-clean) — let `fha confirm xref … --as
  contradicts` spawn it.
- Record no separate `## AI Passes` entry here *unless* you drafted a new claim in this session (a manual
  addition you formatted) — plain acceptance of existing drafts is the human's pass, not the AI's.

## Done when

- Walking a source's suggested claims in a session on `example-archive` produces **one `fha claim` write
  per decision**, a reindex (full `fha index` when the pass minted new people, else `--source`), an `fha xref` pass, a `fha views timeline` +
  `sources-index` + `draft-queue` refresh for each curated person touched, and a final `fha lint`.
- **No** claim reaches `accepted` without an explicit human decision in the transcript; every accepted
  claim carries a `reviewed:` date (post-run `fha lint` shows no **E006**).
- A contradiction surfaced by xref ends in `fha confirm xref … --as contradicts`, leaving the archive
  **E009**-clean.
- `fha lint --root example-archive` still exits 1 with only the documented baseline warnings
  (`_STANDARD.md` §9).
