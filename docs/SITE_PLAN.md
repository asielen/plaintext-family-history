# Site plan — homepage, navigation, and customization roadmap

The roadmap for making the generated site feel like *this* family's site — a real
homepage, unified navigation, and a small, well-chosen set of places a human can
customize — without ever giving up the one property that makes `fha site` safe to
run a thousand times: it is **deterministic and idempotent**.

This is a planning document, not a task list. The *visual language* is
[`DESIGN.md`](DESIGN.md); the *generator mechanics* are [`TOOLING.md`](../TOOLING.md)
§12; the *human-facing how-to* is [`CUSTOMIZING_SITE.md`](CUSTOMIZING_SITE.md). This
doc is the plan for the customization layer that sits on top of all three: what the
source-first model is, which layers a human edits, and the phases that build them
out. Cite the section numbers here when proposing structural changes.

---

## The principle: source-first, deterministic output

`fha site` compiles two things into HTML:

1. **Archive data** — the `.cache/index.sqlite` query surface (claims, relationships,
   vitals, sources, citations, place references) plus the prose read straight from the
   curated person `.md` files (TOOLING §12, "Data source"). The site is exactly as
   fresh as the last `fha index`.
2. **A few human-editable SOURCE files** — the customization layers below.

From those inputs it renders the whole site. The rule that everything else depends on:

> **`fha site` never reads generated HTML.** The output is a pure function of the
> source. Same inputs → same site, every time.

