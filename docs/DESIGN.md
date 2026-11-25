# Design guide

The visual language for everything the archive renders as HTML: the generated site (person, source, place, index and discoveries pages), the family tree, and exported reports and packets. This document explains *what the language is* and *why*, so any change can be checked against it.

The canonical implementation is `design/styles.css`. This document describes it; the stylesheet is the source of truth. A live specimen of every rule is `design/design-system.html`; open it in a browser to see the guide rendered.

Where those two files sit depends on which copy you're in. In the project repo they're at `design/` off the root. In an **installed archive** the design package is vendored with the rest of the machinery, so they're at `.fha/design/styles.css` and `.fha/design/design-system.html`. (Your own overrides go in `.fha/design/custom.css`, which no update touches; see Customizing below.)

---

## The reference

**A printed family register or county record book.** Every decision traces to that object: a text serif set for reading, oldstyle figures for dates, tabular figures and horizontal-ruled tables like a ledger, a thick and thin double rule under the masthead, square corners, and ink-brown accents rather than decorative color.

The test for any new element is whether a real record book or archival finding aid would do it. If not, it needs a better reason than "looks nice." The styling stays quiet so the records are what a reader looks at.

The palette is earthy, because the owner's reference material is earth-toned, but the surfaces are flat and the typography is a document's: few colors, no elevation.

---

## Where it lives, and how pages use it

```
design/
  styles.css          the whole system, the source of truth
  custom.css          local overrides (linked last; wins)
  fonts/              self-hosted woff2 (Fraunces + Literata, latin + latin-ext)
  design-system.html  the rendered specimen / reference
```

Generated pages link the stylesheet, then the override:

```html
<link rel="stylesheet" href="{root}/assets/styles.css">
<link rel="stylesheet" href="{root}/assets/custom.css">
```

`custom.css` comes last on purpose: a CSS-literate user restyles the whole archive from that one file without touching templates or code. Because pages link one shared stylesheet, there is a single place to change anything.

Portable single-file exports (e.g. a research packet meant to be emailed) inline the same `styles.css` at build time rather than linking it, so the linked and inlined copies cannot diverge.

Everything works offline: self-hosted fonts, no CDN, no build step, no JavaScript framework.

---

## Tokens

All visual decisions are CSS custom properties in the `:root` block at the top of `styles.css`. Override them in `custom.css`; don't edit the stylesheet. The values below are the current defaults, and `styles.css` is authoritative if they drift.

### Paper and ink

| Token | Value | Role |
|---|---|---|
| `--paper` | `#F2EDE2` | page ground (toned register paper) |
| `--surface` | `#F8F5EE` | faintly raised areas |
| `--surface-sunken` | `#E8E0D0` | wells, code blocks |
| `--ink` | `#2A2420` | primary text (iron-gall near-black) |
| `--ink-soft` | `#574E42` | secondary text, heavy rules |
| `--muted` | `#6E6656` | meta text |
| `--rule` | `#D8D0BF` | hairlines |
| `--rule-strong` | `#B9AD98` | emphasized hairlines |

### Accent

`--accent: #6E3A26` (deep sienna/oxblood) is the one warm ink. It carries links (`--link`), emphasis, the social-edge lines and the generation numerals. **It is the only accent in the main interface**; don't add a second.

### Earth family, reserved for the tree

`--clay --sage --ochre --rose --navy --crimson --warm-grey`. These exist for the seven tree branch categories (`--branch-1..7`) and nothing else. They are *not* a general-purpose palette; using them across the UI would break the record-book look.

### Status inks (text only, no fills)

`--status-accepted-fg` (green) · `--status-review-fg` (ochre-brown) · `--status-suggested-fg` (navy) · `--status-rejected-fg` (muted). Statuses are words in these inks, not tinted pills.

### Type, spacing, shape

- Type: `--font-serif` (Literata, body and headings), `--font-display` (Fraunces, masthead and names only), `--font-sans` (system stack, small labels and chrome), `--font-mono`. Scale `--text-xs … --text-3xl`.
- Spacing: `--space-1 … --space-8` on a 4px base.
- Shape: `--radius-sm/md` are `2px` (effectively square). `--measure` (~68ch) and `--content-max` (54rem) hold the reading line length.

---

## Typography rules

- **Body and headings are Literata**, a text serif, so documents read as documents.
- **Fraunces appears only in the masthead and person names.** `h2`/`h3` are Literata, differentiated by size and rule.
- **Hierarchy comes from one large size plus rules**, not five evenly spaced steps. `--text-3xl` (the page title or masthead) is the single dominant size; `h2` carries a heavy bottom rule; everything else stays small.
- **Figures are deliberate.** Oldstyle figures in running prose (`font-variant-numeric: oldstyle-nums`, set on `body`); tabular lining figures in tables and indexes so dates and counts align. Dates are the core content, so the numerals get as much care as the text face.
- **No surname capitals.** Printed genealogies often set surnames in small caps; this archive does not (an explicit choice). Names render in normal case.
- Small **uppercase letterspaced labels** are allowed for structural chrome only (table headers, the `.u-label` utility, research-note titles), never for names.

