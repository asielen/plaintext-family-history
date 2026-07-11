---
name: photo-context
description: >
  Run only when the human explicitly asks about a photo's story or caption — "what do we
  know about this photo?", "add context to this photo", "caption these", "update this
  photo's caption with what we now know". Gathers what the archive knows (who's in it and
  how they're related, the event, the place's history), drafts a better summary, and — only
  after the human approves the exact text — writes it as an AI-marked caption via
  `fha photoindex set-summary`. Human-written captions are never overwritten, only added to.
  Never runs automatically, never in bulk.
---

# photo-context

The archive often knows more about a photo than its caption says — who's in it and how they
relate, what event it shows, what the place was like at the time. This skill turns that
accumulated knowledge into a better embedded caption: gather what's known, draft a plain
sentence, get the human's exact-text approval, then write it through the one deterministic
verb built to touch a photo's embedded metadata safely. See [`../_STANDARD.md`](../_STANDARD.md);
this skill is `_STANDARD.md` §6's own worked example of stopping at a missing capability until
the core verb shipped — see [`DESIGN.md`](DESIGN.md) for that history.

## When this runs

Invoked-only — never automatic, never bulk. Triggers: "what do we know about this photo?",
"add context to this photo", "caption these", "update this photo's caption with what we now
know", "refresh the summary on the 1895 portrait." One photo (or a small, explicitly named
batch) per request — this writes into original photo files, so it is never a background pass
and never runs over a whole folder or triage list.

## The contract for this skill

- **Invoked-only, never bulk.** No silent or scheduled runs, no "while I'm here" recaptioning —
  only an explicit, named request, one photo or a small named batch at a time.
- **The human approves the exact text before anything is written** (the house human-gate,
  `_STANDARD.md` §3): show the drafted caption verbatim next to the current one and wait for an
  explicit yes / edit / no — silence is never consent. The write verb's own preview-and-prompt
  comes after, as the mechanical gate, never as a substitute for this one.
- **The write path is always the deterministic tool, never hand-rolled** (`_STANDARD.md` §6 —
  this skill is that rule's own worked example): no shelling any other program, no scripted
  metadata edit of any kind. `fha photoindex set-summary` is AI-marked by construction and can
  never displace human-written text — the skill states that guarantee, the verb enforces it.
- **Record the AI pass** (`_STANDARD.md` §3.3 / SPEC §14) on the photo's source record when one
  exists. An unprocessed photo has no record to write the pass on — say so, and name the
  embedded `AI:` marker as its provenance instead.
- **Respect `living`/`restricted`** in drafted caption text the same way biography prose does —
  a photo caption is embedded metadata that travels with a family photo that may circulate.

## Flow

1. **Locate the photo (or group).**
   ```
   fha photoindex find --text "…"        # or --person P-… / --keyword …
   ```
   Read the printed path, date, and current caption straight from the catalog — never open the
   file itself. If the index warns it's stale, freshen it first (the write verb hard-blocks on a
   stale index anyway, so catching it here saves a round trip):
   ```
   fha photoindex
   ```

2. **Gather the context — all through `fha`, never by bulk-reading the photos tree.**
   - **Who's in it, and the event, when the photo is processed** (it carries an `S-id`):
     ```
     fha find --related S-…
     ```
     prints the source's linked people (name and `P-id`, drawn from both its claims and its
     `people:` list) and any places tied to its claims — one call covers "who" and "what."
   - **How two people relate**, so the draft can say "her father" instead of two bare names:
     ```
     fha relate P-a P-b
     ```
   - **The place's background:**
     ```
     fha find --related L-…
     ```
     for who else and what claims tie to that place. For the place's dated `history:` (what it
     was called or governed by at the time) read that entry in the place record directly — it
     isn't part of the index, so this is the one direct read in the flow, and it's one small
     record, not a tree scan.
   - **When the photo is unprocessed** (no `S-id` yet), there's no source record to query — rely
     on who the human names in the request and resolve each name the way `process-source` does:
     `fha find "<name>"`, present candidates on any ambiguity, never a silent guess.

