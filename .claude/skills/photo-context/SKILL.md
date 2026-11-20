---
name: photo-context
description: >
  Run only when the human explicitly asks about a photo's story or caption: "what do we
  know about this photo?", "add context to this photo", "caption these", "update this
  photo's caption with what we now know". Gathers what the archive knows (who's in it and
  how they're related, the event, the place's history), drafts a better summary, and, only
  after the human approves the exact text, writes it as an AI-marked caption via
  `fha photoindex set-summary`. Human-written captions are never overwritten, only added to.
  Never runs automatically, never in bulk.
---

# photo-context

The archive often knows more about a photo than its caption says: who's in it and how they relate, what event it shows, what the place was like at the time. This skill turns that knowledge into a better embedded caption. It gathers what's known, drafts a plain sentence, gets the human's approval of the exact text, then writes it through `fha photoindex set-summary`, the one deterministic verb built to change a photo's embedded caption safely. See [`../_STANDARD.md`](../_STANDARD.md).

## When this runs

Invoked only, never automatically or in bulk. Triggers: "what do we know about this photo?", "add context to this photo", "caption these", "update this photo's caption with what we now know", "refresh the summary on the 1895 portrait." One photo (or a small, explicitly named batch) per request. This writes into original photo files, so it never runs in the background or over a whole folder or triage list.

## The contract for this skill

- **Invoked only, never bulk.** No silent or scheduled runs and no "while I'm here" recaptioning: only an explicit, named request, one photo or a small named batch at a time.
- **The human approves the exact text before anything is written** (`_STANDARD.md` §3): show the drafted caption verbatim next to the current one and wait for an explicit yes, edit or no. Silence is never consent. The write verb's own preview and prompt come after, as the mechanical gate, and never replace this one.
- **The write path is always the deterministic tool** (`_STANDARD.md` §6): no shelling any other program and no scripted metadata edit of any kind. `fha photoindex set-summary` marks its text as AI-written and can't displace human-written text. The skill states that guarantee; the verb enforces it.
- **Record the AI pass** (`_STANDARD.md` §3.3 / SPEC §14) on the photo's source record when one exists. An unprocessed photo has no record to write the pass on, so say so and name the embedded `AI:` marker as its provenance instead.
- **Respect `living`/`restricted`** in drafted caption text the same way biography prose does. A photo caption is embedded in a family photo that may be passed around.

## Flow

1. **Locate the photo (or group).**
   ```
   fha photoindex find --text "…"        # or --person p-… / --keyword …
   ```
   Read the printed path, date, and current caption from the catalog; never open the file itself. The "caption" `find` prints is the human-written field (`Caption-Abstract`/`Description`) only. It never shows an AI summary an earlier `set-summary` run wrote, because that lives in a separate embedded field `find` doesn't print. Step 4's `--dry-run` preview is where that field shows up (its "old → new" comparison). If it reveals a prior AI note that conflicts with the draft the human approved in step 3, stop and get a fresh yes, edit or no on the full picture before writing; step 3's approval doesn't cover text the human never saw. If the index warns it's stale, refresh it first (the write verb refuses on a stale index anyway, so catching it here saves a round trip):
   ```
   fha photoindex
   ```

2. **Gather the context, all through `fha`, never by bulk-reading the photos tree.**
   - **Who's in it, and the event, when the photo is processed** (it carries an `s-id`):
     ```
     fha find --related s-…
     ```
     prints the source's linked people (name and `p-id`, drawn from both its claims and its `people:` list) and any places tied to its claims. One call covers "who" and "what."
   - **How two people relate**, so the draft can say "her father" instead of two bare names:
     ```
     fha relate p-a p-b
     ```
   - **The place's background:**
     ```
     fha find --related l-…
     ```
     for who else and what claims tie to that place. For the place's dated `history:` (what it was called or governed by at the time), read that entry in the place record directly. It isn't part of the index, so this is the one direct read in the flow, and it's one small record rather than a tree scan.
   - **When the photo is unprocessed** (no `s-id` yet), there's no source record to query. Rely on who the human names in the request and resolve each name the way `process-source` does: `fha find "<name>"`, and present candidates whenever it's ambiguous rather than guessing.