---

## Component conventions

Flat, square and document-like: no drop shadows, no rounded cards, no pills. Separation comes from rules and whitespace, not elevation.

- **Masthead** (`.site-header`): a thick and thin **double rule** sits under the archive name (Fraunces), as on a record-book title page. Every generated page carries it.
- **Tables** (`table.claims`): ruled like a **ledger**. Horizontal rules only, no vertical borders, no zebra fill, a heavier rule under the header row, the header set in a small uppercase sans label, tabular figures.
- **Statuses** (`.status-*`): typographic, a leading dot and the state's word in the state's ink. Only the `-fg` tokens exist; there are no fills.
- **Summaries** (`.summary`): ruled top and bottom, transparent background, like a record summary rather than a card.
- **Citations** (`blockquote.citation`): a hanging italic quote with a normal-case `<cite>` source line. No box, no left bar.
- **Research notes** (`.callout`): a hairline top rule with a small uppercase label, like a margin note rather than a tinted panel.
- **Subjects and keywords** (`.tag`): italic index terms separated by middots (`census · Ohio · Hartley line`), not bordered lozenges.
- **Buttons** (`.btn`): the one place app vocabulary appears, because they're interactive. Kept square and plain; primary uses ink, not the accent color.
- **Source citations** (`.fn-ref` + `ol.footnotes`): the reading view never shows a backend id. A cited source is a small superscript number (`.fn-ref`) that jumps to a numbered Sources list at the foot of the page (`ol.footnotes`), where each source is named and linked. Repeated citations of one source share a number. Withheld sources collapse to a **single shared "Restricted" footnote** (their count and identity never leak, and the label never repeats inline). A source the author named in prose (`[[s-id|text]]`) stays a plain link; a claim reference (`[c-id]`) cites its backing source; a dangling id renders nothing.
- **Stub references** (`.stub-ref`): a person named but without a page of their own gets a dotted underline (no link), matching the tree's dotted stub node. A person with a page is an ordinary link.

Citations, callouts and tree nodes carry no colored left border; each has its own treatment, so one motif doesn't tie unrelated things together. Colored left edges are kept for marks that carry a meaning: the pedigree cards' paternal or maternal tint and the subject card (`.ped-node.ped-branch-1/2`, `.ped-node.ped-self`; see Register patterns), the not-for-publication banner on single-file views (`.view-banner` in `design/view.css`), and the workbench's warning callout (`.wb-warn-callout` in `tools/templates/workbench/workbench.css`).

---

## Register patterns

These are the details specific to genealogy rather than reusable for any brand:

