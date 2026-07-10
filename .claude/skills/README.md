# .claude/skills/ — the workflow skills

The AI-interface layer: portable `SKILL.md` files that turn the deterministic `fha` tools into a
genealogy-aware research assistant. Each skill is **instructions + `fha` invocations only** — no harness
APIs, no Python — so the harness choice stays reversible (the SKILL.md standard works beyond Claude Code).

**Start here:** [`_STANDARD.md`](_STANDARD.md) is the authoring contract every skill conforms to.
[`today/`](today/SKILL.md) is the reference skill — copy its shape. Design lives in
[`TOOLING_INTERFACE.md`](../../TOOLING_INTERFACE.md); the build sequence in
[`BUILD_INTERFACE.md`](../../BUILD_INTERFACE.md).

The governing split: **deterministic work is in `fha` tools; judgment is in skills; the human is the only
gate to `accepted`.**

## The skills

| Skill | Status | What it does | Model tier |
|---|---|---|---|
| [`today`](today/SKILL.md) | **authored** | Session start: read `fha report`, narrate discoveries-first, offer the top item. Read-only. Reference skill for `_STANDARD.md`. | Opus |
| [`review-claims`](review-claims/SKILL.md) | **authored** | Stage C, the human gate: walk a source's `suggested` claims, capture accept/dispute/edit, write with `fha claim`; close with reindex + xref + touched-person view refresh + lint. | Opus |
| [`process-source`](process-source/SKILL.md) | **authored** | The pipeline driver: `fha process` (Stage A) + AI draft (Stage B) + hand-off to `review-claims`. Handles loose notes. | Opus |
| [`mine-transcript`](mine-transcript/SKILL.md) | **authored** | Invoked-only extraction over a transcript: selective `suggested` claims + anchors, stories to `## Stories`, transcript left intact. | Sonnet |
| [`write-biography`](write-biography/SKILL.md) | **authored** | Sourced prose from the draft queue: facts only from `accepted` claims, cite every factual sentence, AI-DRAFT markers until `fha confirm draft`. | Opus |
| [`research-next`](research-next/SKILL.md) | **authored** | Log-aware research leads: check the research log first, combine gaps + questions + hypotheses with era/place context; may draft `origin: agent` hypotheses. | Sonnet |
| [`merge-identities`](merge-identities/SKILL.md) | **authored** | "Same person / two people" judgment: lay out the neighborhood evidence, propose, wait for human confirmation. Enacts a confirmed merge with `fha confirm merge` (dry-run preview first); the split stays hand-guided per SPEC §9. | Opus |
| [`place-research`](place-research/SKILL.md) | **authored** | Place history (loose citations OK): draft dated `history:`, propose registry entries via `fha confirm place`. Never edits coordinates without confirmation. | Sonnet |
| [`reconcile-site-edits`](reconcile-site-edits/SKILL.md) | **authored** | The site escape hatch: when a human hand-edits generated HTML, diff it against a pristine `fha site` baseline and fold the intent into the right source (`custom.css` / `notes/home.md` / a record / `fha.yaml` `site:`), then rebuild. Keeps `fha site` deterministic; every source write is human-confirmed. See [`docs/SITE_PLAN.md`](../../docs/SITE_PLAN.md). | Opus |
| [`photo-context`](photo-context/DESIGN.md) | **designed; core verb shipped — SKILL.md pending** | Rewrite a photo's embedded AI summary with archive knowledge. Its core-tool gap is closed (`fha photoindex set-summary` shipped, BUILD.md M3.5); the SKILL.md is a separate, later skill-mode PR — see the [design note](photo-context/DESIGN.md). | Opus |

Statuses track [`BUILD_INTERFACE.md`](../../BUILD_INTERFACE.md): **authored** = the SKILL.md exists and was
verified against the shipped tools + the lint invariant; the remaining gate is a **behavioral session
check** against `example-archive/` (capture the transcript). **core verb shipped — SKILL.md pending** =
designed, the core (BUILD.md) tool it needed has shipped, and writing the SKILL.md is a separate
skill-mode PR.

## Conventions (all skills)

- One folder per skill: `.claude/skills/{name}/SKILL.md`.
- Every skill obeys [`_STANDARD.md`](_STANDARD.md): suggested-only claims; `accepted` only via `fha claim`
  with a human decision; every AI pass recorded in `## AI Passes`; AI-DRAFT markers for prose; never edit
  below a GENERATED header or overwrite human text.
- No skill imports another; the only cross-skill links are the documented hand-offs into `review-claims`
  (from `process-source` always, and from `mine-transcript` when the human reviews right away) — hand-offs,
  not code dependencies.
- Verification is behavioral — run the skill against `example-archive/` and confirm the documented writes,
  graceful degradation, and that `fha lint` still exits 1 with only the documented baseline warnings
  (`_STANDARD.md` §9).
