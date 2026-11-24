---
name: share-and-export
description: >
  Run when the human wants family material to leave the archive or be kept safe: "export this
  for my aunt", "share the tree with my family", "send grandma's file to my cousin", "make a
  packet for Thomas", "publish this to WikiTree", "give me something for a USB stick", "back
  everything up", "move my tree to RootsMagic / another program", "upload my tree to MyHeritage
  for my DNA matches". Picks the right export tool for the person and medium (packet / gedcom /
  site / wikitree / backup), explains the privacy defaults in plain words BEFORE running,
  previews first, and reports exactly what went out and what stayed home. It writes only export artifacts and backups, never archive records.
  Importing a tree INTO the archive is not this skill (that is a gedcom-import / migration
  conversation).
---

# share-and-export

This skill handles the moment family material leaves the archive: a packet for a relative, a GEDCOM for another program, a public WikiTree profile, a browsable site on a USB stick, or a safety copy that stays home. Each of those is a deterministic `fha` tool. Every export tool except `backup` withholds private records by default; `backup` is a full copy for the owner and redacts nothing. This skill adds the judgment a tool can't make: which export fits the ask, saying the privacy defaults out loud *before* anything runs, previewing, confirming, and then reporting plainly what went out and what was held back. See [`../_STANDARD.md`](../_STANDARD.md).

## When this runs

Any of: "export this for my aunt", "share the tree with my family", "send grandma's file to my cousin", "make a packet for Thomas", "publish this to WikiTree", "give me something for a USB stick", "back everything up", "move my tree to RootsMagic / another program", "upload my tree to MyHeritage for my DNA matches". It does **not** run for "import my Ancestry tree into here" (see Guardrails).

## The contract for this skill

- Rules 1–4 (suggested-only claims, human-gated `accepted`, `ai_passes:`, draft markers) barely apply: **this skill drafts no claims, writes no records, and adds no `ai_passes:` entry.** It reads the archive through the export tools and writes only export artifacts (a packet zip, a `.ged`/wiki text file, a site folder, a backup zip). It never writes anything under `people/`, `sources/`, `places/`, or `notes/`, so there is nothing to log under SPEC §14.
- **Rule 5** (never edit below `<!-- GENERATED … -->`, never hand-edit generated output) applies to the output: a built site, a packet, a GEDCOM file, and a backup zip are all generated artifacts. Fix the source and re-export; never patch the export itself.
- **Rule 6 (privacy) is the one this skill exists to serve.** The tools themselves enforce `living`, `restricted`, `restricted: dna`, and `restricted: by-request`. This skill says what that means in plain words *before* running. It never works around the tools and never reaches for an override flag the human didn't ask for by name or clear intent.

## Flow

1. **Figure out who it's for and what medium.** Ask one plain question only if it's unclear ("Is this for one person to read, or to load into another genealogy program?"), then route:

   | The ask sounds like… | Tool |
   |---|---|
   | Everything about one person, for a relative to read | `fha packet <p-id>` |
   | Loading into another genealogy program (Ancestry, RootsMagic, Gramps…) | `fha gedcom [--all \| <p-id> --mode descendants\|ancestors\|connected] --out FILE` |
   | Uploading a tree to a DNA site (MyHeritage, Ancestry DNA, FamilyTreeDNA…) so it can compare matches | `fha gedcom --basic [--all \| <p-id> --mode ancestors] --out FILE` |
   | A page to print for a relative without a computer (a couple, their parents, their children, each fact numbered to its record) | `fha views family-sheet <p-id> [--spouse <p-id>] --format html` (open it in a browser and print or Save as PDF) |
   | A printed list of someone's ancestors, numbered the way genealogy books do it (1 the person, 2 the father, 3 the mother…) | `fha views ahnentafel <p-id> [--generations N] --format html` |
   | A printed list of everyone descended from one ancestor, numbered 1, 1.1, 1.1.1 so a reader can find any branch | `fha views descendants <p-id> [--generations N] --format html` |
   | A printed book-style list of everyone descended from one ancestor, as genealogy journals print it (each family a short paragraph, children with Roman numerals) | `fha views register <p-id> [--generations N] --format html` |
   | Browsing on a computer, a USB stick, or "the family website" | `fha site --standalone` |
   | Publishing one ancestor's profile publicly | `fha wikitree <p-id> [--out FILE]` |
   | A safety copy, nothing leaves home | `fha backup` |
   | "Import my Ancestry tree" / "move my tree INTO here" | **Not this skill** (see Guardrails) |