3. **Draft the summary and show it verbatim.** Write plain sentences built only from what the archive holds: accepted claims and recorded relationships. Phrase anything speculative as such, or leave it out. Put the drafted text next to the current caption and **wait for an explicit yes, edit or no**; silence is never consent. Whatever the human approves, edited or not, is the exact text to write.

4. **Preview, then write.**
   ```
   fha photoindex set-summary --group <s-id-or-group> --text "<approved text>" --dry-run
   ```
   When the photo is processed, address it by the source's `s-id` via `--group`. The verb accepts a bare `s-…` as shorthand and writes every variant (front, back, copy) the same way. Otherwise use the literal `<path>` `find` printed, to write that one file only. The dry-run shows old → new per file and says whether existing human text is kept; nothing is written yet. On the human's go-ahead, run the same command without `--dry-run`. The tool's own `[y/N]` prompt is the final, mechanical gate. Use `--append` only when the human wants a prior AI note kept alongside the new one rather than replaced.

5. **Record the pass.** If the photo's group has a source record (`s-id` present), append to that source's `ai_passes:` frontmatter list:
   ```yaml
   ai_passes:
     - {date: 2026-07-01, by: {your-model-id}, task: caption, note: "photo-context summary, approved text"}
     # `by` is your real model identifier; the braces are a placeholder to fill in
   ```
   Then reindex just that source (a source edit makes the main index stale) and refresh the photo catalog too. `photoindex_status` watches every `sources/photos/*.md` mtime, so this edit also makes `.cache/photos.sqlite` look stale, and the next `fha photoindex find` or `set-summary` in this session would warn or refuse otherwise:
   ```
   fha index --source s-…
   fha photoindex
   ```
   If the photo is **unprocessed** (no source record to write the pass on), say so plainly: the embedded `AI:` marker the write just added is the provenance inside the file (SPEC §20 rule 5). Suggest `fha process` when the photo looks like evidence (a dated scene, named people, something that deserves its own source record). That's a hand-off to `process-source`, not something this skill does. (No `sources/photos/*.md` was touched, so no catalog refresh is needed here.)

6. **Close out.** The embedded write itself needs no rescan: the write verb updates the catalog in the same call, so `fha find` / `fha photoindex find` already see the new text. Step 5's source-record edit is the one thing that makes the catalog look stale, and step 5 handles it. Run `fha lint` only when step 5 touched a source record (the done-gate, `_STANDARD.md` §8). End by naming the next step: "Want me to do the other farm photos one at a time?"

## Guardrails

- **Never** hand-write embedded metadata or shell any other program to do it. Every write goes through `fha photoindex set-summary` (`_STANDARD.md` §6).
- **Never** run over a folder or a triage list: one photo, or a small, explicitly named batch, per human request.
- **Never** put a record ID in caption text. Embedded caption prose carries plain names only, with no `s-…`/`p-…` ids and no `[[ ]]` links (_STANDARD.md §11: family members read that text as-is wherever the photo goes).
- **Never** write a caption the human hasn't read verbatim and approved.
- **Never** touch a human-caption field (`Caption-Abstract` / `XMP-dc:Description`). The verb enforces this; the skill promises it.
- **Working-copy mode:** the write verb refuses there (the photo files live on the main computer). Say so plainly rather than retrying.
- A drafted caption keeps each paragraph on one line, with a blank line between paragraphs: never hard-wrapped at a column and never one sentence per line (_STANDARD.md §4).

## Done when

- "What do we know about this photo?" gathers via `photoindex find` + `fha relate` + `fha find --related` and answers with **zero writes**.
- "Add context to this photo" shows a drafted caption verbatim. Whatever the human approves, edited or not, is the exact text both the `--dry-run` preview and the live write use.
- The written comment is AI-marked, and any existing human comment text survives byte-for-byte above the AI block.
- The AI pass lands on the photo's source record when one exists, `fha index --source s-…` is rerun, and `fha lint --root example-archive` still exits 1 with only the documented baseline warnings (`_STANDARD.md` §9).
- Declining at the human-approval step, or at the tool's own `[y/N]` prompt, ends the session with nothing written anywhere.
