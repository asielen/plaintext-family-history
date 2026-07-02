# `photo-context` — design note (step 09)

**Status: BLOCKED on a core-tool gap.** The design below is settled; the `SKILL.md` is intentionally
**not** written, because the deterministic write path it requires does not exist yet. Per the
interface-skills index and [`../_STANDARD.md`](../_STANDARD.md) §6 (the stop-don't-improvise rule), this
step halts and surfaces the gap as core (BUILD.md) work rather than hand-rolling the write in skill prose.

This note satisfies step 09's first job: name the trigger, inputs, the deterministic write verb, and the
provenance/AI-marking rule — and explicitly confirm whether a tool gap exists. It does.

---

## Why this skill

The photo pipeline's embedded captions (the `UserComment` AI summary, SPEC §20) are written once at intake
and never improve. As the archive grows, it *knows* more about a photo than its caption says — who the
tagged people are to each other, what event or claim it depicts, the history of the place it was taken.
`photo-context` would rewrite a photo's embedded AI summary with that accumulated knowledge, so captions
get smarter over time. It began as **backlog** in TOOLING_INTERFACE.md §2.3 and is now **designed but
blocked** on a core-tool gap (BUILD_INTERFACE.md Layer I4); this note is that settled design.

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

## The gap (why this blocks)

**No `fha` verb writes a photo's embedded AI summary / `UserComment`.** Verified against the shipped
suite (`tools/photoindex.py`): the photoindex write surface is `fha photoindex tag-person`, which writes
**bare `P-id` (and `SOURCE:`) keywords only** (SPEC §20 rules 3-4). There is:

- **no** verb that writes/rewrites the `UserComment` (the AI caption/summary) field, and
- **no** verb that appends an AI-marked summary while preserving a human caption.

SPEC §20's preamble and rule 5 *sanction* AI captions as embedded metadata and require AI output to stay marked as AI —
but the tool that performs that specific write was never built. `photo-context`'s entire job is that write.
Per _STANDARD.md §6, the skill must not shell exiftool or hand-roll the metadata write; therefore the skill
cannot be built until the core verb exists.

## What unblocks it (proposed BUILD.md / TOOLING.md core work)

A deterministic photoindex write verb, e.g.:

```
fha photoindex set-summary <photo|group> --text "<AI summary>" [--append] --dry-run
```

- writes `UserComment` (and/or XMP description) via exiftool, **AI-marked** per SPEC §20 rule 5;
- **preserves** any existing human caption (append/annotate, never clobber);
- previews the change (`--dry-run`) and returns a `Result` whose `changed[]` lists the file written —
  mirroring `photoindex tag-person`'s contract;
- honors working-copy mode (refuse when the asset is absent, like other asset-mutating verbs, TOOLING §13d).

This is a **core (BUILD.md) PR**, not skill work. It should be specified in TOOLING.md §9 (photoindex) and
added to BUILD.md's photoindex phase; SPEC §20 already permits the write, so no SPEC amendment is required —
only the tool.

## Definition of done for step 09 (this note)

- [x] Design named: trigger (invoked-only, one photo/small batch), inputs (`photoindex find`, `photo_people`,
      `fha relate`, claim/place context), the deterministic write verb (must exist), the provenance rule
      (AI-marked, human caption preserved).
- [x] Tool gap explicitly confirmed: **yes** — no `UserComment`-write verb exists; `photoindex tag-person`
      writes keywords only.
- [ ] **Blocked:** `SKILL.md` is deferred until the core `fha photoindex set-summary` (or equivalent) verb
      ships. BUILD_INTERFACE.md Layer I4 stays "not yet designed → **blocked on core PR**"; it does **not**
      flip to shipped.

When the write verb lands, write `photo-context/SKILL.md` against this design, conforming to
[`../_STANDARD.md`](../_STANDARD.md), and flip BUILD_INTERFACE.md Layer I4 to shipped.