That is why the site is a *snapshot, not a live view* (TOOLING §12, "Modularity:
regenerating is idempotent"), and it is why **you customize by editing SOURCE, never
the generated HTML.** Hand-editing a file under the output folder is futile — the next
`fha site` overwrites it, because the generator rebuilds from source and never consults
what was there before. This is not a limitation to work around; it is the guarantee.
A deterministic build is one you can re-run without fear: after any archive change,
`fha site` reproduces the whole site correctly, and no manual patch is silently lost or
silently preserved-and-stale.

Everything in this plan protects that guarantee. Each customization layer is a
**source** the generator reads on the way in — never a diff applied to the output on
the way out.

---

## The customization layers

Five layers, ordered from "most people never touch it" to "the escape hatch." Each is
an input to the deterministic build.

| Layer | Where | What it customizes | Who edits it | Status |
|---|---|---|---|---|
| **(a) Data & records** | `sources/`, `people/`, `places/` | The facts, prose, portraits, and relationships the site renders | Human + AI, through the normal research loop | shipped |
| **(b) Styling** | `design/custom.css` | Colours, fonts, spacing, any CSS — the whole look, from one file | A CSS-literate human | shipped |
| **(c) Homepage intro** | `notes/home.md` | The welcome prose at the top of the homepage — the family's own words | Human + AI, markdown | planned (Phase A) |
| **(d) Titles & hero** | `fha.yaml` `site:` sub-section | The archive name on the masthead and the homepage hero (title, tagline, image) | Human, plain YAML | planned (Phase A) |
| **(e) AI reconciliation** | the `reconcile-site-edits` skill | Folds an accidental hand-edit of generated HTML *back into the right source above* | The assistant, on request | planned (Phase E) |

### (a) Data & records — the substance

The site is mostly a rendering of the archive itself. The best way to change what the
site says is to improve the records: accept a claim, write a biography paragraph, set a
person's `profile_photo`, add a place's `history:`. None of this is "site work" — it is
the ordinary research loop, and the site reflects it on the next `fha index` +
`fha site`. This layer is the reason the site needs so few *other* knobs.

### (b) `design/custom.css` — the look

Already shipped and already the right seam (DESIGN.md, "Customizing"). Generated pages
link `styles.css` then `custom.css` last, so a CSS-literate human restyles the entire
archive — every page, the tree, the exports — from one file without touching a template
or a line of Python. Most restyles are a few token overrides (`--paper`, `--accent`,
`--font-serif`). This layer needs no new work; the plan only points people at it.

### (c) `notes/home.md` — the homepage intro

A markdown file created in every archive from the template with friendly default
boilerplate, so a brand-new archive's homepage already reads as a welcome, not a blank.
The human (or the assistant, with the human's blessing) rewrites it into the family's
own introduction: who this archive is for, where the family is from, what a visitor is
looking at. It is prose, so it supports the **photo embeds** of Phase B
(`![[S-id|caption]]`). `fha site` reads it and renders it into the homepage's intro
region — it is *source*, never generated, so the generator never overwrites the human's
words.

### (d) `fha.yaml` `site:` sub-section — titles and hero

Today the masthead name is the top-level `archive_name:` key (DESIGN.md, "Customizing";
defaults to "Family History Archive"). The plan groups site-scoped settings under a
`site:` sub-section so the one config file stays the single place to set them:

```yaml
# fha.yaml (planned shape — Phase A builds this)
site:
  archive_name: "The Hartley Family Archive"   # the masthead name for the site
  hero:
    title: "The Hartley Family"
    tagline: "Six generations in Breton County, Ohio — 1798 to today"
    image: S-ea61339378          # an S-id (or photo path) resolved through the photo index
```

Plain YAML a non-technical human can edit in Notepad. `site.archive_name` names the
masthead; `site.hero` supplies the homepage hero band (a title, a one-line tagline, and
an optional lead image run through the same EXIF-stripped derivative + privacy pipeline
as every other site image, so a hero photo that co-depicts a living person is dropped
from the standalone snapshot, never leaked). Absent keys fall back to sensible defaults
— an archive that sets nothing still gets a coherent homepage.

### (e) AI reconciliation — the escape hatch

The one layer that exists *because* humans will occasionally do the thing the model
forbids: open a generated `.html` file and edit it directly. That edit is doomed on the
next build. Rather than lecture, the plan provides a skill (Phase E,
`reconcile-site-edits`) that reads the hand-edited HTML, diffs it against a pristine
`fha site` baseline to isolate the human's intent, and **folds that intent into the
correct source layer above** — styling to `custom.css`, homepage copy to `notes/home.md`,
person prose to that person's record, a title/hero change to `fha.yaml` — then re-runs
the deterministic build so the edit survives cleanly. The fuzzy reconciliation lives
**only** in the skill; `fha site` stays deterministic and never learns to read its own
output. See DELIVERABLE-2 of this effort and the skill's own `SKILL.md`.

---

## Phases

Build order, each phase standing on its own. A phase is done when its source layer
exists, the generator reads it, and a fresh archive gets a coherent default without any
hand-configuration.

### Phase A — Homepage + unified navigation

The centrepiece. Turn the current landing page into a real homepage and give every page
a consistent way back to it.

- **Hero band** from `fha.yaml` `site.hero` (layer d) — the family name, a tagline, an
  optional lead image.
- **Intro prose** from `notes/home.md` (layer c) — the family's welcome, rendered below
  the hero.
- **A generated `fha family-summary` block** — a deterministic at-a-glance panel
  (people counted, surnames, the span of years the archive covers, the apex ancestor,
  a recent-discoveries teaser). Generated from the index like every other fact section;
  it is *not* hand-edited (it carries a `GENERATED` header and is rebuilt each run).
- **Get-home-everywhere** — the masthead links to the homepage from every generated
  page, so the site navigates as one site rather than a pile of pages. Fully relative
  hrefs (works from `file://` and a USB stick, per TOOLING §12).

Existing home elements — the interactive descendant explorer, the surname A–Z index,
the discoveries teaser (TOOLING §12, "Pages: Home") — stay; Phase A frames them with the
hero, the intro, and the summary.

### Phase B — Photo embeds in prose

Let a human place a specific photo inline in `notes/home.md` (and, later, any curated
prose) with **Obsidian embed syntax**:

```
![[S-ea61339378|Margaret and the children on the porch, about 1901]]
```

`fha site` resolves the `S-id` (or a photo path) through the photo index, renders the
same EXIF-stripped, privacy-filtered derivative used everywhere else, and uses the text
after the pipe as the caption. A living-person co-depiction is dropped from the
standalone snapshot exactly as in the photo strip — the embed obeys the same redaction
rules, so prose can never become a privacy leak. This is the same wikilink family the
archive already uses (`[[S-id]]` for citations, `[[P-id]]` for people); the leading `!`
is the Obsidian "embed, don't link" marker.

### Phase C — Person-page mini family strip

A small, legible family strip at the head of each person page: **parents, siblings, and
children only** — the immediate family a reader wants at a glance, not the whole tree.
Rendered server-side from the `relationships` edges (no JavaScript), redaction-aware
(living/unknown relatives render as "Living Person" with no link, same as everywhere).
Deliberately scoped tight: the full pedigree and descendant explorer already live in the
interactive trees (TOOLING §12); this is the quick-orientation strip, not a second tree.

### Phase D — Large-tree pan/zoom

The interactive trees work but get unwieldy on a big line. Add pan/zoom (and sensible
collapse defaults) to the vendored renderer so a thousand-person descendant explorer is
navigable. This stays within the existing "borrow the engine" seam (TOOLING §12, "Tree
rendering"): the change is confined to the vendored renderer + its adapter
(`tools/templates/vendor/`), never the data contract. No new dependency, no CDN, no
build step — the offline-safe rule (DESIGN.md, "Do / don't") holds.

### Phase E — Edit-aware skill + docs

Ship the reconciliation escape hatch (layer e): the `reconcile-site-edits` skill and the
human-facing [`CUSTOMIZING_SITE.md`](CUSTOMIZING_SITE.md) guide. This phase adds no
capability to `fha` and changes nothing about determinism; it teaches the source-first
model and provides the one graceful recovery path for a hand-edited HTML file. (This
document and its two siblings are the front edge of Phase E.)

---

## Backlog — deliberately not now

Called out explicitly so a future session does not mistake silence for permission.

### Multiple custom pages — a possible `notes/narrative/` folder

`notes/home.md` is the first custom prose page. The obvious next want is *more* of them:
a `notes/narrative/` folder of "book chapters" — a migration story, a war letter, a
family-recipe page — with `home.md` as the main chapter and the site building a small
table of contents across them. This is a natural extension of the same source-first
model (each chapter is a markdown source the generator renders; photo embeds already
work in prose), so **leave the door open** — but do **not** build it now. It multiplies
navigation, ordering, and privacy-scoping questions (does a chapter naming a living
person get redacted? how are chapters ordered? do they appear in the standalone
snapshot?) that Phases A–E do not have to answer. Ship the single homepage first; let
real use tell us whether the chapter folder earns its complexity.

### What this is NOT: a CMS

The line this plan will not cross. Plaintext is a **family-history archive that can
render a site**, not a content-management system. We are not adding: a page builder,
themes beyond `custom.css`, arbitrary user-defined page types, WYSIWYG editing, plugins,
a database of "content," or a live server. Every customization stays a *plain source
file a human can read and edit in a text editor* — CSS, markdown, YAML — and the site
stays a deterministic, offline, no-build-step snapshot of the archive. When a proposed
feature starts to look like "let the user assemble arbitrary pages from components,"
that is the signal we are drifting toward a CMS, and the answer is no. The archive's
durability comes from staying plain files; the site must not be the thing that erodes it.
