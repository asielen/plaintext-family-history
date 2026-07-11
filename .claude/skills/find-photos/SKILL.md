---
name: find-photos
description: >
  Run when the human asks to see photos — "show me grandma's photos", "which photos have
  Margaret in them?", "any pictures from the 1950s?", "do we have photos of the farm?",
  "which photos haven't we worked through yet?". Resolves the person or topic, answers from
  the photo index in plain language (one line per physical photo, fronts/backs/copies counted
  once), and offers a clickable gallery page of the results. Read-only: it never writes to
  photo files; identifying a person hands off to `fha photoindex tag-person` and its own
  yes/no prompt.
---

# find-photos

The missing front door to the photo library: "show me grandma's photos" should just be answered,
not left to sit because no skill owns the photo index. This skill orchestrates the deterministic
photo surfaces (`fha find`, `fha photoindex`, `fha photoindex find`/`triage`/`gallery`) and adds
only the judgment a tool can't have — resolving "grandma" to a person, choosing the right filter,
narrating groups of photos in plain words. See [`../_STANDARD.md`](../_STANDARD.md).

## When this runs

Any photo-shaped ask: "show me photos of X", "pictures of grandma", "which photos have X in
them", "any photos from the 1950s / the war / Suwałki?", "do we have any photos of the farm?",
"which photos haven't we worked through yet?", "who's in this photo?" It is safe to run anytime —
answering a photo question never requires a write.

## The contract for this skill

- **Read-only.** This skill drafts no claims, edits no records, and **never writes to photo
  files itself.**
- **The one adjacent write is a hand-off, not this skill's write.** When the human identifies
  someone mid-conversation ("that's definitely Margaret"), the skill runs
  `fha photoindex tag-person` — but that tool's own preview-and-`[y/N]` prompt is the gate. The
  skill runs the command; the human answers the prompt. It never pre-answers it.
- **No AI-pass entry.** Nothing here is written to any source record, so there is nothing to
  record in `## AI Passes`.
- **Privacy.** Results are spoken to the archive's owner in-session; the gallery page is a
  private research artifact (unredacted, for the owner's own machine) — nothing produced here is
  publication output.

## Flow

1. **Resolve who or what.**
   - A person-shaped ask → `fha find "<name as spoken>"` to land on a P-id. If more than one
     person matches, ask **one** plain question — "two Margarets: the one born 1849 or her
     granddaughter?" — never a refusal.
   - A time-shaped ask → translate to EDTF yourself, never quiz the human on it: "the 1950s" →
     `195X`, "around 1912" → `1912~`.
   - A topic-shaped ask ("the farm") → a `--keyword` or `--text` term.

2. **Freshen the index if needed.** Run the query first. If `fha photoindex find` warns the
   index is stale, say so in one plain clause and refresh it:
   ```
   fha photoindex
   ```
   This is incremental (only new or changed files get rescanned) — seconds, not a rebuild — then
   re-run the query. If the index is absent entirely, run that same scan once; in working-copy
   mode, say plainly that the photo files live on the main machine instead of attempting a scan.
   Never leave the human staring at a raw "WARNING: photo index is stale" — narrate it, then move
   past it.

3. **Answer in words first.**
   ```
   fha photoindex find --person P-… [--edtf …] [--keyword …] [--text …]
   ```
   Filters combine (AND together); the result is already one row per physical photo, fronts,
   backs, and copies folded into one group. Narrate the count, the date spread, a few highlights
   (captioned ones first), and how many have backs or copies. Never dump raw paths unless asked,
   and never reach for `--files` unless the human asks about one specific photo's versions.

4. **Offer the gallery.** "Want a clickable page of all 64? I'll build it." On yes:
   ```
   fha photoindex gallery --person P-… [--edtf …] [--keyword …] [--text …] [--out FILE]
   ```
   It lands under `generated/gallery/` — the exact filename depends on the filters, so relay the
   absolute path and the `file://` link the command **prints** rather than predicting the name.
   Tell the human **exactly where it is** — that printed path and link — and offer to open it (the
   command itself never auto-opens anything). If the gallery calls out a "verify these" section, mention it
   plainly: "9 of these matched by name only — the page lists them at the bottom if you want to
   confirm they're really her."

5. **Sidelines, on request.**
   - "Which photos should we work through next?" →
     ```
     fha photoindex triage --top 5
     ```
     Narrate the ranked candidates, with `fha process` named as the next step — that's a
     `process-source` hand-off, not something this skill does itself.
   - "Tell me about this photo" (a processed one) →
     ```
     fha find --related <S-id>
     ```
     for its source context.
   - "That's definitely Margaret in this one" → hand off the identification:
     ```
     fha photoindex tag-person P-… --paths <path> --dry-run
     fha photoindex tag-person P-… --paths <path>
     ```
     Preview first, then the live run — its `[y/N]` prompt is the human's to answer, not this
     skill's.

6. **Close out.** Name one concrete next step — usually the gallery, the triage queue, or a
   tag-person confirmation. There is nothing to reindex or lint: this skill wrote nothing.

## Guardrails

- **Never** shell `exiftool` and **never** bulk-read the `photos/` tree — photo questions are
  `fha` calls; ten thousand photos should cost zero context.
- **Never** write a keyword, caption, or file directly. `tag-person` is the only adjacent write,
  and its interactive prompt belongs to the human. Caption *writing* belongs to the
  `photo-context` skill — if the human asks to fix a caption or summary, hand off there.
- **Working-copy mode:** queries still answer from the copied cache, but say plainly that the
  photo files (and any gallery) live on the main machine — don't attempt a scan or a gallery
  build there; both refuse by design.
- **Speak groups, not variants.** "One photo with three scans" is one photo — never surface raw
  per-file rows unless the human specifically asks about one photo's versions.
- **Never** hand the human a raw exit code or the word "EDTF" without a plain gloss and an
  example.

## Done when

- Each of the four trigger shapes (person / date / topic / triage) produces a useful
  plain-language answer in a session on `example-archive`, with **zero archive writes**.
- A stale photo index is freshened silently-but-narrated (one clause), never surfaced as a raw
  warning.
- "Any pictures from the 1950s?" maps to `--edtf 195X` without quizzing the human about EDTF.
- The gallery offer runs `fha photoindex gallery` and reports the exact file location (the
  absolute path and the `file://` link).
- A "that's her" identification goes through `tag-person`'s dry-run preview, then its live
  `[y/N]` prompt; declining the prompt ends with nothing written.
- `fha lint --root example-archive` still exits 1 with only the documented baseline warnings
  (`_STANDARD.md` §9) — nothing this skill did introduces a new one.