3. **Draft the summary and show it verbatim.** Plain sentence(s), built only from what the
   archive actually holds — accepted claims and recorded relationships; phrase anything
   speculative as such, or leave it out. Put the drafted text next to the current caption and
   **wait for an explicit yes / edit / no** — this is the house human-gate; silence is never
   consent. Whatever the human approves, edited or not, becomes the exact text to write.

4. **Preview, then write.**
   ```
   fha photoindex set-summary --group <S-id-or-group> --text "<approved text>" --dry-run
   ```
   Address by the source's `S-id` via `--group` when the photo is processed — the verb accepts a
   bare `S-…` as shorthand and writes every variant (front, back, copy) consistently. Otherwise
   use the literal `<path>` `find` printed, to write that one file only. The dry-run shows old →
   new per file and flags whether existing human text is being kept; nothing is written yet. On
   the human's go-ahead, run the same command without `--dry-run` — the tool's own `[y/N]` prompt
   is the final, mechanical gate. Use `--append` only when the human wants a prior AI note kept
   alongside the new one rather than replaced.

5. **Record the pass.** If the photo's group has a source record (`S-id` present), append to
   that source's `## AI Passes` block:
   ```yaml
   ## AI Passes
   - {date: 2026-07-01, model: {your-model-id}, harness: {your-harness},
      # use your real model/harness identifiers - these two are placeholders, not values to copy
      task: "photo-context caption", outputs: [UserComment], human_reviewed: true}
   ```
   then reindex just that source (a source edit makes the main index stale) and refresh the photo
   catalog too — `photoindex_status` watches every `sources/photos/*.md` mtime, so this same edit
   also makes `.cache/photos.sqlite` look stale, and the very next `fha photoindex find` or
   `set-summary` in this session would warn or refuse on that staleness otherwise:
   ```
   fha index --source S-…
   fha photoindex
   ```
   If the photo is **unprocessed** — no source record exists to write the pass on — say so
   plainly: the embedded `AI:` marker the write just added is the in-file provenance (SPEC §20
   rule 5). Suggest `fha process` when the photo looks evidence-worthy (a dated scene, named
   people, something worth its own source record) — that's a hand-off to `process-source`, not
   something this skill does itself. (No `sources/photos/*.md` was touched, so no stale-cache
   follow-up is needed here.)

6. **Close out.** The embedded write itself needs no rescan — the write verb patches the catalog
   in the same call, so `fha find` / `fha photoindex find` already see the new text. Step 5's
   source-record edit is the one thing that does make the catalog look stale, and it's already
   handled there. Run `fha lint` only when step 5 touched a source record (the done-gate,
   `_STANDARD.md` §8). End by naming the next step — "want me to do the other farm photos one at
   a time?"

## Guardrails

- **Never** hand-write embedded metadata or shell any other program to do it — every write goes
  through `fha photoindex set-summary` (this skill is `_STANDARD.md` §6's own worked example of
  that rule).
- **Never** run over a folder or a triage list — one photo, or a small, explicitly named batch,
  per human request.
- **Never** write a caption the human hasn't read verbatim and approved.
- **Never** touch a human-caption field (`Caption-Abstract` / `XMP-dc:Description`) — the verb
  enforces this; the skill promises it.
- **Working-copy mode:** the write verb refuses cleanly there (the photo files live on the main
  machine) — say so plainly rather than retrying.

## Done when

- "What do we know about this photo?" gathers via `photoindex find` + `fha relate` +
  `fha find --related` and answers with **zero writes**.
- "Add context to this photo" shows a drafted caption verbatim; whatever the human approves —
  edited or not — is the exact text both the `--dry-run` preview and the live write use.
- The written comment is AI-marked, and any pre-existing human comment text survives byte-for-byte
  above the AI block.
- The AI pass lands on the photo's source record when one exists, `fha index --source S-…` is
  rerun, and `fha lint --root example-archive` still exits 1 with only the documented baseline
  warnings (`_STANDARD.md` §9).
- Declining at the human-approval step, or at the tool's own `[y/N]` prompt, ends the session
  with nothing written anywhere.
