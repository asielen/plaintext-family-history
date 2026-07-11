---
name: share-and-export
description: >
  Run when the human wants family material to leave the archive or be kept safe: "export this
  for my aunt", "share the tree with my family", "send grandma's file to my cousin", "make a
  packet for Thomas", "publish this to WikiTree", "give me something for a USB stick", "back
  everything up", "move my tree to RootsMagic / another program". Picks the right export tool
  for the person and medium (packet / gedcom / site / wikitree / backup), explains the privacy
  defaults in plain words BEFORE running, previews first, and reports exactly what went out and
  what stayed home. It writes only export artifacts and backups - never archive records.
  Importing a tree INTO the archive is not this skill (that is a gedcom-import / migration
  conversation).
---

# share-and-export

The conversational front door for the moment a family history stops being private research and
starts leaving the house — a packet for a cousin, a GEDCOM for another program, a public WikiTree
profile, a browsable site on a USB stick, or just a safety copy that stays home. Every one of
those is a deterministic `fha` tool that already fails closed on privacy; this skill's only job is
the judgment a tool can't have — which export fits the ask, saying the privacy defaults out loud
*before* anything runs, previewing, confirming, and then reporting plainly what went out and what
was held back. See [`../_STANDARD.md`](../_STANDARD.md).

## When this runs

Any of: "export this for my aunt", "share the tree with my family", "send grandma's file to my
cousin", "make a packet for Thomas", "publish this to WikiTree", "give me something for a USB
stick", "back everything up", "move my tree to RootsMagic / another program". It does **not** run
for "import my Ancestry tree into here" — see Guardrails.

## The contract for this skill

- Rules 1–4 (suggested-only claims, human-gated `accepted`, `## AI Passes`, draft markers) barely
  apply: **this skill drafts no claims, writes no records, and adds no `## AI Passes` entry.** It
  reads the archive through the export tools and writes only export artifacts (a packet zip, a
  `.ged`/wiki text file, a site folder, a backup zip) — never anything under `people/`, `sources/`,
  `places/`, or `notes/`. There is nothing here for a future session to have "touched" in the
  archive's own records, so there is nothing to log under SPEC §14.
- **Rule 5** (never edit below `<!-- GENERATED … -->`, never hand-edit generated output) bites on
  the output side: a built site, a packet, a GEDCOM file, and a backup zip are all generated
  artifacts. Fix the source and re-export; never patch the export itself.
- **Rule 6 (privacy) is the one this skill exists to serve.** `living`, `restricted`,
  `restricted: dna`, and `restricted: by-request` are enforced by the tools themselves — this
  skill's job is to say what that means in plain words *before* running, never to work around it,
  and never to reach for an override flag the human didn't ask for by name or clear intent.

## Flow

1. **Figure out who it's for and what medium.** Ask one plain question only if it's unclear —
   "Is this for one person to read, or to load into another genealogy program?" — then route:

   | The ask sounds like… | Tool |
   |---|---|
   | Everything about one person, for a relative to read | `fha packet <P-id>` |
   | Loading into another genealogy program (Ancestry, RootsMagic, Gramps…) | `fha gedcom [--all \| <P-id> --mode descendants\|ancestors\|connected] --out FILE` |
   | Browsing on a computer, a USB stick, or "the family website" | `fha site --standalone` |
   | Publishing one ancestor's profile publicly | `fha wikitree <P-id> [--out FILE]` |
   | A safety copy, nothing leaves home | `fha backup` |
   | "Import my Ancestry tree" / "move my tree INTO here" | **Not this skill** — see Guardrails |

2. **Say the privacy defaults BEFORE running, in the recipient's terms.** Every export tool fails
   closed on its own; say what that means in plain words first, so nothing here is a surprise:
   - **packet** — "Anything you've marked private stays out unless you tell me otherwise, and DNA
     material stays out even then. A packet is only made for someone who has passed away — for a
     living person the tool declines outright. If someone still living is mentioned inside, the
     packet notes that in its README as a caution, not a redaction."
   - **gedcom** — "Living people go in as 'Living', with their dates and details withheld, so the
     tree's shape survives but their information stays home. Anything private or DNA-backed is
     left out entirely — there's no switch to include that."
   - **site --standalone** — "This build is the redacted version, safe to hand to family or carry
     on a drive. (There's also an unredacted `--linked` preview, but that one's for checking your
     own work on this machine only — never to share.)"
   - **wikitree** — "This is public output: the tool refuses outright to publish a living person,
     and it refuses a profile whose story leans on a private source rather than quietly rewriting
     around it — you'd need to fix the story first."
   - **backup** — "Nothing is redacted here — it's your own safety copy, kept outside the archive
     folder, and it stays with you. Photos and documents are only included if you ask for them
     (`--include-assets`), since they're often huge."
   Name `--include-restricted`, `--include-dna`, or `--include-living` only if the human asks why
   something is missing, or clearly wants it included on purpose — never offer them up front.
   `restricted: by-request` is never overridden by any flag; say so plainly if it comes up.