2. **Say the privacy defaults BEFORE running, in the recipient's terms,** so nothing is a surprise:
   - **packet**: "Anything you've marked private stays out unless you tell me otherwise, and DNA material stays out even then. A note you've fenced off as private always stays out. A packet is only made for someone who has passed away, so for a living person the tool declines. If someone still living is mentioned inside, the packet's README points that out as a caution; it does not remove them."
   - **family-sheet**: "This is a printed page for a relative. Anyone still living, or not yet recorded as having died, is never named: a living parent or spouse shows as 'Living Person' and a living child is only counted ('and 2 living children'). Anything private stays out and isn't counted. Every date and place has a little number that points to the record it came from, listed at the bottom. It's made only for someone who has passed away." If the person is living the tool declines; say why in plain words, and offer `fha person set-living <p-id> false` only if they have in fact died.
   - **ahnentafel**: "Same rules as the family sheet: anyone still living, or not yet recorded as having died, shows only as 'A living relative (not named)', and anything private stays out, along with everyone above it. Unlike the family sheet, it can be made for someone living, yourself included: number 1 is then that label and the ancestors who have died follow. It follows birth parents only."
   - **descendants**: "Same rules as the family sheet: anyone still living, or not yet recorded as having died, is only counted under their parent ('and 2 living children'), and nobody below them is listed or counted at all. Anything private stays out. It's made only for someone who has passed away."
   - **register**: "Same rules as the family sheet: it can only be made for someone who has died. Living descendants are only counted under their parent ('and 2 living children (not named)'), never named, and their own children are left out. Anything private stays out."
   - **gedcom**: "Living people go in as 'Living', with their dates and details withheld, so the shape of the tree survives but their information stays home. Anything private or DNA-backed is left out entirely, and there's no switch to include it."
   - **gedcom --basic** (a DNA site): "A DNA site only needs names, birth, death and marriage dates and places, and who is whose parent, to compare your matches. This version sends just that: none of your source titles, notes or the archive's own reference numbers go with it, and living people are still 'Living'." Use `--basic` for every DNA-site upload, without waiting to be asked: a full export carries every source's title, and a title such as an interview or a cousin's letter can name a living relative the export otherwise hides.
   - **site --standalone**: "This build leaves out private and living people's details, so it's safe to hand to family or carry on a drive. (There's also a `--linked` preview that shows everything, but that one is only for checking your own work on this computer. Don't share it.)"
   - **wikitree**: "This goes on a public website. The tool won't publish a living person, and it won't publish a profile whose story relies on a private source; you'd need to fix the story first. Notes you've fenced off as private are never published."
   - **backup**: "Nothing is left out of this one. It's your own safety copy, kept outside the archive folder, and it stays with you. Photos and documents are only included if you ask for them (`--include-assets`), since they're often huge."

   The override flags belong to specific tools: `--include-restricted` and `--include-dna` to `packet`, `--include-living` to `gedcom`. Name one only if the human asks why something is missing or clearly wants it included on purpose; never offer them up front. `restricted: by-request` is never overridden by any flag; say so plainly if it comes up.

3. **Preview before writing.** `packet` and `site` take `--dry-run`: use it, and read the human the plan (what's included, what's withheld, where it would land) before the real run. `gedcom` and `wikitree` have no `--dry-run`. Their default is to print to stdout, and that output **is** the preview: run it unredirected first, summarize it in plain words (how many people, who came out as "Living" or was left off and why), and only add `--out FILE` once the human confirms. `backup --dry-run` prints the full plan (destination, what's included) and writes nothing.

4. **Confirm, then run the real command, and echo it first.** State the exact command you're about to run (§8 execution hygiene) before running it, so nothing happens the human didn't see coming. Unless the human names their own destination, steer file output to `out/` by convention. `packet` already defaults there; suggest an `out/…` path for `gedcom --out` and `wikitree --out` too (e.g. `out/family.ged`, `out/thomas-hartley.wiki`) rather than leaving a file at the archive root or in stdout history.

5. **Report where it landed and what's in and out.** Give the exact path (`out/packet_…zip`, `out/family.ged`, `generated/site/`, the backup zip's folder), a one-breath contents summary (how many people, sources, photos), and restate the exclusions as reassurance rather than as an error: *"Your uncle is in there as 'Living' with no dates. That's the privacy setting doing its job."* End with one concrete next step: how to hand it over (email, USB, upload), or "Want a backup too, since a copy is about to leave the house?"

## Guardrails

- **Never pass `--include-restricted`, `--include-dna`, `--include-living`, or `--linked` without the human's explicit, informed ask in this session.** Don't offer them proactively, and don't reach for one just because an export came back smaller than expected.
- **`restricted: by-request` is never overridden, no matter what is asked.** No flag lifts it; say so if the human pushes on it.
- **Never work around a refusal by hand-editing output.** A packet's living-subject refusal, a wikitree living-subject or broken-citation refusal, a gedcom/site redaction: each is the tool working as designed. Fix the underlying record (mark someone `living: false` only if that's actually true, resolve the citation) and re-export; never patch the export file to add back what was withheld.
- **A packet re-run for the same person on the same day refuses by default** because its output already exists. This is the routine case, not a privacy refusal: name `--overwrite` as the fix and confirm the human wants the old packet replaced before adding it, as with any other flag here.
- **Import is not this skill.** "Import my Ancestry tree" / "move my tree into here" hands off to the `fha gedcom import` path, run as an AGENTS.md **migration-mode** conversation (PLAN → DRY-RUN → human approval → bounded batches). A single loose file (a document, a photo, a note) goes to `process-source` instead. Say this plainly the moment an import-shaped request appears, so the wrong skill doesn't fire in either direction.
- **Never hand-edit anything under `generated/` or an export artifact** (packet zip, `.ged` file, wikitree text, site HTML, backup zip). The fix is always upstream: the source record, then re-export. A hand-edited site goes to `reconcile-site-edits`, never to a raw HTML patch here.
- **This skill writes no claims, no records, no `ai_passes:` entry.** It reads and exports; there is nothing to record under SPEC §14, and no session should invent a pass entry for a run of this skill.

## Done when

- Each trigger phrase (packet / gedcom / site / wikitree / backup) routes to the right tool on `example-archive/`, with the privacy script spoken in plain words before any run.
- A packet request for a living or unknown subject is declined, and the decline is explained in plain language, not worked around.
- A dry-run or stdout preview precedes every write; the final report names the artifact's path and restates what was excluded and why.
- No override flag (`--include-restricted`, `--include-dna`, `--include-living`, `--linked`) appears in any transcript unless the human asked for it by name or clear intent; `restricted: by-request` is never overridden.
- An "import my tree" request is met with a hand-off, not an export.
- `fha lint --root example-archive` still exits 1 with only the documented baseline warnings (`_STANDARD.md` §9). Export artifacts land under `out/` or `generated/`, outside the record trees, so nothing new is flagged.
