# `photo-context` — design note (step 09)

**Status: core verb shipped — SKILL.md pending.** The design below is settled, and the deterministic
write path it requires now exists (`fha photoindex set-summary`, BUILD.md M3.5). The `SKILL.md` is still
intentionally **not** written — that is a separate, later skill-mode PR against this design. Per the
interface-skills index and [`../_STANDARD.md`](../_STANDARD.md) §6 (the stop-don't-improvise rule), this
step originally halted and surfaced the gap as core (BUILD.md) work rather than hand-rolling the write in
skill prose; that core work has since landed.

This note satisfies step 09's first job: name the trigger, inputs, the deterministic write verb, and the
provenance/AI-marking rule — and explicitly confirm whether a tool gap exists. It did; the gap is now closed.

---

## Why this skill

The photo pipeline's embedded captions (the `UserComment` AI summary, SPEC §20) are written once at intake
and never improve. As the archive grows, it *knows* more about a photo than its caption says — who the
tagged people are to each other, what event or claim it depicts, the history of the place it was taken.
`photo-context` would rewrite a photo's embedded AI summary with that accumulated knowledge, so captions
get smarter over time. It began as **backlog** in TOOLING_INTERFACE.md §2.3, was **designed but blocked**
on a core-tool gap (BUILD_INTERFACE.md Layer I4), and is now unblocked — the core verb shipped
(BUILD.md M3.5) and only the SKILL.md remains; this note is that settled design.

## The design (settled)

- **Trigger & scope — invoked-only, one photo (or a small explicit batch) at a time.** Like
  `mine-transcript`, it **never runs automatically**: it writes embedded metadata into an original file, so
  it must be an explicit human request ("update this photo's caption with what we now know", "refresh the
  summary on the 1895 portrait"). No silent or bulk rewrites.
- **Inputs (all via `fha`, never bulk-reading the photos tree):**
  - `fha photoindex find --person <P-id> | --text "…"` — locate the photo group and read its current
    caption / `user_comment` / keywords from the catalog (not the file).
  - the photo's identified people via `photo_people` (the bare `P-id` keywords + `face_tags:` resolution,
    SPEC §20 rule 4).
  - their relationships: `fha relate <P-a> <P-b>` — so the summary can say "her father" instead of just
    listing names.
  - the event/claim context: `fha find --related <S-id>` / the depiction claim on the photo source.
  - the place's history: `fha find --related <L-id>` → the place's `history:`.
- **The write path — MUST go through a deterministic tool, marked as AI (SPEC §20 rule 5).** The embedded
  `UserComment` write is an exiftool operation on an original file; a skill **never** shells exiftool
  itself and **never** bulk-reads the photos tree. The rewritten summary is AI-marked; the human's own
  caption text is **appended/annotated, never overwritten** (SPEC §20 rule 5: "human captions are preserved").
- **Provenance:** the new summary carries an AI marker per SPEC §20; the original human caption survives;
  the `import_date`/date keywords are untouched (SPEC §20 rule 2 — the technical date never becomes truth).

## The gap (closed — the core verb shipped)

**When this note was written, no `fha` verb wrote a photo's embedded AI summary / `UserComment`** — the
photoindex write surface was `fha photoindex tag-person`, bare `P-id` (and `SOURCE:`) keywords only
(SPEC §20 rules 3-4). SPEC §20's preamble and rule 5 *sanction* AI captions as embedded metadata and
require AI output to stay marked as AI, but the tool that performs that specific write had never been
built, and per _STANDARD.md §6 the skill must not shell exiftool or hand-roll the metadata write.

That gap is now closed: `fha photoindex set-summary` shipped (BUILD.md M3.5, TOOLING.md §9).

## What unblocked it (shipped: BUILD.md M3.5)

The deterministic photoindex write verb, as proposed here:

```
fha photoindex set-summary (<path> | --group <group-id>) --text "<AI summary>" [--append] [--dry-run]
```

- writes `UserComment` via exiftool, **AI-marked** per SPEC §20 rule 5 (`AI: <text>`);
- **preserves** any existing human comment text verbatim (append below, never clobber — no flag can
  replace human text); never touches the human-caption fields (`Caption-Abstract`/`XMP-dc:Description`);
- previews old → new (`--dry-run`), prompts `[y/N]` before a live write, and returns a `Result` whose
  `changed[]` lists the files written — mirroring `photoindex tag-person`'s contract;
- honors working-copy mode (refuses when the assets are absent, like other asset-mutating verbs,
  TOOLING §13d) and hard-blocks on a stale photoindex;
- `--group` writes every member of a variation group so fronts/backs/copies stay consistent.

SPEC §20 already permitted the write, so no SPEC amendment was required — only the tool.

## Definition of done for step 09 (this note)

- [x] Design named: trigger (invoked-only, one photo/small batch), inputs (`photoindex find`, `photo_people`,
      `fha relate`, claim/place context), the deterministic write verb (must exist), the provenance rule
      (AI-marked, human caption preserved).
- [x] Tool gap explicitly confirmed: **yes** — at design time no `UserComment`-write verb existed;
      `photoindex tag-person` writes keywords only.
- [x] Core verb shipped: `fha photoindex set-summary` (BUILD.md M3.5) — the write path exists.
- [ ] **Pending the SKILL.md:** write `photo-context/SKILL.md` against this design (a separate skill-mode
      PR). BUILD_INTERFACE.md Layer I4 stays "**designed; core verb shipped — SKILL.md pending**"; it
      flips to shipped only when the SKILL.md lands.

The write verb has landed. Write `photo-context/SKILL.md` against this design, conforming to
[`../_STANDARD.md`](../_STANDARD.md), and flip BUILD_INTERFACE.md Layer I4 to shipped.