3. **Preview before writing.** `packet` and `site` take `--dry-run` — use it, and read the human
   the plan (what's included, what's withheld, where it would land) before the real run. `gedcom`
   and `wikitree` have no `--dry-run`: their default is to print to stdout, and that render **is**
   the preview — run it unredirected first, summarize what it contains in plain words (how many
   people, who came out as "Living" or was left off and why), and only add `--out FILE` once the
   human confirms. `backup --dry-run` prints the full plan (destination, what's included) and
   writes nothing.

4. **Confirm, then run the real command — and echo it first.** State the exact command you're
   about to run (§8 execution hygiene) before running it, so nothing happens the human didn't see
   coming. Unless the human names their own destination, steer file output to `out/` by
   convention — `packet` already defaults there; suggest an `out/…` path for `gedcom --out` and
   `wikitree --out` too (e.g. `out/family.ged`, `out/thomas-hartley.wiki`) rather than leaving a
   file at the archive root or in stdout history.

5. **Report where it landed and what's in and out.** Give the exact path (`out/packet_…zip`,
   `out/family.ged`, `generated/site/`, the backup zip's folder), a one-breath contents summary
   (how many people, sources, photos), and restate the exclusions as reassurance, not as an
   error — *"your uncle is in there as 'Living' with no dates — that's the redaction working, not
   a bug."* End with one concrete next step: how to hand it over (email, USB, upload), or "want a
   backup too, since a copy is about to leave the house?"

## Guardrails

- **Never pass `--include-restricted`, `--include-dna`, `--include-living`, or `--linked` without
  the human's explicit, informed ask in this session.** Don't offer them proactively, and don't
  reach for one just because an export came back smaller than expected.
- **`restricted: by-request` is never overridden, no matter what is asked.** No flag lifts it; say
  so if the human pushes on it.
- **Never work around a refusal by hand-editing output.** A packet's living-subject refusal, a
  wikitree living-subject or broken-citation refusal, a gedcom/site redaction — each is the tool
  working as designed. Fix the underlying record (mark someone `living: false` only if that's
  actually true, resolve the citation) and re-export; never patch the export file to add back what
  was withheld.
- **Import is not this skill.** "Import my Ancestry tree" / "move my tree into here" hands off to
  the `fha gedcom import` path, run as an AGENTS.md **migration-mode** conversation (PLAN →
  DRY-RUN → human approval → bounded batches) — never handled here. A single loose file (a
  document, a photo, a note) instead goes to `process-source`. Say this plainly the moment an
  import-shaped request appears, so the wrong skill doesn't fire in either direction.
- **Never hand-edit anything under `generated/` or an export artifact** (packet zip, `.ged` file,
  wikitree text, site HTML, backup zip). A fix is always upstream — the source record, then
  re-export. A hand-edited site specifically routes to `reconcile-site-edits`, never to a raw HTML
  patch here.
- **This skill writes no claims, no records, no `## AI Passes` entry.** It reads and exports;
  there is nothing to record under SPEC §14, and no session should invent a pass entry for a run
  of this skill.

## Done when

- Each trigger phrase (packet / gedcom / site / wikitree / backup) routes to the right tool on
  `example-archive/`, with the privacy script spoken in plain words before any run.
- A packet request for a living or unknown subject is declined, and the decline is translated into
  plain language, not worked around.
- A dry-run or stdout preview precedes every write; the final report names the artifact's path and
  restates what was excluded and why.
- No override flag (`--include-restricted`, `--include-dna`, `--include-living`, `--linked`)
  appears in any transcript unless the human asked for it by name or clear intent; `restricted:
  by-request` is never overridden.
- An "import my tree" request is met with a hand-off, not an export.
- `fha lint --root example-archive` still exits 1 with only the documented baseline warnings
  (`_STANDARD.md` §9 — W101 + W102) — export artifacts land under `out/` or `generated/`, outside
  the record trees, so nothing new is flagged.