- **Double-rule masthead**: see above.
- **Generation numerals** (`.gen-num`): a large oldstyle numeral (Ahnentafel or generation number) set in the left margin of a person record, in the accent ink. A person page's own family-tree number is the small `.ahnentafel-no` tag in the relationship line (`.kin-line`) under the name.
- **Dotted-leader indexes** (`ul.leaders`): surname and place indexes with dotted leaders and tabular counts, like the back of a printed register.
- **Portraits**: a person's chosen `profile_photo` (SPEC person field) sits at the head of their record (`.person-portrait`, floated right) and appears as the small tree square. It is a filename, path or s-id resolved through the photo index and run through the same derivative and privacy pipeline as the photo strip (a portrait that also shows a living person is dropped from the shared snapshot). With no photo, a monogram placeholder appears, never a broken image.
- **Lifespan chart** (`.lifespan-chart`, `.lifespan-bar`, `.lifespan-cap`, `.lifespan-mark`, `.lifespan-grid`, `.lifespan-tick`, `.lifespan-name`, `.lifespan-role`, `.lifespan-note`, `.lifespan-hatch`, `.lifespan-key`): "Who overlapped" on a person page, a static SVG with one bar per person on a shared year axis. The solid `.lifespan-bar` runs from the latest possible birth to the earliest possible death; a `.lifespan-cap` over an approximate or unrecorded end is hatched and dashed, so it reads in black and white. The subject's bar takes `--accent`, everyone else `--ink-soft`, and `.lifespan-mark` draws the subject's birth and death as dotted vertical lines. Retint it in `custom.css`; the chart sets no inline colour.
- **Ancestor fan chart** (`.fan-chart`, `.fan-seg`, `.fan-label`, `.fan-years`): a static, print-friendly pedigree fan on each person page, five generations deep, with the subject at the hub and ancestors fanning up a 180° semicircle by generation (`site.fan_degrees` can open it to 270° or a full 360° circle for a poster: on the lower half the curved text runs the other way so every name stays upright, a half-circle parent cell reads level, and the hub becomes a sector or a whole disk with the given name over the surname). Each segment's fill is a grandparent-line branch colour lightened outward by generation, set inline as `--seg-color` and `--gen-fade` and composed in the stylesheet, so `custom.css` can retint the whole chart. A ring whose arc is long enough keeps its labels curved along the ring: the name on one line, or given names and surname on two, with the years (`1840-1941`, `b. 1840`) on a smaller `.fan-years` line under it. A qualified date keeps its meaning in short form, on the fan and the family chart alike: `bef. 1920`, `aft. 1920`, `abt. 1850`, `1850s` for a decade, `1918–1920` for a range (with a range on either side the line spells out `b. 1918–1920, d. 1990`, so a range never reads as a birth and a death). A long years line shrinks toward the 8px floor before it is left off; the tooltip always has it. The outer rings, where a curved line would be too short, are thicker and run the name and years outward along the radius, upright on both sides. Each line is sized to fit its own cell, and that size ships as an SVG `font-size` attribute on the text. To change the default size, set a font-size on `.fan-chart` (it is inherited, and a label's own attribute still wins). Never set it on `.fan-label` or `.fan-years`, which would flatten every label to one size and clip the long names. An ancestor who fills two places (cousins married) gets `.fan-seg-repeat` on both cells: the same `--branch-N` colour (numbered by lowest slot), a dashed ink edge and a lettered `.fan-repeat-mark` badge, so colour is never the only signal; the tooltip adds "Also no. 12 on this chart", and the higher slot adds where their ancestors are drawn. Cells on the straight father's-father or mother's-mother line carry `.fan-line-paternal` / `.fan-line-maternal` (only when each step's sex is recorded), and three `.fan-mode` radio buttons placed before `.chart-body` fade the rest with no script (hidden in print, which keeps the chosen view). In a shared site a living adult's cell is a neutral `.fan-seg-private` reading "Private" (`.fan-label-private`), with no tooltip or link. Rendered server-side as inline SVG, so it prints cleanly; once opened, `fha-tree.js`'s `wrapStatic()` adds pan and zoom.
- **Heritage chart** (`.heritage-chart`, `.heritage-seg`, `.heritage-seg-unknown`, `.heritage-seg-untraced`, `.heritage-legend`): "Where the family came from" on a person page, one 100% bar per generation with a segment per country. A country's colour is a `--branch-1..7` token set inline as `--seg-color` and lightened in the stylesheet; "Birthplace unknown" is a plain sunken segment and "Not traced" a dotted outline, so colour is never the only cue. Wide segments carry their own name, every segment a tooltip, and the legend lists each country with its count. Static inline SVG. The idea follows WikiTree's heritage page; nothing is copied.
- **Pedigree and family chart** (`.pedigree`, `.ped-node`, `.ped-link`): a static, server-rendered SVG box chart (children | subject + spouse(s) | ancestor generations, stepping right). The same engine draws it on every curated person's page and, at a deeper configurable depth (`.pedigree-home`), as the centre of the home page. An unresearched ancestor slot with a known child draws a faint dashed `.ped-empty` "Unknown" placeholder; in a shared site a living adult ancestor draws a `.tree-card-private` card reading "Private" (no name, dates, link or line tint) and a living person not known to be an adult is left out. The home chart also tints each ancestor card's left edge by paternal (`.ped-branch-1`) or maternal (`.ped-branch-2`) line, using the same `--branch-1`/`--branch-2` tokens as the fan chart and family tree, recovered from Ahnentafel parity with no extra data. It shows the hub's own siblings (`.ped-sibling`) stacked above the subject in one lightly dotted `.ped-link-sibling` group. No JavaScript is required; `fha-tree.js`'s `wrapStatic()` adds the family tree's pan and zoom viewport on top for a deep chart.

These patterns are where the design is allowed to be bold; everything else stays plain so they stand out.

---

## The family tree

Nodes read like **index entries**, not app cards: square, a bottom hairline, the name in Fraunces with the **branch category shown as a colored underline** (the only place the earth tones appear in the UI). The branch color is driven by a `data-branch="1".."7"` attribute mapping to `--branch-1..7`, so the whole categorical scheme lives in one place.

Each node (200×64) also carries a **small fixed-size portrait square** (`.fha-portrait`, 38px) to the left of the name: the person's `profile_photo` if set, otherwise a **monogram placeholder** (`.fha-portrait-empty`, their initial in Fraunces). The portrait size is locked so it never changes the card geometry; the name **wraps to two lines** (clamped) so ordinary full names fit without truncating. A stub (no page) gets a dotted name underline. See Portraits under Register patterns.

**Edge kinds** are told apart by **dash pattern** (legible in print and for colorblind viewers), not by color alone:

| Kind | Line | Meaning | Class |
|---|---|---|---|
| Genetic | solid | parent and child by blood | `.fha-tree-edge` |
| Legal | long dash | adoptive / step / foster / guardian / in-law | `.fha-tree-edge-legal` |
| Other | dotted | friend / coworker / associate (affiliations) | `.fha-tree-edge-other` |

A tie rated low is drawn thin and dash-dot (`.fha-tree-edge-weak`), and a person linked to the subject only through one is faded to about half strength with a dotted outline (`.fha-node-weak` on the explorer, `.ped-weak` on the family chart, `.fan-seg-weak` on the fan). The broken outline is there so the cue does not rest on fading alone. A key line (`.fha-weak-key`, `.fan-key`) under the chart says what it means.

**Couples.** In the explorer a husband and wife stand side by side, joined by a short line (`.fha-tree-couple`); their children hang from the middle of it. A person's later marriages sit to their right, each spouse joined to them by a dotted line (`.fha-tree-couple-later`, the explorer's version of the family chart's `ped-link-later`). A person met a second time is a smaller dashed card (`.fha-node-repeat`) whose button (`.fha-repeat-link`) reads "Already in the tree: go there"; a husband or wife also drawn elsewhere carries the same link (`.fha-also-shown`). On the shared site a living adult is a `.fha-node-private` card: "Private" in small capitals, a blank portrait, no line colour.

The stylesheet also keeps `.fha-tree-edge-social` as an alias for `-legal`. The legal/other split is a drawing distinction: the data model (SPEC §12.2) numbers only genetic parent edges into the pedigree and marks the rest, and "other" overlaps the affiliations model.

---

## Print

Every site page has print styles (`styles.css` section 10), so a relative can print a page and get the page. The menu, search box and zoom buttons are left off the paper, and so are the workbench's bar and its edit and add links. Charts and lists folded shut on screen are opened for the printer (the calendar's whole year prints under its "All months" heading), and a chart zoomed or dragged on screen prints whole (`vendor/fha-site.js` does both, then puts the page back). Colours that carry meaning, such as the branch colours on the family chart, print. The interactive descendant explorer is screen-only, because it draws itself only when opened on screen. A line at the foot says which site the page was printed from. Single-file views (`view.css`) print on white with the private banner kept, so a printed copy left on a table still says it is private.

