---
name: reconcile-site-edits
description: >
  Run when the human says "regenerate the site but keep my manual edits" / "I edited the site's HTML by
  hand, don't lose it" / "fold my page changes back in". `fha site` is deterministic and never reads its
  own output, so a hand-edited HTML file is overwritten on the next build. This skill reads the edited
  HTML, diffs it against a pristine `fha site` baseline to recover the human's intent, folds that intent
  into the correct SOURCE (styling → `design/custom.css`, homepage copy → `notes/home.md`, person prose →
  that person's record, title/hero → `fha.yaml` `site:`), then re-runs the deterministic build so the edit
  survives cleanly. Every source write is human-confirmed first.
---

# reconcile-site-edits

The recovery path for the one thing the source-first model forbids: hand-editing a generated HTML file.
`fha site` compiles archive data plus a few human-editable source files into HTML and **never reads the
generated HTML back** (TOOLING §12; [`docs/SITE_PLAN.md`](../../docs/SITE_PLAN.md)) — which is exactly what
makes regeneration safe and idempotent, and exactly why a hand-edit of the output is doomed on the next
build. This skill does not change that. `fha site` stays deterministic; the fuzzy reconciliation lives
**only here**. The skill reads the human's edited HTML, recovers what he meant, folds it into the right
*source* layer, and lets the deterministic build carry it forward. See [`../_STANDARD.md`](../_STANDARD.md)
and [`docs/CUSTOMIZING_SITE.md`](../../docs/CUSTOMIZING_SITE.md).

## When this runs

Invoked only. Triggers: "regenerate the site keeping my manual edits", "I tweaked the site's HTML — don't
lose it", "make my page change stick", "fold my edits back into the source". It never runs on its own, and
it never runs as part of a routine `fha site` — a clean rebuild has no edits to reconcile.

## The contract for this skill (state it before you start)

- **`fha` stays deterministic; the judgment lives here.** This skill never teaches `fha site` to read its
  output. It reads the edited HTML *itself*, decides intent, and writes source. The generator only ever
  runs in its normal, deterministic mode.
- **Never keep the edit in the HTML.** The generated file is not a source and is not the fix. The fix is
  always a change to a source layer that the next `fha site` reads. Folding the intent into source and
  re-running the build is the only durable outcome; a patched HTML file is not.
- **Every source write is previewed and human-confirmed first.** Reconciliation is inference, and inference
  can be wrong. Show the human the exact source change you propose, in plain language, and apply it only on
  his say-so — one confirmation per change. Silence is not consent (_STANDARD.md §3).
- **Never overwrite human-written text, never edit below a `<!-- GENERATED … -->` header** (_STANDARD.md §3).
  A homepage or person edit is *merged into* existing prose, not pasted over it. If the human's HTML edit
  landed on a `GENERATED` block (e.g. the `fha family-summary` panel or a timeline row), that content is not
  hand-editable — say so and route the real fix to its source (a claim, a record), don't fold it anywhere.
- **Facts still need sources.** If a hand-edit added a *factual* sentence to a person page, it is a claim in
  disguise, not styling. Route it through the normal gate — draft it `suggested` and hand to `review-claims`
  / `write-biography` — never launder an unsourced fact into a record as accepted prose (_STANDARD.md §3.1).
- **Respect privacy on rebuild.** The re-run is a normal `fha site --standalone`, so living/unknown and
  `restricted` redaction still applies; a hero image or embedded photo the human added is subject to the
  same EXIF-stripping and living-co-depiction drop as every other site image (TOOLING §12).

## Flow

### 1. Find the edited output and confirm the intent is real

Ask the human which generated file(s) he changed, or locate the edited site folder. Confirm you are looking
at a *generated* site (an `fha site` output dir), not a source file — reconciliation only makes sense for
hand-edits of the output.

### 2. Rebuild a pristine baseline to diff against

`fha site` is a pure function of the source, so a fresh build **into a scratch folder** reproduces exactly
what the generator last produced from the current sources — without touching the human's edited copy:

```
fha site --out .cache/site-baseline --standalone
```

The human's edited HTML minus this pristine HTML **is** his intent. Diff the edited file against its
baseline twin; the delta is what to reconcile. (If the sources changed since he edited, note that the diff
may also show real data updates — separate those from his hand-edit before folding anything.)

### 3. Classify each delta by the source layer it belongs to

For every change in the diff, decide which source owns it. This is the whole judgment of the skill:

| The hand-edit changed… | It belongs in source layer… |
|---|---|
| A colour, font, spacing, or any CSS / inline style | `design/custom.css` |
| Homepage welcome/intro copy (prose in the home intro region) | `notes/home.md` |
| A person's biography or story prose on their page | that person's curated `people/…` record |
| The masthead archive name, or the homepage hero title / tagline / image | `fha.yaml` `site:` (`site.archive_name`, `site.hero`) |
| A generated fact block (family-summary panel, timeline row, sources index, claims table) | **none — it's `GENERATED`.** The real fix is upstream: a claim, a vital, a record. Say so; don't fold it. |

An edit can split across layers (a restyled *and* reworded homepage). Split it: the colour goes to
`custom.css`, the words go to `notes/home.md`.

### 4. Propose each source change in plain language, and confirm

For each classified delta, show the human the precise source edit you intend — the file, and the before/after
in words a text-editor user understands — and get his yes before writing:

- **Styling → `design/custom.css`.** Translate the inline HTML style into a token override or rule appended
  to `custom.css` (DESIGN.md, "Customizing"): *"You made the links green in the HTML. I'll add
  `:root { --accent: #3e4a3a; }` to `design/custom.css` so every page picks it up. OK?"* Prefer a token
  override to a brittle selector; append, don't rewrite his existing `custom.css`.
- **Homepage copy → `notes/home.md`.** Merge the reworded prose into `home.md`, preserving his existing
  words around it. Photo embeds stay in Obsidian embed form (`![[S-id|caption]]`), not raw `<img>` — the
  build renders the derivative and caption from that (SITE_PLAN.md Phase B).
- **Person prose → the record.** Fold the edited biography/story text into that person's `people/…` file,
  merged around existing prose, never over it. If the edit is a **fact**, stop and route it as a `suggested`
  claim through `review-claims` (or draft it with `write-biography`'s marker discipline) — a factual
  sentence needs a `[[S-…]]` citation, and only the human moves it to accepted.
- **Title / hero → `fha.yaml` `site:`.** Set `site.archive_name`, or `site.hero.title` / `.tagline` /
  `.image`, in the one config file — plain YAML, previewed as the exact lines you'll add.

Apply each confirmed change to source. Leave anything he does not confirm unwritten and say what you skipped.

### 5. Re-run the deterministic build and verify the edit survived

With the intent now in source, rebuild the real site the normal way:

```
fha site --out .cache/site --standalone --dry-run   # preview the rebuild
fha site --out .cache/site --standalone
```

Open the regenerated page and confirm the human's change is present *because it now comes from source* — the
proof that it will survive every future build. Then remove the scratch baseline (`\.cache/site-baseline`).
Tell the human, in plain words, where his edit now lives (which source file) so next time he edits the
source directly, not the HTML.

## Guardrails

- **Never** patch or preserve the generated HTML as the fix — the source is the fix; the HTML is rebuilt.
- **Never** teach or trick `fha site` into reading its own output; the diff-against-baseline reconciliation
  is this skill's alone.
- **Never** write a source change without showing it and getting an explicit yes — one confirmation per
  change, no batch-accept-by-silence.
- **Never** overwrite the human's existing `custom.css`, `home.md`, or record prose — append and merge
  around it — and **never** edit below a `GENERATED` header; route a "fix" that landed on a generated block
  to its upstream record instead.
- **Never** fold an unsourced *fact* from a hand-edit straight into a record as accepted prose — it goes
  through the claim gate like any other fact.
- Respect privacy on the rebuild: it is a standalone snapshot, so redaction, EXIF-stripping, and
  living-co-depiction drops all still apply to anything the human added.

## Done when

- A hand-edited generated HTML page is reconciled by diffing it against a pristine `fha site` baseline,
  classifying each delta to its source layer, and — on the human's per-change confirmation — folding it into
  `design/custom.css` / `notes/home.md` / the person record / `fha.yaml` `site:`, with the generated HTML
  itself left to be rebuilt, never patched.
- A re-run of `fha site` reproduces the human's edit **from source**, proving it now survives regeneration;
  the human is told which source file now holds it.
- Any factual hand-edit is routed through the claim gate (`suggested` → `review-claims`), never written as
  accepted fact; no human prose is overwritten and nothing below a `GENERATED` header is touched.
- `fha lint --root example-archive` still exits 1 with only the documented baseline warnings
  (`_STANDARD.md` §9) — the skill introduced nothing new.
