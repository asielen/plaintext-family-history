# Design guide

The visual language for everything the archive renders as HTML — the generated
site (person, source, place, index, discoveries pages), the family tree, and
exported reports and packets. This is a reference, not a task list: it explains
*what the language is* and *why*, so any change can be checked against it.

The canonical implementation is [`design/styles.css`](../design/styles.css). This
document describes it; the stylesheet is the source of truth. A live specimen of
every rule is [`design/design-system.html`](../design/design-system.html) — open
it in a browser to see the guide rendered.

---

## The reference

**A printed family register / county record book.** Every decision traces to that
artifact: a text serif set for reading, oldstyle figures for dates, tabular
figures and horizontal-ruled tables like a ledger, a thick–thin double rule under
the masthead, square corners, and ink-brown accents rather than decorative color.

The test for any new element: *does a real record book or archival finding aid do
this?* If not, it needs a better reason than "looks nice." The content is the
subject; the styling recedes.

This grew out of a warmer, more "editorial" first pass. The palette stayed earthy
(the owner's references are genuinely earth-toned), but the polish was pulled back
toward the register: fewer colors, flatter surfaces, document typography.

---

## Where it lives, and how pages use it

```
design/
  styles.css          the whole system — the source of truth
  custom.css          local overrides (linked last; wins)
  fonts/              self-hosted woff2 (Fraunces + Literata, latin + latin-ext)
  design-system.html  the rendered specimen / reference
```

Generated pages link the stylesheet, then the override:

```html
<link rel="stylesheet" href="{root}/assets/styles.css">
<link rel="stylesheet" href="{root}/assets/custom.css">
```

`custom.css` comes last on purpose — a CSS-literate user restyles the whole
archive from that one file without touching templates or code. Because pages link
one shared stylesheet, there is a single place to change anything.

Portable single-file exports (e.g. a research packet meant to be emailed) inline
the same `styles.css` at build time rather than linking it — one canonical source,
two delivery modes, no divergence.

Everything is offline-safe: self-hosted fonts, no CDN, no build step, no
JavaScript framework. Keep it that way.

---

## Tokens

All visual decisions are CSS custom properties in the `:root` block at the top of
`styles.css`. Override them in `custom.css`; don't edit the stylesheet. The values
below are the current defaults — `styles.css` is authoritative if they drift.

### Paper & ink

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

### Accent — one warm ink, used sparingly

`--accent: #6E3A26` (deep sienna/oxblood). Carries links (`--link`), emphasis, the
social-edge lines, and the generation numerals. **This is the only accent in the
main interface.** Resist adding a second.

### Earth family — reserved for the tree

`--clay --sage --ochre --rose --navy --crimson --warm-grey`. These exist for the
seven tree branch categories (`--branch-1..7`) and nothing else. They are *not* a
general-purpose palette — spreading them across the UI is what the record-book
direction deliberately moved away from.

### Status inks (text only, no fills)

`--status-accepted-fg` (green) · `--status-review-fg` (ochre-brown) ·
`--status-suggested-fg` (navy) · `--status-rejected-fg` (muted). Statuses are words
in these inks, not tinted pills.

### Type, spacing, shape

- Type: `--font-serif` (Literata — body & headings), `--font-display` (Fraunces —
  masthead & names only), `--font-sans` (system stack — small labels/chrome),
  `--font-mono`. Scale `--text-xs … --text-3xl`.
- Spacing: `--space-1 … --space-8` on a 4px base.
- Shape: `--radius-sm/md` are `2px` (effectively square). `--measure` (~68ch) and
  `--content-max` (54rem) hold the reading line length.

---

## Typography rules

- **Body and headings are Literata**, a text serif. Documents read as documents.
- **Fraunces appears only at display moments** — the masthead and person names.
  Not on every heading. `h2`/`h3` are Literata, differentiated by size and rule.
- **Hierarchy comes from one loud size plus rules**, not five evenly-spaced steps.
  `--text-3xl` (the page title / masthead) is the single dominant size; `h2`
  carries a heavy bottom rule; everything else stays quiet.
- **Figures are deliberate.** Oldstyle figures in running prose
  (`font-variant-numeric: oldstyle-nums`, set on `body`); tabular lining figures in
  tables and indexes so dates and counts align. Dates are the core content — treat
  numerals as a first-class decision.
- **No surname capitals.** Printed genealogies often set surnames in small caps;
  this archive does not (an explicit choice). Names render in normal case.
- Small **uppercase letterspaced labels** are allowed for structural chrome only
  (table headers, the `.u-label` utility, research-note titles) — never for names.

---

## Component conventions

Flat, square, document-like. No drop shadows, no rounded cards, no pills. Separation
comes from rules and whitespace, not elevation.

- **Masthead** (`.site-header`) — the signature element. A thick–thin **double
  rule** sits under the archive name (Fraunces), straight off a record-book title
  page. Every generated page carries it.
- **Tables** (`table.claims`) — ruled like a **ledger**: horizontal rules only, no
  vertical borders, no zebra fill, a heavier rule under the header row, header set
  in a small uppercase sans label, tabular figures.
- **Statuses** (`.status-*`) — typographic: a leading dot and the state's word in
  the state's ink. The `-bg` fills of the old pills are gone; `-fg` tokens remain.
- **Summaries** (`.summary`) — ruled top and bottom, transparent — a record
  summary, not a card.
- **Citations** (`blockquote.citation`) — a hanging italic quote with a normal-case
  `<cite>` source line. No box, no left bar.
- **Research notes** (`.callout`) — a hairline top rule with a quiet uppercase
  label. A margin note, not a tinted panel.
- **Subjects / keywords** (`.tag`) — italic index terms separated by middots
  (`census · Ohio · Hartley line`), not bordered lozenges.
- **Buttons** (`.btn`) — the one place app vocabulary survives (they're
  interactive). Kept square and quiet; primary uses ink, not accent color.
- **Source citations** (`.fn-ref` + `ol.footnotes`) — the reading view never shows
  a backend id. A cited source is a small superscript number (`.fn-ref`) that jumps
  to a numbered Sources list at the foot of the page (`ol.footnotes`), where each
  source is named and linked. Repeated citations of one source share a number.
  Withheld sources collapse to a **single shared "Restricted" footnote** (their
  count and identity never leak, and the label never repeats inline). A source the
  author named in prose (`[[S-id|text]]`) stays a plain link; a claim reference
  (`[C-id]`) cites its backing source; a dangling id renders nothing.
- **Stub references** (`.stub-ref`) — a person named but without a page of their
  own gets a dotted underline (no link), mirroring the tree's dotted stub node. A
  person with a page is an ordinary link.

The 3px colored left-border that a warmer draft repeated across citations, callouts
and tree nodes is **gone** — a single motif carrying unrelated components reads as a
habit, not a system.

---

## Register patterns (the genealogy-native details)

These are the moves that make the design specific to this subject rather than
reusable for any brand:

- **Double-rule masthead** — see above; the archive's letterhead.
- **Generation numerals** (`.gen-num`) — a large oldstyle numeral (Ahnentafel /
  generation number) set in the left margin of a person record, in the accent ink.
- **Dotted-leader indexes** (`ul.leaders`) — surname and place indexes with dotted
  leaders and tabular counts, like the back of a printed register.
- **Portraits** — a person's chosen `profile_photo` (SPEC person field) plates the
  head of their record (`.person-portrait`, floated right) and appears as the small
  tree square. It's a filename/path/S-id resolved through the photo index, run
  through the same derivative + privacy pipeline as the photo strip (a portrait that
  co-depicts a living person is dropped from the shared snapshot). No photo → a
  monogram placeholder, never a broken image.
- **Ancestor fan chart** (`.fan-chart`, `.fan-seg`, `.fan-label`) — a static,
  print-friendly pedigree fan on each person page: the subject at the hub, ancestors
  fanning up a 180° semicircle by generation. Each segment's fill is a grandparent-
  line branch colour lightened outward by generation, set inline as `--seg-color` /
  `--gen-fade` and composed in the stylesheet so `custom.css` can retint the whole
  chart. Labels run tangentially on the roomy inner rings, radially on the outer.
  Rendered server-side as inline SVG — no JavaScript, prints cleanly.

Spend boldness here and keep everything else quiet so these read.

---

## The family tree

Nodes read like **index entries**, not app cards: square, a bottom hairline, the
name in Fraunces with the **branch category shown as a colored underline** (the only
place the earth tones appear in the UI). The branch color is driven by a
`data-branch="1".."7"` attribute mapping to `--branch-1..7`, so the whole
categorical scheme lives in one place.

Each node (200×64) also carries a **small fixed-size portrait square**
(`.fha-portrait`, 38px) to the left of the name — the person's `profile_photo` if
set, otherwise a **monogram placeholder** (`.fha-portrait-empty`, their initial in
Fraunces). The portrait size is locked so it never changes the card geometry; the
name **wraps to two lines** (clamped) so ordinary full names fit without
truncating. A stub (no page) gets a dotted name underline. See Portraits under
Register patterns.

**Edge kinds** are told apart by **dash pattern** (legible in print and for
colorblind viewers), not by color alone:

| Kind | Line | Meaning | Class |
|---|---|---|---|
| Genetic | solid | parent–child by blood | `.fha-tree-edge` |
| Legal | long dash | adoptive / step / foster / guardian / in-law | `.fha-tree-edge-legal` |
| Other | dotted | friend / coworker / associate (affiliations) | `.fha-tree-edge-other` |

`.fha-tree-edge-social` remains as a back-compat alias for `-legal`. The
data-model side of the genetic/legal/other distinction (SPEC §12.2 currently
splits only genetic vs. non-genetic, and "other" overlaps the affiliations model)
is tracked separately from this styling.

---

## Accessibility floor

Do not regress these:

- **AA contrast** for every text token on paper. Re-measure after any palette
  change — don't eyeball. Current: `--accent`/`--link` ≈ 7.8:1, `--muted` ≈ 4.8:1,
  `--ink` far above.
- **Distinguish by more than color** — tree edges use dash patterns; statuses carry
  a word, not just a hue.
- `:focus-visible` styling, the `.visually-hidden` utility, and `font-display: swap`
  stay.
- Self-hosted fonts, latin **and** latin-ext subsets — European names keep their
  diacritics (Wróblewska, Şerban, Ólafur). Any replacement face must be fetched,
  subset to the same unicode ranges, and dropped into `design/fonts/` the same way.

---

## Customizing

Put overrides in `design/custom.css` (linked after `styles.css`, so it wins). Most
restyles are a few tokens:

```css
:root {
  --paper:  #f4f1ea;                                /* cooler paper        */
  --accent: #3e4a3a;                                /* bottle-green links  */
  --font-serif: "Iowan Old Style", Georgia, serif;  /* your own text face  */
}

h2 { border-bottom-width: 3px; }                    /* reach past tokens if needed */
```

**Dark mode** and an optional **paper grain** are both off by default; `styles.css`
marks exactly where to enable each.

The archive's **name** (masthead and page titles) is not a CSS concern — set it in
`fha.yaml` with `archive_name:` (it defaults to "Family History Archive").

---

## Do / don't

**Do:** keep it flat and square; use one accent; reserve the earth tones for tree
branches; set dates with intentional figures; let rules and whitespace do the
separating; name things plainly.

**Don't:** add gradients, shadows, or rounded pills; introduce a second UI accent;
spread the branch colors across the interface; set surnames in capitals; add icon
libraries, web fonts from a CDN, JavaScript frameworks, or a build step; or write
copy that describes the design's own "warmth."