Each chart has "Save as a picture (PNG)" and "Save as a drawing (SVG)" under it (`vendor/fha-save-chart.js`). Pictures of people are left out of the saved file, and so are links and page data; a private card stays "Private". The saved chart is the whole chart, not the zoomed view. The buttons are left off the paper.

## Accessibility floor

Do not regress these:

- **AA contrast** for every text token on paper. Re-measure after any palette change rather than judging by eye. Current values: `--accent`/`--link` ≈ 7.8:1, `--muted` ≈ 4.8:1, `--ink` far above.
- **Distinguish by more than color**: tree edges use dash patterns, and statuses carry a word as well as a hue.
- `:focus-visible` styling, the `.visually-hidden` utility and `font-display: swap` stay.
- Self-hosted fonts, latin **and** latin-ext subsets, so European names keep their diacritics (Wróblewska, Şerban, Ólafur). Any replacement face must be fetched, subset to the same unicode ranges, and dropped into `design/fonts/` the same way.

---

## Customizing

Put overrides in `custom.css` (`.fha/design/custom.css` in an installed archive; linked after `styles.css`, so it wins). Most restyles are a few tokens:

```css
:root {
  --paper:  #f4f1ea;                                /* cooler paper        */
  --accent: #3e4a3a;                                /* bottle-green links  */
  --font-serif: "Iowan Old Style", Georgia, serif;  /* your own text face  */
}

h2 { border-bottom-width: 3px; }                    /* reach past tokens if needed */
```

**Dark mode** and an optional **paper grain** are both off by default; `styles.css` marks where to enable each.

The archive's **name** (masthead and page titles) is not a CSS concern. Set it in `fha.yaml` under `site:` as `archive_name:` (a top-level `archive_name:` is also read); it defaults to "Family History Archive". `docs/CUSTOMIZING_SITE.md` covers the other `site:` settings.

---

## Do and don't

**Do:** keep it flat and square; use one accent; reserve the earth tones for tree branches; set dates with intentional figures; let rules and whitespace do the separating; name things plainly.

**Don't:** add gradients, shadows or rounded pills; introduce a second UI accent; spread the branch colors across the interface; set surnames in capitals; add icon libraries, web fonts from a CDN, JavaScript frameworks or a build step; or write copy that describes the design's own "warmth."
